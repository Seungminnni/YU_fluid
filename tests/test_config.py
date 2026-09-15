from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from cfd_twin.config import load_config
from cfd_twin.errors import ConfigError
from cfd_twin.geometry import cell_placements


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = yaml.safe_load(BASELINE.read_text(encoding="utf-8"))

    def _write(self, raw: dict[str, object], directory: str) -> Path:
        path = Path(directory) / "case.yaml"
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        return path

    def test_baseline_derived_values(self) -> None:
        config = load_config(BASELINE)
        derived = config.derived
        self.assertAlmostEqual(derived.cell_volume_m3, 1.927995411508056e-5)
        self.assertAlmostEqual(derived.cell_heat_w, 0.3147452509286901)
        self.assertAlmostEqual(derived.total_heat_w, 7.239140771359873)
        self.assertAlmostEqual(derived.reynolds_table4, 6773.5)
        self.assertAlmostEqual(
            derived.viscosity_implied_by_paper_re_pa_s,
            8.638487815987247e-4,
        )
        self.assertAlmostEqual(derived.nominal_current_a, 4.0)
        self.assertAlmostEqual(derived.equivalent_resistance_ohm, 0.01967157818304313)
        self.assertAlmostEqual(derived.right_wall_clearance_m, 0.0149)
        self.assertAlmostEqual(derived.top_wall_clearance_m, 0.0063)
        self.assertAlmostEqual(derived.bottom_wall_clearance_m, 0.0057)
        self.assertAlmostEqual(config.enclosure.notch.depth_m, 0.01125)

    def test_24_mm_port_is_rejected_for_paper_geometry(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["geometry"]["ports"]["inlet_diameter"] = "24 mm"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "inlet diameter must be 0.019 m"):
                load_config(self._write(raw, directory))

    def test_overrides_change_physics_and_provenance(self) -> None:
        base = load_config(BASELINE)
        changed = load_config(
            BASELINE,
            overrides=("boundary.inlet_velocity=0.6 m/s",),
        )
        self.assertAlmostEqual(changed.boundary.inlet_velocity_m_s, 0.6)
        self.assertAlmostEqual(changed.derived.reynolds_table4, base.derived.reynolds_table4 * 1.5)
        self.assertNotEqual(changed.resolved_input_sha256, base.resolved_input_sha256)
        self.assertEqual(changed.applied_overrides, ("boundary.inlet_velocity=0.6 m/s",))

    def test_overlay_selects_reviewed_layout_variant(self) -> None:
        changed = load_config(
            BASELINE,
            overlay_paths=(ROOT / "configs" / "layout_regular_triangular.yaml",),
        )
        self.assertAlmostEqual(changed.cells.column_pitch_x_m, 0.01948557)
        self.assertAlmostEqual(changed.derived.diagonal_surface_gap_m, 0.0035, places=7)

    def test_fig8_profiles_remain_separate_and_concentric(self) -> None:
        digitized = load_config(
            BASELINE,
            overlay_paths=(ROOT / "configs" / "paper_fig8_digitized_4mm.yaml",),
        )
        text_gap = load_config(
            BASELINE,
            overlay_paths=(ROOT / "configs" / "regular_triangular_3p5mm.yaml",),
        )
        self.assertEqual(digitized.enclosure.profile, "paper_fig8_digitized")
        self.assertAlmostEqual(digitized.cells.column_pitch_x_m, 0.020)
        self.assertAlmostEqual(text_gap.cells.column_pitch_x_m, 0.01948557)
        self.assertNotEqual(digitized.geometry_sha256, text_gap.geometry_sha256)
        digitized_cells = cell_placements(digitized)
        text_cells = cell_placements(text_gap)
        self.assertEqual(digitized.ports.inlet_center_xy_m, (digitized_cells[0].center_x_m, digitized_cells[0].center_y_m))
        self.assertAlmostEqual(text_gap.ports.inlet_center_xy_m[0], text_cells[0].center_x_m)
        self.assertAlmostEqual(text_gap.ports.inlet_center_xy_m[1], text_cells[0].center_y_m)

    def test_293p15_temperature_is_an_explicit_overlay(self) -> None:
        changed = load_config(
            BASELINE,
            overlay_paths=(ROOT / "configs" / "temperature_293p15K.yaml",),
        )
        self.assertAlmostEqual(changed.boundary.inlet_temperature_k, 293.15)
        self.assertAlmostEqual(changed.boundary.initial_fluid_temperature_k, 293.15)

    def test_ppt_bernardi_overlay_is_restricted_to_published_conditions(self) -> None:
        overlay = ROOT / "configs" / "ppt_bernardi_udf.yaml"
        config = load_config(BASELINE, overlay_paths=(overlay,))
        self.assertEqual(
            config.battery.heat_source.mode,
            "peneklioglu_bernardi_2c_293p15",
        )
        self.assertAlmostEqual(config.derived.cell_heat_w, 1.5173195359225133)
        for override, message in (
            ("battery.electrical_metadata.discharge_rate_c=1.0", "restricted to 2C"),
            ("battery.electrical_metadata.capacity=2.5 Ah", "restricted to a 2 Ah"),
            ("battery.heat_source.reference_temperature=298.15 K", "restricted to 293.15 K"),
        ):
            with self.subTest(override=override):
                with self.assertRaisesRegex(ConfigError, message):
                    load_config(
                        BASELINE,
                        overlay_paths=(overlay,),
                        overrides=(override,),
                    )

    def test_unsafe_full_transient_settings_are_rejected(self) -> None:
        mutations = (
            ("max_courant", 5.01),
            ("min_outer_correctors", 2),
            ("expose_in_gui", True),
        )
        for key, value in mutations:
            with self.subTest(key=key):
                raw = copy.deepcopy(self.raw)
                raw["solver"]["full_transient_validation"][key] = value
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(ConfigError):
                        load_config(self._write(raw, directory))


if __name__ == "__main__":
    unittest.main()
