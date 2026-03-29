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

import time, math, signal
from dasbus.connection import SystemMessageBus
from dasbus.loop import EventLoop, GLib
from dasbus.typing import Variant

MAXTEMP=1802.5
WATCHTIME=90.1
INKBIRD_NAME="IDT-34c-B"
FRIENDLY_NAME="INKBIRD"
SERVICE_NAME="org.bluez"
ADAPTER_PATH="/org/bluez/hci0"
DEVICE_IFACE="org.bluez.Device1"
GATT_CHAR_IFACE="org.bluez.GattCharacteristic1"
PROP_IFACE="org.freedesktop.DBus.Properties"
MAXWAIT=20
DEBUG=True

thermostamp=[float("nan")]*24
thermofilter=[0.0]*24
thermocount=[0]*24
allocated_offsets={}
free_offsets={0:0,4:4,8:8,12:12,16:16,20:20}
fout=open("/tmp/thermal.dat","w")

def dprint(*a, **kw):
    if DEBUG:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}]", *a, **kw)

def allocate(p):
    if p in allocated_offsets:
        return
    best = None
    off = 1e9
    for k, v in free_offsets.items():
        if v < off:
            best, off = k, v
    if best is not None:
        allocated_offsets[p] = off
        del free_offsets[best]

def deallocate(p):
    if p in allocated_offsets:
        free_offsets[p] = allocated_offsets[p]
        del allocated_offsets[p]

# ---------------------------------------------------------------------
def send_activation(dev):
    if not dev or not dev.command:
        dprint(f"[!] No command char for {getattr(dev, 'obj_path', '?')}")
        return
    try:
        pkt = Variant('ay', [0xfd, 0x00, 0, 0, 0, 0, 0])
        dprint(f"[‡] Activating {dev.obj_path}")
        dev.command.WriteValue(pkt, {"type": Variant("s", "request")})
    except Exception as e:
        dprint(f"[!] Activation failed: {e}")

# ---------------------------------------------------------------------
class InkbirdDevice:
    def __init__(self, bus, obj_path, props):
        self.bus = bus
        self.obj_path = obj_path
        self.name = props.get("Name").unpack() if "Name" in props else "?"
        self.proxy = bus.get_proxy(SERVICE_NAME, obj_path)
        self.temperature = self.command = self.battery = None
        self.connected = False
        self.binds_done = False
        self.offset = None
        self.connect_signals()

    def connect_signals(self):
        self.proxy.PropertiesChanged.connect(self.on_properties)

    def connect(self):
        try:
            dprint(f"[+] Connecting {self.name} {self.obj_path}")
            self.proxy.Connect()
            self.proxy.Trusted = True
        except Exception as e:
            dprint(f"[!] Connect failed: {e}")

    def cleanup(self):
        dprint(f"[!] Cleaning {self.obj_path}")
        for p in (self.temperature, self.command, self.battery):
            try:
                if p:
                    p.StopNotify()
            except Exception:
                pass
        deallocate(self.obj_path)
        self.binds_done = False

    def on_properties(self, iface, changed, inv):
        if "Connected" in changed:
            self.connected = changed["Connected"].unpack()
            dprint(f"  Connected={self.connected} for {self.obj_path}")
            if not self.connected:
                self.cleanup()

        if "ServicesResolved" in changed and changed["ServicesResolved"].unpack():
            dprint(f"  ServicesResolved=True for {self.obj_path}")
            if not self.binds_done:
                self.on_services_resolved()

    def on_services_resolved(self):
        allocate(self.obj_path)
        self.offset = allocated_offsets.get(self.obj_path, 0)
        dprint(f"[+] Allocated offset {self.offset} for {self.obj_path}")

        mgr = self.bus.get_proxy(SERVICE_NAME, "/")
        for path, objdict in mgr.GetManagedObjects().items():
            if not path.startswith(self.obj_path):
                continue
            if GATT_CHAR_IFACE not in objdict:
                continue

            uuid = objdict[GATT_CHAR_IFACE]["UUID"].unpack()
            proxy = self.bus.get_proxy(SERVICE_NAME, path)

            if uuid == "0000ff01-0000-1000-8000-00805f9b34fb":
                self.temperature = proxy
                proxy.PropertiesChanged.connect(self.temp_cb)
                proxy.StartNotify()
                dprint(f"[+] Temperature notify bound for {self.obj_path}")

            elif uuid == "0000ff02-0000-1000-8000-00805f9b34fb" or "/char0018" in path:
                self.command = proxy
                dprint(f"[+] Command char bound for {self.obj_path}")

            elif uuid == "00002a19-0000-1000-8000-00805f9b34fb":
                self.battery = proxy
                proxy.PropertiesChanged.connect(self.batt_cb)
                proxy.StartNotify()

        self.binds_done = True
        GLib.timeout_add_seconds(1, lambda: send_activation(self))

    def temp_cb(self, iface, changed, inv):
        if "Value" not in changed:
            return
        data = changed["Value"].unpack()
        update_temperatures(self.obj_path, data)

    def batt_cb(self, iface, changed, inv):
        if "Value" in changed:
            val = changed["Value"].unpack()
            if val:
                dprint(f"Battery {self.obj_path}: {val[0]}%")

