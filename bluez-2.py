#!/usr/bin/env python3
# Read temperature from Inkbird IDT‑34c‑B
# Refactored version 0.99.12‑dev
# Copyright © 2025‑2026 Andrew Robinson of Scappoose.
# This code is released under the GNU Public License v3.0
# https://www.gnu.org/licenses/gpl‑3.0.en.html
#
# Requires: Python 3, dasbus ≥ 1.6, BlueZ 5
# Same temperature logging interface as v0.99.11 — writes /tmp/thermal.dat
#
# ---------------------------------------------------------------------

import os, time, signal, math
from dasbus.connection import SystemMessageBus
from dasbus.loop import EventLoop, GLib
from dasbus.typing import Variant

# ---------------------------------------------------------------------
# Constants and global state (unchanged variable names)
# ---------------------------------------------------------------------

MAXTEMP        = 1802.5
WATCHTIME      = 90.1
INKBIRD_NAME   = 'IDT-34c-B'
FRIENDLY_NAME  = 'INKBIRD'
SERVICE_NAME   = 'org.bluez'
ADAPTER_PATH   = '/org/bluez/hci0'
DEVICE_IFACE   = 'org.bluez.Device1'
PROP_IFACE     = 'org.freedesktop.DBus.Properties'
GATT_SERVICE_IFACE = 'org.bluez.GattService1'
GATT_CHAR_IFACE    = 'org.bluez.GattCharacteristic1'
TEMPERATURE_UUID   = '0000ff00-0000-1000-8000-00805f9b34fb'
MAXWAIT        = 20
DEBUG          = True

# shared temperature arrays (same semantic as old script)
thermostamp = [float('nan')]*24
thermofilter = [0.0]*24
thermocount = [0]*24
allocated_offsets = {}
free_offsets = {0:0,4:4,8:8,12:12,16:16,20:20}

# open the same output file
fout = open("/tmp/thermal.dat","w")

def dprint(*a,**kw):
    if DEBUG: print(*a,**kw)

# ---------------------------------------------------------------------
# Helper allocation functions (preserve names)
# ---------------------------------------------------------------------

def allocate(obj_path):
    offset = 1e9
    best=None
    if obj_path in free_offsets:
        best,offset=obj_path,free_offsets[obj_path]
    else:
        for k,v in free_offsets.items():
            if v<offset:
                best,offset=k,v
    if best is not None:
        allocated_offsets[obj_path]=offset
        del free_offsets[best]

def deallocate(obj_path):
    if obj_path in allocated_offsets:
        free_offsets[obj_path]=allocated_offsets[obj_path]
        del allocated_offsets[obj_path]

# ---------------------------------------------------------------------
# InkbirdDevice  – represents one Inkbird BLE peripheral
# ---------------------------------------------------------------------

