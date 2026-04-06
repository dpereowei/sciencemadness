#!/usr/bin/env python3

import time, math, signal
from dasbus.connection import SystemMessageBus
from dasbus.loop import EventLoop, GLib
from dasbus.typing import Variant

MAXTEMP=1802.5
WATCHTIME=90
INKBIRD_NAME="IDT-34c-B"
FRIENDLY_NAME="INKBIRD"
SERVICE_NAME="org.bluez"
ADAPTER_PATH="/org/bluez/hci0"
DEVICE_IFACE="org.bluez.Device1"
GATT_CHAR_IFACE="org.bluez.GattCharacteristic1"

MAXWAIT=20
DEBUG=True

# ---------------- GLOBAL STATE ----------------
thermostamp=[float("nan")]*24
thermofilter=[0.0]*24
thermocount=[0]*24

# FIX: stable slot mapping
device_slots={}
free_slots=[0,4,8,12,16,20]

fout=open("/tmp/thermal.dat","w")

def dprint(*a):
    if DEBUG: print(*a)

# ---------------- SLOT MGMT ----------------
def allocate_slot(dev_id):
    if dev_id in device_slots:
        return device_slots[dev_id]

    if not free_slots:
        return None

    slot=free_slots.pop(0)
    device_slots[dev_id]=slot
    return slot

def free_slot(dev_id):
    if dev_id in device_slots:
        free_slots.append(device_slots[dev_id])
        del device_slots[dev_id]

# ---------------- DEVICE ----------------
class InkbirdDevice:
    def __init__(self,bus,adapter,obj_path,props):
        self.bus=bus
        self.adapter=adapter
        self.obj_path=obj_path
        self.proxy=bus.get_proxy(SERVICE_NAME,obj_path)

        self.name=props.get("Name").unpack() if "Name" in props else "?"

        self.temperature=None
        self.command=None
        self.battery=None

        self.connected=False
        self.binds_done=False

        # FIX: liveness tracking
        self.last_seen=0

        self._sig_hooked=False
        self.connect_signals()

    # ---------------- SIGNALS ----------------
    def connect_signals(self):
        if self._sig_hooked:
            return
        self.proxy.PropertiesChanged.connect(self.on_properties)
        self._sig_hooked=True

    def disconnect_signals(self):
        if not self._sig_hooked:
            return
        try:
            self.proxy.PropertiesChanged.disconnect(self.on_properties)
        except:
            pass
        self._sig_hooked=False

    # ---------------- CORE ----------------
    def connect(self):
        try:
            dprint(f"[+] Connect {self.obj_path}")
            self.proxy.Connect()
        except Exception as e:
            dprint(f"[!] connect fail {e}")

    def cleanup(self):
        dprint(f"[!] Cleanup {self.obj_path}")

        for p in (self.temperature,self.command,self.battery):
            try:
                if p: p.StopNotify()
            except: pass

        self.disconnect_signals()

        free_slot(self.obj_path)

        self.binds_done=False

    # FIX: HARD RESET
    def force_reset(self):
        dprint(f"[!!!] FORCE RESET {self.obj_path}")

        try:
            self.proxy.Disconnect()
        except: pass

        try:
            self.adapter.RemoveDevice(self.obj_path)
        except: pass

        self.cleanup()

    # ---------------- EVENTS ----------------
    def on_properties(self,iface,changed,inv):

        if "Connected" in changed:
            self.connected=changed["Connected"].unpack()
            dprint(f"  Connected={self.connected}")

        if "ServicesResolved" in changed:
            ready=changed["ServicesResolved"].unpack()
            dprint(f"  ServicesResolved={ready}")

            if ready:
                self.on_services_resolved()

    def on_services_resolved(self):
        if self.binds_done:
            return

        slot=allocate_slot(self.obj_path)
        if slot is None:
            dprint("[!] No slots available")
            return

        self.proxy.Trusted=True

        mgr=self.bus.get_proxy(SERVICE_NAME,"/")

        for path,objdict in mgr.GetManagedObjects().items():
            if not path.startswith(self.obj_path):
                continue
            if GATT_CHAR_IFACE not in objdict:
                continue

            uuid=objdict[GATT_CHAR_IFACE]["UUID"].unpack()
            proxy=self.bus.get_proxy(SERVICE_NAME,path)

            if uuid=="0000ff01-0000-1000-8000-00805f9b34fb":
                self.temperature=proxy
                proxy.PropertiesChanged.connect(self.temp_cb)
                proxy.StartNotify()

            elif uuid=="0000ff02-0000-1000-8000-00805f9b34fb":
                self.command=proxy

        self.binds_done=True

        # FIX: immediate activation
        GLib.timeout_add(500, lambda: self.activate())

    def activate(self):
        if not self.command:
            return False
        try:
            pkt=Variant('ay',[0xfd,0x00,0,0,0,0,0])
            self.command.WriteValue(pkt,{"type":Variant("s","request")})
            dprint(f"[‡] Activated {self.obj_path}")
        except Exception as e:
            dprint(f"[!] activation fail {e}")
        return False

    # ---------------- DATA ----------------
    def temp_cb(self,iface,objdict,inv):
        if "Value" not in objdict:
            return

        data=objdict["Value"].unpack()

        slot=device_slots.get(self.obj_path)
        if slot is None:
            return

        # FIX: liveness update
        self.last_seen=time.time()

        update_temperatures(slot,data)

