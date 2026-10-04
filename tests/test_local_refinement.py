import re
import unittest
from pathlib import Path

from cfd_twin.config import load_config
from cfd_twin.production_mesh import MESH_PRESETS, _snappy_dict


class LocalRefinementTests(unittest.TestCase):
    def test_only_selected_interfaces_and_rims_are_refined(self):
        config = load_config(Path(__file__).resolve().parents[1] / 'configs/baseline_23cell.yaml')
        preset = MESH_PRESETS['production_frame_fit_8um_10layers_local2']
        text = _snappy_dict(config, preset)
        for number in range(1, 24):
            name = f'battery_C{number:02d}'
            section = re.search(r'\b' + name + r'\s*\{\s*level\s*\((\d+) (\d+)\);', text)
            self.assertIsNotNone(section)
            self.assertEqual(section.groups(), ('1', '2') if number in {4, 9} else ('0', '1'))
            self.assertIn(f'faceZone {name}_interface;', text)
            self.assertIn(f'cellZone {name};', text)
        self.assertIn('firstLayerThickness     8e-06;', text)
        self.assertIn('nSurfaceLayers 10;', text)
        self.assertEqual(preset.maximum_non_orthogonality, 70)
        self.assertEqual(preset.maximum_skewness, 5)
        self.assertEqual(preset.target_cells, 600000)

    def test_frame_fit_800k_trial_preserves_port_cap_refinement(self):
        configs = Path(__file__).resolve().parents[1] / 'configs'
        config = load_config(
            configs / 'baseline_23cell.yaml',
            overlay_paths=(configs / 'frame_fit_water_phi34_20260929.yaml',),
        )
        preset = MESH_PRESETS['production_frame_fit_800k_6um_11layers_motion']
        text = _snappy_dict(config, preset)
        self.assertIn('outletCapRefinement', text)
        self.assertIn('inletCapRefinement', text)
        self.assertRegex(
            text,
            r'outletCapRefinement\s*\{\s*mode inside;\s*levels \(\(1e15 2\)\);',
        )
        self.assertIn('firstLayerThickness     6e-06;', text)
        self.assertIn('nSurfaceLayers 11;', text)

    def test_frame_fit_800k_port_rim_retry_changes_only_cap_box_level(self):
        configs = Path(__file__).resolve().parents[1] / 'configs'
        config = load_config(
            configs / 'baseline_23cell.yaml',
            overlay_paths=(configs / 'frame_fit_water_phi34_20260929.yaml',),
        )
        preset = MESH_PRESETS['production_frame_fit_800k_6um_11layers_motion_port_l1']
        text = _snappy_dict(config, preset)
        self.assertRegex(
            text,
            r'outletCapRefinement\s*\{\s*mode inside;\s*levels \(\(1e15 1\)\);',
        )
        self.assertRegex(text, r'outerDomain\s*\{\s*level \(1 2\);')
        self.assertEqual(preset.background_cell_m, 0.0029)
        self.assertEqual(preset.first_layer_thickness_m, 6e-6)
        self.assertEqual(preset.prism_layer_count, 11)

    def test_frame_fit_800k_three_mm_retry_keeps_port_and_wall_settings(self):
        preset = MESH_PRESETS[
            'production_frame_fit_800k_6um_11layers_motion_3mm_port_l1'
        ]
        self.assertEqual(preset.target_cells, 800000)
        self.assertEqual(preset.minimum_total_cells, 800000)
        self.assertEqual(preset.background_cell_m, 0.003)
        self.assertEqual(preset.port_cap_refinement_level, 1)
        self.assertEqual(preset.first_layer_thickness_m, 6e-6)
        self.assertEqual(preset.prism_layer_count, 11)
        self.assertEqual(preset.mesh_shrinker, 'displacementMotionSolver')
