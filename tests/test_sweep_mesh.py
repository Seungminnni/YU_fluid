from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin.config import load_config
from cfd_twin.sweep_mesh import build_sweep_mesh_plan, write_sweep_mesh_source_bundle


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"
ALIGNED_PORTS = ROOT / "configs" / "recalculation_800k_c05_inlet_c19_outlet_phi19_20261013.yaml"


class SweepMeshTests(unittest.TestCase):
    def test_plan_partitions_ports_plenums_and_battery_body(self) -> None:
        config = load_config(BASELINE, overlay_paths=(ALIGNED_PORTS,))
        plan = build_sweep_mesh_plan(config, 800_000)
        self.assertEqual(plan["status"], "SWEEP_MESH_PLAN_READY")
        self.assertEqual(len(plan["battery_centres_m"]), 23)
        self.assertEqual([item["name"] for item in plan["sections"]], [
            "bottom_port", "lower_plenum", "battery_body", "upper_plenum", "top_port"
        ])
        self.assertAlmostEqual(plan["z_partitions_m"][2], 0.007)
        self.assertAlmostEqual(plan["z_partitions_m"][3], 0.075)
        self.assertLess(plan["estimated_target_error_fraction"], 0.12)
        self.assertEqual(plan["cap_layer_count"], 11)
        self.assertEqual(plan["cap_bulk_intervals"], 4)
        self.assertEqual(plan["battery_circumferential_intervals"], 36)
        self.assertEqual(plan["source_mesh_algorithm"], 5)
        self.assertEqual(plan["source_recombination_algorithm"], 1)
        self.assertAlmostEqual(plan["cap_transition_depth_m"], 0.00025)
        self.assertAlmostEqual(plan["cap_transition_width_m"], 0.02)
        self.assertEqual(plan["cap_transition_easing"], "linear")
        self.assertTrue(plan["hard_gates"]["volume_cells_all_hexahedral"])

    def test_bundle_contains_quad_sweep_and_fail_closed_checks(self) -> None:
        config = load_config(BASELINE, overlay_paths=(ALIGNED_PORTS,))
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as directory:
            output = Path(directory) / "sweep"
            manifest = write_sweep_mesh_source_bundle(
                config, output, target_cells=400_000
            )
            source = (output / "generate_sweep_mesh.py").read_text(encoding="utf-8")
            run_script = (output / "run_sweep_mesh.sh").read_text(encoding="utf-8")
            patch_script = (output / "set_openfoam_patch_types.py").read_text(
                encoding="utf-8"
            )
            plan = json.loads((output / "sweep_mesh_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "SWEEP_MESH_SOURCE_CREATED")
            self.assertIn('setRecombine', source)
            self.assertIn('layer_offsets', source)
            self.assertIn('circumferential_nodes', source)
            self.assertIn('mapped_surfaces', source)
            self.assertIn('extrude_stage', source)
            self.assertIn('cap_heights', source)
            self.assertIn('non-sweep elements rejected', source)
            self.assertIn('f"battery_{name}_interface"', source)
            self.assertIn('splitMeshRegions -cellZonesOnly -addZones', run_script)
            self.assertIn('checkMesh -constant -allRegions -meshQuality', run_script)
            self.assertIn('set_openfoam_patch_types.py', run_script)
            self.assertIn('outerWall', patch_script)
            self.assertIn("wall", patch_script)
            self.assertTrue((output / "constant" / "regionProperties").is_file())
            self.assertTrue((output / "system" / "meshQualityDict").is_file())
            self.assertEqual(plan["wall_layer_count"], 11)
            self.assertEqual(plan["first_layer_height_m"], 6e-6)
            self.assertEqual(plan["battery_circumferential_intervals"], 36)
            self.assertEqual(plan["cap_bulk_intervals"], 1)
            sections = {item["name"]: item for item in plan["sections"]}
            self.assertEqual(sections["battery_body"]["axial_intervals"], 2)
            self.assertEqual(plan["source_recombination_algorithm"], 1)
            self.assertEqual(plan["cap_transition_easing"], "linear")
            self.assertLess(plan["estimated_target_error_fraction"], 0.12)

    def test_true_million_profile_exceeds_one_million_without_changing_wall_contract(self) -> None:
        config = load_config(BASELINE, overlay_paths=(ALIGNED_PORTS,))
        plan = build_sweep_mesh_plan(config, 1_050_000)
        sections = {item["name"]: item for item in plan["sections"]}
        self.assertEqual(plan["estimated_cells"], 1_049_808)
        self.assertEqual(sections["battery_body"]["axial_intervals"], 32)
        self.assertEqual(plan["battery_circumferential_intervals"], 36)
        self.assertEqual(plan["wall_layer_count"], 11)
        self.assertEqual(plan["cap_layer_count"], 11)
        self.assertEqual(plan["first_layer_height_m"], 6e-6)
        self.assertEqual(plan["cap_first_layer_height_m"], 6e-6)
        self.assertTrue(plan["hard_gates"]["volume_cells_all_hexahedral"])

    def test_600k_profile_uses_same_reference_sweep_with_only_axial_redistribution(self) -> None:
        config = load_config(BASELINE, overlay_paths=(ALIGNED_PORTS,))
        plan = build_sweep_mesh_plan(config, 600_000)
        sections = {item["name"]: item for item in plan["sections"]}
        self.assertGreaterEqual(plan["estimated_cells"], 600_000)
        self.assertLessEqual(plan["estimated_cells"], 690_000)
        self.assertEqual(sections["battery_body"]["axial_intervals"], 13)
        self.assertEqual(plan["battery_circumferential_intervals"], 36)
        self.assertEqual(plan["wall_layer_count"], 11)
        self.assertEqual(plan["cap_layer_count"], 11)
        self.assertEqual(plan["cap_bulk_intervals"], 2)
        self.assertEqual(plan["first_layer_height_m"], 6e-6)
        self.assertEqual(plan["cap_first_layer_height_m"], 6e-6)
        self.assertTrue(plan["hard_gates"]["volume_cells_all_hexahedral"])


if __name__ == "__main__":
    unittest.main()