# ---------------- MONITOR ----------------
class InkbirdMonitor:
    def __init__(self):
        self.bus=SystemMessageBus()
        self.loop=EventLoop()
        self.manager=self.bus.get_proxy(SERVICE_NAME,"/")
        self.adapter=self.bus.get_proxy(SERVICE_NAME,ADAPTER_PATH)

        self.devices={}

        self.manager.InterfacesAdded.connect(self.on_added)
        self.manager.InterfacesRemoved.connect(self.on_removed)

        GLib.timeout_add_seconds(15,self.watchdog)

    def on_added(self,p,d):
        if DEVICE_IFACE not in d:
            return

        props=d[DEVICE_IFACE]
        name=props.get("Name").unpack() if "Name" in props else ""

        if name not in (INKBIRD_NAME,FRIENDLY_NAME):
            return

        if p not in self.devices:
            dprint(f"[+] New device {p}")
            dev=InkbirdDevice(self.bus,self.adapter,p,props)
            self.devices[p]=dev
            dev.connect()

    def on_removed(self,p,i):
        if DEVICE_IFACE in i and p in self.devices:
            dprint(f"[-] Removed {p}")
            self.devices[p].cleanup()
            del self.devices[p]

    # FIX: SMART WATCHDOG
    def watchdog(self):
        now=time.time()

        for p,dev in list(self.devices.items()):
            try:
                # zombie detection
                if dev.connected and dev.binds_done:
                    if now - dev.last_seen > 30:
                        dprint("[!] stale device detected")
                        dev.force_reset()
                        continue

                # broken connection
                if not dev.connected:
                    dev.connect()

            except Exception as e:
                dprint(f"[!] watchdog error {e}")

        return True

    def run(self):
        dprint("[*] Running monitor")
        self.loop.run()

# ---------------- LOGGING ----------------
def update_temperatures(slot,data):
    def t(ls,ms): return(((ms^0x80)<<8)+ls-0x8000-320)/18

    if len(data)<12 or data[8:12]!=[0xFE,0x7F,0xFE,0x7F]:
        return

    vals=[t(*data[2*i:2*i+2]) for i in range(4)]

    for i,v in enumerate(vals):
        idx=slot+i
        thermostamp[idx]=v

# ---------------- MAIN ----------------
def main():
    InkbirdMonitor().run()

if __name__=="__main__":
    main()