"""Minimal LIS3DH I2C driver for enclosure orientation readings."""

from __future__ import annotations

import time

from .i2c import AccelerometerIdentityError, SMBusLike, open_smbus
from .models import AccelerationSample


class LIS3DHIdentityError(AccelerometerIdentityError):
    """Raised when the expected LIS3DH identity register is not present."""


class LIS3DH:
    """Read a LIS3DH configured for +/-2g high-resolution measurements."""

    NAME = "lis3dh"
    DEFAULT_ADDRESS = 0x18
    ADDRESSES = (0x18, 0x19)
    WHO_AM_I = 0x0F
    WHO_AM_I_VALUE = 0x33
    CTRL_REG1 = 0x20
    CTRL_REG4 = 0x23
    OUT_X_L = 0x28
    AUTO_INCREMENT = 0x80

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
        self._closed = False

    @classmethod
    def identifies(cls, identity: int) -> bool:
        """Return whether a WHO_AM_I value belongs to this chip."""
        return identity == cls.WHO_AM_I_VALUE

    def initialize(self) -> None:
        """Verify identity and enable 100 Hz, high-resolution XYZ sampling."""
        identity = self.bus.read_byte_data(self.address, self.WHO_AM_I)
        if identity != self.WHO_AM_I_VALUE:
            raise LIS3DHIdentityError(
                f"LIS3DH WHO_AM_I expected 0x{self.WHO_AM_I_VALUE:02x}, got 0x{identity:02x}"
            )
        self.bus.write_byte_data(self.address, self.CTRL_REG1, 0x57)
        self.bus.write_byte_data(self.address, self.CTRL_REG4, 0x88)

    @staticmethod
    def _axis_g(low: int, high: int) -> float:
        """Decode one left-aligned 12-bit high-resolution +/-2g axis."""
        raw = (high << 8) | low
        if raw & 0x8000:
            raw -= 1 << 16
        return (raw >> 4) * 0.001

    def read(self, *, timestamp: float | None = None) -> AccelerationSample:
        """Read one XYZ sample in units of gravity."""
        data = self.bus.read_i2c_block_data(
            self.address,
            self.OUT_X_L | self.AUTO_INCREMENT,
            6,
        )
        if len(data) != 6:
            raise OSError(f"LIS3DH returned {len(data)} data bytes; expected 6")
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
