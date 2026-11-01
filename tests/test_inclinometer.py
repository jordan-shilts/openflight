"""LIS3DH/LSM6DS3 transport, auto-detection, pitch math, filtering, and snapshot selection tests."""

import math

import pytest

from openflight.inclinometer import (
    LIS3DH,
    LSM6DS3,
    AccelerationSample,
    AccelerometerIdentityError,
    AccelerometerNotFoundError,
    InclinometerService,
    LIS3DHIdentityError,
    LSM6DS3IdentityError,
    detect_accelerometer,
)
from openflight.inclinometer.service import pitch_degrees


def _axis_bytes(value_g: float) -> list[int]:
    counts = round(value_g / 0.001)
    raw = (counts << 4) & 0xFFFF
    return [raw & 0xFF, raw >> 8]


class FakeBus:
    def __init__(self, *, identity=0x33, axes=(0.0, 0.0, 1.0)):
        self.identity = identity
        self.axes = axes
        self.writes = []
        self.closed = False

    def read_byte_data(self, address, register):
        assert address == 0x18
        assert register == LIS3DH.WHO_AM_I
        return self.identity

    def write_byte_data(self, address, register, value):
        self.writes.append((address, register, value))

    def read_i2c_block_data(self, address, register, length):
        assert (address, register, length) == (0x18, 0xA8, 6)
        return sum((_axis_bytes(axis) for axis in self.axes), [])

    def close(self):
        self.closed = True


def test_lis3dh_initializes_high_resolution_xyz_mode_and_reads_signed_axes():
    bus = FakeBus(axes=(-0.125, 0.25, 0.975))
    sensor = LIS3DH(bus=bus)

    sensor.initialize()
    sample = sensor.read(timestamp=123.0)
    sensor.close()

    assert bus.writes == [(0x18, 0x20, 0x57), (0x18, 0x23, 0x88)]
    assert sample == AccelerationSample(123.0, -0.125, 0.25, 0.975)
    assert bus.closed


def test_lis3dh_rejects_wrong_identity():
    sensor = LIS3DH(bus=FakeBus(identity=0x00))

    with pytest.raises(LIS3DHIdentityError, match="expected 0x33, got 0x00"):
        sensor.initialize()


def test_lis3dh_identity_error_is_a_generic_accelerometer_identity_error():
    assert issubclass(LIS3DHIdentityError, AccelerometerIdentityError)
    assert issubclass(LSM6DS3IdentityError, AccelerometerIdentityError)


def _lsm_axis_bytes(value_g: float) -> list[int]:
    raw = round(value_g / LSM6DS3.G_PER_LSB) & 0xFFFF
    return [raw & 0xFF, raw >> 8]


class FakeLSMBus:
    def __init__(self, *, address=0x6A, identity=0x6A, axes=(0.0, 0.0, 1.0), data=None):
        self.address = address
        self.identity = identity
        self.axes = axes
        self.data = data
        self.writes = []
        self.closed = False

    def read_byte_data(self, address, register):
        assert address == self.address
        assert register == LSM6DS3.WHO_AM_I
        return self.identity

    def write_byte_data(self, address, register, value):
        self.writes.append((address, register, value))

    def read_i2c_block_data(self, address, register, length):
        assert (address, register, length) == (self.address, 0x28, 6)
        if self.data is not None:
            return self.data
        return sum((_lsm_axis_bytes(axis) for axis in self.axes), [])

    def close(self):
        self.closed = True


@pytest.mark.parametrize(("identity", "variant"), [(0x69, "LSM6DS3"), (0x6A, "LSM6DS3TR-C")])
def test_lsm6ds3_initializes_accelerometer_only_and_reads_signed_axes(identity, variant):
    bus = FakeLSMBus(identity=identity, axes=(-0.125, 0.25, 0.975))
    sensor = LSM6DS3(bus=bus)

    sensor.initialize()
    sample = sensor.read(timestamp=55.0)
    sensor.close()

    # BDU + auto-increment first, gyro off, then 104 Hz +/-2g accelerometer.
    assert bus.writes == [(0x6A, 0x12, 0x44), (0x6A, 0x11, 0x00), (0x6A, 0x10, 0x40)]
    assert sensor.variant == variant
    assert sample.timestamp == 55.0
    assert sample.x_g == pytest.approx(-0.125, abs=LSM6DS3.G_PER_LSB)
    assert sample.y_g == pytest.approx(0.25, abs=LSM6DS3.G_PER_LSB)
    assert sample.z_g == pytest.approx(0.975, abs=LSM6DS3.G_PER_LSB)
    assert bus.closed


