#!/usr/bin/env python3
"""
Minimal Inkbird IDT-34c-B temperature reader – single device focus
Goal: Connect → Activate → Receive & print temperatures
"""

import time
import threading
from dasbus.connection import SystemMessageBus
from dasbus.loop import EventLoop
from dasbus.typing import Variant

SERVICE_NAME = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"
DEVICE_IFACE = "org.bluez.Device1"
GATT_CHAR_IFACE = "org.bluez.GattCharacteristic1"

bus = SystemMessageBus()
loop = EventLoop()
manager = bus.get_proxy(SERVICE_NAME, "/")
adapter = bus.get_proxy(SERVICE_NAME, ADAPTER_PATH)

# We'll store the device proxy and characteristics here
inkbird_device = None
temp_char = None
cmd_char = None
thermostamp = [float('NaN')] * 4  # only 1 device for min.py
fout = open("/tmp/min_thermal.dat", 'w')
stamp = False

def logger():
    global stamp
    if stamp:
        t = time.time()
        stamp = False
        line = f"{t:6.2f}   " + "  ".join(f"{v:5.1f}" for v in thermostamp if not isnan(v))
        print(line)
        print(line, file=fout)
        fout.flush()
    threading.Timer(1, logger).start()

def temperature_callback(path, iface, changed, invalidated):
    if "Value" in changed:
        data = changed["Value"].unpack()
        print(f"RAW ff01 notify on {path}: {data} (len={len(data)})")
        parsed = parse_temperatures(data)
        if parsed:
            print(f"Parsed temps: {parsed}")

def parse_temperatures(data):
    if len(data) < 12 or data[8:12] != [0xFE, 0x7F, 0xFE, 0x7F]:
        print("Suspicious packet:", data)
        return None
    def temp(ls, ms):
        val = ((ms ^ 0x80) << 8) + ls - 0x8000
        return round((val - 320) / 18, 1)
    return [
        temp(data[0], data[1]),
        temp(data[2], data[3]),
        temp(data[4], data[5]),
        temp(data[6], data[7])
    ]

def scan_for_inkbird():
    managed = manager.GetManagedObjects()
    for path, interfaces in managed.items():
        on_interfaces_added(path, interfaces)

def on_properties_changed(path, iface, changed, invalidated):
    if "Connected" in changed:
            if changed["Connected"].unpack():
                print(f"Connected: {path}")
            else:
                print(f"Disconnected: {path} — will retry scan")
                threading.Timer(5, scan_for_inkbird).start()
    
    if "ServicesResolved" in changed and changed["ServicesResolved"].unpack():
        print("Services resolved → attempting activation")
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
            print("Found temperature notify char (ff01)")
        elif uuid == "0000ff02-0000-1000-8000-00805f9b34fb":
            cmd_char = char_proxy
            print("Found command write char (ff02)")

    if not (temp_char and cmd_char):
        print("Missing ff01 or ff02 – waiting longer...")
        threading.Timer(2.0, lambda: activate_device(device_path)).start()
        return

    try:
        temp_char.StartNotify()
        temp_char.PropertiesChanged.connect(temperature_callback)
        print("Notifications enabled on ff01")
        time.sleep(0.4)
    except Exception as e:
        print(f"Failed to enable ff01 notify: {e}")
        return

    try:
        cmd = [0xfd, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        cmd_char.WriteValue(Variant('ay', cmd), {'type': Variant('s', 'request')})
        print(f"Activation command sent: fd000000000000")
    except Exception as e:
        print(f"Activation write failed: {e}")
        return

    print("Activation sequence sent – waiting for data...")

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
            inkbird_device.Connect()
            print("Issued Connect()")
            inkbird_device.Trusted = True
        except Exception as e:
            print(f"Connect failed: {e}")

def main():
    print("Starting minimal Inkbird reader – looking for IDT-34c-B...")
    manager.InterfacesAdded.connect(on_interfaces_added)

    managed = manager.GetManagedObjects()
    for path, interfaces in managed.items():
        on_interfaces_added(path, interfaces)

    loop.run()
    threading.Timer(1, logger).start()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user")
    except Exception as e:
        print(f"Main error: {e}")