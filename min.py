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

def on_properties_changed(path, iface, changed, invalidated):
    if "Connected" in changed and changed["Connected"].unpack():
        print(f"Device connected: {path}")
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

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user")
    except Exception as e:
        print(f"Main error: {e}")