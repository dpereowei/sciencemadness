#!/bin/env python
# Read temperature from inkbird IDT-34c-B 
# Written  November, 2025 - January, 2026 by Andrew Robinson of Scappoose.
# Version 0.99.11 (unstable alpha code).
# This code is released under the GNU public license, 3.0
# https://www.gnu.org/licenses/gpl-3.0.en.html
#
# Pre-Requisites virtual python, and dasbus library.
# Virtual python is required on most systems with system package installs of python.
# V.P. allows a local user (not superuser) to install python packages without wrecking
# the operating system's version of python.
#
# python3 -m venv --system-site-packages py_envs
# pip3 install dasbus

# Note, the inkbird protocol is proprietary and may change.
# To list your active bluetooth devices and services do:
# busctl tree org.bluez
# busctl introspect "org.bluez" "/org/bluez/hci0/dev_xx_xx_xx_xx_xx_xx"

# To log bluetooth bus activity for wireshark analysis:
# sudo btmon > rpi.log
# Scan

import time
import threading
import signal
import os
from dasbus.connection import SystemMessageBus
from dasbus.loop import EventLoop
from dasbus.typing import Variant
from dasbus.signal import Signal

import weakref

# Characteristic properties bitmask
# [Extended][Auth_Sign][Indicate][Notify]  [Write][Write-NoResp][Read][Broadcast]

MAXTEMP = 1802.5
WATCHTIME = 90.1 
INKBIRD_NAME='IDT-34c-B'
FRIENDLY_NAME='INKBIRD'
ADAPTER_PATH = "/org/bluez/hci0"
SERVICE_NAME = "org.bluez"
PROP_IFACE="org.freedesktop.DBus.Properties"
DEVICE_IFACE="org.bluez.Device1"
ADAPTER_IFACE="org.bluez.Adapter1"
GATT_SERVICE_IFACE="org.bluez.GattService1"
GATT_CHAR_IFACE="org.bluez.GattCharacteristic1"
GATT_DESC_IFACE="org.bluez.GattDescriptor1"

TEMPERATURE_UUID="0000ff00-0000-1000-8000-00805f9b34fb"

bus=SystemMessageBus()
loop=EventLoop()
adapter = bus.get_proxy( SERVICE_NAME, ADAPTER_PATH )
manager = bus.get_proxy( SERVICE_NAME, "/" ) 

MAXWAIT=20 # Maximum stamping before termperature is not considered redundant. 
fout = open( "/tmp/thermal.dat", 'w' )
thermostamp=[ float('NaN') ]*24
thermofilter=[ 0. ]*24
thermocount=[ 0 ]*24  # How many times has temperature already been stamped to the log file.  (Redundancy limiter).
stamp = False
laststamp = time.time()

allocated_offsets={}
free_offsets={ 0:0, 4:4, 8:8, 12:12, 16:16, 20:20 }
inkbirds=weakref.WeakValueDictionary()
gatt_services={}
commands=weakref.WeakValueDictionary()
temperatures=weakref.WeakValueDictionary()
batteries=weakref.WeakValueDictionary()
bind={}
last_temp=[]

def signal_handler( signum, frame ):
    print("Signal received, tearing down all devices")
    for path in list(inkbirds.keys()):
        teardown_device(path)
    if loop is not None:
        loop.quit()
    exit(2)

def deallocate(obj_path):
    if obj_path in allocated_offsets:
       free_offsets[ obj_path ] = allocated_offsets[ obj_path ] 
       del allocated_offsets[ obj_path ]
   
def allocate(obj_path):
    offset = 1e9 
    best = None
    if obj_path in free_offsets:
        best,offset = obj_path, free_offsets[ obj_path ]
    else:
        for key in free_offsets:
            if offset > free_offsets[key]:
                best,offset = key,free_offsets[key]
    allocated_offsets[obj_path] = offset
    del free_offsets[best]
    
