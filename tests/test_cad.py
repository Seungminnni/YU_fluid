from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mdpi_cfd.cad import write_cad_source_bundle
from mdpi_cfd.config import load_config


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


class CadSourceTests(unittest.TestCase):
    def test_cadquery_source_contains_full_ppt_geometry(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_cad_source_bundle(config, directory)
            output = Path(directory)
            model = (output / "cad_model.json").read_text(encoding="utf-8")
            script = (output / "build_cad.py").read_text(encoding="utf-8")
            self.assertEqual(manifest["cell_count"], 23)
            self.assertEqual(manifest["port_diameter_mm"], 19.0)
            self.assertEqual(manifest["status"], "CAD_SOURCE_CREATED_NOT_EXECUTED")
            self.assertIn('"enclosure_profile": "pptx_measured_v1"', model)
            self.assertIn('"cell_diameter_mm": 19.0', model)
            self.assertIn('"coolant_volume_mm3"', model)
            self.assertIn("outer_with_ports = enclosure.union(inlet).union(outlet)", script)
            self.assertIn("coolant = coolant.cut(solid)", script)
            self.assertIn('"coolant_is_one_connected_solid"', script)
            self.assertIn('"volumes_and_areas_within_relative_1e_6"', script)
            self.assertIn('"inlet_area_mm2": sum(face.Area()', script)
            self.assertIn('production_surface_dir = OUTPUT / "production_surfaces"', script)
            self.assertIn("solid.scale(0.001)", script)
            self.assertIn('"production_surfaces/outerDomain.stl"', script)
            self.assertIn('"production_surfaces/inlet.stl"', script)
            self.assertIn('"production_surfaces/outlet.stl"', script)
            self.assertIn('"production_surfaces/outerWall.stl"', script)
            self.assertIn("production_battery_files", script)
            self.assertIn('result["file_sha256"]', script)
            self.assertGreater(manifest["analytic_expectations"]["inlet_area_mm2"], 283.0)
            self.assertFalse(manifest["production_cad_ready"])


if __name__ == "__main__":
    unittest.main()
