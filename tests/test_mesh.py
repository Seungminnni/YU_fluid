from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin.config import load_config
from cfd_twin.mesh import write_mesh_source_bundle


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


class MeshSourceTests(unittest.TestCase):
    def test_gmsh_source_requires_validated_cad_and_names_all_regions(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cad = root / "cad"
            cad.mkdir()
            (cad / "coolant.step").write_text("fixture")
            (cad / "battery_cells.step").write_text("fixture")
            (cad / "cad_result.json").write_text(
                json.dumps(
                    {
                        "cad_geometry_checks_passed": True,
                        "expected": {
                            "inlet_area_mm2": 283.5287369864788,
                            "outlet_area_mm2": 283.5287369864788,
                            "battery_interface_area_mm2": 106397.88919545231,
                            "coolant_volume_mm3": 646693.7145202818,
                            "battery_total_volume_mm3": 443438.9446468529,
                        },
                    }
                )
            )
            manifest = write_mesh_source_bundle(config, cad, root / "mesh")
            script = (root / "mesh" / "build_mesh.py").read_text()
            self.assertEqual(manifest["mesh_level"], "smoke")
            self.assertIn('"coolant"', script)
            self.assertIn('"batterySolid"', script)
            self.assertIn('"batteryInterfaces"', script)
            self.assertIn('"inlet"', script)
            self.assertIn('"outlet"', script)
            self.assertIn('"minSICN"', script)
            self.assertIn('"minDetJac"', script)
            self.assertIn("area_relative_errors", script)
            self.assertIn("mesh_integrals_si", script)
            self.assertIn("mesh_integral_gate_passed", script)
            self.assertIn('"file_sha256"', script)
            self.assertEqual(manifest["intended_use"], "pipeline_smoke_test_only")
            self.assertEqual(manifest["production_backend"], "blockMesh+snappyHexMesh")
            self.assertFalse(manifest["production_mesh_ready"])
            self.assertFalse(manifest["mesh_generated"])

    def test_gmsh_rejects_production_profile_because_production_uses_snappy(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cad = root / "cad"
            cad.mkdir()
            for filename in ("coolant.step", "battery_cells.step"):
                (cad / filename).write_text("fixture")
            (cad / "cad_result.json").write_text(
                json.dumps({"cad_geometry_checks_passed": True, "expected": {}})
            )
            with self.assertRaisesRegex(Exception, "production mesh uses"):
                write_mesh_source_bundle(config, cad, root / "mesh", mesh_level="single")


if __name__ == "__main__":
    unittest.main()
