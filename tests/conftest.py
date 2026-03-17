"""
Shared pytest fixtures for Tango device tests.

Starts BOTH the detector device(s) and the Microscope device in ONE Tango
test device server using MultiDeviceTestContext, so the Microscope can
create DeviceProxy connections to detectors by device name.

This avoids:
- "No proxy found for detector 'haadf'. Available: []"
- Needing a real Tango DB
- Flaky multi-context issues from spinning up multiple separate servers
"""

import numpy as np
import pytest
import tango
from tango.test_context import MultiDeviceTestContext

# Import device classes to test
from asyncroscopy.detectors.HAADF import HAADF
from asyncroscopy.ThermoDigitalTwin import ThermoDigitalTwin
from asyncroscopy.ThermoMicroscope import ThermoMicroscope


# We use ThermoDigitalTwin as our simulated microscope for all tests.

@pytest.fixture(scope="session")
def tango_ctx():
    """
    One Tango device server hosting HAADF + Microscope together.

    Device names here MUST match what you put into Microscope properties.
    """
    devices_info = [
        {
            "class": HAADF,
            "devices": [
                {
                    "name": "test/nodb/haadf",
                    "properties": {
                        # put HAADF defaults here if you want
                        # e.g. "dwell_time": 2e-6  (only if it's a device_property)
                    },
                }
            ],
        },
        {
            "class": ThermoDigitalTwin,
            "devices": [
                {
                    "name": "test/nodb/twin",
                    "properties": {
                        "haadf_device_address": "test/nodb/haadf",
                    },
                }
            ],
        },

        {
            "class": ThermoMicroscope,
            "devices": [
                {
                    "name": "test/nodb/thermomicroscope",
                    "properties": {
                        "simulate_hardware_for_tests": True,
                        "haadf_device_address": "test/nodb/haadf",
                    },
                }
            ],
        },
    ]

    # process=False keeps everything in the same process (fast, debuggable).
    # Also we only create ONE context, so the "second DeviceTestContext segfault"
    # issue doesn't apply.
    ctx = MultiDeviceTestContext(devices_info, process=False)
    with ctx:
        yield ctx



@pytest.fixture(scope="session")
def haadf_proxy(tango_ctx):
    return tango.DeviceProxy(tango_ctx.get_device_access("test/nodb/haadf"))


@pytest.fixture(scope="session")
def twin_proxy(tango_ctx):
    return tango.DeviceProxy(tango_ctx.get_device_access("test/nodb/twin"))


@pytest.fixture(scope="session")
def thermo_proxy(tango_ctx):
    return tango.DeviceProxy(tango_ctx.get_device_access("test/nodb/thermomicroscope"))



@pytest.fixture
def patched_single_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Patch ThermoMicroscope._acquire_stem_image so get_image() works
    without AutoScript/hardware.
    """
    def fake_acquire(self, detector_name : str, image_width: int, image_height: int, dwell_time: float):
        # Deterministic image makes tests stable
        arr = np.arange(image_height * image_width, dtype=np.uint16)
        return arr.reshape(image_height, image_width)

    monkeypatch.setattr(
        ThermoMicroscope,
        "_acquire_stem_image",
        fake_acquire,
    )