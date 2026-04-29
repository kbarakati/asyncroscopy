"""
Tests for the ThermoDigitalTwin Tango device.
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import numpy as np
import pytest
import tango

# Using shared twin_proxy from conftest.py

class TestThermoDigitalTwin:

    def test_state_is_on(self, twin_proxy: tango.DeviceProxy):
        assert twin_proxy.state() == tango.DevState.ON

    def test_manufacturer_is_digital_twin(self, twin_proxy: tango.DeviceProxy):
        assert twin_proxy.manufacturer == "UTKTeam"

    def test_get_image_returns_valid_data(self, twin_proxy: tango.DeviceProxy, patched_single_image: pytest.MonkeyPatch):
        json_meta, raw_bytes = twin_proxy.get_scanned_image()
        meta = json.loads(json_meta)
        
        assert meta["detector"] == "haadf"
        assert "shape" in meta
        assert "dtype" in meta
        
        image = np.frombuffer(raw_bytes, dtype=meta["dtype"]).reshape(meta["shape"])
        assert image.shape == tuple(meta["shape"])

    def test_unknown_detector_raises(self, twin_proxy: tango.DeviceProxy):
        with pytest.raises(tango.DevFailed):
            twin_proxy.get_spectrum("void")

    def test_stage_navigation_changes_and_restores_view(
        self,
        twin_proxy: tango.DeviceProxy,
        scan_proxy: tango.DeviceProxy,
    ):
        scan_proxy.imsize = 64
        scan_proxy.dwell_time = 1e-6

        twin_proxy.move_stage([0.0, 0.0, 0.0, 0.0, 0.0])
        _, raw_a = twin_proxy.get_scanned_image()

        twin_proxy.move_stage([8e-9, -7e-9, 0.0, 0.0, 0.0])
        _, raw_b = twin_proxy.get_scanned_image()
        assert raw_a != raw_b

        twin_proxy.move_stage([0.0, 0.0, 0.0, 0.0, 0.0])
        _, raw_a_again = twin_proxy.get_scanned_image()
        assert raw_a == raw_a_again

    def test_spectrum_is_repeatable_at_same_pose_and_beam(
        self,
        twin_proxy: tango.DeviceProxy,
        eds_proxy: tango.DeviceProxy,
    ):
        eds_proxy.exposure_time = 0.05
        twin_proxy.move_stage([0.0, 0.0, 0.0, 0.0, 0.0])
        twin_proxy.place_beam([0.45, 0.55])

        _meta_1, raw_1 = twin_proxy.get_spectrum("eds")
        _meta_2, raw_2 = twin_proxy.get_spectrum("eds")

        spec_1 = json.loads(raw_1.decode("utf-8"))
        spec_2 = json.loads(raw_2.decode("utf-8"))
        assert spec_1 == spec_2
