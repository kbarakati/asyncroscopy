"""
HAADF (High-Angle Annular Dark-Field) detector Tango device.

This device holds acquisition settings for the HAADF detector.
It does NOT talk to AutoScript directly — the Microscope device
reads these attributes via DeviceProxy before acquiring.
"""

from tango import AttrWriteType, DevState
from tango.server import Device, attribute


class HAADF(Device):
    """HAADF detector settings device."""

    # ------------------------------------------------------------------
    # Device properties — set per-deployment in the Tango DB
    # ------------------------------------------------------------------

    # (no hardware connection properties needed — HAADF is settings-only)

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------

    dwell_time = attribute(
        label="Dwell Time",
        dtype=float,
        access=AttrWriteType.READ_WRITE,
        unit="s",
        format="%e",
        min_value=1e-9,
        max_value=1e-3,
        doc="Per-pixel dwell time in seconds (e.g. 1e-6 = 1 µs)",
    )

    imsize = attribute(
        label="Image Size",
        dtype=int,
        access=AttrWriteType.READ_WRITE,
        unit="px",
        doc="Acquisition width in pixels (should match an AutoScript ImageSize preset)",
    )

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def init_device(self) -> None:
        Device.init_device(self)
        self.set_state(DevState.ON)

        # Sensible defaults — operators override via Tango DB or client writes
        self._dwell_time: float = 1e-6   # 1 µs
        self._imsize: int = 512

        self.info_stream("HAADF device initialised")

    # ------------------------------------------------------------------
    # Attribute read / write
    # ------------------------------------------------------------------

    def read_dwell_time(self) -> float:
        return self._dwell_time

    def write_dwell_time(self, value: float) -> None:
        self._dwell_time = value

    def read_imsize(self) -> int:
        return self._imsize

    def write_imsize(self, value: int) -> None:
        self._imsize = value


# ----------------------------------------------------------------------
# Server entry point
# ----------------------------------------------------------------------

if __name__ == "__main__":
    HAADF.run_server()