def teardown_device(dev_path):
    """Force clean disconnect, reference drop, and BlueZ cache flush.
    Call this when a device fails, is removed, or suspected stalled.
    """
    print(f"Teardown device: {dev_path}")
    
    # Stop Notifications
    for char_dict in [temperatures, commands, batteries]:
        if dev_path in char_dict:
            try:
                char_dict[dev_path].StopNotify()
            except Exception as e:
                print(f"StopNotify failed on {dev_path}: {e}")
                
    # Disconnect
    if dev_path in inkbirds:
        try:
            inkbirds[dev_path].Disconnect()
        except Exception as e:
            print(f"Disconnect failed on {dev_path}: {e}")
    # Clean up
    for d in [inkbirds, temperatures, commands, batteries]:
        if dev_path in d:
            d.pop(dev_path, None)
            
    if dev_path in bind:
        del bind[dev_path]
        
    # Free offset
    deallocate(dev_path)
    
    try:
        adapter.RemoveDevice(dev_path)
        print(f"Called RemoveDevice({dev_path}) -> BlueZ cache flushed")
    except Exception as e:
        print(f"RemoveDevice failed (probably already gone): {e}")

def reinitialize_inkbird( obj_path ):
    generic_init=[ 
        Variant( 'ay', [0x02,0x01,0x00,0x00,0x00,0x00,0x00] ),  # self +0x0000
        Variant( 'ay', [0x02,0x02,0x00,0x00,0x00,0x00,0x00] ),  # self +0x0000
        Variant( 'ay', [0x02,0x04,0x00,0x00,0x00,0x00,0x00] ),  # self +0x0000
        Variant( 'ay', [0x02,0x08,0x00,0x00,0x00,0x00,0x00] ),  # self +0x0000

        Variant( 'ay', [0x04,0x00,0x00,0x00,0x00,0x00,0x00] ),  # 0x0446
        Variant( 'ay', [0x06,0x00,0x00,0x00,0x00,0x00,0x00] ),  # 0x0663
        Variant( 'ay', [0x08] ),                                # 0x080f00
        Variant( 'ay', [0x0a,0x0f,0x00,0x00,0x00,0x00,0x00] ),  # self +0x0000
        Variant( 'ay', [0x0c,0x00,0x00,0x00,0x00,0x00,0x00] ),  # 0x0c5a

        Variant( 'ay', [0x0f,0x00,0x00,0x00,0x00,0x00,0x00] ),  # *Hash returned,varies.
        Variant( 'ay', [0x11,0x00,0x00,0x00,0x00,0x00,0x00] ),  # 0x111100
        Variant( 'ay', [0x13,0x00,0x00,0x00,0x00,0x00,0x00] ),  # 0x13fe
        Variant( 'ay', [0x18]),                                 # self +0x000000000000
        Variant( 'ay', [0x24]),                                 # self +0x0f0000000000000000 *droppable
        Variant( 'ay', [0x26,0x01]),                            # self +0x0h000000000000000   *droppable
        Variant( 'ay', [0x26,0x02]),                            # self +0x0000000000000000   *droppable
        Variant( 'ay', [0x26,0x04]),                            # self +0x0000000000000000   *droppable
        Variant( 'ay', [0x26,0x08]),                            # self +0x0000000000000000
    ]
    print("re-initializing ",obj_path )
    for i in generic_init:
        commands[ obj_path ].WriteValue( i, { 'type':Variant('s','request') } )

def print_battery( data ):
    print( "battery=",data[0],"%" )

def update_temperatures( obj_path, data ):
    global stamp
    def temperature( lsbyte, msbyte ):
        value = ((msbyte^0x80)<<8)+lsbyte - 0x8000
        return (value-320)/18   # Convert to celsius
    if data[8:12] != [0xFE,0x7F,0xFE,0x7F]:
        print( "Suspicious temperature packet", data )
        return # Do not process questionable packets.
    t4vec = [ temperature( *data[2*i:2*i+2] ) for i in range(0,4) ] 
    offset = allocated_offsets[ obj_path ]
    for i,value in enumerate( t4vec ):
        vlast = thermostamp[offset+i]
        redundant = thermocount[offset+i]
        if ( redundant and (redundant<MAXWAIT) and
            (value == vlast or value==thermofilter[offset+i]) ): 
            continue
        if abs( value-vlast )>1.5 :
            if (value>MAXTEMP) or thermostamp[offset+i]>MAXTEMP:
                thermostamp[ offset+i ] = value 
            else:
                thermostamp[ offset+i ] = (value+thermostamp[offset+i])/2.
            thermofilter[ offset+i ] = thermostamp[ offset+i ]
            continue
        thermofilter[offset+i]=thermostamp[offset+i]
        thermostamp[offset+i]=value
        if not stamp:
            thermocount[offset+i]=0
            stamp = True

