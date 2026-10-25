"""Minimal LSM6DS3 / LSM6DS3TR-C I2C driver for enclosure orientation readings."""

from __future__ import annotations

import time

from .i2c import AccelerometerIdentityError, SMBusLike, open_smbus
from .models import AccelerationSample


class LSM6DS3IdentityError(AccelerometerIdentityError):
    """Raised when the expected LSM6DS3 identity register is not present."""


class LSM6DS3:
    """Read an LSM6DS3-family accelerometer at +/-2g; the gyroscope stays off."""

    NAME = "lsm6ds3"
    DEFAULT_ADDRESS = 0x6A
    ADDRESSES = (0x6A, 0x6B)
    WHO_AM_I = 0x0F
    # LSM6DS3/LSM6DS33 report 0x69; the LSM6DS3TR-C fitted to most cheap
    # breakouts reports 0x6A. The registers used below are identical on both.
    CHIP_VARIANTS = {0x69: "LSM6DS3", 0x6A: "LSM6DS3TR-C"}
    CTRL1_XL = 0x10
    CTRL2_G = 0x11
    CTRL3_C = 0x12
    OUTX_L_XL = 0x28
    # 104 Hz output data rate, +/-2g full scale.
    CTRL1_XL_104HZ_2G = 0x40
    # Block data update + register auto-increment for multi-byte reads.
    CTRL3_C_BDU_IF_INC = 0x44
    G_PER_LSB = 0.000061

    def __init__(
        self,
        *,
        bus_number: int = 1,
        address: int = DEFAULT_ADDRESS,
        bus: SMBusLike | None = None,
    ):
        self.bus = open_smbus(bus_number) if bus is None else bus
        self.bus_number = bus_number
        self.address = address
        self.variant: str | None = None
        self._closed = False

    @classmethod
    def identifies(cls, identity: int) -> bool:
        """Return whether a WHO_AM_I value belongs to this chip family."""
        return identity in cls.CHIP_VARIANTS

    def initialize(self) -> None:
        """Verify identity and enable 104 Hz +/-2g accelerometer sampling."""
        identity = self.bus.read_byte_data(self.address, self.WHO_AM_I)
        if not self.identifies(identity):
            expected = "/".join(f"0x{value:02x}" for value in self.CHIP_VARIANTS)
            raise LSM6DS3IdentityError(
                f"LSM6DS3 WHO_AM_I expected {expected}, got 0x{identity:02x}"
            )
        self.variant = self.CHIP_VARIANTS[identity]
        self.bus.write_byte_data(self.address, self.CTRL3_C, self.CTRL3_C_BDU_IF_INC)
        self.bus.write_byte_data(self.address, self.CTRL2_G, 0x00)
        self.bus.write_byte_data(self.address, self.CTRL1_XL, self.CTRL1_XL_104HZ_2G)

    @classmethod
    def _axis_g(cls, low: int, high: int) -> float:
        """Decode one 16-bit two's-complement +/-2g axis."""
        raw = (high << 8) | low
        if raw & 0x8000:
            raw -= 1 << 16
        return raw * cls.G_PER_LSB

    def read(self, *, timestamp: float | None = None) -> AccelerationSample:
        """Read one XYZ sample in units of gravity."""
        data = self.bus.read_i2c_block_data(self.address, self.OUTX_L_XL, 6)
        if len(data) != 6:
            raise OSError(f"LSM6DS3 returned {len(data)} data bytes; expected 6")
        return AccelerationSample(
            timestamp=time.time() if timestamp is None else timestamp,
            x_g=self._axis_g(data[0], data[1]),
            y_g=self._axis_g(data[2], data[3]),
            z_g=self._axis_g(data[4], data[5]),
        )

    def close(self) -> None:
        """Close the owned I2C bus."""
        if not self._closed:
            self.bus.close()
            self._closed = True
