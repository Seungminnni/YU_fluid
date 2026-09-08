from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mdpi_cfd.case_builder import prepare_case_inputs
from mdpi_cfd.config import load_config


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


class CaseBuilderTests(unittest.TestCase):
    def test_complete_input_bundle_still_blocks_unvalidated_solver_run(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            manifest = prepare_case_inputs(config, directory)
            self.assertEqual(manifest["status"], "INPUT_BUNDLE_READY_NOT_RUNNABLE")
            self.assertFalse(manifest["runnable"])
            self.assertFalse(manifest["geometry_production_ready"])
            blocker_ids = {item["id"] for item in manifest["blockers"]}
            self.assertTrue({"DEPENDENCY_CADQUERY", "CAD_NOT_EXECUTED"} & blocker_ids)
            self.assertIn("SMOKE_CHECKMESH", blocker_ids)
            self.assertIn("PRODUCTION_CHECKMESH", blocker_ids)
            self.assertFalse(manifest["runnable_smoke"])
            self.assertFalse(manifest["runnable_production"])
            self.assertEqual([item["id"] for item in manifest["warnings"]], ["A-04"])
            output = Path(directory)
            self.assertTrue((output / "README_NOT_RUNNABLE.md").is_file())
            self.assertTrue((output / "provenance.json").is_file())
            self.assertTrue((output / "cad_source" / "build_cad.py").is_file())
            self.assertTrue((output / "constant" / "regionProperties").is_file())
            self.assertIsNone(manifest["production_mesh_source"])


if __name__ == "__main__":
    unittest.main()
