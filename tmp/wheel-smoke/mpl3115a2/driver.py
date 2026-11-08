"""Polling driver based on NXP MPL3115A2 datasheet Rev. 8.1, sections 14.2-14.22."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Literal, Optional, Protocol, Sequence


class SMBusLike(Protocol):
    """Subset shared by smbus.SMBus and smbus2.SMBus."""

    def read_byte_data(self, address: int, register: int) -> int: ...
    def write_byte_data(self, address: int, register: int, value: int) -> None: ...
    def read_i2c_block_data(self, address: int, register: int, length: int) -> Sequence[int]: ...
    def close(self) -> None: ...


class DeviceNotFoundError(OSError):
    """The device responded with an unexpected WHO_AM_I identifier."""


@dataclass(frozen=True)
class Reading:
    """One conversion. Only pressure or altitude is populated, plus temperature."""

    temperature_c: float
    pressure_pa: Optional[float] = None
    altitude_m: Optional[float] = None


class MPL3115A2:
    """Blocking I2C driver using standby one-shot conversions.

    Pass ``bus=`` to borrow an existing smbus-compatible object, or omit it to
    open Linux I2C ``bus_number`` (default 1). Only an internally opened bus is
    closed by this driver. Initialization takes control of this sensor's
    acquisition configuration: FIFO and interrupts are disabled. Do not share
    the same sensor with other drivers/processes.

    ``oversampling`` is a ratio (1, 2, 4, ..., 128), not a register bit value.
    ``timeout`` bounds each wait for sensor completion; Linux I2C calls also
    depend on the operating system's own bus timeout.
    """

    _RATIOS = (1, 2, 4, 8, 16, 32, 64, 128)

    def __init__(
        self, bus_number: int = 1, *, bus: Optional[SMBusLike] = None,
        address: int = 0x60, oversampling: int = 128, timeout: float = 2.0,
    ) -> None:
        if type(address) is not int or not 0x08 <= address <= 0x77:
            raise ValueError("address must be a usable 7-bit I2C address")
        if type(oversampling) is not int or oversampling not in self._RATIOS:
            raise ValueError("oversampling must be 1, 2, 4, 8, 16, 32, 64 or 128")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if type(bus_number) is not int or bus_number < 0:
            raise ValueError("bus_number must be a nonnegative integer")
        self._address = address
        self._timeout = timeout
        self._control = self._RATIOS.index(oversampling) << 3
        self._lock = threading.RLock()
        self._closed = False
        self._owns_bus = bus is None
        if bus is None:
            try:
                from smbus2 import SMBus
            except ImportError:
                try:
                    from smbus import SMBus
                except ImportError as exc:
                    raise ImportError(
                        "Install the I2C backend: python -m pip install smbus2; "
                        "or supply an existing bus with bus=..."
                    ) from exc
            bus = SMBus(bus_number)
        self._bus = bus
        try:
            identity = self._read_byte(0x0C)
            if identity != 0xC4:
                raise DeviceNotFoundError(
                    f"Expected MPL3115A2 ID 0xC4 at 0x{address:02X}, got 0x{identity:02X}"
                )
            # Enter standby without changing ALT/OS during an active conversion.
            current = self._read_byte(0x26)
            self._write_byte(0x26, current & ~1)
            self._wait_idle()
            self._write_byte(0x26, self._control)
            self._write_byte(0x0F, 0)  # FIFO off: 0x01 is now the output register.
            self._write_byte(0x27, 0)  # Normal output updates; no target-load modes.
            self._write_byte(0x29, 0)  # Disable interrupt sources; this driver polls.
            self._write_byte(0x13, 7)  # DREM | PDEFE | TDEFE.
        except BaseException:
            if self._owns_bus:
                self._bus.close()
            self._closed = True
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Sensor is closed")

    def _read_byte(self, register: int) -> int:
        return self._bus.read_byte_data(self._address, register)

    def _write_byte(self, register: int, value: int) -> None:
        self._bus.write_byte_data(self._address, register, value)

    def _pause(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"MPL3115A2 at 0x{self._address:02X} did not complete within "
                f"{self._timeout:g} s"
            )
        time.sleep(min(0.005, remaining))

    def _wait_idle(self) -> None:
        deadline = time.monotonic() + self._timeout
        while self._read_byte(0x26) & 2:
            self._pause(deadline)

    def _read_output(self) -> Sequence[int]:
        data = self._bus.read_i2c_block_data(self._address, 0x01, 5)
        if len(data) != 5:
            raise OSError(f"Expected 5 sensor bytes, received {len(data)}")
        return data

    def read(self, *, mode: Literal["pressure", "altitude"] = "pressure") -> Reading:
        """Acquire a fresh pressure/altitude and temperature pair.

        Pressure is absolute Pa; altitude is the sensor's atmosphere-based
        estimate in meters using ``sea_level_pressure_pa``. Switching modes
        costs a new conversion. TimeoutError indicates incomplete conversion;
        transport OSError exceptions propagate unchanged.
        """
        if mode not in ("pressure", "altitude"):
            raise ValueError("mode must be 'pressure' or 'altitude'")
        with self._lock:
            self._ensure_open()
            # A previous timed-out conversion may still be running. Let it finish
            # before touching ALT/OS or starting another one-shot.
            self._wait_idle()
            control = self._control | (0x80 if mode == "altitude" else 0)
            self._write_byte(0x26, control)
            self._read_output()  # Discard old data and clear both ready flags.
            self._write_byte(0x26, control | 2)  # OST=1, SBYB=0.
            deadline = time.monotonic() + self._timeout
            while True:
                idle = not self._read_byte(0x26) & 2
                if idle and self._read_byte(0x06) & 6 == 6:
                    break
                self._pause(deadline)
            data = self._read_output()
            raw_t = ((data[3] << 8) | data[4]) >> 4
            if raw_t & 0x800:
                raw_t -= 0x1000
            temperature = raw_t / 16.0
            raw_p = ((data[0] << 16) | (data[1] << 8) | data[2]) >> 4
            if mode == "altitude":
                if raw_p & 0x80000:
                    raw_p -= 0x100000
                return Reading(temperature_c=temperature, altitude_m=raw_p / 16.0)
            return Reading(temperature_c=temperature, pressure_pa=raw_p / 4.0)

    def read_pressure(self) -> float:
        """Return a fresh absolute pressure measurement in Pascals."""
        reading = self.read()
        assert reading.pressure_pa is not None
        return reading.pressure_pa

    def read_altitude(self) -> float:
        """Return a fresh hardware-computed altitude estimate in meters."""
        reading = self.read(mode="altitude")
        assert reading.altitude_m is not None
        return reading.altitude_m

    def read_temperature(self) -> float:
        """Return fresh die temperature in Celsius (also converts pressure)."""
        return self.read().temperature_c

    @property
    def sea_level_pressure_pa(self) -> float:
        """Altitude reference in Pa; hardware resolution is 2 Pa."""
        with self._lock:
            self._ensure_open()
            return ((self._read_byte(0x14) << 8) | self._read_byte(0x15)) * 2.0

    @sea_level_pressure_pa.setter
    def sea_level_pressure_pa(self, value: float) -> None:
        if not math.isfinite(value) or not 2 <= value <= 131070:
            raise ValueError("sea level pressure must be between 2 and 131070 Pa")
        encoded = int(value / 2 + 0.5)
        with self._lock:
            self._ensure_open()
            self._wait_idle()
            self._write_byte(0x14, encoded >> 8)
            self._write_byte(0x15, encoded & 0xFF)

    def close(self) -> None:
        """Close an internally opened bus; borrowed buses remain open.

        A conversion in flight after a timeout may finish on its own, returning
        the sensor to standby. Closing does not interrupt that conversion.
        """
        with self._lock:
            if not self._closed:
                if self._owns_bus:
                    self._bus.close()
                self._closed = True

    def __enter__(self) -> MPL3115A2:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
