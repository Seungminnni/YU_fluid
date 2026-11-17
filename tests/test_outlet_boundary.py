import gzip
import json
from pathlib import Path
import tempfile
import unittest

from cfd_twin.config import load_config
from cfd_twin.openfoam_case import _fluid_fields
from cfd_twin.outlet_boundary import apply_profile, record_outlet
from cfd_twin.workflow_run import BASE_CONFIG


class OutletBoundaryTests(unittest.TestCase):
    def test_only_outlet_velocity_treatment_changes_in_fresh_case(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            fields = _fluid_fields(load_config(BASE_CONFIG))
            for name, text in fields.items():
                path = case / '0/coolant' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
            apply_profile(case, 'pressure_backflow')
            old = 'type inletOutlet;\n        inletValue uniform (0 0 0);'
            for name, original in fields.items():
                expected = original.replace(old, 'type pressureInletOutletVelocity;') if name == 'U' else original
                self.assertEqual((case / '0/coolant' / name).read_text(encoding='utf-8'), expected)
            audit = json.loads((case / 'reports/outlet_boundary.json').read_text())
            self.assertTrue(audit['boundary_condition_changed'])
            self.assertFalse(audit['operating_inputs_changed'])
            self.assertFalse(audit['acceptance_limits_changed'])
            (case / 'runs/steady_cold_flow').mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'fresh'):
                apply_profile(case, 'pressure_backflow')

    def test_compressed_measured_backflow_counts_and_nonzero_reverse_velocity(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            run = case / 'runs/steady_cold_flow'
            boundary = run / 'constant/coolant/polyMesh/boundary'
            boundary.parent.mkdir(parents=True)
            boundary.write_text('FoamFile { format ascii; }\n1\n(\noutlet { nFaces 3; startFace 0; }\n)')
            fields = run / '1500/coolant'
            fields.mkdir(parents=True)
            for name, value in {
                'phi': 'nonuniform List<scalar> 3 (-0.1 0.3 -0.05)',
                'U': 'nonuniform List<vector> 3 ((0 0 -0.01) (0 0 0.5) (0 0 0))',
            }.items():
                with gzip.open(fields / (name + '.gz'), 'wt') as stream:
                    stream.write('FoamFile { format ascii; }\nboundaryField\n{\noutlet\n{\nvalue ' + value + ';\n}\n}')
            report = record_outlet(case, '1500')
            self.assertEqual(report['backflow_face_count'], 2)
            self.assertEqual(report['zero_speed_backflow_faces'], 1)
            self.assertAlmostEqual(report['backflow_mass_kg_s'], 0.15)
            self.assertAlmostEqual(report['backflow_to_net_mass_ratio'], 1)
            self.assertAlmostEqual(report['maximum_backflow_speed_m_s'], 0.01)
            with gzip.open(fields / 'U.gz', 'wt') as stream:
                stream.write('FoamFile { format ascii; }\nboundaryField { outlet { value uniform (0 0 0); } }')
            self.assertEqual(record_outlet(case, '1500')['zero_speed_backflow_faces'], 2)
            with self.assertRaises(ValueError):
                record_outlet(case, '../9500')

    def test_default_is_unchanged_and_unknown_profile_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            apply_profile(case, None)
            self.assertEqual(list(case.iterdir()), [])
            with self.assertRaises(ValueError):
                apply_profile(case, 'typo')

    def test_flux_corrected_outflow_profile_uses_flux_consistent_velocity(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            fields = _fluid_fields(load_config(BASE_CONFIG))
            for name, text in fields.items():
                path = case / '0/coolant' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
            apply_profile(case, 'flux_corrected_outflow')
            velocity = (case / '0/coolant/U').read_text(encoding='utf-8')
            self.assertIn('type fluxCorrectedVelocity;', velocity)
            self.assertNotIn('type inletOutlet;', velocity)
            audit = json.loads((case / 'reports/outlet_boundary.json').read_text())
            self.assertEqual(audit['after_type'], 'fluxCorrectedVelocity')
            self.assertFalse(audit['operating_inputs_changed'])
