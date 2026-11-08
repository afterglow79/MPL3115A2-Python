# MPL3115A2 Python driver

A Python 3.9+ I2C library for the MPL3115A2, including the MPL3115A2ST1
ordering variant. Reads absolute pressure (Pa), die temperature (Celsius),
and hardware-computed altitude (meters).

The core driver has no third-party dependencies. Linux I2C access uses
`smbus2` or `smbus`. Other hosts can supply a compatible bus adapter.

## Installation

On a Raspberry Pi, enable I2C in `sudo raspi-config` (Interface Options ->
I2C). From the library checkout, create an environment and install the
I2C adapter:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[i2c]'
python examples/read_sensor.py
```

This installs the library and `smbus2`. If you already use the original
`smbus` package, `python -m pip install -e .` is sufficient. The driver prefers
`smbus2`, falls back to `smbus`, and can borrow either type of bus object.
The editable install (`-e`) picks up source changes when the application
restarts. Omit `-e` for a regular installation.

### Use with uv

From an application project, add the library checkout as an editable
dependency. For a new project without a `pyproject.toml`, run `uv init` first.
These examples assume a sibling checkout named `MPL3115A2ST1`. Adjust the
path to match the checkout location.

On a Raspberry Pi or another Linux I2C host:

```sh
uv add --editable ../MPL3115A2ST1 --extra i2c
uv run python main.py
```

On Windows, for development with a custom adapter or simulated tests:

```powershell
uv add --editable '..\MPL3115A2ST1'
uv run python main.py
```

Replace `main.py` with the application script. Select the application's
`.venv` interpreter in the editor or IDE. Run `uv add` in each application
project that needs the library; each project has its own environment.
`uv tool install` creates a separate command-line tool environment and
does not add an importable dependency to an application.

To work on the library itself, run these commands from its checkout:

```sh
uv sync --extra test
uv run python -m pytest -q
```

On a Linux I2C host, add `--extra i2c` to `uv sync` before running the
hardware example with `uv run python examples/read_sensor.py`.

### Use with pip across projects

Install the checkout into each application's Python environment. Replace
the example path with the checkout location:

```sh
python -m pip install -e '/path/to/MPL3115A2ST1[i2c]'
python /path/to/project/main.py
```

For Windows development, from an activated project environment:

```powershell
python -m pip install -e 'C:\path\to\MPL3115A2ST1[test]'
python 'C:\path\to\project\main.py'
```

An installed package can be imported from any folder when using the
environment containing it. Select that environment's interpreter in the
editor. Editable installs reference the local checkout; other machines
need their own copy of the source and their own installation.

The default backends require a Linux I2C device, such as `/dev/i2c-1`,
and permission to access it, usually through the `i2c` group. Windows
supports development and simulated tests. Run Raspberry Pi hardware code
on the Pi, for example through an editor's SSH connection. Local hardware
access on other platforms requires a compatible bus adapter.

## Read measurements

```python
from mpl3115a2 import MPL3115A2

with MPL3115A2(bus_number=1) as sensor:
    reading = sensor.read()
    print(f"Pressure: {reading.pressure_pa:.2f} Pa")
    print(f"Temperature: {reading.temperature_c:.2f} C")
    print(f"Altitude: {sensor.read_altitude():.2f} m")
```

To use an existing bus:

```python
import smbus
from mpl3115a2 import MPL3115A2

bus = smbus.SMBus(1)
try:
    with MPL3115A2(bus=bus) as sensor:
        print(sensor.read_pressure())
finally:
    bus.close()  # The caller closes a bus passed to the driver.
```

Run `python examples/read_sensor.py` after installation for a continuous
pressure/temperature display. Press Ctrl+C to stop.

## API

`MPL3115A2(bus_number=1, *, bus=None, address=0x60, oversampling=128, timeout=2.0)`

- `bus`: optional object implementing `read_byte_data`, `write_byte_data`,
  and `read_i2c_block_data` with the usual SMBus signatures. Its lifetime
  stays with the caller. Internally opened buses also implement `close`.
- `oversampling`: ratio of 1, 2, 4, 8, 16, 32, 64, or 128, set at creation.
  Higher values reduce noise but increase conversion time. At 128x the
  datasheet lists 512 ms minimum between samples; default timeout is 2 s.
- `timeout`: positive, finite seconds per sensor wait. A read following
  a timeout can also wait for the earlier conversion before starting its
  own. OS-level I2C calls have their own timeout and can block separately.
- `address`: defaults to the chip's fixed address; only change this when
  your system explicitly remaps addresses (for example through a bridge).

| Operation | Result |
| --- | --- |
| `read()` | Frozen `Reading` with `pressure_pa`, `temperature_c`; `altitude_m=None` |
| `read(mode="altitude")` | `Reading` with `altitude_m`, `temperature_c`; `pressure_pa=None` |
| `read_pressure()` | Fresh absolute pressure in Pa; divide by 100 for hPa |
| `read_temperature()` | Fresh temperature in Celsius |
| `read_altitude()` | Fresh altitude estimate in meters |
| `sea_level_pressure_pa` | Get/set the altitude calculation's sea-level reference in Pa |
| `close()` | Close this driver and its internally opened bus; safe to repeat |

Every read method triggers a new conversion. Use `read()` to get pressure
and temperature from the **same** conversion. Pressure and altitude share
the same hardware registers, so acquiring both requires separate conversions.
The sensor returns to standby after each conversion. Per-instance operations
are serialized with a lock, but multiple instances/processes must not control
the same sensor. The bus itself must be coordinated externally if shared
with unrelated code.

For local altitude correction, supply a current sea-level pressure reference:

```python
from mpl3115a2 import MPL3115A2

