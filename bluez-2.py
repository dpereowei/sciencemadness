#!/bin/env python
# Read temperature from Inkbird IDT-34c-B
# Original author: Andrew Robinson of Scappoose, November 2025 – January 2026.
# Rewritten for correctness and clarity, March 2026.
# Version 1.0.0
# Released under the GNU General Public License v3.0
# https://www.gnu.org/licenses/gpl-3.0.en.html
#
# Prerequisites: virtual python environment + dasbus library.
#   python3 -m venv --system-site-packages py_envs
#   pip3 install dasbus
#
# Useful diagnostics:
#   busctl tree org.bluez
#   busctl introspect "org.bluez" "/org/bluez/hci0/dev_XX_XX_XX_XX_XX_XX"
#   sudo btmon > rpi.log   # capture for Wireshark
#
# Note: the Inkbird protocol is proprietary and may change without notice.

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from dasbus.connection import SystemMessageBus
from dasbus.loop import EventLoop
from dasbus.typing import Variant

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INKBIRD_NAMES   = {"IDT-34c-B", "INKBIRD"}   # accepted device name strings
ADAPTER_PATH    = "/org/bluez/hci0"
SERVICE_NAME    = "org.bluez"

DEVICE_IFACE    = "org.bluez.Device1"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHAR_IFACE = "org.bluez.GattCharacteristic1"

# The parent GATT service UUID that groups all Inkbird characteristics.
TEMPERATURE_SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"

# Characteristic UUIDs
UUID_TEMPERATURE = "0000ff01-0000-1000-8000-00805f9b34fb"
UUID_COMMAND     = "0000ff02-0000-1000-8000-00805f9b34fb"
UUID_BATTERY     = "00002a19-0000-1000-8000-00805f9b34fb"
UUID_SKIP        = "0000ff05-0000-1000-8000-00805f9b34fb"

# Any value above this is treated as "no sensor present" (sentinel).
INVALID_TEMP     = 1802.5

# Maximum number of identical readings logged before forcing a refresh.
REDUNDANCY_LIMIT = 20

# Slots: up to 6 devices × 4 probes each.
MAX_DEVICES      = 6
PROBES_PER_DEVICE = 4

# How often (seconds) to re-scan D-Bus for new/lost devices.
WATCHTIME        = 90.1

# Output file
OUTPUT_PATH      = "/tmp/thermal.dat"

# How many seconds without a stamp before the logger tries a manual ReadValue.
STALL_TIMEOUT    = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("inkbird")

# ---------------------------------------------------------------------------
# Per-device state
# ---------------------------------------------------------------------------

@dataclass
class InkbirdDevice:
    """All mutable state for one connected Inkbird thermometer."""
    path: str
    proxy: object                           # dasbus device proxy
    slot: int                               # index into the shared channel arrays (0, 4, 8 …)

    # GATT proxies — populated during service discovery
    temperature_proxy: Optional[object] = None
    command_proxy:     Optional[object] = None
    battery_proxy:     Optional[object] = None
    extra_proxies:     list = field(default_factory=list)

    # Per-probe rolling state (4 probes)
    values:     list = field(default_factory=lambda: [float("nan")] * PROBES_PER_DEVICE)
    filtered:   list = field(default_factory=lambda: [0.0]          * PROBES_PER_DEVICE)
    counts:     list = field(default_factory=lambda: [0]            * PROBES_PER_DEVICE)

    # Set True once all GATT notifications are bound
    bound: bool = False

    def channel(self, probe_index: int) -> int:
        """Absolute channel index into the shared output arrays."""
        return self.slot + probe_index

    def reset_gatt(self):
        """Clear GATT state so services can be rediscovered on reconnect."""
        self.temperature_proxy = None
        self.command_proxy     = None
        self.battery_proxy     = None
        self.extra_proxies     = []
        self.bound             = False

    def is_fully_discovered(self) -> bool:
        """True when at least temperature + command + battery proxies are set."""
        return (
            self.temperature_proxy is not None and
            self.command_proxy     is not None and
            self.battery_proxy     is not None
        )


# ---------------------------------------------------------------------------
# Shared output state  (written by device callbacks, read by logger)
# ---------------------------------------------------------------------------

