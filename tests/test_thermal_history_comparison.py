from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin.thermal_history_comparison import compare_thermal_histories


def _write_series(run: Path, region: str, name: str, filename: str, values: list[float]) -> None:
    path = run / "postProcessing" / region / name / "0" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Time value\n" + "".join(
        f"{time_s} {value}\n" for time_s, value in enumerate(values, start=1)
    ), encoding="utf-8")


def _write_history(run: Path, outlet: list[float], average: list[float], maximum: list[float]) -> None:
    _write_series(run, "coolant", "coolant_outlet_mass_weighted_temperature",
                  "surfaceFieldValue.dat", outlet)
    _write_series(run, "batterySolid", "batterySolid_temperature_volume_integral",
                  "volFieldValue.dat", [value * 2.0 for value in average])
    for index in range(1, 24):
        _write_series(run, "batterySolid", f"battery_C{index:02d}_Tmax",
                      "volFieldValue.dat", maximum if index == 5 else [value - 1.0 for value in maximum])


class ThermalHistoryComparisonTests(unittest.TestCase):
    def test_compares_all_seconds_without_making_difference_a_validation_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, adaptive = root / "baseline", root / "adaptive"
            _write_history(baseline, [293.0, 294.0], [293.1, 294.1], [294.0, 295.0])
            _write_history(adaptive, [293.2, 293.8], [293.2, 294.0], [294.5, 294.5])
            mesh = root / "mesh.json"
            mesh.write_text(json.dumps({"actual": {"battery_total_volume_m3": 2.0}}), encoding="utf-8")
            output = root / "comparison.json"

            report = compare_thermal_histories(baseline, adaptive, mesh, output, end_time_s=2)

            self.assertEqual(report["status"], "THERMAL_HISTORY_COMPARISON_EXTRACTED")
            self.assertFalse(report["comparison_is_validation_gate"])
            self.assertEqual(report["sample_count"], 2)
            self.assertAlmostEqual(report["series"]["battery_volume_maximum_K"]["max_abs_delta_K"], 0.5)
            self.assertEqual(report["series"]["battery_volume_maximum_K"]["max_abs_delta_time_s"], 1)
            self.assertAlmostEqual(report["series"]["outlet_temperature_K"]["final_delta_K"], -0.2)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))["rows"]), 2)

    def test_missing_second_is_not_silently_compared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, adaptive = root / "baseline", root / "adaptive"
            _write_history(baseline, [293.0, 294.0], [293.1, 294.1], [294.0, 295.0])
            _write_history(adaptive, [293.2], [293.2], [294.5])
            mesh = root / "mesh.json"
            mesh.write_text(json.dumps({"actual": {"battery_total_volume_m3": 2.0}}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "adaptive outlet_temperature_K: missing 2 s sample"):
                compare_thermal_histories(baseline, adaptive, mesh, root / "comparison.json", end_time_s=2)


if __name__ == "__main__":
    unittest.main()
