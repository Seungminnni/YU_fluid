from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from mdpi_cfd.config import load_config
from mdpi_cfd.overlay import write_geometry_overlay


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"
PPTX = ROOT / "document" / "20260826 (1).pptx"


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow not installed")
class OverlayTests(unittest.TestCase):
    def test_original_ppt_detects_all_cells_but_keeps_a04_blocked(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            report = write_geometry_overlay(config, PPTX, directory)
            self.assertEqual(report["detected_cell_count"], 23)
            self.assertEqual(report["detected_topology"], [5, 4, 5, 4, 5])
            self.assertLess(report["candidate"]["rms_center_error_mm"], 1.0)
            self.assertTrue(report["port_centers_resolved"])
            self.assertEqual(report["port_centers_status"], "ASSUMPTION_A04")
            self.assertTrue(report["notch_depth_resolved"])
            self.assertEqual(report["notch_depth_mm"], 11.25)
            self.assertTrue(report["notch_profile_fully_constrained"])
            self.assertFalse(report["geometry_gate_g1_passed"])
            self.assertTrue((Path(directory) / "top_overlay.svg").is_file())
            self.assertTrue((Path(directory) / "overlay_report.json").is_file())


if __name__ == "__main__":
    unittest.main()
