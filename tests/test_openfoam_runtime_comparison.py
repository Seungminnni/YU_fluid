from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin.openfoam_runtime_comparison import compare_runtime_windows


def _write_log(path: Path, values: tuple[tuple[int, int], ...]) -> None:
    path.write_text("".join(
        f"Time = {physical}\nExecutionTime = {wall - 1} s  ClockTime = {wall} s\n"
        for physical, wall in values
    ), encoding="utf-8")


class RuntimeComparisonTests(unittest.TestCase):
    def test_equal_physical_time_windows_are_compared_without_becoming_a_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new, output = root / "old.log", root / "new.log", root / "report.json"
            _write_log(old, ((1000, 200), (1500, 700), (1800, 1000)))
            _write_log(new, ((1000, 50), (1500, 400), (1800, 650)))

            report = compare_runtime_windows(old, new, output)

            self.assertEqual(report["status"], "RUNTIME_WINDOWS_COMPARED")
            self.assertFalse(report["comparison_is_validation_gate"])
            self.assertEqual(report["windows"][0]["baseline_wall_s"], 800)
            self.assertEqual(report["windows"][0]["adaptive_wall_s"], 600)
            self.assertAlmostEqual(report["windows"][0]["observed_wall_reduction_fraction"], 0.25)
            self.assertEqual(report["windows"][1]["baseline_wall_s"], 300)
            self.assertEqual(report["windows"][1]["adaptive_wall_s"], 250)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))["windows"]), 2)

    def test_missing_clock_sample_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old.log", root / "new.log"
            _write_log(old, ((1000, 200), (1500, 700), (1800, 1000)))
            _write_log(new, ((1000, 50), (1800, 650)))
            with self.assertRaisesRegex(ValueError, "missing ClockTime at 1500 s"):
                compare_runtime_windows(old, new, root / "report.json")


if __name__ == "__main__":
    unittest.main()