with MPL3115A2() as sensor:
    sensor.sea_level_pressure_pa = 101000  # Example only; use your local value.
    print(sensor.read_altitude())
```

The hardware reference defaults to 101326 Pa, has 2 Pa resolution, and
accepts values from 2 to 131070 Pa. The setter rounds to the nearest 2 Pa
(ties upward); read the property back to see the applied value. This is a
reference for altitude estimation, not a correction to raw pressure.
Altitude varies with weather and the atmosphere model; this is not GPS
altitude. Temperature is sensor die temperature.

### Errors and ownership

- `DeviceNotFoundError` (an `OSError`) means WHO_AM_I did not match `0xC4`.
- `TimeoutError` means the conversion did not finish or both ready flags
  were not raised. Confirm power, wiring, bus and timeout settings.
- Bus `OSError` exceptions propagate, preserving the original failure.
- Short output transfers raise `OSError`; invalid settings raise `ValueError`.
- Reading after `close()` raises `RuntimeError`.

Allow the sensor to power up before creating the driver. Initialization
checks identity, enters standby, disables FIFO and interrupt sources,
selects normal output updates, and enables data-ready flags. It does not
reset calibration/reference registers or rewrite user offsets. Each read
waits for any previous one-shot to finish, selects the requested mode in
standby, discards stale output, triggers OST, waits for OST to clear and
both PDR/TDR flags, then reads all five bytes in one burst. Closing after
a timeout leaves any in-flight conversion to complete and return to standby.

## Acquisition and data conversion

The driver uses standby one-shot conversions, enables data-ready flags
with `PT_DATA_CFG=0x07`, and checks completion before reading output.
At 128x oversampling, allow at least the listed 512 ms conversion time
when choosing a timeout.

Conversions discard the reserved low nibble:

- Pressure: unsigned 20-bit output divided by 4 (Q18.2).
- Altitude: signed two's-complement 20-bit output divided by 16 (Q16.4).
- Temperature: signed two's-complement 12-bit output divided by 16 (Q8.4).

## Wiring

Follow the board schematic and the datasheet's application circuit for
VDD, VDDIO, CAP, and decoupling. The table covers bus connections rather
than a complete bare-chip circuit. Use 3.3 V logic for a direct Raspberry
Pi connection and account for pull-ups already on the connected boards.

| I2C signal | Pi GPIO (BCM) | Physical header pin |
| --- | --- | --- |
| SDA | GPIO2 / SDA1 | 3 |
| SCL | GPIO3 / SCL1 | 5 |
| Ground | GND | e.g. 6 |

The fixed sensor address is `0x60` in 7-bit notation. Examples use I2C bus 1.

## Other boards and testing

Pass a custom bus as `MPL3115A2(bus=adapter)`. It must provide
`read_byte_data(address, register)`,
`write_byte_data(address, register, value)`, and
`read_i2c_block_data(address, register, length)` with SMBus-compatible
semantics. Block reads must return the requested bytes in register order;
raise `OSError` for transport failures. The caller owns the adapter and
closes it after use.

The supported runtime is CPython. MicroPython and CircuitPython need
changes for dataclasses, typing, threading, and bus APIs.

To run simulated tests from the checkout with pip:

```sh
python -m pip install -e '.[test]'
python -m pytest -q
```

Tests use an SMBus sensor model and literal register vectors. They cover
fractional and signed conversions, fresh reads, mode changes, standby
sequencing, oversampling, timeouts and recovery, identity failures,
transport errors, reference encoding and bus ownership.

Physical hardware validation is pending. For an initial hardware check,
run the example and compare pressure with a local reference and temperature
with an expected range. The driver supports polling one-shot acquisition.
FIFO streaming, GPIO interrupts, threshold events and offset calibration
have no public API.

## Datasheet source

Implemented from [NXP MPL3115A2 datasheet, Rev. 8.1, 17 May 2024](https://www.nxp.com/docs/en/data-sheet/MPL3115A2.pdf),
especially sections 14.2-14.4, 14.7, 14.8, 14.12, 14.13 and 14.22.
