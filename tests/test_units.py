from __future__ import annotations

import unittest

from mdpi_cfd.errors import ConfigError
from mdpi_cfd.units import quantity


class UnitTests(unittest.TestCase):
    def test_supported_units_are_converted_to_si(self) -> None:
        self.assertAlmostEqual(quantity("19 mm", "length", "x"), 0.019)
        self.assertAlmostEqual(quantity("20 °C", "temperature", "t"), 293.15)
        self.assertAlmostEqual(
            quantity("1.12e-3 Pa·s", "dynamic_viscosity", "mu"), 0.00112
        )
        self.assertAlmostEqual(quantity("19.6716 mΩ", "resistance", "r"), 0.0196716)

    def test_dimension_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            quantity("19 mm", "pressure", "outlet")

    def test_nonfinite_value_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            quantity(float("nan"), "length", "x")


if __name__ == "__main__":
    unittest.main()