def temperature_callback( obj_path, obj_iface, obj_dict, invalidated ):
    if ( "Value" in obj_dict ):
        if not (obj_path in allocated_offsets ):
            if inkbirds[obj_path].Connected == True:
                allocate(obj_path)
                inkbirds[obj_path].Trusted=True
            else:
                print( "Temperature notify for disconnected inkbird:", obj_path, allocated_offsets )
            return False
        update_temperatures( obj_path, obj_dict['Value'].unpack() )
    return True

def command_callback( obj_path, obj_iface, obj_dict, invalidated ):
    print( "Command notify\t\t", obj_path,invalidated )
    if ( "Value" in obj_dict ):
        Value = obj_dict['Value'].unpack()
        print( "Value=",Value )
    return True

def extra_callback( obj_path, obj_iface, obj_dict, invalidated ):
    print( "Extra notify\t\t", obj_path,obj_dict )
    if ( "Value" in obj_dict ):
        Value=obj_dict['Value'].unpack()
        print( "Value=",Value )
    return True

def battery_callback( obj_path, obj_iface, obj_dict, invalidated ):
    print("Battery notify\t\t", obj_path, invalidated )
    if ( "Value" in obj_dict ):
        print_battery( obj_dict['Value'].unpack() )
    return True

def bind_notify( proxy, callback, o_path ):
    proxy.PropertiesChanged.connect(
        lambda o_iface,o_dict,o_inval:callback(o_path,o_iface,o_dict,o_inval)
    )
    proxy.StartNotify()

def services_resolved_callback( obj_path, obj_iface, obj_dict, invalidated ):
    if not 'ServicesResolved' in obj_dict:
        return False
    if obj_dict['ServicesResolved'].unpack()==True:
        print( "ServicesResolved." )
        for path in gatt_services:
            if path.startswith( obj_path ):
                if gatt_services[path]==False and len(bind[obj_path])<6:
                    print("Service has wrong size. Disconnecting:",obj_path)
                    inkbirds[obj_path].Disconnect()
                    return True
                gatt_services[path]=True
                
        if obj_path in bind and bind[obj_path]:
            try:
                for n,i in enumerate( bind[obj_path] ):
                    bind_notify(*i)
            except Exception as e:
                print( f"Binding failed during loop: {e}" )
                bind[obj_path]=[]
                inkbirds[obj_path].Disconnect()
                return True
        else:
            print(f"No bind entries for {obj_path} yet, waiting for GATT discovery")
        
        print("Pseudo Pairing")
        if obj_path in commands:  
            try:
                commands[ obj_path ].WriteValue( 
                    Variant('ay',[0xfd,0x00,0x00,0x00,0x00,0x00,0x00]), { 'type':Variant('s','request') } 
            )
            except Exception as e:
                print( f"Pseudo Pairing failed:{e}" )
                pass
            return True
        else:
            print(f"No command characteristics for {obj_path} yet")
    else:
        print("ServiceResolved -> False")
        teardown_device(obj_path)
        return True

