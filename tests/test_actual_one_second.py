from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfd_twin.actual_one_second import (
    build_actual_one_second_bundle,
    iter_probe_rows,
    read_probe_header,
    validate_actual_one_second_bundle,
)


class ActualOneSecondTests(unittest.TestCase):
    def _probe_file(self, root: Path, name: str) -> Path:
        path = root / name
        path.write_text(
            "# Probe 0 (0.0005 0.0005 0.041)\n"
            "# Probe 1 (0.0015 0.0005 0.041) # Not Found\n"
            "# Probe 2 (0.0025 0.0005 0.041)\n"
            "# Time T\n"
            "1 293.1501 -1e+300 293.1503\n"
            "2 293.1502 -1e+300 293.1505\n",
            encoding="ascii",
        )
        return path

    def test_parser_preserves_invalid_probe_without_filling_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._probe_file(Path(directory), "T")
            probe = read_probe_header(path)
            self.assertEqual(probe.valid_indices, [0, 2])
            rows = list(iter_probe_rows(probe))
            self.assertEqual([time for time, _ in rows], [1, 2])
            self.assertLess(rows[0][1][1], -1e299)

    def test_bundle_has_exact_integer_frames_and_no_interpolation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._probe_file(root, "T")
            specs = {
                name: {
                    "path": source,
                    "kind": "battery-near-surface" if name == "cell_surface" else "xy",
                    "columns": 3,
                    "rows": 1,
                    **(
                        {
                            "angular_segments": 1,
                            "axial_segments": 1,
                            "cell_count": 1,
                            "samples_per_cell": 3,
                        }
                        if name == "cell_surface"
                        else {}
                    ),
                }
                for name in (
                    "xy15", "xy33", "xy", "xy68", "xy85",
                    "xz15", "xz33", "xz50", "xz68", "xz85", "cell_surface",
                )
            }
            with (
                patch("cfd_twin.actual_one_second._probe_specs", return_value=specs),
                patch(
                    "cfd_twin.actual_one_second._mesh_identity",
                    return_value=("single", 216_577),
                ),
            ):
                manifest = build_actual_one_second_bundle(
                    root, root / "output", initial_temperature_k=293.15, end_time_s=2
                )
            self.assertEqual(manifest["mesh_level"], "single")
            self.assertEqual(manifest["mesh_cell_count"], 216_577)
            self.assertFalse(manifest["temporal_interpolation"])
            self.assertFalse(manifest["spatial_interpolation"])
            self.assertFalse(manifest["padding"])
            dataset = manifest["datasets"]["xy"]
            self.assertEqual(dataset["valid_indices"], [0, 2])
            self.assertEqual(dataset["frame_count"], 3)
            self.assertEqual(dataset["temperature_range_k"], [293.15, 293.1505])
            self.assertEqual((root / "output" / "xy.u16").stat().st_size, 3 * 2 * 2)
            with patch("cfd_twin.actual_one_second._probe_specs", return_value=specs):
                report = validate_actual_one_second_bundle(root, root / "output")
            self.assertEqual(
                report["status"], "ACTUAL_ONE_SECOND_BUNDLE_VALIDATION_PASSED"
            )


if __name__ == "__main__":
    unittest.main()
