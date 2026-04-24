#!/usr/bin/env python3
# Read temperature from Inkbird IDT‑34c‑B
# Written November, 2025 - March, 2026 by Andrew Robinson of Scappoose, Pereowei Daniel.
# Version 0.99.17‑dev (control‑0018 activation build)
# This code is released under the GNU public license, 3.0
# https://www.gnu.org/licenses/gpl‑3.0.en.html
#
# # Pre-Requisites virtual python, and dasbus library.
# Virtual python is required on most systems with system package installs of python.
# V.P. allows a local user (not superuser) to install python packages without wrecking
# the operating system's version of python.
#
# python3 -m venv --system-site-packages py_envs
# pip3 install dasbus

# Note, the inkbird protocol is proprietary and may change.
# To list your active bluetooth devices and services do:
# busctl tree org.bluez
# busctl introspect "org.bluez" "/org/bluez/hci0/dev_xx_xx_xx_xx_xx_xx"

# To log bluetooth bus activity for wireshark analysis:
# sudo btmon > rpi.log
# Scan
# 
# Automatically binds command channel service14/char0018 (write→0019)
# Sends 0xFD 00 sequence immediately after ServicesResolved → double‑blink.
# ---------------------------------------------------------------------

import time
import math
import signal
from dasbus.connection import SystemMessageBus
from dasbus.loop import EventLoop, GLib
from dasbus.typing import Variant
MAXTEMP = 1802.5
WATCHTIME = 90.1
INKBIRD_NAME = "IDT-34c-B"
FRIENDLY_NAME = "INKBIRD"
SERVICE_NAME = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"
DEVICE_IFACE = "org.bluez.Device1"
GATT_CHAR_IFACE = "org.bluez.GattCharacteristic1"
MAXWAIT = 20
DEBUG = True
thermostamp = [float("nan")] * 24
thermofilter = [0.0] * 24
thermocount = [0] * 24
# Logging slot ownership only.
mac_to_slot = {}
path_to_mac = {}
free_slots = [0, 4, 8, 12, 16, 20]
fout = open("/tmp/thermal.dat", "w")
def dprint(*a, **kw):
    if DEBUG:
        print(*a, **kw)
def extract_mac(obj_path):
    tail = obj_path.rsplit("/dev_", 1)[-1]
    return tail.replace("_", ":")
def get_or_assign_slot(mac):
    if mac in mac_to_slot:
        return mac_to_slot[mac]
    if not free_slots:
        dprint(f"[!] No free slot for {mac}")
        return None
    slot = free_slots.pop(0)
    mac_to_slot[mac] = slot
    dprint(f"[+] Slot {slot // 4 + 1} assigned to {mac}")
    return slot
def clear_slot_for_mac(mac):
    slot = mac_to_slot.get(mac)
    if slot is None:
        return
    for i in range(slot, slot + 4):
        thermostamp[i] = float("nan")
        thermofilter[i] = 0.0
        thermocount[i] = 0
def send_activation(dev):
    """Write 0xFD 00 activation packet to command channel."""
    if not dev or not dev.command:
        dprint(f"[!] No command char for {getattr(dev, 'obj_path', '?')}")
        return False
    try:
        pkt = Variant('ay', [0xfd, 0x00, 0, 0, 0, 0, 0])
        dprint(f"[‡] Activating {dev.obj_path}")
        dev.command.WriteValue(pkt, {"type": Variant("s", "request")})
    except Exception as e:
        dprint(f"[!] Activation failed {e}")
    return False
