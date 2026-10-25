"""Shared I2C plumbing for the enclosure accelerometer drivers."""

from __future__ import annotations

from typing import Protocol


class SMBusLike(Protocol):  # pylint: disable=unnecessary-ellipsis
    """Subset of smbus2 used by the drivers, allowing deterministic tests."""

    def read_byte_data(self, address: int, register: int) -> int:
        """Read one register byte."""
        ...  # pylint: disable=unnecessary-ellipsis

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        """Write one register byte."""
        ...  # pylint: disable=unnecessary-ellipsis

    def read_i2c_block_data(self, address: int, register: int, length: int) -> list[int]:
        """Read a contiguous register block."""
        ...  # pylint: disable=unnecessary-ellipsis

    def close(self) -> None:
        """Close the bus."""
        ...  # pylint: disable=unnecessary-ellipsis


class AccelerometerIdentityError(RuntimeError):
    """Raised when a driver's WHO_AM_I register holds an unexpected value."""


def open_smbus(bus_number: int) -> SMBusLike:
    """Open a Linux I2C bus through smbus2."""
    from smbus2 import SMBus  # pylint: disable=import-outside-toplevel,import-error

    return SMBus(bus_number)