class InkbirdDevice:
    def __init__(self,bus,obj_path,props):
        self.bus=bus
        self.obj_path=obj_path
        self.props=props
        self.name=props.get('Name').unpack() if 'Name' in props else '?'
        self.proxy = bus.get_proxy(SERVICE_NAME,obj_path)
        self.gatt_services={}
        self.temperature=None
        self.command=None
        self.battery=None
        self.bound=[]
        self.connected=False
        self.connect_signals()

    # -------------
    def connect_signals(self):
        # monitor property changes (ServicesResolved etc.)
        self.proxy.PropertiesChanged.connect(
            lambda iface,changed,inv: self.on_properties(iface,changed,inv))
    # -------------

    def connect(self):
        try:
            dprint(f"[+] Connecting {self.name} {self.obj_path}")
            self.proxy.Connect()
        except Exception as e:
            dprint(f"[!] Connect failed {e}")

    # -------------
    def cleanup(self):
        """Stop notifications, remove proxies, free offsets."""
        dprint(f"[!] Cleaning up {self.obj_path}")
        try:
            if self.temperature: self.temperature.StopNotify()
        except Exception: pass
        try:
            if self.command: self.command.StopNotify()
        except Exception: pass
        deallocate(self.obj_path)
    # -------------

    def on_properties(self,iface,changed,inv):
        if 'Connected' in changed:
            self.connected = changed['Connected'].unpack()
            dprint(f"    Connected={self.connected}")
        if 'ServicesResolved' in changed:
            ready = changed['ServicesResolved'].unpack()
            dprint(f"    ServicesResolved={ready}")
            if ready:
                self.on_services_resolved()

    # -------------
    def on_services_resolved(self):
        """Bind characteristics and start notifications."""
        try:
            allocate(self.obj_path)
            self.proxy.Trusted=True
        except Exception: pass
        dprint(f"[+] Services resolved for {self.obj_path}")
        for path,objdict in self.bus.get_proxy(SERVICE_NAME,'/').GetManagedObjects().items():
            if not path.startswith(self.obj_path): continue
            if GATT_CHAR_IFACE in objdict:
                uuid=objdict[GATT_CHAR_IFACE]['UUID'].unpack()
                proxy=self.bus.get_proxy(SERVICE_NAME,path)
                if uuid=='0000ff01-0000-1000-8000-00805f9b34fb':
                    self.temperature=proxy
                    proxy.PropertiesChanged.connect(
                        lambda a,b,c:self.temperature_callback(a,b,c))
                    proxy.StartNotify()
                elif uuid=='0000ff02-0000-1000-8000-00805f9b34fb':
                    self.command=proxy
                elif uuid=='00002a19-0000-1000-8000-00805f9b34fb':
                    self.battery=proxy
                    proxy.PropertiesChanged.connect(
                        lambda a,b,c:self.battery_callback(a,b,c))
                    proxy.StartNotify()
        dprint(f"[+] Bound services for {self.obj_path}")

    # -------------
    def temperature_callback(self,iface,objdict,inv):
        if 'Value' not in objdict: return
        data=objdict['Value'].unpack()
        if self.obj_path not in allocated_offsets:
            if self.proxy.Connected: allocate(self.obj_path)
            else:
                dprint(f"Temperature notify for disconnected {self.obj_path}")
                return
        update_temperatures(self.obj_path,data)

    def battery_callback(self,iface,objdict,inv):
        if 'Value' in objdict:
            val=objdict['Value'].unpack()
            if val: dprint(f"Battery {val[0]}%")

# ---------------------------------------------------------------------
# InkbirdMonitor – orchestrates add/remove and overall state
# ---------------------------------------------------------------------

class InkbirdMonitor:
    def __init__(self):
        self.bus=SystemMessageBus()
        self.loop=EventLoop()
        self.manager=self.bus.get_proxy(SERVICE_NAME,'/')
        self.adapter=self.bus.get_proxy(SERVICE_NAME,ADAPTER_PATH)
        self.inkbirds={}
        # DBus signals
        self.manager.InterfacesAdded.connect(self.on_added)
        self.manager.InterfacesRemoved.connect(self.on_removed)
        GLib.timeout_add_seconds(int(WATCHTIME),self.scan_dbus)
        # Watchdog: check every 15 s for disconnected devices
        GLib.timeout_add_seconds(15, self.watchdog)

    def on_added(self, obj_path, obj_dict):
        """Handle new BlueZ objects (device discovery)."""
        if DEVICE_IFACE not in obj_dict:
            return
        props = obj_dict[DEVICE_IFACE]
        name = props.get("Name").unpack() if "Name" in props else ""
        if name not in (INKBIRD_NAME, FRIENDLY_NAME):
            return
        # Check if we already know this path
        existing = self.inkbirds.get(obj_path)
        if existing:
            # If previously disconnected, rebuild the proxy so reconnect works.
            if not existing.connected:
                dprint(f"[↻] Re‑creating proxy for {obj_path}")
                try:
                    existing.cleanup()
                except Exception:
                    pass
                newdev = InkbirdDevice(self.bus, obj_path, props)
                self.inkbirds[obj_path] = newdev
                newdev.connect()
            else:
                # Still connected; ignore duplicate Added signal.
                dprint(f"[≡] Still connected {obj_path}")
            return
        # Brand‑new discovery
        dprint(f"[+] New Inkbird discovered {obj_path}")
        newdev = InkbirdDevice(self.bus, obj_path, props)
        self.inkbirds[obj_path] = newdev
        newdev.connect()

    def on_removed(self,obj_path,ifaces):
        if obj_path in self.inkbirds:
            dprint(f"[−] Device removed {obj_path}")
            self.inkbirds[obj_path].cleanup()
            del self.inkbirds[obj_path]
            # keep offset freed for future rediscovery
            deallocate(obj_path)

    def scan_dbus(self):
        """Periodic sync with BlueZ object tree."""
        try:
            for p,d in self.manager.GetManagedObjects().items():
                self.on_added(p,d)
        except Exception as e:
            dprint(f"scan_dbus error {e}")
        return True  # keep repeating
    
    def watchdog(self):
        """Reconnect any devices that show Connected=False."""
        for path, dev in list(self.inkbirds.items()):
            try:
                if not dev.proxy.Connected:
                    dprint(f"[⚙] Watchdog reconnect {path}")
                    dev.connect()
            except Exception as e:
                dprint(f"[⚠] Watchdog error {e}")
        return True  # keep repeating

    def run(self):
        dprint("[*] InkbirdMonitor running")
        self.loop.run()