class InkbirdDevice:
    def __init__(self, bus, obj_path, props):
        self.bus = bus
        self.obj_path = obj_path
        self.name = props.get("Name").unpack() if "Name" in props else "?"
        self.mac = extract_mac(obj_path)
        self.proxy = bus.get_proxy(SERVICE_NAME, obj_path)
        self.temperature = None
        self.command = None
        self.battery = None
        self.connected = False
        self.binds_done = False
        self.last_connect = time.time() - 5
        self._sig_hooked = False
        self._temp_hooked = False
        self._batt_hooked = False
        self.connect_signals()
    def connect_signals(self):
        if self._sig_hooked:
            return
        self.proxy.PropertiesChanged.connect(self.on_properties)
        self._sig_hooked = True
    def connect(self):
        """Throttle reconnects to avoid event storms."""
        if (time.time() - self.last_connect) < 2:
            return
        self.last_connect = time.time()
        try:
            dprint(f"[+] Connecting {self.name} {self.obj_path}")
            self.proxy.Connect()
        except Exception as e:
            dprint(f"[!] connect failed {e}")
    def cleanup(self):
        dprint(f"[!] Cleaning {self.obj_path}")
        for p in (self.temperature, self.command, self.battery):
            try:
                if p:
                    p.StopNotify()
            except Exception:
                pass
        self.binds_done = False
    def on_properties(self, iface, changed, inv):
        if "Connected" in changed:
            self.connected = changed["Connected"].unpack()
            dprint(f"  Connected={self.connected}")
            if not self.connected and self.mac:
                clear_slot_for_mac(self.mac)
        if "ServicesResolved" in changed:
            ready = changed["ServicesResolved"].unpack()
            dprint(f"  ServicesResolved={ready}")
            if ready:
                self.on_services_resolved()
    def on_services_resolved(self):
        if self.binds_done:
            return
        try:
            self.proxy.Trusted = True
            mgr = self.bus.get_proxy(SERVICE_NAME, "/")
            for path, objdict in mgr.GetManagedObjects().items():
                if not path.startswith(self.obj_path):
                    continue
                if GATT_CHAR_IFACE not in objdict:
                    continue
                uuid = objdict[GATT_CHAR_IFACE]["UUID"].unpack()
                proxy = self.bus.get_proxy(SERVICE_NAME, path)
                # Temperature notify
                if uuid == "0000ff01-0000-1000-8000-00805f9b34fb":
                    self.temperature = proxy
                    if not self._temp_hooked:
                        proxy.PropertiesChanged.connect(self.temp_cb)
                        self._temp_hooked = True
                    try:
                        proxy.StartNotify()
                    except Exception:
                        pass
                # Command channel: prefer char0018 path, fallback ff02
                elif "/char0018" in path or uuid == "0000ff02-0000-1000-8000-00805f9b34fb":
                    self.command = proxy
                    dprint(f"[+] Command bound {path}")
                # Battery notify
                elif uuid == "00002a19-0000-1000-8000-00805f9b34fb":
                    self.battery = proxy
                    if not self._batt_hooked:
                        proxy.PropertiesChanged.connect(self.batt_cb)
                        self._batt_hooked = True
                    try:
                        proxy.StartNotify()
                    except Exception:
                        pass
            self.binds_done = True
            GLib.timeout_add_seconds(1, lambda: send_activation(self))
        except Exception as e:
            dprint(f"[!] on_services_resolved error {e}")
    def temp_cb(self, iface, objdict, inv):
        if "Value" not in objdict:
            return
        data = objdict["Value"].unpack()
        update_temperatures(self.mac, data)
    def batt_cb(self, iface, objdict, inv):
        if "Value" in objdict:
            val = objdict["Value"].unpack()
            if val:
                dprint(f"Battery={val[0]}%")
