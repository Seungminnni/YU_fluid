from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cfd_twin.config import load_config
from cfd_twin.openfoam_bridge import write_openfoam_bridge


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


class OpenFoamBridgeTests(unittest.TestCase):
    def test_bridge_preserves_names_and_scales_mm_to_m(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_openfoam_bridge(config, directory)
            script = (Path(directory) / "Allmesh").read_text()
            self.assertIn("gmshToFoam", script)
            self.assertIn("-scale '(0.001 0.001 0.001)'", script)
            self.assertIn("splitMeshRegions", script)
            self.assertIn("-cellZonesOnly", script)
            self.assertIn("-allRegions", script)
            self.assertIn("parse_checkmesh.py", script)
            self.assertTrue((Path(directory) / "parse_checkmesh.py").is_file())
            self.assertEqual(manifest["mandatory_coordinate_scale"], 0.001)
            self.assertEqual(manifest["expected_volume_zones"], ["coolant", "batterySolid"])
            self.assertFalse(manifest["openfoam_checkmesh_passed"])


if __name__ == "__main__":
    unittest.main()
