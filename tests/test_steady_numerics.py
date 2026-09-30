import json
from pathlib import Path
import tempfile
import unittest

from cfd_twin.openfoam_case import _fluid_schemes, _fluid_solution, _solid_schemes
from cfd_twin.steady_numerics import apply_profile


class SteadyNumericsTests(unittest.TestCase):
    def test_absolute_pressure_trial_only_changes_normal_pressure_relative_tolerance(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            original = _fluid_solution(conservative=True)
            paths = [case / 'system/coolant/fvSolution'] + [
                case / 'profiles' / stage / 'system/coolant/fvSolution'
                for stage in ('steady_cold_flow', 'steady', 'frozen_flow_actual_1s')
            ]
            for path in paths:
                path.parent.mkdir(parents=True)
                path.write_text(original, encoding='utf-8')
                path.with_name('fvSchemes').write_text(_fluid_schemes(), encoding='utf-8')
            apply_profile(case, 'pressure_reltol_zero')
            old = '    p_rgh\n    {\n        solver GAMG;\n        tolerance 1e-8;\n        relTol 0.05;'
            new = old.replace('relTol 0.05;', 'relTol 0;')
            for path in paths[:3]:
                value = path.read_text(encoding='utf-8')
                self.assertEqual(value, original.replace(old, new))
                self.assertNotIn(b'\r', path.read_bytes())
                self.assertEqual(path.with_name('fvSchemes').read_text(encoding='utf-8'), _fluid_schemes())
            self.assertEqual(paths[-1].read_text(encoding='utf-8'), original)
            report = json.loads((case / 'reports/steady_numerics.json').read_text())
            self.assertEqual(report['relTol'], 0)
            self.assertFalse(report['acceptance_limits_changed'])

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
            for profile in ('pressure_limited_0p5', 'pressure_reltol_zero', 'typo'):
                with self.assertRaises(ValueError):
                    apply_profile(case, profile)

    def test_default_does_not_change_existing_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            apply_profile(case, None)
            self.assertEqual(list(case.iterdir()), [])