def interface_added_callback( obj_path, obj_dict ):
    if DEVICE_IFACE in obj_dict:
        try:
            properties = obj_dict[DEVICE_IFACE]
            name = properties.get('Name').unpack()
            if ( name!=INKBIRD_NAME and name!=FRIENDLY_NAME ): return False
        except:
            print("Ignoring unstable interface in memory")
            return True
        print("inkbird ",obj_path)
        try:
            new_inkbird=inkbirds[obj_path]
        except:
            if len(free_offsets)==0:
                print("Inkbird script has insufficient thermometer memory")
                return False
            new_inkbird=bus.get_proxy(SERVICE_NAME, obj_path)
            print("new-inkbird proxy")
        else:
                print(" Already known")
                return True # All good connections exit from here.
        # Either the connection is new or it is corrupted.
        if new_inkbird.Connected:
            print( "Corrupted connection state:",obj_path, name )
            deallocate( obj_path )
            try:
                new_inkbird.Disconnect()
            except: # FIXME: not sure the following is allowed.
                new_inkbird.Connected=False
                new_inkbird.ServicesResolved=False
            return False  # Something's wrong, see if time resolves it.
        # new device connection. 
        print( "Connecting inkbird device ",obj_path )
        if not obj_path in inkbirds:
            inkbirds[ obj_path ]=new_inkbird
            new_inkbird.PropertiesChanged.connect( 
                lambda  a,b,c : services_resolved_callback( obj_path, a,b,c )
            )
        new_inkbird.Connect()
        return True

    parent_path = os.path.dirname(obj_path)
    if GATT_SERVICE_IFACE in obj_dict:
        if obj_path in gatt_services: return True
        properties=obj_dict[GATT_SERVICE_IFACE]
        if properties["UUID"].unpack()==TEMPERATURE_UUID:
            if properties["Device"].unpack() in inkbirds:
                gatt_services[ obj_path ]=False
                bind[parent_path]=[]
                print( "gatt service ", obj_path )
            else:
                print(" Error",properties['Device'].unpack())
        return True

    if GATT_CHAR_IFACE in obj_dict:
        if parent_path in gatt_services:
            if gatt_services[parent_path]: return True # Proxies are already bound
            uuid = obj_dict[GATT_CHAR_IFACE]["UUID"].unpack()
            proxy = bus.get_proxy( SERVICE_NAME, obj_path )
            dev_path = os.path.dirname(parent_path)
            if "0000ff01-0000-1000-8000-00805f9b34fb"==uuid:
                temperatures[dev_path]=proxy
                bind[dev_path].append((proxy, temperature_callback, dev_path ))
                return
            if "0000ff02-0000-1000-8000-00805f9b34fb"==uuid:
                commands[dev_path]=proxy
                bind[dev_path].append((proxy, command_callback, dev_path ))
                return
            if uuid.startswith("0000ff"):
                if uuid.startswith("0000ff05"):
                    return
                bind[dev_path].append((proxy, extra_callback, dev_path ))
                return
            if uuid=="00002a19-0000-1000-8000-00805f9b34fb":
                batteries[dev_path]=proxy
                bind[dev_path].append((proxy, battery_callback, dev_path ))
                return
        return True
    return False
    
def interfaces_removed_callback(path, interfaces):
    if 'org.bluez.Device1' in interfaces and path in inkbirds:
        print(f"BlueZ removed device object: {path}")
        teardown_device(path)

def logger():
    global stamp,laststamp
    if (stamp==False and (time.time()-laststamp)>30):
        print("logger stalled, attempting to clear.")
        for services in gatt_services:
            if gatt_services[services] is True:
                obj_path = os.path.dirname( services ) 
                print("Unstalling ",obj_path)
                temperatures[ obj_path ].ReadValue({ 'type':Variant('s','request') })
                laststamp = time.time()
    if (stamp==True):
        t=time.time()
        sample = enumerate( list(thermostamp) )
        stamp,laststamp=False,t
        for i in range( 0,len(thermocount) ):
            n=thermocount[i]
            thermocount[i] = n+1 if n<MAXWAIT else 0
        print( "%6.2f  "%(t), end="", file=fout )
        for i,value in sample:
            print( "% 6.1f "%( value if value<MAXTEMP else float('NaN') ), end="", file=fout )
        print( "  [°C] ",file=fout )
        fout.flush()
    (threading.Timer( 1, logger )).start()

def scan_dbus():
    print("Scan dbus")
    managed = manager.GetManagedObjects()
    for obj_path, obj_dict in managed.items():
        interface_added_callback(obj_path, obj_dict)
        
    # Clean up any known devices removed without InterfacesRemoved
    current_paths = set(managed.keys())
    for path in list(inkbirds.keys()):
        if path not in current_paths:
            print(f"Proactive cleanup: {path} missing from managed objects")
            teardown_device(path)
            
    # Timer starts
    (threading.Timer(WATCHTIME, scan_dbus)).start()
    return
#
# ------------------- Main logic proceedure begins here --------------------------
#

try:
    signal.signal(signal.SIGINT, signal_handler )
    signal.signal(signal.SIGTERM, signal_handler )
    manager.InterfacesAdded.connect( interface_added_callback )
    manager.InterfacesRemoved.connect( interfaces_removed_callback )
    (threading.Timer( 1, scan_dbus )).start()
    (threading.Timer( 1, logger )).start()
    loop.run()
except Exception as e:
    print(f"Main loop exception {e}")
    raise