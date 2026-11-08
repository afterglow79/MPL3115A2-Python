"""Print pressure and temperature from a sensor on I2C bus 1."""

import time

from mpl3115a2 import MPL3115A2


def main():
    try:
        with MPL3115A2(bus_number=1, oversampling=128) as sensor:
            while True:
                reading = sensor.read()
                print(
                    f"Pressure: {reading.pressure_pa:.2f} Pa "
                    f"({reading.pressure_pa / 100:.2f} hPa), "
                    f"temperature: {reading.temperature_c:.2f} C"
                )
                time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
