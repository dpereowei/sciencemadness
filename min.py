#!/usr/bin/env python3
"""
Ultra-minimal diagnostic – focus on callback + logging
"""

import time
import datetime
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

devices = {}  # path -> {'temp_char': proxy}

fout = open("/tmp/min_diag_thermal.dat", 'a')

def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(msg):
    print(f"[{ts()}] {msg}")

def temperature_callback(device_path, iface, changed, invalidated):
    log(f"Callback fired for {device_path} – iface: {iface}")
    if "Value" in changed:
        data = changed["Value"].unpack()
        log(f"RAW notify from {device_path}: {data} (len={len(data)})")
        # Try basic parse for 4 temps (no full logic yet)
        if len(data) >= 8:
            try:
                t1 = round(((data[1] ^ 0x80) << 8 + data[0] - 0x8000 - 320) / 18, 1)
                t2 = round(((data[3] ^ 0x80) << 8 + data[2] - 0x8000 - 320) / 18, 1)
                t3 = round(((data[5] ^ 0x80) << 8 + data[4] - 0x8000 - 320) / 18, 1)
                t4 = round(((data[7] ^ 0x80) << 8 + data[6] - 0x8000 - 320) / 18, 1)
                line = f"{time.time():8.2f}   {t1:5.1f}  {t2:5.1f}  {t3:5.1f}  {t4:5.1f}"
                log(f"QUICK PARSE: {line}")
                print(line, file=fout)
                fout.flush()
            except Exception as e:
                log(f"Parse error: {e}")
    else:
        log(f"Changed keys: {list(changed.keys())}")

def activate_device(device_path):
    managed = manager.GetManagedObjects()
    temp_char = None

    for obj_path, interfaces in managed.items():
        if not obj_path.startswith(device_path + "/"):
            continue
        if GATT_CHAR_IFACE not in interfaces:
            continue

        uuid = interfaces[GATT_CHAR_IFACE]["UUID"].unpack()
        if uuid == "0000ff01-0000-1000-8000-00805f9b34fb":
            temp_char = bus.get_proxy(SERVICE_NAME, obj_path)
            log(f"Found ff01 for {device_path}")
            temp_char.PropertiesChanged.connect(
                lambda i, c, inv: temperature_callback(device_path, i, c, inv)
            )
            break

    if not temp_char:
        log(f"No ff01 found for {device_path}")
        return

    try:
        temp_char.StartNotify()
        log(f"ff01 notifications ENABLED for {device_path}")
    except Exception as e:
        log(f"StartNotify failed {device_path}: {e}")

def on_properties_changed(path, iface, changed, invalidated):
    if "ServicesResolved" in changed and changed["ServicesResolved"].unpack():
        log(f"Services resolved: {path}")
        activate_device(path)

def on_interfaces_added(path, interfaces):
    if DEVICE_IFACE in interfaces:
        name = interfaces[DEVICE_IFACE].get("Name", Variant("s", "")).unpack()
        if name in ['IDT-34c-B', 'INKBIRD']:
            log(f"Found Inkbird: {path}")
            proxy = bus.get_proxy(SERVICE_NAME, path)
            proxy.PropertiesChanged.connect(
                lambda i, c, inv: on_properties_changed(path, i, c, inv)
            )
            try:
                proxy.Connect()
                log(f"Connect issued: {path}")
                proxy.Trusted = True
            except Exception as e:
                log(f"Connect failed: {e}")

def main():
    log("Starting diagnostic reader...")
    manager.InterfacesAdded.connect(on_interfaces_added)

    # Initial scan
    managed = manager.GetManagedObjects()
    for p, d in managed.items():
        on_interfaces_added(p, d)

    loop.run()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped")
    finally:
        fout.close()