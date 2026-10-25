"""Enclosure orientation sensing for radar tilt compensation."""

from .detect import AccelerometerNotFoundError, detect_accelerometer
from .i2c import AccelerometerIdentityError
from .lis3dh import LIS3DH, LIS3DHIdentityError
from .lsm6ds3 import LSM6DS3, LSM6DS3IdentityError
from .models import AccelerationSample, OrientationSnapshot, SnapshotSelection
from .service import InclinometerService

__all__ = [
    "AccelerationSample",
    "AccelerometerIdentityError",
    "AccelerometerNotFoundError",
    "InclinometerService",
    "LIS3DH",
    "LIS3DHIdentityError",
    "LSM6DS3",
    "LSM6DS3IdentityError",
    "OrientationSnapshot",
    "SnapshotSelection",
    "detect_accelerometer",
]
