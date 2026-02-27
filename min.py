#!/usr/bin/env python3
"""
Minimal Inkbird reader – logging fixed + multi-device
"""

import time
import threading
import datetime
import math
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

devices = {}  # path -> {'proxy':, 'temp_char':, 'cmd_char':, 'thermostamp': [NaN]*4, 'stamp': False}

fout = open("/tmp/min_thermal.dat", 'a')  # append mode

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
                dev['stamp'] = True  # trigger logger

def logger():
    for path, dev in list(devices.items()):
        if dev['stamp']:
            dev['stamp'] = False
            t = time.time()
            temps = dev['thermostamp']
            line = f"{t:8.2f}   " + "  ".join(f"{v:5.1f}" if not math.isnan(v) else "  NaN" for v in temps)
            print(f"[{ts()}] Logging: {line}")
            print(line, file=fout)
            fout.flush()
    threading.Timer(1.0, logger).start()

def on_properties_changed(device_path, iface, changed, invalidated):
    if "Connected" in changed:
        connected = changed["Connected"].unpack()
        log(f"Device { 'connected' if connected else 'DISCONNECTED' }: {device_path}")
        if not connected:
            devices.pop(device_path, None)
            threading.Timer(5.0, scan_for_inkbird).start()

    if "ServicesResolved" in changed and changed["ServicesResolved"].unpack():
        log(f"Services resolved for {device_path}")
        activate_device(device_path)

def activate_device(device_path):
    if device_path in devices:
        log(f"{device_path} already activating")
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
        log(f"Missing chars for {device_path} — retry")
        threading.Timer(2.0, lambda: activate_device(device_path)).start()
        return

    try:
        temp_char.StartNotify()
        log(f"ff01 enabled for {device_path}")
        time.sleep(0.5)
    except Exception as e:
        log(f"ff01 enable failed {device_path}: {e}")
        return

    try:
        cmd = [0xfd, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        cmd_char.WriteValue(Variant('ay', cmd), {'type': Variant('s', 'request')})
        log(f"Activation sent {device_path}")
        time.sleep(0.5)
    except Exception as e:
        log(f"Activation failed {device_path}: {e}")
        return

    devices[device_path] = {
        'proxy': bus.get_proxy(SERVICE_NAME, device_path),
        'temp_char': temp_char,
        'cmd_char': cmd_char,
        'thermostamp': [float('NaN')] * 4,
        'stamp': False
    }
    log(f"Activation complete {device_path}")

def scan_for_inkbird():
    log("Scanning for Inkbirds...")
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
                log(f"{path} already connected")
            else:
                proxy.Connect()
                log(f"Connect issued to {path}")
            proxy.Trusted = True
        except Exception as e:
            log(f"Connect failed {path}: {e}")

def periodic_scan():
    scan_for_inkbird()
    threading.Timer(15.0, periodic_scan).start()

def main():
    log("Starting reader...")
    manager.InterfacesAdded.connect(on_interfaces_added)
    scan_for_inkbird()
    threading.Timer(15.0, periodic_scan).start()
    threading.Timer(1.0, logger).start()
    loop.run()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped")
    except Exception as e:
        log(f"Main error: {e}")
    finally:
        fout.close()