class InkbirdMonitor:
    def __init__(self):
        self.bus = SystemMessageBus()
        self.loop = EventLoop()
        self.manager = self.bus.get_proxy(SERVICE_NAME, "/")
        self.adapter = self.bus.get_proxy(SERVICE_NAME, ADAPTER_PATH)
        self.inkbirds = {}
        self.manager.InterfacesAdded.connect(self.on_added)
        self.manager.InterfacesRemoved.connect(self.on_removed)
        GLib.timeout_add_seconds(int(WATCHTIME), self.scan)
        GLib.timeout_add_seconds(15, self.watchdog)
    def on_added(self, p, d):
        if DEVICE_IFACE not in d:
            return
        props = d[DEVICE_IFACE]
        name = props.get("Name").unpack() if "Name" in props else ""
        if name not in (INKBIRD_NAME, FRIENDLY_NAME):
            return
        path_to_mac[p] = extract_mac(p)
        dev = self.inkbirds.get(p)
        if dev and not dev.connected:
            dprint(f"[↻] Re-creating proxy {p}")
            try:
                dev.cleanup()
            except Exception:
                pass
            new = InkbirdDevice(self.bus, p, props)
            self.inkbirds[p] = new
            new.connect()
            return
        if not dev:
            dprint(f"[+] New Inkbird {p}")
            dev = InkbirdDevice(self.bus, p, props)
            self.inkbirds[p] = dev
            dev.connect()
    def on_removed(self, p, i):
        if DEVICE_IFACE in i and p in self.inkbirds:
            dprint(f"[−] Device removed {p}")
            mac = path_to_mac.get(p)
            try:
                self.inkbirds[p].cleanup()
            except Exception:
                pass
            del self.inkbirds[p]
            if mac:
                clear_slot_for_mac(mac)
            path_to_mac.pop(p, None)
    def scan(self):
        try:
            for p, d in self.manager.GetManagedObjects().items():
                self.on_added(p, d)
        except Exception as e:
            dprint(f"scan error {e}")
        return True
    def watchdog(self):
        for p, dev in list(self.inkbirds.items()):
            try:
                if not dev.proxy.Connected:
                    dprint(f"[⚙] Watchdog reconnect {p}")
                    dev.connect()
            except Exception as e:
                dprint(f"[⚠] Watchdog error {e}")
        if not self.inkbirds:
            try:
                disc = self.adapter.Get("org.bluez.Adapter1", "Discovering")
                if not disc:
                    self.adapter.StartDiscovery()
                    dprint("[🔍] Restart discovery")
            except Exception as e:
                dprint(f"[!] discovery check failed {e}")
        return True
    def run(self):
        dprint("[*] InkbirdMonitor running")
        self.loop.run()
class InkbirdLogger:
    def __init__(self):
        GLib.timeout_add_seconds(1, self.tick)
    def tick(self):
        write = False
        for i, v in enumerate(thermostamp):
            if math.isnan(v):
                continue
            if thermocount[i] == 0:
                write = True
        if write:
            t = time.time()
            fout.write(f"{t:6.2f} ")
            for v in thermostamp:
                fout.write(f"{v if v < MAXTEMP else float('nan'):6.1f} ")
            fout.write(" [°C]\n")
            fout.flush()
            for i in range(len(thermocount)):
                thermocount[i] = (thermocount[i] + 1 if thermocount[i] < MAXWAIT else 0)
        return True
def update_temperatures(mac, data):
    if mac is None:
        return
    slot = get_or_assign_slot(mac)
    if slot is None:
        return
    def t(ls, ms):
        return (((ms ^ 0x80) << 8) + ls - 0x8000 - 320) / 18
    if len(data) < 12:
        return
    if data[8:12] != [0xFE, 0x7F, 0xFE, 0x7F]:
        return
    vals = [t(*data[2 * i:2 * i + 2]) for i in range(4)]
    for i, v in enumerate(vals):
        idx = slot + i
        lv = thermostamp[idx]
        r = thermocount[idx]
        if r and r < MAXWAIT and (v == lv or v == thermofilter[idx]):
            continue
        if not math.isnan(lv) and abs(v - lv) > 1.5:
            thermostamp[idx] = v if v > MAXTEMP or lv > MAXTEMP else (v + lv) / 2
            thermofilter[idx] = thermostamp[idx]
            continue
        thermofilter[idx] = thermostamp[idx] = v
        thermocount[idx] = 0
def shutdown(sig):
    dprint(f"[!] Signal {sig}, exit.")
    try:
        fout.close()
    except Exception:
        pass
    raise SystemExit
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, shutdown, signal.SIGINT)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, shutdown, signal.SIGTERM)
def main():
    InkbirdLogger()
    InkbirdMonitor().run()
if __name__ == "__main__":
    main()