# Total channels = MAX_DEVICES × PROBES_PER_DEVICE
_NUM_CHANNELS = MAX_DEVICES * PROBES_PER_DEVICE

thermostamp = [float("nan")] * _NUM_CHANNELS   # last accepted temperature per channel
thermofilter= [0.0]          * _NUM_CHANNELS   # previous accepted value (spike filter)
thermocount = [0]            * _NUM_CHANNELS   # redundancy counter per channel

stamp      = False          # True when a new sample is ready to log
laststamp  = time.time()    # wall-clock time of last logged sample

_state_lock = threading.Lock()  # guards all of the above globals + devices dict

# ---------------------------------------------------------------------------
# Device registry
# ---------------------------------------------------------------------------

# Maps D-Bus object path → InkbirdDevice
devices: dict[str, InkbirdDevice] = {}

# Free slot pool: maps slot_offset → slot_offset  (consumed on allocate)
_free_slots: dict[int, int] = {
    i * PROBES_PER_DEVICE: i * PROBES_PER_DEVICE
    for i in range(MAX_DEVICES)
}

def _allocate_slot(path: str) -> Optional[int]:
    """Assign the lowest free slot to this device path. Returns slot or None."""
    with _state_lock:
        if not _free_slots:
            return None
        slot = min(_free_slots)
        del _free_slots[slot]
        return slot

def _free_slot(slot: int):
    """Return a slot to the free pool."""
    with _state_lock:
        _free_slots[slot] = slot

# ---------------------------------------------------------------------------
# D-Bus setup  (module-level singletons, initialised once)
# ---------------------------------------------------------------------------

bus     = SystemMessageBus()
loop    = EventLoop()
adapter = bus.get_proxy(SERVICE_NAME, ADAPTER_PATH)
manager = bus.get_proxy(SERVICE_NAME, "/")

# ---------------------------------------------------------------------------
# Temperature maths
# ---------------------------------------------------------------------------

def _decode_temp(lsbyte: int, msbyte: int) -> float:
    """Decode a raw two-byte Inkbird temperature into degrees Celsius."""
    raw = ((msbyte ^ 0x80) << 8) + lsbyte - 0x8000
    return (raw - 320) / 18.0


def _parse_temperature_packet(data: list[int]) -> Optional[list[float]]:
    """
    Parse a 12-byte temperature notification packet.
    Returns a list of 4 floats, or None if the packet looks invalid.
    The bytes at positions 8–11 must be [0xFE, 0x7F, 0xFE, 0x7F] as a
    sanity-check footer; packets that fail this check are discarded.
    """
    if len(data) < 12:
        log.warning("Short temperature packet (%d bytes): %s", len(data), data)
        return None
    if data[8:12] != [0xFE, 0x7F, 0xFE, 0x7F]:
        log.warning("Suspicious temperature packet (bad footer): %s", data)
        return None
    return [_decode_temp(data[2 * i], data[2 * i + 1]) for i in range(4)]


# ---------------------------------------------------------------------------
# Temperature update logic
# ---------------------------------------------------------------------------

def _update_device_temperatures(dev: InkbirdDevice, data: list[int]):
    """
    Apply new raw packet data to a device's per-probe rolling state,
    then set the global `stamp` flag if any channel changed.

    Spike filter: if a reading jumps by more than 1.5 °C in one packet,
    average it with the previous value rather than accepting it outright.
    Redundancy limiter: suppress repeated identical readings after
    REDUNDANCY_LIMIT consecutive identical stamps.
    """
    global stamp

    temps = _parse_temperature_packet(data)
    if temps is None:
        return

    with _state_lock:
        for i, value in enumerate(temps):
            ch      = dev.channel(i)
            vlast   = dev.values[i]
            vfilter = dev.filtered[i]
            count   = dev.counts[i]

            # Suppress redundant readings
            if (count > 0 and count < REDUNDANCY_LIMIT and
                    (value == vlast or value == vfilter)):
                continue

            delta = abs(value - vlast) if not _is_invalid(vlast) else 0.0

            if delta > 1.5:
                # Spike: blend toward new value rather than hard-accept
                if _is_invalid(value) or _is_invalid(vlast):
                    accepted = value
                else:
                    accepted = (value + vlast) / 2.0
                dev.filtered[i]   = accepted
                dev.values[i]     = accepted
                thermostamp[ch]   = accepted
                thermofilter[ch]  = accepted
                continue

            # Normal update
            dev.filtered[i]  = dev.values[i]
            dev.values[i]    = value
            thermofilter[ch] = thermostamp[ch]
            thermostamp[ch]  = value
            dev.counts[i]    = 0
            stamp            = True


