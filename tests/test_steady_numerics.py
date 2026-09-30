import json
from pathlib import Path
import tempfile
import unittest

from cfd_twin.openfoam_case import _fluid_schemes, _solid_schemes
from cfd_twin.steady_numerics import apply_profile


class SteadyNumericsTests(unittest.TestCase):
    def test_pressure_operator_only_changes_in_both_steady_stages(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            paths = [case / 'system/coolant/fvSchemes'] + [
                case / 'profiles' / stage / 'system/coolant/fvSchemes'
                for stage in ('steady_cold_flow', 'steady', 'frozen_flow_actual_1s')
            ]
            for path in paths:
                path.parent.mkdir(parents=True)
                path.write_text(_fluid_schemes(), encoding='utf-8')
            solid = case / 'profiles/steady/system/batterySolid/fvSchemes'
            solid.parent.mkdir(parents=True)
            solid.write_text(_solid_schemes(), encoding='utf-8')
            apply_profile(case, 'pressure_limited_0p5')
            line = '    laplacian(rhorAUf,p_rgh) Gauss linear limited 0.5;\n'
            for path in paths[:3]:
                value = path.read_text(encoding='utf-8')
                self.assertIn(line, value)
                self.assertEqual(value.replace(line, ''), _fluid_schemes())
                self.assertNotIn(b'\r', path.read_bytes())
            self.assertEqual(paths[-1].read_text(encoding='utf-8'), _fluid_schemes())
            self.assertEqual(solid.read_text(encoding='utf-8'), _solid_schemes())
            report = json.loads((case / 'reports/steady_numerics.json').read_text())
            self.assertEqual(len(report['changes']), 3)
            self.assertFalse(report['acceptance_limits_changed'])

    def test_existing_run_or_unknown_profile_cannot_be_modified(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            (case / 'runs/steady_cold_flow').mkdir(parents=True)
            for profile in ('pressure_limited_0p5', 'typo'):
                with self.assertRaises(ValueError):
                    apply_profile(case, profile)

    def test_default_does_not_change_existing_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            apply_profile(case, None)
            self.assertEqual(list(case.iterdir()), [])
