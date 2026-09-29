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