# ---------------------------------------------------------------------
def update_temperatures(p, data):
    def t(ls, ms):
        return round(((ms ^ 0x80) << 8 + ls - 0x8000 - 320) / 18, 1)
    if len(data) < 12 or data[8:12] != [0xFE, 0x7F, 0xFE, 0x7F]:
        return
    vals = [t(*data[2*i:2*i+2]) for i in range(4)]
    off = allocated_offsets.get(p, 0)
    for i, v in enumerate(vals):
        idx = off + i
        lv = thermostamp[idx]
        r = thermocount[idx]
        if r and r < MAXWAIT and (v == lv or v == thermofilter[idx]):
            continue
        if abs(v - lv) > 1.5:
            thermostamp[idx] = v if v > MAXTEMP or lv > MAXTEMP else (v + lv) / 2
            thermofilter[idx] = thermostamp[idx]
            continue
        thermofilter[idx] = thermostamp[idx] = v
        thermocount[idx] = 0

# ---------------------------------------------------------------------
class InkbirdLogger:
    def __init__(self):
        GLib.timeout_add_seconds(1, self.tick)

    def tick(self):
        write = False
        for i, v in enumerate(thermostamp):
            if not math.isnan(v):
                write = True
                break
        if write:
            t = time.time()
            line = f"{t:6.2f}  "
            for v in thermostamp:
                line += f"{v if v < MAXTEMP else float('nan'):6.1f} "
            line += " [°C]"
            print(line)
            print(line, file=fout)
            fout.flush()
        return True

# ---------------------------------------------------------------------
class InkbirdMonitor:
    def __init__(self):
        self.bus = SystemMessageBus()
        self.loop = EventLoop()
        self.manager = self.bus.get_proxy(SERVICE_NAME, "/")
        self.inkbirds = {}
        self.manager.InterfacesAdded.connect(self.on_added)
        self.manager.InterfacesRemoved.connect(self.on_removed)
        GLib.timeout_add_seconds(int(WATCHTIME), self.scan)
        GLib.timeout_add_seconds(20, self.watchdog)   # slower periodic scan

    def on_added(self, p, d):
        if DEVICE_IFACE not in d:
            return
        props = d[DEVICE_IFACE]
        name = props.get("Name").unpack() if "Name" in props else ""
        if name not in (INKBIRD_NAME, FRIENDLY_NAME):
            return

        if p in self.inkbirds:
            dev = self.inkbirds[p]
            if not dev.connected:
                dev.cleanup()
                self.inkbirds[p] = InkbirdDevice(self.bus, p, props)
                self.inkbirds[p].connect()
            return

        dprint(f"[+] New Inkbird {p}")
        dev = InkbirdDevice(self.bus, p, props)
        self.inkbirds[p] = dev
        dev.connect()

    def on_removed(self, p, i):
        if DEVICE_IFACE in i and p in self.inkbirds:
            dprint(f"[−] Removed {p}")
            self.inkbirds[p].cleanup()
            del self.inkbirds[p]
            deallocate(p)

    def scan(self):
        try:
            for p, d in self.manager.GetManagedObjects().items():
                self.on_added(p, d)
        except Exception as e:
            dprint(f"scan error: {e}")
        return True

    def watchdog(self):
        for p, dev in list(self.inkbirds.items()):
            try:
                if not dev.proxy.Connected:
                    dprint(f"[⚙] Watchdog reconnect {p}")
                    dev.connect()
            except Exception:
                pass
        return True

    def run(self):
        dprint("[*] InkbirdMonitor running")
        self.loop.run()

# ---------------------------------------------------------------------
def shutdown(sig):
    dprint(f"[!] Signal {sig}, exiting")
    try:
        fout.close()
    except Exception:
        pass
    GLib.MainLoop().quit()

GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, shutdown, signal.SIGINT)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, shutdown, signal.SIGTERM)

def main():
    InkbirdLogger()
    InkbirdMonitor().run()

if __name__ == "__main__":
    main()