# ---------------------------------------------------------------------
# InkbirdLogger – periodic file logger to /tmp/thermal.dat
# ---------------------------------------------------------------------

class InkbirdLogger:
    def __init__(self):
        self.laststamp=time.time()
        GLib.timeout_add_seconds(1,self.tick)

    def tick(self):
        global thermostamp, thermofilter, thermocount
        stamp=False
        # find if any channel changed
        for i,v in enumerate(thermostamp):
            if math.isnan(v): continue
            if thermocount[i]==0: stamp=True
        if stamp:
            t=time.time()
            print("%6.2f "%t,end="",file=fout)
            for v in thermostamp:
                print("%6.1f "%(v if v<MAXTEMP else float('nan')),end="",file=fout)
            print(" [°C]",file=fout)
            fout.flush()
            for i in range(len(thermocount)):
                n=thermocount[i]
                thermocount[i]=n+1 if n<MAXWAIT else 0
            self.laststamp=t
        return True

# ---------------------------------------------------------------------
# Utility: temperature update (preserve same math)
# ---------------------------------------------------------------------

def update_temperatures(obj_path,data):
    def temperature(ls,ms):
        value=((ms^0x80)<<8)+ls-0x8000
        return (value-320)/18
    if data[8:12]!=[0xFE,0x7F,0xFE,0x7F]:
        dprint("Suspicious packet",data)
        return
    t4=[temperature(*data[2*i:2*i+2]) for i in range(4)]
    offs=allocated_offsets.get(obj_path,0)
    for i,val in enumerate(t4):
        idx=int(offs+i)
        vlast=thermostamp[idx]
        redundant=thermocount[idx]
        if redundant and redundant<MAXWAIT and (val==vlast or val==thermofilter[idx]):
            continue
        if abs(val-vlast)>1.5:
            if val>MAXTEMP or vlast>MAXTEMP:
                thermostamp[idx]=val
            else:
                thermostamp[idx]=(val+vlast)/2.
            thermofilter[idx]=thermostamp[idx]
            continue
        thermofilter[idx]=thermostamp[idx]
        thermostamp[idx]=val
        thermocount[idx]=0

# ---------------------------------------------------------------------
# Signal handling and entry point
# ---------------------------------------------------------------------

def shutdown(*a):
    dprint("[!] Caught termination signal")
    fout.close()
    raise SystemExit

def main():
    signal.signal(signal.SIGINT,shutdown)
    signal.signal(signal.SIGTERM,shutdown)
    InkbirdLogger()
    mon=InkbirdMonitor()
    mon.run()

if __name__=="__main__":
    main()
