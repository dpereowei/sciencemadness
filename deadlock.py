import time
import threading
import signal
import os
from enum import Enum
from dasbus.connection import SystemMessageBus
from dasbus.loop import EventLoop
from dasbus.typing import Variant
import weakref

# ---------------- Constants ----------------
MAXTEMP = 1802.5
WATCHTIME = 45.0
INKBIRD_NAME = 'IDT-34c-B'
FRIENDLY_NAME = 'INKBIRD'
ADAPTER_PATH = "/org/bluez/hci0"
SERVICE_NAME = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHAR_IFACE = "org.bluez.GattCharacteristic1"
TEMPERATURE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"

MAXWAIT = 20
INITIAL_BACKOFF = 2.0
MAX_BACKOFF = 16.0

# ---------------- Globals ----------------
bus = SystemMessageBus()
loop = EventLoop()
adapter = bus.get_proxy(SERVICE_NAME, ADAPTER_PATH)
manager = bus.get_proxy(SERVICE_NAME, "/")

fout = open("/tmp/thermal.dat", 'w')
thermostamp = [float('NaN')] * 24
thermofilter = [0.] * 24
thermocount = [0] * 24
stamp = False
laststamp = time.time()

allocated_offsets = {}
free_offsets = {0: 0, 4: 4, 8: 8, 12: 12, 16: 16, 20: 20}

temperatures = weakref.WeakValueDictionary()
commands = weakref.WeakValueDictionary()
batteries = weakref.WeakValueDictionary()
bind = {}
gatt_services = {}

