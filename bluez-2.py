#!/usr/bin/env python3
# Read temperature from Inkbird IDT‑34c‑B
# Version 0.99.16‑test (minimal activation sequence)
# GPL v3 — [gnu.org](https://www.gnu.org/licenses/gpl‑3.0.en.html)
#
# Everything else unchanged from v0.99.15 — but the activation
# packet has been trimmed to 0xfd 0x00 to test for handshake.

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
PROP_IFACE="org.freedesktop.DBus.Properties"
GATT_SERVICE_IFACE="org.bluez.GattService1"
GATT_CHAR_IFACE="org.bluez.GattCharacteristic1"
MAXWAIT=20
DEBUG=True

thermostamp=[float("nan")]*24
thermofilter=[0.0]*24
thermocount=[0]*24
allocated_offsets={}
free_offsets={0:0,4:4,8:8,12:12,16:16,20:20}
fout=open("/tmp/thermal.dat","w")

def dprint(*a,**kw):
    if DEBUG: print(*a,**kw)

# ---------------------------------------------------------------------
def allocate(obj_path):
    best=None; offset=1e9
    if obj_path in free_offsets:
        best,offset=obj_path,free_offsets[obj_path]
    else:
        for k,v in free_offsets.items():
            if v<offset: best,offset=k,v
    if best is not None:
        allocated_offsets[obj_path]=offset
        del free_offsets[best]

def deallocate(obj_path):
    if obj_path in allocated_offsets:
        free_offsets[obj_path]=allocated_offsets[obj_path]
        del allocated_offsets[obj_path]

# ---------------------------------------------------------------------
# Minimal activation packet
# ---------------------------------------------------------------------
def reinitialize_inkbird(dev):
    """Send simple activation to enable Inkbird measurement (test)."""
    if not dev or not dev.command:
        dprint(f"[!] No command characteristic for {getattr(dev,'obj_path','?')}")
        return
    try:
        dprint(f"[‡] Activating {dev.obj_path}")
        packet=Variant('ay',[0xfd,0x00,0,0,0,0,0])
        dev.command.WriteValue(packet,{"type":Variant("s","request")})
    except Exception as e:
        dprint(f"[!] Activation write failed {e}")

# ---------------------------------------------------------------------
class InkbirdDevice:
    def __init__(self,bus,obj_path,props):
        self.bus=bus; self.obj_path=obj_path
        self.name=props.get("Name").unpack() if "Name" in props else "?"
        self.proxy=bus.get_proxy(SERVICE_NAME,obj_path)
        self.temperature=self.command=self.battery=None
        self.connected=False; self.ready_for_handshake=False
        self.connect_signals()

    def connect_signals(self):
        self.proxy.PropertiesChanged.connect(
            lambda iface,ch,inv:self.on_properties(iface,ch,inv))

    def connect(self):
        try:
            dprint(f"[+] Connecting {self.name} {self.obj_path}")
            self.proxy.Connect()
        except Exception as e: dprint(f"[!] connect() failed {e}")

    def cleanup(self):
        dprint(f"[!] Cleaning up {self.obj_path}")
        for p in (self.temperature,self.command,self.battery):
            try:
                if p: p.StopNotify()
            except Exception: pass
        self.ready_for_handshake=False
        deallocate(self.obj_path)

    def on_properties(self,iface,changed,inv):
        if "Connected" in changed:
            self.connected=changed["Connected"].unpack()
            dprint(f"  Connected={self.connected}")
        if "ServicesResolved" in changed:
            ready=changed["ServicesResolved"].unpack()
            dprint(f"  ServicesResolved={ready}")
            if ready: self.on_services_resolved()

    def on_services_resolved(self):
        try:
            allocate(self.obj_path)
            self.proxy.Trusted=True
            mgr=self.bus.get_proxy(SERVICE_NAME,"/")
            for path,objdict in mgr.GetManagedObjects().items():
                if not path.startswith(self.obj_path): continue
                if GATT_CHAR_IFACE not in objdict: continue
                uuid=objdict[GATT_CHAR_IFACE]["UUID"].unpack()
                proxy=self.bus.get_proxy(SERVICE_NAME,path)
                if uuid=="0000ff01-0000-1000-8000-00805f9b34fb":
                    self.temperature=proxy
                    proxy.PropertiesChanged.connect(
                        lambda a,b,c:self.temp_cb(a,b,c))
                    proxy.StartNotify()
                elif uuid=="0000ff02-0000-1000-8000-00805f9b34fb":
                    self.command=proxy
                elif uuid=="00002a19-0000-1000-8000-00805f9b34fb":
                    self.battery=proxy
                    proxy.PropertiesChanged.connect(
                        lambda a,b,c:self.batt_cb(a,b,c))
                    proxy.StartNotify()
            dprint(f"[+] Services bound for {self.obj_path}")
        except Exception as e:
            dprint(f"[!] on_services_resolved error {e}")

    def start_handshake(self):
        try: reinitialize_inkbird(self)
        except Exception as e:
            dprint(f"[!] start_handshake failed {e}")
        return False

    def temp_cb(self,iface,objdict,inv):
        if "Value" not in objdict: return
        data=objdict["Value"].unpack()
        if not self.ready_for_handshake:
            self.ready_for_handshake=True
            dprint(f"[→] Notifications active → begin activation {self.obj_path}")
            GLib.timeout_add_seconds(1,self.start_handshake)
        if self.obj_path not in allocated_offsets:
            if self.proxy.Connected: allocate(self.obj_path)
            else: return
        update_temperatures(self.obj_path,data)

    def batt_cb(self,iface,objdict,inv):
        if "Value" in objdict:
            val=objdict["Value"].unpack()
            if val: dprint(f"Battery={val[0]}%")

