from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cfd_twin.config import load_config
from cfd_twin.geometry import cell_placements, geometry_report, write_geometry_bundle


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


class GeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(BASELINE)

    def test_stable_23_cell_mapping_and_clearances(self) -> None:
        placements = cell_placements(self.config)
        self.assertEqual(len(placements), 23)
        self.assertEqual([cell.cell_id for cell in placements], [f"C{i:02d}" for i in range(1, 24)])
        self.assertEqual(
            [sum(cell.column_index == column for cell in placements) for column in range(5)],
            [5, 4, 5, 4, 5],
        )
        report = geometry_report(self.config)
        self.assertAlmostEqual(report["clearances"]["left_wall_m"], 0.0041)
        self.assertAlmostEqual(report["clearances"]["right_wall_m"], 0.0149)
        self.assertAlmostEqual(report["clearances"]["bottom_wall_m"], 0.0057)
        self.assertAlmostEqual(report["clearances"]["top_wall_m"], 0.0063)
        self.assertAlmostEqual(
            report["clearances"]["minimum_cell_cell_surface_m"],
            0.0030808174667515363,
        )
        self.assertEqual(report["clearances"]["minimum_cell_outline_id"], "C01")
        self.assertAlmostEqual(
            report["clearances"]["minimum_cell_outline_surface_m"], 0.0041
        )
        self.assertGreater(report["ports"]["opening_to_outline_clearance_m"]["inlet"], 0)
        self.assertGreater(report["ports"]["opening_to_outline_clearance_m"]["outlet"], 0)
        self.assertTrue(report["ports"]["centers_resolved"])
        self.assertEqual(report["ports"]["centers_status"], "USER_CONFIRMED_REFERENCE")
        self.assertEqual(
            [value * 1000 for value in self.config.ports.inlet_center_xy_m],
            [100.3, 107.5],
        )
        self.assertEqual(
            [value * 1000 for value in self.config.ports.outlet_center_xy_m],
            [13.5, 13.5],
        )
        self.assertEqual(
            self.config.ports.placement,
            "corner_4mm_clearance",
        )
        self.assertEqual(report["nonblocking_assumptions"], [])
        self.assertFalse(report["production_cad_ready"])

    def test_preview_artifacts_are_written_without_claiming_production_cad(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = write_geometry_bundle(self.config, directory)
            output = Path(directory)
            self.assertTrue((output / "geometry_report.json").is_file())
            self.assertTrue((output / "top_view.svg").is_file())
            self.assertTrue((output / "side_view_xz.svg").is_file())
            self.assertTrue((output / "battery_surfaces.vtk").is_file())
            self.assertFalse(report["production_cad_ready"])
            top_view = (output / "top_view.svg").read_text()
            self.assertIn("상면도 · X–Y 정투영", top_view)
            self.assertIn("114 mm", top_view)
            self.assertIn("121 mm", top_view)
            self.assertIn("#0067c5", top_view)
            self.assertIn("#d62828", top_view)
            self.assertNotIn("A-04", top_view)
            side_view = (output / "side_view_xz.svg").read_text()
            self.assertIn("측면도 · X–Z 정투영", side_view)
            self.assertIn("114 mm", side_view)
            self.assertIn("82 mm", side_view)
            self.assertIn("Cell 68 mm", side_view)
            self.assertIn("C01–C05", side_view)
            self.assertIn("C19–C23", side_view)
            self.assertIn('fill="#004b93">INLET', side_view)
            self.assertIn('fill="#a31d1d">OUTLET', side_view)


if __name__ == "__main__":
    unittest.main()