# ---------------- Device State Machine ----------------
class DeviceState(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    PSEUDO_PAIRING = 3
    ACTIVE = 4
    TEARDOWN = 5

class InkbirdDevice:
    def __init__(self, obj_path, proxy):
        self.obj_path = obj_path
        self.proxy = proxy
        self.state = DeviceState.DISCONNECTED
        self.lock = threading.Lock()
        self.allocated = False
        self.retry_backoff = INITIAL_BACKOFF
        self.retry_timer = None

    # ---------------- State Transitions ----------------
    def transition(self, new_state):
        with self.lock:
            self.state = new_state

    def can_act(self, expected_state):
        with self.lock:
            return self.state == expected_state

    # ---------------- Retry Scheduling ----------------
    def schedule_retry(self, callback):
        with self.lock:
            if self.retry_timer:
                self.retry_timer.cancel()
            backoff = min(self.retry_backoff, MAX_BACKOFF)
            self.retry_timer = threading.Timer(backoff, callback)
            self.retry_timer.start()
            self.retry_backoff *= 2

    def cancel_retry(self):
        with self.lock:
            if self.retry_timer:
                self.retry_timer.cancel()
                self.retry_timer = None
            self.retry_backoff = INITIAL_BACKOFF

# ---------------- Helper Functions ----------------
def deallocate(obj_path):
    if obj_path in allocated_offsets:
        free_offsets[obj_path] = allocated_offsets[obj_path]
        del allocated_offsets[obj_path]

def allocate(obj_path):
    offset = 1e9
    best = None
    if obj_path in free_offsets:
        best, offset = obj_path, free_offsets[obj_path]
    else:
        for key in free_offsets:
            if offset > free_offsets[key]:
                best, offset = key, free_offsets[key]
    allocated_offsets[obj_path] = offset
    if best in free_offsets:
        del free_offsets[best]

# ---------------- Teardown ----------------
def teardown_device(dev_path):
    if dev_path not in inkbirds:
        return
    device = inkbirds[dev_path]
    device.transition(DeviceState.TEARDOWN)
    device.cancel_retry()

    print(f"Teardown device: {dev_path}")
    for char_dict in [temperatures, commands, batteries]:
        if dev_path in char_dict:
            try:
                char_dict[dev_path].StopNotify()
            except Exception as e:
                print(f"StopNotify failed: {e}")
    try:
        device.proxy.Disconnect()
    except Exception:
        pass

    for d in [temperatures, commands, batteries]:
        if dev_path in d:
            d.pop(dev_path, None)
    if dev_path in bind:
        del bind[dev_path]
    deallocate(dev_path)
    try:
        adapter.RemoveDevice(dev_path)
    except Exception:
        pass
    del inkbirds[dev_path]

# ---------------- Temperature Handling ----------------
def temperature(lsbyte, msbyte):
    value = ((msbyte^0x80)<<8)+lsbyte - 0x8000
    return (value-320)/18

def update_temperatures(obj_path, data):
    global stamp
    if len(data) < 12 or data[8:12] != [0xFE,0x7F,0xFE,0x7F]:
        return
    t4vec = [temperature(*data[2*i:2*i+2]) for i in range(4)]
    offset = allocated_offsets[obj_path]
    for i, value in enumerate(t4vec):
        vlast = thermostamp[offset+i]
        redundant = thermocount[offset+i]
        if redundant and redundant < MAXWAIT and (value == vlast or value == thermofilter[offset+i]):
            continue
        thermostamp[offset+i] = (value + thermostamp[offset+i])/2. if abs(value-vlast) > 1.5 else value
        thermofilter[offset+i] = thermostamp[offset+i]
        if not stamp:
            thermocount[offset+i] = 0
            stamp = True

def temperature_callback(obj_path, obj_iface, obj_dict, invalidated):
    if "Value" in obj_dict:
        if obj_path in inkbirds and obj_path not in allocated_offsets:
            if inkbirds[obj_path].can_act(DeviceState.ACTIVE):
                allocate(obj_path)
        update_temperatures(obj_path, obj_dict['Value'].unpack())

# ---------------- Binding ----------------
def bind_notify(proxy, callback, o_path):
    proxy.PropertiesChanged.connect(lambda iface, dict, inval: callback(o_path, iface, dict, inval))
    proxy.StartNotify()

# ---------------- Pseudo-pairing ----------------
def reinitialize_inkbird(obj_path):
    generic_init = [Variant('ay', [0x02,0x01,0x00,0x00,0x00,0x00,0x00]),
                    Variant('ay', [0x02,0x02,0x00,0x00,0x00,0x00,0x00]),
                    Variant('ay', [0x02,0x04,0x00,0x00,0x00,0x00,0x00])]
    for i in generic_init:
        commands[obj_path].WriteValue(i, {'type':Variant('s','request')})

def run_pseudo_pairing(obj_path):
    device = inkbirds.get(obj_path)
    if not device or not device.can_act(DeviceState.CONNECTED):
        return False
    try:
        temperatures[obj_path].StartNotify()
        start_cmd = [0xfd,0x00,0x00,0x00,0x00,0x00,0x00]
        commands[obj_path].WriteValue(Variant('ay', start_cmd), {'type':Variant('s','request')})
        reinitialize_inkbird(obj_path)
        if obj_path in bind:
            for proxy, cb, path in bind[obj_path]:
                bind_notify(proxy, cb, path)
        device.transition(DeviceState.ACTIVE)
        device.retry_backoff = INITIAL_BACKOFF
        return True
    except Exception as e:
        device.schedule_retry(lambda: retry_pseudo_pairing(obj_path))
        return False

def retry_pseudo_pairing(obj_path):
    device = inkbirds.get(obj_path)
    if device and device.can_act(DeviceState.CONNECTED):
        run_pseudo_pairing(obj_path)

# ---------------- ServicesResolved ----------------
def services_resolved_callback(obj_path, obj_iface, obj_dict, invalidated):
    if 'ServicesResolved' not in obj_dict:
        return False
    resolved = obj_dict['ServicesResolved'].unpack()
    device = inkbirds.get(obj_path)
    if not device:
        return False
    if resolved:
        device.transition(DeviceState.CONNECTED)
        run_pseudo_pairing(obj_path)
    else:
        teardown_device(obj_path)

# ---------------- Interface Callbacks ----------------
def interface_added_callback(obj_path, obj_dict):
    if DEVICE_IFACE not in obj_dict:
        return
    name = obj_dict[DEVICE_IFACE].get('Name', Variant('s','')).unpack()
    if name not in [INKBIRD_NAME, FRIENDLY_NAME]:
        return

    device = inkbirds.get(obj_path)
    if device is None:
        proxy = bus.get_proxy(SERVICE_NAME, obj_path)
        device = InkbirdDevice(obj_path, proxy)
        inkbirds[obj_path] = device

    if device.can_act(DeviceState.DISCONNECTED):
        device.transition(DeviceState.CONNECTING)
        try:
            device.proxy.Connect()
            device.proxy.Trusted = True
            device.transition(DeviceState.CONNECTED)
        except Exception:
            device.transition(DeviceState.DISCONNECTED)

def interfaces_removed_callback(path, interfaces):
    if DEVICE_IFACE in interfaces and path in inkbirds:
        teardown_device(path)

# ---------------- Logger & Scan ----------------
def logger():
    global stamp, laststamp
    if not stamp and (time.time()-laststamp) > 120:
        for services in gatt_services:
            if gatt_services[services]:
                obj_path = os.path.dirname(services)
                temperatures[obj_path].ReadValue({'type':Variant('s','request')})
                laststamp = time.time()
    if stamp:
        t = time.time()
        sample = enumerate(list(thermostamp))
        stamp, laststamp = False, t
        for i in range(len(thermocount)):
            thermocount[i] = min(thermocount[i]+1, MAXWAIT)
        print(f"{t:6.2f} ", end="", file=fout)
        for i, value in sample:
            print(f"{value if value < MAXTEMP else float('NaN'):6.1f} ", end="", file=fout)
        print("  [°C]", file=fout)
        fout.flush()
    threading.Timer(1, logger).start()

def scan_dbus():
    managed = manager.GetManagedObjects()
    for obj_path, obj_dict in managed.items():
        interface_added_callback(obj_path, obj_dict)
    current_paths = set(managed.keys())
    for path in list(inkbirds.keys()):
        if path not in current_paths:
            teardown_device(path)
    threading.Timer(WATCHTIME, scan_dbus).start()

# ---------------- Main ----------------
def signal_handler(signum, frame):
    for path in list(inkbirds.keys()):
        teardown_device(path)
    if loop is not None:
        loop.quit()
    exit(0)

try:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    manager.InterfacesAdded.connect(interface_added_callback)
    manager.InterfacesRemoved.connect(interfaces_removed_callback)
    threading.Timer(1, scan_dbus).start()
    threading.Timer(1, logger).start()
    loop.run()
except Exception as e:
    print(f"Main loop exception: {e}")
    raise
