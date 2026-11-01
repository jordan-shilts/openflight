"""Find whichever supported accelerometer is wired to the I2C bus."""

from __future__ import annotations

from typing import Union

from .i2c import SMBusLike, open_smbus
from .lis3dh import LIS3DH
from .lsm6ds3 import LSM6DS3

SupportedAccelerometer = Union[LIS3DH, LSM6DS3]

# Probe order: the validated LIS3DH first, then the LSM6DS3 alternative.
DRIVERS: tuple[type[LIS3DH] | type[LSM6DS3], ...] = (LIS3DH, LSM6DS3)


class AccelerometerNotFoundError(OSError):
    """Raised when no supported accelerometer answers on the probed addresses."""


def _candidates(address: int | None):
    for driver in DRIVERS:
        for candidate in driver.ADDRESSES:
            if address is None or candidate == address:
                yield driver, candidate


def detect_accelerometer(
    *,
    bus_number: int = 1,
    address: int | None = None,
    bus: SMBusLike | None = None,
) -> SupportedAccelerometer:
    """Return a driver for the first supported chip found on the bus.

    With ``address`` set, only that address is probed. The returned driver owns
    the bus; on failure the bus is closed before raising.
    """
    if address is not None and next(_candidates(address), None) is None:
        known = ", ".join(f"0x{value:02x}" for driver in DRIVERS for value in driver.ADDRESSES)
        raise ValueError(f"0x{address:02x} is not a supported accelerometer address ({known})")

    bus = open_smbus(bus_number) if bus is None else bus
    findings = []
    for driver, candidate in _candidates(address):
        try:
            identity = bus.read_byte_data(candidate, driver.WHO_AM_I)
        except OSError:
            findings.append(f"0x{candidate:02x}: no response")
            continue
        if driver.identifies(identity):
            return driver(bus_number=bus_number, address=candidate, bus=bus)
        findings.append(f"0x{candidate:02x}: unrecognized WHO_AM_I 0x{identity:02x}")
    bus.close()
    raise AccelerometerNotFoundError(
        f"No LIS3DH or LSM6DS3 found on I2C-{bus_number} ({'; '.join(findings)})"
    )