def test_lsm6ds3_decodes_full_scale_extremes():
    bus = FakeLSMBus(data=[0x00, 0x80, 0xFF, 0x7F, 0xFF, 0xFF])
    sample = LSM6DS3(bus=bus).read(timestamp=1.0)

    assert sample.x_g == pytest.approx(-32768 * LSM6DS3.G_PER_LSB)
    assert sample.y_g == pytest.approx(32767 * LSM6DS3.G_PER_LSB)
    assert sample.z_g == pytest.approx(-LSM6DS3.G_PER_LSB)


def test_lsm6ds3_rejects_short_reads():
    sensor = LSM6DS3(bus=FakeLSMBus(data=[0, 0, 0]))

    with pytest.raises(OSError, match="returned 3 data bytes; expected 6"):
        sensor.read()


@pytest.mark.parametrize("identity", [0x00, 0x33, 0x6C])
def test_lsm6ds3_rejects_wrong_identity_without_configuring(identity):
    bus = FakeLSMBus(identity=identity)

    with pytest.raises(LSM6DS3IdentityError, match=f"expected 0x69/0x6a, got 0x{identity:02x}"):
        LSM6DS3(bus=bus).initialize()
    assert bus.writes == []


def test_lsm6ds3_close_is_idempotent():
    bus = FakeLSMBus()
    sensor = LSM6DS3(bus=bus)
    sensor.close()
    bus.closed = False
    sensor.close()

    assert bus.closed is False


class ProbeBus:
    """Bus answering WHO_AM_I from a {address: identity} map; others NACK."""

    def __init__(self, devices):
        self.devices = devices
        self.probed = []
        self.closed = False

    def read_byte_data(self, address, register):
        assert register == 0x0F
        self.probed.append(address)
        if address not in self.devices:
            raise OSError(121, "Remote I/O error")
        return self.devices[address]

    def write_byte_data(self, address, register, value):
        raise AssertionError("detection must not configure the chip")

    def read_i2c_block_data(self, address, register, length):
        raise AssertionError("detection must not read samples")

    def close(self):
        self.closed = True


def test_detect_prefers_lis3dh_at_default_address():
    bus = ProbeBus({0x18: 0x33, 0x6A: 0x6A})

    sensor = detect_accelerometer(bus=bus)

    assert isinstance(sensor, LIS3DH)
    assert (sensor.address, sensor.bus) == (0x18, bus)
    assert bus.probed == [0x18]
    assert bus.closed is False


def test_detect_finds_lis3dh_at_alternate_address():
    sensor = detect_accelerometer(bus=ProbeBus({0x19: 0x33}))

    assert isinstance(sensor, LIS3DH)
    assert sensor.address == 0x19


@pytest.mark.parametrize("address", [0x6A, 0x6B])
@pytest.mark.parametrize("identity", [0x69, 0x6A])
def test_detect_finds_lsm6ds3_family(address, identity):
    bus = ProbeBus({address: identity})

    sensor = detect_accelerometer(bus_number=3, bus=bus)

    assert isinstance(sensor, LSM6DS3)
    assert (sensor.address, sensor.bus_number, sensor.bus) == (address, 3, bus)
    assert bus.closed is False


def test_detect_reports_every_probe_and_closes_bus_when_nothing_matches():
    bus = ProbeBus({0x6A: 0x6C})

    with pytest.raises(AccelerometerNotFoundError) as error:
        detect_accelerometer(bus=bus)

    message = str(error.value)
    assert "I2C-1" in message
    assert "0x18: no response" in message
    assert "0x19: no response" in message
    assert "0x6a: unrecognized WHO_AM_I 0x6c" in message
    assert "0x6b: no response" in message
    assert bus.probed == [0x18, 0x19, 0x6A, 0x6B]
    assert bus.closed


def test_detect_not_found_is_an_oserror_for_fail_soft_callers():
    assert issubclass(AccelerometerNotFoundError, OSError)


def test_detect_with_address_probes_only_that_address():
    bus = ProbeBus({0x18: 0x33, 0x6B: 0x69})

    sensor = detect_accelerometer(address=0x6B, bus=bus)

    assert isinstance(sensor, LSM6DS3)
    assert bus.probed == [0x6B]


