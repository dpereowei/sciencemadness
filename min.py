#!/usr/bin/env python3
"""
Minimal Inkbird IDT-34c-B reader – fixed logging + multi-device basics
"""

import time
import threading
import datetime
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

# Dict for multi-device support
devices = {}  # path -> {'proxy': proxy, 'temp_char': char, 'cmd_char': char, 'thermostamp': [NaN]*4}

def ts():
    return datetime.datetime.now().strftime("%H:%M:%S")

def log(msg):
    print(f"[{ts()}] {msg}")

def parse_temperatures(data):
    if len(data) < 12 or data[8:12] != [0xFE, 0x7F, 0xFE, 0x7F]:
        log(f"Suspicious packet: {data}")
        return None
    def temp(ls, ms):
        val = ((ms ^ 0x80) << 8) + ls - 0x8000
        return round((val - 320) / 18, 1)
    t4vec = [temp(data[2*i], data[2*i+1]) for i in range(4)]
    if any(t > 1000 for t in t4vec):
        log("Invalid high temps — skipping")
        return None
    return t4vec

def temperature_callback(device_path, iface, changed, invalidated):
    if "Value" in changed:
        data = changed["Value"].unpack()
        log(f"RAW ff01 notify from {device_path}: {data}")
        temps = parse_temperatures(data)
        if temps:
            log(f"Parsed temps from {device_path}: {temps}")
            dev = devices.get(device_path)
            if dev:
                dev['thermostamp'] = temps
                # Log to file
                t = time.time()
                line = f"{t:8.2f}   " + "  ".join(f"{v:5.1f}" if not math.isnan(v) else "  NaN" for v in temps)
                print(line)
                with open("/tmp/min_thermal.dat", 'a') as f:
                    print(line, file=f)

def on_properties_changed(device_path, iface, changed, invalidated):
    if "Connected" in changed:
        connected = changed["Connected"].unpack()
        log(f"Device { 'connected' if connected else 'DISCONNECTED' }: {device_path}")
        if not connected:
            # Cleanup and retry scan
            if device_path in devices:
                del devices[device_path]
            threading.Timer(5.0, scan_for_inkbird).start()

    if "ServicesResolved" in changed and changed["ServicesResolved"].unpack():
        log(f"Services resolved for {device_path} → activation")
        activate_device(device_path)

def activate_device(device_path):
    if device_path in devices:
        log(f"Already activating {device_path} — skipping")
        return

    temp_char = None
    cmd_char = None

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
            log(f"Found ff01 for {device_path}")
            temp_char.PropertiesChanged.connect(
                lambda i, c, inv: temperature_callback(device_path, i, c, inv)
            )
        elif uuid == "0000ff02-0000-1000-8000-00805f9b34fb":
            cmd_char = char_proxy
            log(f"Found ff02 for {device_path}")

    if not (temp_char and cmd_char):
        log(f"Missing chars for {device_path} — retry in 2s")
        threading.Timer(2.0, lambda: activate_device(device_path)).start()
        return

    try:
        temp_char.StartNotify()
        log(f"ff01 notifications enabled for {device_path}")
        time.sleep(0.5)
    except Exception as e:
        log(f"ff01 notify enable failed for {device_path}: {e}")
        return

    try:
        cmd = [0xfd, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        cmd_char.WriteValue(Variant('ay', cmd), {'type': Variant('s', 'request')})
        log(f"Activation sent for {device_path}: fd000000000000")
        time.sleep(0.5)
    except Exception as e:
        log(f"Activation failed for {device_path}: {e}")
        return

    # Store device state
    devices[device_path] = {
        'proxy': bus.get_proxy(SERVICE_NAME, device_path),
        'temp_char': temp_char,
        'cmd_char': cmd_char,
        'thermostamp': [float('NaN')] * 4
    }
    log(f"Activation complete for {device_path} — expecting temps...")

def scan_for_inkbird():
    log("Scanning for Inkbird devices...")
    managed = manager.GetManagedObjects()
    for path, interfaces in managed.items():
        on_interfaces_added(path, interfaces)

def on_interfaces_added(path, interfaces):
    if DEVICE_IFACE in interfaces:
        props = interfaces[DEVICE_IFACE]
        try:
            name = props["Name"].unpack()
        except:
            return

        if name not in ['IDT-34c-B', 'INKBIRD']:
            return

        log(f"Found Inkbird: {path}")
        proxy = bus.get_proxy(SERVICE_NAME, path)
        proxy.PropertiesChanged.connect(
            lambda i, c, inv: on_properties_changed(path, i, c, inv)
        )

        try:
            if proxy.Connected:
                log(f"{path} already connected — skipping Connect()")
            else:
                proxy.Connect()
                log(f"Issued Connect() to {path}")
            proxy.Trusted = True
        except Exception as e:
            log(f"Connect failed for {path}: {e}")

def periodic_scan():
    scan_for_inkbird()
    threading.Timer(15.0, periodic_scan).start()

def main():
    log("Minimal Inkbird reader starting...")
    manager.InterfacesAdded.connect(on_interfaces_added)
    scan_for_inkbird()  # initial
    threading.Timer(15.0, periodic_scan).start()
    loop.run()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped by user")
    except Exception as e:
        log(f"Main error: {e}")
    finally:
        fout.close()