def _is_invalid(v: float) -> bool:
    """True for NaN or above the sentinel threshold."""
    import math
    return math.isnan(v) or v >= INVALID_TEMP


# ---------------------------------------------------------------------------
# Inkbird protocol: initialisation sequence
# ---------------------------------------------------------------------------

_INIT_COMMANDS = [
    [0x02, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x02, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x02, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x02, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x06, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x08],
    [0x0a, 0x0f, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x0c, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x0f, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x11, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x13, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x18],
    [0x24],
    [0x26, 0x01],
    [0x26, 0x02],
    [0x26, 0x04],
    [0x26, 0x08],
]

_WRITE_OPTS = {"type": Variant("s", "request")}


def _send_init_sequence(dev: InkbirdDevice):
    """Send the pseudo-pairing handshake followed by all init commands."""
    log.info("Pseudo-pairing %s", dev.path)
    try:
        dev.command_proxy.WriteValue(
            Variant("ay", [0xfd, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
            _WRITE_OPTS,
        )
    except Exception as exc:
        log.error("Pseudo-pair handshake failed for %s: %s", dev.path, exc)
        return

    log.info("Sending init sequence to %s", dev.path)
    for cmd in _INIT_COMMANDS:
        try:
            dev.command_proxy.WriteValue(Variant("ay", cmd), _WRITE_OPTS)
        except Exception as exc:
            log.error("Init command %s failed for %s: %s", cmd, dev.path, exc)
            return


# ---------------------------------------------------------------------------
# GATT notification callbacks
# ---------------------------------------------------------------------------

def _on_temperature_notify(dev: InkbirdDevice, _iface, changed: dict, _inval):
    if "Value" not in changed:
        return
    data = changed["Value"].unpack()
    _update_device_temperatures(dev, data)


def _on_command_notify(dev: InkbirdDevice, _iface, changed: dict, _inval):
    if "Value" in changed:
        log.debug("Command notify %s: %s", dev.path, changed["Value"].unpack())


def _on_extra_notify(dev: InkbirdDevice, _iface, changed: dict, _inval):
    if "Value" in changed:
        log.debug("Extra notify %s: %s", dev.path, changed["Value"].unpack())


def _on_battery_notify(dev: InkbirdDevice, _iface, changed: dict, _inval):
    if "Value" in changed:
        pct = changed["Value"].unpack()[0]
        log.info("Battery %s: %d%%", dev.path, pct)


def _bind_characteristic(proxy, callback, dev: InkbirdDevice):
    """Connect PropertiesChanged and start notifications on one characteristic."""
    proxy.PropertiesChanged.connect(
        lambda iface, changed, inval: callback(dev, iface, changed, inval)
    )
    proxy.StartNotify()


def _bind_all_notifications(dev: InkbirdDevice):
    """
    Bind all discovered GATT characteristics for a device.
    Disconnects the device if any binding fails.
    """
    pairs = [(dev.temperature_proxy, _on_temperature_notify),
             (dev.command_proxy,     _on_command_notify),
             (dev.battery_proxy,     _on_battery_notify)]
    pairs += [(p, _on_extra_notify) for p in dev.extra_proxies]

    # Require at minimum temperature + command + battery
    if not dev.is_fully_discovered():
        log.warning("Incomplete GATT discovery for %s — disconnecting", dev.path)
        dev.proxy.Disconnect()
        return

    for proxy, cb in pairs:
        try:
            _bind_characteristic(proxy, cb, dev)
        except Exception as exc:
            log.error("Failed to bind characteristic on %s: %s", dev.path, exc)
            dev.proxy.Disconnect()
            return

    dev.bound = True
    _send_init_sequence(dev)


# ---------------------------------------------------------------------------
# Device lifecycle: connect / disconnect / rediscover
# ---------------------------------------------------------------------------

def _on_device_disconnected(dev: InkbirdDevice):
    """Called when BlueZ reports the device disconnected."""
    log.info("Disconnected: %s", dev.path)
    with _state_lock:
        _free_slot(dev.slot)
    dev.reset_gatt()
    # Schedule a single reconnect attempt; scan_dbus is the long-term backstop.
    threading.Timer(5.0, _attempt_reconnect, args=[dev.path]).start()


def _attempt_reconnect(path: str):
    """Try once to reconnect a known device. Called from a Timer thread."""
    dev = devices.get(path)
    if dev is None:
        return
    try:
        if dev.proxy.Connected:
            log.info("Already reconnected: %s", path)
            return
        log.info("Reconnecting: %s", path)
        dev.proxy.Connect()
        # ServicesResolved callback handles the rest from here.
    except Exception as exc:
        log.warning("Reconnect attempt failed for %s: %s — scan_dbus will retry", path, exc)


def _on_services_resolved(dev: InkbirdDevice, _iface, changed: dict, _inval):
    """
    PropertiesChanged handler attached to every Inkbird device proxy.
    Handles both disconnect events and the ServicesResolved→True transition.
    """
    if "Connected" in changed and changed["Connected"].unpack() is False:
        _on_device_disconnected(dev)
        return

    if "ServicesResolved" not in changed:
        return

    if changed["ServicesResolved"].unpack() is not True:
        log.info("Services un-resolved for %s", dev.path)
        with _state_lock:
            _free_slot(dev.slot)
        dev.reset_gatt()
        return

    log.info("ServicesResolved: %s", dev.path)

    # Allocate a slot if we don't have one yet (can happen on reconnect)
    if dev.slot is None:
        slot = _allocate_slot(dev.path)
        if slot is None:
            log.error("No free slots — cannot track %s", dev.path)
            dev.proxy.Disconnect()
            return
        dev.slot = slot

    # Walk the object tree to (re)discover GATT services and characteristics.
    for child_path, child_dict in manager.GetManagedObjects().items():
        if child_path.startswith(dev.path) and child_path != dev.path:
            _handle_gatt_object(child_path, child_dict, dev)

    _bind_all_notifications(dev)


# ---------------------------------------------------------------------------
# GATT object discovery
# ---------------------------------------------------------------------------

# Tracks which GATT service paths belong to which device path.
_gatt_service_to_device: dict[str, str] = {}  # service_path → device_path


def _handle_gatt_object(obj_path: str, obj_dict: dict, dev: InkbirdDevice):
    """
    Process one GATT service or characteristic object during discovery.
    `dev` is the owning InkbirdDevice — no lookup needed.
    """
    if GATT_SERVICE_IFACE in obj_dict:
        props = obj_dict[GATT_SERVICE_IFACE]
        if props["UUID"].unpack() == TEMPERATURE_SERVICE_UUID:
            _gatt_service_to_device[obj_path] = dev.path
            log.debug("GATT service %s → %s", obj_path, dev.path)
        return

    if GATT_CHAR_IFACE in obj_dict:
        # Parent is the service path (one level up in the D-Bus tree)
        service_path = obj_path.rsplit("/", 1)[0]
        if service_path not in _gatt_service_to_device:
            return
        if _gatt_service_to_device[service_path] != dev.path:
            return

        uuid  = obj_dict[GATT_CHAR_IFACE]["UUID"].unpack()
        proxy = bus.get_proxy(SERVICE_NAME, obj_path)

        if uuid == UUID_TEMPERATURE:
            dev.temperature_proxy = proxy
        elif uuid == UUID_COMMAND:
            dev.command_proxy = proxy
        elif uuid == UUID_BATTERY:
            dev.battery_proxy = proxy
        elif uuid == UUID_SKIP:
            pass  # deliberately ignored
        elif uuid.startswith("0000ff"):
            dev.extra_proxies.append(proxy)


# ---------------------------------------------------------------------------
# Interface-added handler  (called by D-Bus signal + scan_dbus)
# ---------------------------------------------------------------------------

def interface_added_callback(obj_path: str, obj_dict: dict):
    """
    Handles InterfacesAdded signals from BlueZ.
    Identifies Inkbird devices and kicks off the connection sequence.
    """
    if DEVICE_IFACE not in obj_dict:
        return

    try:
        name = obj_dict[DEVICE_IFACE]["Name"].unpack()
    except Exception:
        return  # device not yet fully announced; will appear again

    if name not in INKBIRD_NAMES:
        return

    if obj_path in devices:
        log.debug("Already tracking %s", obj_path)
        return

    slot = _allocate_slot(obj_path)
    if slot is None:
        log.error("No free slots — ignoring %s (%s)", name, obj_path)
        return

    proxy = bus.get_proxy(SERVICE_NAME, obj_path)

    if proxy.Connected:
        # Shouldn't normally happen on a clean start; force a clean slate.
        log.warning("Found already-connected device on startup: %s — disconnecting", obj_path)
        _free_slot(slot)
        try:
            proxy.Disconnect()
        except Exception:
            pass
        return

    dev = InkbirdDevice(path=obj_path, proxy=proxy, slot=slot)
    devices[obj_path] = dev

    proxy.PropertiesChanged.connect(
        lambda iface, changed, inval: _on_services_resolved(dev, iface, changed, inval)
    )

    log.info("Connecting to %s (%s) slot=%d", name, obj_path, slot)
    try:
        proxy.Connect()
    except Exception as exc:
        log.error("Connect() failed for %s: %s", obj_path, exc)
        del devices[obj_path]
        _free_slot(slot)


# ---------------------------------------------------------------------------
# Periodic scan
# ---------------------------------------------------------------------------

def scan_dbus():
    """
    Periodically walk the BlueZ object tree to pick up any devices that
    appeared before we were listening, or that need reconnecting.
    """
    log.info("scan_dbus")
    try:
        for obj_path, obj_dict in manager.GetManagedObjects().items():
            interface_added_callback(obj_path, obj_dict)

        for path, dev in list(devices.items()):
            try:
                if not dev.proxy.Connected:
                    log.info("scan_dbus: reconnecting %s", path)
                    dev.reset_gatt()
                    dev.proxy.Connect()
            except Exception as exc:
                log.warning("scan_dbus reconnect failed for %s: %s", path, exc)
    except Exception as exc:
        log.error("scan_dbus error: %s", exc)
    finally:
        threading.Timer(WATCHTIME, scan_dbus).start()


# ---------------------------------------------------------------------------
# Logger: writes /tmp/thermal.dat
# ---------------------------------------------------------------------------

def logger():
    """
    Fires every second. Writes a new line to thermal.dat whenever `stamp`
    is True. Also performs a manual ReadValue if data has stalled.
    """
    global stamp, laststamp

    try:
        now = time.time()

        # Stall detection: nudge the device to send a fresh reading.
        if not stamp and (now - laststamp) > STALL_TIMEOUT:
            log.debug("Logger stalled — requesting manual ReadValue")
            for path, dev in list(devices.items()):
                if dev.bound and dev.temperature_proxy is not None:
                    try:
                        dev.temperature_proxy.ReadValue(_WRITE_OPTS)
                    except Exception as exc:
                        log.warning("ReadValue failed for %s: %s", path, exc)
            laststamp = now

        if stamp:
            with _state_lock:
                snapshot = list(thermostamp)
                stamp    = False
                laststamp = now
                for i in range(_NUM_CHANNELS):
                    n = thermocount[i]
                    thermocount[i] = n + 1 if n < REDUNDANCY_LIMIT else 0

            cols = "  ".join(
                "%6.1f" % v if not _is_invalid(v) else "   NaN"
                for v in snapshot
            )
            fout.write("%10.2f  %s  [°C]\n" % (now, cols))
            fout.flush()

    except Exception as exc:
        log.error("Logger error: %s", exc)
    finally:
        threading.Timer(1.0, logger).start()


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _signal_handler(signum, frame):
    log.info("Caught signal %d — shutting down", signum)
    for path, dev in list(devices.items()):
        try:
            dev.proxy.Disconnect()
        except Exception:
            pass
    fout.flush()
    fout.close()
    if loop is not None:
        loop.quit()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

fout = open(OUTPUT_PATH, "w")

try:
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    manager.InterfacesAdded.connect(interface_added_callback)

    threading.Timer(1.0, scan_dbus).start()
    threading.Timer(1.0, logger).start()

    loop.run()

except Exception as exc:
    log.critical("Fatal error in main loop: %s", exc)
    fout.close()
    raise
