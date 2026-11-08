import sys
import types

import pytest

from mpl3115a2 import MPL3115A2, DeviceNotFoundError


class Bus:
    """Simulate the sensor registers and finish each conversion after two polls."""

    def __init__(self):
        self.regs = {0x0C: 0xC4, 0x26: 0, 0x14: 0xC5, 0x15: 0xE7}
        self.writes = []
        self.pending = 0
        self.stuck = False
        self.ready = 6
        self.samples = [[0x62, 0xF3, 0x40, 0x19, 0x80]]
        self.data = [0] * 5
        self.closed = False
        self.short = False

    def read_byte_data(self, addr, reg):
        assert addr == 0x60
        if reg == 0x26 and self.pending and not self.stuck:
            self.pending -= 1
            if not self.pending:
                self.regs[reg] &= ~2
                self.regs[6] = self.ready
                self.data = self.samples.pop(0)
        return self.regs.get(reg, 0)

    def write_byte_data(self, addr, reg, value):
        assert addr == 0x60
        if reg != 0x26:
            assert self.regs[0x26] & 3 == 0, "configure only while idle"
        else:
            # Reject mode or oversampling changes while the sensor is active.
            assert not ((self.regs[reg] ^ value) & 0xB8) or not self.regs[reg] & 3
        self.regs[reg] = value
        self.writes.append((reg, value))
        if reg == 0x26 and value & 2:
            assert not value & 1, "use true standby one-shot"
            self.pending = 2

    def read_i2c_block_data(self, addr, reg, count):
        assert (addr, reg, count) == (0x60, 1, 5)
        self.regs[6] = 0
        return self.data[:2] if self.short else self.data[:]

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    import mpl3115a2.driver as driver
    now = [0.0]
    monkeypatch.setattr(driver.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(driver.time, "sleep", lambda n: now.__setitem__(0, now[0] + n))


def test_pressure_temperature_and_stale_data():
    bus = Bus()
    bus.data = [0xFF] * 5
    bus.regs[6] = 6
    sensor = MPL3115A2(bus=bus)
    sample = sensor.read()
    assert sample.pressure_pa == 101325.0
    assert sample.temperature_c == 25.5
    assert sample.altitude_m is None
    assert bus.regs[0x13] == 7
    assert bus.regs[0x0F] == 0
    assert bus.regs[0x26] == 0x38


@pytest.mark.parametrize("raw, expected", [
    ([0, 0, 0, 0, 0], 0),
    ([0x6B, 0x6C, 0xB0, 0, 0], 110002.75),
    ([0xFF, 0xFF, 0xF0, 0, 0], 262143.75),
])
def test_unsigned_pressure_fraction(raw, expected):
    bus = Bus()
    bus.samples = [raw]
    assert MPL3115A2(bus=bus).read_pressure() == expected


@pytest.mark.parametrize("raw, expected", [
    ([0, 0x7B, 0x70, 0, 0], 123.4375),
    ([0xFF, 0xFF, 0xF0, 0, 0], -0.0625),
    ([0xFF, 0xF5, 0x80, 0, 0], -10.5),
    ([0x80, 0, 0, 0, 0], -32768),
])
def test_signed_altitude(raw, expected):
    bus = Bus()
    bus.samples = [raw]
    sensor = MPL3115A2(bus=bus)
    sample = sensor.read(mode="altitude")
    assert sample.altitude_m == expected
    assert sample.pressure_pa is None
    assert bus.regs[0x26] == 0xB8


@pytest.mark.parametrize("raw, expected", [([0xF5, 0x80], -10.5), ([0xFF, 0xF0], -0.0625), ([0x80, 0], -128)])
def test_signed_temperature(raw, expected):
    bus = Bus()
    bus.samples = [[0, 0, 0] + raw]
    assert MPL3115A2(bus=bus).read_temperature() == expected


def test_mode_switching_and_repeated_fresh_reads():
    bus = Bus()
    bus.samples = [[0, 10, 0, 0, 0], [0x62, 0xF3, 0x50, 0, 0], [0, 20, 0, 0, 0]]
    sensor = MPL3115A2(bus=bus)
    assert sensor.read_altitude() == 10
    assert sensor.read_pressure() == 101325.25
    assert sensor.read_altitude() == 20


@pytest.mark.parametrize("stuck, ready", [(True, 6), (False, 2), (False, 4)])
def test_timeout_and_recovery(stuck, ready):
    bus = Bus()
    sensor = MPL3115A2(bus=bus, timeout=0.03)
    bus.stuck, bus.ready = stuck, ready
    with pytest.raises(TimeoutError):
        sensor.read()
    bus.stuck, bus.ready = False, 6
    bus.samples = [[0x62, 0xF3, 0x40, 0, 0]] * 2
    assert sensor.read_pressure() == 101325


def test_identity_failure_does_not_configure():
    bus = Bus()
    bus.regs[0x0C] = 0
    with pytest.raises(DeviceNotFoundError):
        MPL3115A2(bus=bus)
    assert bus.writes == []


def test_bad_bus_data_and_io_errors():
    bus = Bus()
    sensor = MPL3115A2(bus=bus)
    bus.short = True
    with pytest.raises(OSError):
        sensor.read()
    bus.short = False
    def fail(*args):
        raise OSError("bus disconnected")
    bus.read_byte_data = fail
    with pytest.raises(OSError, match="disconnected"):
        sensor.read()


@pytest.mark.parametrize("kwargs", [{"oversampling": 3}, {"oversampling": True}, {"timeout": 0}, {"timeout": float('nan')}, {"address": 0x80}])
def test_invalid_settings(kwargs):
    with pytest.raises(ValueError):
        MPL3115A2(bus=Bus(), **kwargs)


@pytest.mark.parametrize("osr, bits", [(1, 0), (2, 8), (4, 16), (8, 24), (16, 32), (32, 40), (64, 48), (128, 56)])
def test_oversampling(osr, bits):
    bus = Bus()
    MPL3115A2(bus=bus, oversampling=osr).read()
    assert bus.regs[0x26] == bits


def test_sea_level_pressure_units_and_validation():
    bus = Bus()
    sensor = MPL3115A2(bus=bus)
    assert sensor.sea_level_pressure_pa == 101326
    sensor.sea_level_pressure_pa = 100000
    assert (bus.regs[0x14], bus.regs[0x15]) == (0xC3, 0x50)
    assert sensor.sea_level_pressure_pa == 100000
    for value in [0, -1, 131071, float('inf'), float('nan')]:
        with pytest.raises(ValueError):
            sensor.sea_level_pressure_pa = value


def test_borrowed_bus_is_not_closed_and_closed_sensor_rejects_reads():
    bus = Bus()
    with MPL3115A2(bus=bus) as sensor:
        sensor.read()
    assert not bus.closed
    sensor.close()
    with pytest.raises(RuntimeError):
        sensor.read()


def test_owned_bus_closed_on_exit_and_init_failure(monkeypatch):
    bus = Bus()
    monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=lambda n: bus))
    with MPL3115A2() as sensor:
        assert sensor.read_pressure() == 101325
    assert bus.closed
    bus.closed = False
    bus.regs[0x0C] = 0
    with pytest.raises(DeviceNotFoundError):
        MPL3115A2()
    assert bus.closed


def test_invalid_mode_has_no_io():
    bus = Bus()
    sensor = MPL3115A2(bus=bus)
    before = bus.writes[:]
    with pytest.raises(ValueError):
        sensor.read(mode="wrong")
    assert bus.writes == before
