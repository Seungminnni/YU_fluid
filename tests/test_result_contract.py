from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin.config import load_config
from cfd_twin.result_contract import update_figure_status, write_result_contract


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


class ResultContractTests(unittest.TestCase):
    def test_all_required_figures_are_declared_without_fake_results(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_result_contract(config, directory)
            self.assertEqual(manifest["figure_ids"], [1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14])
            self.assertEqual(manifest["figure_5_status"], "EXCLUDED_BY_USER")
            self.assertFalse(manifest["synthetic_data"])
            self.assertEqual(len(manifest["cell_ids"]), 23)
            output = Path(directory)
            figure6 = json.loads((output / "figures" / "figure_06.json").read_text())
            figure14 = json.loads((output / "figures" / "figure_14.json").read_text())
            self.assertEqual(figure6["required_panels"], ["fluid_temperature", "fluid_velocity"])
            self.assertEqual(figure14["required_regions"], ["fluid", "solid"])
            self.assertEqual(figure14["plane"], "XY")
            with (output / "per_cell_time_series.csv").open(newline="") as stream:
                header = next(csv.reader(stream))
            self.assertIn("cell_id", header)
            self.assertIn("surface_tmax_K", header)
            updated = update_figure_status(
                directory,
                2,
                "MESH_READY",
                artifacts=["mesh_xy.svg"],
                metadata={"actual_solver_data": True},
            )
            self.assertEqual(updated["status"], "MESH_READY")
            self.assertEqual(updated["artifacts"], ["mesh_xy.svg"])
            self.assertTrue(updated["metadata"]["actual_solver_data"])
            self.assertFalse(updated["synthetic_data"])


if __name__ == "__main__":
    unittest.main()
