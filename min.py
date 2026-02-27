#!/usr/bin/env python3
"""
Minimal Inkbird IDT-34c-B temperature reader – with disconnect/reconnect handling
"""

import time
import threading
from dasbus.connection import SystemMessageBus
from dasbus.loop import EventLoop
from dasbus.typing import Variant
import math

SERVICE_NAME = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"
DEVICE_IFACE = "org.bluez.Device1"
GATT_CHAR_IFACE = "org.bluez.GattCharacteristic1"

bus = SystemMessageBus()
loop = EventLoop()
manager = bus.get_proxy(SERVICE_NAME, "/")
adapter = bus.get_proxy(SERVICE_NAME, ADAPTER_PATH)

inkbird_device = None
temp_char = None
cmd_char = None

thermostamp = [float('NaN')] * 4
fout = open("/tmp/min_thermal.dat", 'w')

def parse_temperatures(data):
    if len(data) < 12 or data[8:12] != [0xFE, 0x7F, 0xFE, 0x7F]:
        print("Suspicious packet:", data)
        return None
    def temp(ls, ms):
        val = ((ms ^ 0x80) << 8) + ls - 0x8000
        return round((val - 320) / 18, 1)
    t4vec = [temp(data[2*i], data[2*i+1]) for i in range(4)]
    if any(t > 1000 for t in t4vec):
        print("Invalid high temps — skipping")
        return None
    return t4vec

def temperature_callback(obj_path, iface, changed, invalidated):
    if "Value" in changed:
        data = changed["Value"].unpack()
        print(f"RAW ff01 notify from {obj_path}: {data}")
        temps = parse_temperatures(data)
        if temps:
            print(f"Parsed temps: {temps}")
            t = time.time()
            line = f"{t:8.2f}   " + "  ".join(f"{v:5.1f}" if not math.isnan(v) else "  NaN" for v in temps)
            print(line)
            print(line, file=fout)
            fout.flush()

def on_properties_changed(path, iface, changed, invalidated):
    if "Connected" in changed:
        if changed["Connected"].unpack():
            print(f"Device connected: {path}")
        else:
            print(f"Device DISCONNECTED: {path} — retry scan in 5s")
            threading.Timer(5.0, scan_for_inkbird).start()

    if "ServicesResolved" in changed and changed["ServicesResolved"].unpack():
        print("Services resolved → activation")
        activate_device(path)

def activate_device(device_path):
    global temp_char, cmd_char

    managed = manager.GetManagedObjects()
    for obj_path, interfaces in managed.items():
        if not obj_path.startswith(device_path + "/"):
            continue
        if GATT_CHAR_IFACE not in interfaces:
            continue

        props = interfaces[GATT_CHAR_IFACE]
        uuid = props["UUID"].unpack()
        char_proxy = bus.get_proxy(SERVICE_NAME, obj_path)

        if uuid == "0000ff01-0000-1000-8000-00805f9b34fb":
            temp_char = char_proxy
            print("Found ff01")
            temp_char.PropertiesChanged.connect(
                lambda i, c, inv: temperature_callback(device_path, i, c, inv)
            )
        elif uuid == "0000ff02-0000-1000-8000-00805f9b34fb":
            cmd_char = char_proxy
            print("Found ff02")

    if not (temp_char and cmd_char):
        print("Missing chars — retry in 2s")
        threading.Timer(2.0, lambda: activate_device(device_path)).start()
        return

    try:
        temp_char.StartNotify()
        print("ff01 notifications enabled")
        time.sleep(0.5)
    except Exception as e:
        print(f"ff01 notify enable failed: {e}")
        return

    try:
        cmd = [0xfd, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        cmd_char.WriteValue(Variant('ay', cmd), {'type': Variant('s', 'request')})
        print("Activation sent: fd000000000000")
        time.sleep(0.5)
    except Exception as e:
        print(f"Activation failed: {e}")
        return

    print("Activation complete — expecting temps...")

def scan_for_inkbird():
    print("Scanning managed objects for Inkbird...")
    managed = manager.GetManagedObjects()
    for path, interfaces in managed.items():
        on_interfaces_added(path, interfaces)

def on_interfaces_added(path, interfaces):
    global inkbird_device

    if DEVICE_IFACE in interfaces:
        props = interfaces[DEVICE_IFACE]
        try:
            name = props["Name"].unpack()
        except:
            return

        if name not in ['IDT-34c-B', 'INKBIRD']:
            return

        print(f"Found Inkbird: {path}")
        inkbird_device = bus.get_proxy(SERVICE_NAME, path)
        inkbird_device.PropertiesChanged.connect(
            lambda i, c, inv: on_properties_changed(path, i, c, inv)
        )

        try:
            if inkbird_device.Connected:
                print(f"Already connected — skipping Connect()")
            else:
                inkbird_device.Connect()
                print("Issued Connect()")
            inkbird_device.Trusted = True
        except Exception as e:
            print(f"Connect failed: {e}")

def periodic_scan():
    scan_for_inkbird()
    threading.Timer(15.0, periodic_scan).start()

def main():
    print("Minimal Inkbird reader starting...")
    manager.InterfacesAdded.connect(on_interfaces_added)
    scan_for_inkbird()  # initial
    threading.Timer(15.0, periodic_scan).start()  # every 15s
    loop.run()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped")
        fout.close()
    except Exception as e:
        print(f"Main error: {e}")
        fout.close()