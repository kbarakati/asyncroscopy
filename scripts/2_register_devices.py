#!/usr/bin/env python
"""
register_devices.py
Run once to register devices and properties in the Tango DB.

workflow:

Start Tango DB
      ↓
Register device in DB
      ↓
Run device server
      ↓
Connect using DeviceProxy

"""

import tango

# ── Settings ──────────────────────────────────────────────────
SCAN_SERVER  = "SCAN/scan_instance" 
SCAN_CLASS   = "SCAN" 
SCAN_DEVICE  = "test/scan/1"

EDS_SERVER  = "EDS/eds_instance" 
EDS_CLASS   = "EDS" 
EDS_DEVICE  = "test/eds/1"

STAGE_SERVER  = "STAGE/stage_instance" 
STAGE_CLASS   = "STAGE" 
STAGE_DEVICE  = "test/stage/1"

MICRO_SERVER  = "ThermoMicroscope/microscope_instance"
MICRO_CLASS   = "ThermoMicroscope"
MICRO_DEVICE  = "test/microscope/1"
# ──────────────────────────────────────────────────────────────


def add_device(db, server, classname, device):
    info = tango.DbDevInfo()
    info.server = server
    info._class = classname
    info.name   = device
    db.add_device(info)
    print(f"  registered: {device}")


def main():
    db = tango.Database()
    print(f"Connected: {db.get_db_host()}:{db.get_db_port()}\n")

    add_device(db, SCAN_SERVER, SCAN_CLASS, SCAN_DEVICE)
    add_device(db, EDS_SERVER, EDS_CLASS, EDS_DEVICE)
    add_device(db, STAGE_SERVER, STAGE_CLASS, STAGE_DEVICE)
    add_device(db, MICRO_SERVER, MICRO_CLASS, MICRO_DEVICE)

    db.put_device_property(MICRO_DEVICE, {"scan_device_address": [SCAN_DEVICE]})
    db.put_device_property(MICRO_DEVICE, {"eds_device_address": [EDS_DEVICE]})
    db.put_device_property(MICRO_DEVICE, {"stage_device_address": [STAGE_DEVICE]})

    print(f"  property:   scan_device_address = {SCAN_DEVICE}")
    print(f"  property:   eds_device_address = {EDS_DEVICE}")
    print(f"  property:   stage_device_address = {STAGE_DEVICE}")

    print("\nDone!")


if __name__ == "__main__":
    main()