def test_detect_with_address_fails_when_that_chip_is_absent():
    bus = ProbeBus({0x18: 0x33})

    with pytest.raises(AccelerometerNotFoundError, match="0x19: no response"):
        detect_accelerometer(address=0x19, bus=bus)
    assert bus.probed == [0x19]
    assert bus.closed


def test_detect_rejects_unsupported_address_without_touching_bus():
    bus = ProbeBus({})

    with pytest.raises(ValueError, match="0x53 is not a supported accelerometer address"):
        detect_accelerometer(address=0x53, bus=bus)
    assert bus.probed == []
    assert bus.closed is False


def test_detected_lsm6ds3_feeds_the_service_end_to_end():
    angle = math.radians(4.0)
    bus = FakeLSMBus(axes=(0.0, math.sin(angle), math.cos(angle)))
    sensor = LSM6DS3(bus=bus)
    sensor.initialize()
    service = InclinometerService(sensor=sensor, zero_offset_deg=1.0, window_samples=3)

    for timestamp in (1.0, 1.1, 1.2):
        service.add_sample(sensor.read(timestamp=timestamp))
    selection = service.snapshot_for_impact(1.3)

    assert selection.status == "stable"
    assert selection.snapshot.raw_pitch_deg == pytest.approx(4.0, abs=0.01)
    assert selection.snapshot.calibrated_pitch_deg == pytest.approx(5.0, abs=0.01)


def test_pitch_is_positive_when_sensor_y_tilts_back():
    angle = math.radians(12.0)
    sample = AccelerationSample(1.0, 0.0, math.sin(angle), math.cos(angle))

    assert pitch_degrees(sample) == pytest.approx(12.0)


def test_service_applies_zero_offset_and_selects_preimpact_snapshot():
    service = InclinometerService(
        sensor=None,
        zero_offset_deg=1.5,
        window_samples=4,
        max_snapshot_age_s=2.0,
    )
    angle = math.radians(3.0)
    for timestamp in (9.0, 9.1, 9.2, 9.3, 10.1):
        service.add_sample(AccelerationSample(timestamp, 0.0, math.sin(angle), math.cos(angle)))

    selection = service.snapshot_for_impact(10.0)

    assert selection.status == "stable"
    assert selection.age_s == pytest.approx(0.7)
    assert selection.snapshot.timestamp == 9.3
    assert selection.snapshot.raw_pitch_deg == pytest.approx(3.0)
    assert selection.snapshot.calibrated_pitch_deg == pytest.approx(4.5)


def test_service_rejects_moving_window_and_stale_snapshot():
    service = InclinometerService(
        sensor=None,
        window_samples=4,
        max_pitch_std_deg=0.2,
        max_snapshot_age_s=1.0,
    )
    for timestamp, angle_deg in zip((1.0, 1.1, 1.2, 1.3), (0.0, 3.0, -2.0, 4.0)):
        angle = math.radians(angle_deg)
        assert (
            service.add_sample(AccelerationSample(timestamp, 0.0, math.sin(angle), math.cos(angle)))
            is None
        )
    assert service.snapshot_for_impact(1.4).status == "no_stable_preimpact_reading"

    for timestamp in (2.0, 2.1, 2.2, 2.3):
        service.add_sample(AccelerationSample(timestamp, 0.0, 0.0, 1.0))
    selection = service.snapshot_for_impact(4.0)
    assert selection.status == "stale"
    assert selection.snapshot is None
    assert selection.age_s == pytest.approx(1.7)


def test_service_rejects_non_gravity_acceleration():
    service = InclinometerService(sensor=None, window_samples=3)

    for timestamp in (1.0, 1.1, 1.2):
        assert service.add_sample(AccelerationSample(timestamp, 0.0, 0.0, 1.5)) is None

    assert service.snapshot_for_impact(1.3).status == "no_stable_preimpact_reading"


def test_service_does_not_reuse_old_position_after_movement():
    service = InclinometerService(sensor=None, window_samples=3, max_pitch_std_deg=0.2)
    for timestamp in (1.0, 1.1, 1.2):
        service.add_sample(AccelerationSample(timestamp, 0.0, 0.0, 1.0))
    assert service.snapshot_for_impact(1.25).status == "stable"

    moved = math.radians(8.0)
    service.add_sample(AccelerationSample(1.3, 0.0, math.sin(moved), math.cos(moved)))

    selection = service.snapshot_for_impact(1.35)
    assert selection.status == "moving"
    assert selection.snapshot is None