# ---------------------------------------------------------------------
class InkbirdMonitor:
    def __init__(self):
        self.bus=SystemMessageBus()
        self.loop=EventLoop()
        self.manager=self.bus.get_proxy(SERVICE_NAME,"/")
        self.adapter=self.bus.get_proxy(SERVICE_NAME,"/org/bluez/hci0")
        self.inkbirds={}
        self.manager.InterfacesAdded.connect(self.on_added)
        self.manager.InterfacesRemoved.connect(self.on_removed)
        GLib.timeout_add_seconds(int(WATCHTIME),self.scan_dbus)
        GLib.timeout_add_seconds(15,self.watchdog)

    def on_added(self,obj_path,obj_dict):
        if DEVICE_IFACE not in obj_dict: return
        props=obj_dict[DEVICE_IFACE]
        name=props.get("Name").unpack() if "Name" in props else ""
        if name not in (INKBIRD_NAME,FRIENDLY_NAME): return
        dev=self.inkbirds.get(obj_path)
        if dev:
            if not dev.connected:
                dprint(f"[↻] Re‑creating proxy {obj_path}")
                try: dev.cleanup()
                except Exception: pass
                new=InkbirdDevice(self.bus,obj_path,props)
                self.inkbirds[obj_path]=new; new.connect()
            return
        dprint(f"[+] New Inkbird {obj_path}")
        d=InkbirdDevice(self.bus,obj_path,props)
        self.inkbirds[obj_path]=d; d.connect()

    def on_removed(self,obj_path,ifaces):
        if DEVICE_IFACE in ifaces and obj_path in self.inkbirds:
            dprint(f"[−] Device removed {obj_path}")
            try:self.inkbirds[obj_path].cleanup()
            except Exception: pass
            del self.inkbirds[obj_path]; deallocate(obj_path)

    def scan_dbus(self):
        try:
            for p,d in self.manager.GetManagedObjects().items():
                self.on_added(p,d)
        except Exception as e: dprint(f"scan error {e}")
        return True

    def watchdog(self):
        for p,dev in list(self.inkbirds.items()):
            try:
                if not dev.proxy.Connected:
                    dprint(f"[⚙] Watchdog reconnect {p}")
                    dev.connect()
            except Exception as e: dprint(f"[⚠] Watchdog error {e}")
        if not self.inkbirds:
            try:
                disc=self.adapter.Get("org.bluez.Adapter1","Discovering")
                if not disc:
                    dprint("[🔍] Restart discovery")
                    self.adapter.StartDiscovery()
            except Exception as e: dprint(f"[!] discovery check failed {e}")
        return True

    def run(self):
        dprint("[*] InkbirdMonitor running"); self.loop.run()

# ---------------------------------------------------------------------
class InkbirdLogger:
    def __init__(self):
        GLib.timeout_add_seconds(1,self.tick)
    def tick(self):
        global thermostamp,thermofilter,thermocount
        write=False
        for i,v in enumerate(thermostamp):
            if math.isnan(v): continue
            if thermocount[i]==0: write=True
        if write:
            t=time.time()
            print(f"{t:6.2f} ",end="",file=fout)
            for v in thermostamp:
                print(f"{v if v<MAXTEMP else float('nan'):6.1f} ",end="",file=fout)
            print(" [°C]",file=fout); fout.flush()
            for i in range(len(thermocount)):
                thermocount[i]=(thermocount[i]+1 if thermocount[i]<MAXWAIT else 0)
        return True

# ---------------------------------------------------------------------
def update_temperatures(obj_path,data):
    def temp(ls,ms): return (((ms^0x80)<<8)+ls-0x8000-320)/18
    if data[8:12]!=[0xFE,0x7F,0xFE,0x7F]: dprint("Suspicious",data); return
    t4=[temp(*data[2*i:2*i+2]) for i in range(4)]
    offs=allocated_offsets.get(obj_path,0)
    for i,val in enumerate(t4):
        idx=int(offs+i)
        vlast=thermostamp[idx]; red=thermocount[idx]
        if red and red<MAXWAIT and (val==vlast or val==thermofilter[idx]): continue
        if abs(val-vlast)>1.5:
            thermostamp[idx]=val if val>MAXTEMP or vlast>MAXTEMP else (val+vlast)/2
            thermofilter[idx]=thermostamp[idx]; continue
        thermofilter[idx]=thermostamp[idx]=val; thermocount[idx]=0

# ---------------------------------------------------------------------
def shutdown(sig):
    dprint(f"[!] Signal {sig}, exiting.")
    try:fout.close()
    except Exception:pass
    GLib.MainLoop().quit()

GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT,  shutdown, signal.SIGINT)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, shutdown, signal.SIGTERM)

def main():
    InkbirdLogger(); InkbirdMonitor().run()

if __name__=="__main__":
    main()
