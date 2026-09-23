from __future__ import annotations

import math
import unittest

from cfd_twin.cad import _cad_model, _script
from cfd_twin.parametric_cad import (
    BASE_GROSS_VOLUME_L,
    parametric_config,
    parametric_config_overrides,
    preview_parametric_cad,
    resolve_parametric_spec,
)


class ParametricCadTests(unittest.TestCase):
    def test_baseline_dimensions_reproduce_reference_geometry(self) -> None:
        preview = preview_parametric_cad({
            "template": "reference_23cell",
            "size_mode": "dimensions",
            "tank_width_mm": 114,
            "tank_length_mm": 121,
            "tank_height_mm": 82,
            "inlet_diameter_mm": 19,
            "outlet_diameter_mm": 19,
        })
        self.assertEqual(preview["derived"]["tank_mm"], [114.0, 121.0, 82.0])
        self.assertEqual(preview["derived"]["cell_count"], 23)
        self.assertEqual(preview["derived"]["inlet_center_xy_mm"], [100.3, 107.5])
        self.assertEqual(preview["derived"]["outlet_center_xy_mm"], [13.5, 13.5])
        self.assertFalse(preview["calculation_started"])

    def test_parametric_ports_keep_confirmed_corner_clearance(self) -> None:
        preview = preview_parametric_cad({
            "template": "reference_23cell",
            "size_mode": "dimensions",
            "tank_width_mm": 140,
            "tank_length_mm": 150,
            "tank_height_mm": 90,
            "inlet_diameter_mm": 20,
            "outlet_diameter_mm": 16,
        })
        inlet_x, inlet_y = preview["derived"]["inlet_center_xy_mm"]
        outlet_x, outlet_y = preview["derived"]["outlet_center_xy_mm"]
        self.assertAlmostEqual(140 - inlet_x - 10, 4.2)
        self.assertAlmostEqual(150 - inlet_y - 10, 4.0)
        self.assertAlmostEqual(outlet_x - 8, 4.0)
        self.assertAlmostEqual(outlet_y - 8, 4.0)

    def test_volume_mode_preserves_reference_aspect_ratio(self) -> None:
        spec = resolve_parametric_spec({
            "template": "reference_23cell",
            "size_mode": "volume",
            "tank_volume_l": BASE_GROSS_VOLUME_L * 1.2,
            "inlet_diameter_mm": 20,
            "outlet_diameter_mm": 16,
        })
        scale = 1.2 ** (1 / 3)
        self.assertTrue(math.isclose(spec.tank_width_mm, 114 * scale))
        self.assertTrue(math.isclose(spec.tank_length_mm, 121 * scale))
        self.assertTrue(math.isclose(spec.tank_height_mm, 82 * scale))
        self.assertTrue(math.isclose(spec.gross_volume_l, BASE_GROSS_VOLUME_L * 1.2))

    def test_distinct_port_diameters_reach_cad_script(self) -> None:
        spec = resolve_parametric_spec({
            "template": "reference_23cell",
            "size_mode": "dimensions",
            "tank_width_mm": 125,
            "tank_length_mm": 135,
            "tank_height_mm": 90,
            "inlet_diameter_mm": 20,
            "outlet_diameter_mm": 16,
        })
        config = parametric_config(spec)
        model = _cad_model(config)
        self.assertEqual(model["ports"]["inlet_diameter_mm"], 20.0)
        self.assertEqual(model["ports"]["outlet_diameter_mm"], 16.0)
        self.assertNotEqual(model["expected"]["inlet_area_mm2"], model["expected"]["outlet_area_mm2"])
        script = _script(model)
        self.assertIn('inlet_radius = MODEL["ports"]["inlet_diameter_mm"] / 2', script)
        self.assertIn('outlet_radius = MODEL["ports"]["outlet_diameter_mm"] / 2', script)
        self.assertIn('build_port(MODEL["ports"]["inlet"], inlet_radius)', script)
        self.assertIn('build_port(MODEL["ports"]["outlet"], outlet_radius)', script)

    def test_parametric_overrides_preserve_tank_dimensions_for_solver(self) -> None:
        spec = resolve_parametric_spec({
            "template": "reference_23cell",
            "size_mode": "dimensions",
            "tank_width_mm": 125,
            "tank_length_mm": 135,
            "tank_height_mm": 90,
            "inlet_diameter_mm": 20,
            "outlet_diameter_mm": 16,
        })
        overrides = parametric_config_overrides(spec)
        self.assertIn("geometry.enclosure.width_x=125 mm", overrides)
        self.assertIn("geometry.enclosure.length_y=135 mm", overrides)
        self.assertIn("geometry.enclosure.height_z=90 mm", overrides)
        self.assertIn("geometry.ports.inlet_diameter=20 mm", overrides)
        self.assertIn("geometry.ports.outlet_diameter=16 mm", overrides)

    def test_out_of_range_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "입구 직경"):
            resolve_parametric_spec({
                "template": "reference_23cell",
                "size_mode": "dimensions",
                "tank_width_mm": 114,
                "tank_length_mm": 121,
                "tank_height_mm": 82,
                "inlet_diameter_mm": 2,
                "outlet_diameter_mm": 19,
            })


if __name__ == "__main__":
    unittest.main()
