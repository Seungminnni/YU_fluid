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

    def test_pressure_relaxation_trials_only_change_pressure_field_relaxation(self):
        with tempfile.TemporaryDirectory() as temp:
            for index, (profile, relaxation) in enumerate((
                ('pressure_relaxation_0p15', 0.15),
                ('pressure_relaxation_0p5', 0.5),
            )):
                with self.subTest(profile=profile):
                    case = Path(temp) / str(index)
                    original = _fluid_solution(conservative=True)
                    paths = [case / 'system/coolant/fvSolution'] + [
                        case / 'profiles' / stage / 'system/coolant/fvSolution'
                        for stage in ('steady_cold_flow', 'steady', 'frozen_flow_actual_1s')
                    ]
                    for path in paths:
                        path.parent.mkdir(parents=True)
                        path.write_text(original, encoding='utf-8')
                    apply_profile(case, profile)
                    old = '    fields\n    {\n        p_rgh 0.3;\n    }'
                    new = old.replace('p_rgh 0.3;', f'p_rgh {relaxation:g};')
                    for path in paths[:3]:
                        self.assertEqual(path.read_text(encoding='utf-8'), original.replace(old, new))
                        self.assertNotIn(b'\r', path.read_bytes())
                    self.assertEqual(paths[-1].read_text(encoding='utf-8'), original)
                    report = json.loads((case / 'reports/steady_numerics.json').read_text())
                    self.assertEqual(report['relaxation'], relaxation)
                    self.assertFalse(report['acceptance_limits_changed'])

    def test_strong_coupled_profile_changes_only_steady_numerics(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            solution = _fluid_solution(conservative=True)
            schemes = _fluid_schemes()
            solution_paths = [case / 'system/coolant/fvSolution'] + [
                case / 'profiles' / stage / 'system/coolant/fvSolution'
                for stage in ('steady_cold_flow', 'steady', 'frozen_flow_actual_1s')
            ]
            scheme_paths = [case / 'system/coolant/fvSchemes'] + [
                case / 'profiles' / stage / 'system/coolant/fvSchemes'
                for stage in ('steady_cold_flow', 'steady', 'frozen_flow_actual_1s')
            ]
            for path in solution_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(solution, encoding='utf-8')
            for path in scheme_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(schemes, encoding='utf-8')

            apply_profile(case, 'coupled_relaxation_0p1')

            for path in solution_paths[:3]:
                value = path.read_text(encoding='utf-8')
                self.assertIn('p_rgh 0.1;', value)
                self.assertIn('U 0.05;', value)
                self.assertIn('h 0.05;', value)
                self.assertIn('relTol 0;', value)
            for path in scheme_paths[:3]:
                self.assertIn('laplacian(rhorAUf,p_rgh) Gauss linear limited 0.5;',
                              path.read_text(encoding='utf-8'))
            self.assertEqual(solution_paths[-1].read_text(encoding='utf-8'), solution)
            self.assertEqual(scheme_paths[-1].read_text(encoding='utf-8'), schemes)
            report = json.loads((case / 'reports/steady_numerics.json').read_text())
            self.assertFalse(report['acceptance_limits_changed'])
            self.assertFalse(report['physical_inputs_changed'])

    def test_pressure_only_follow_up_halves_coupled_pressure_relaxation(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            solution = _fluid_solution(conservative=True)
            schemes = _fluid_schemes()
            solution_paths = [case / 'system/coolant/fvSolution'] + [
                case / 'profiles' / stage / 'system/coolant/fvSolution'
                for stage in ('steady_cold_flow', 'steady')
            ]
            scheme_paths = [case / 'system/coolant/fvSchemes'] + [
                case / 'profiles' / stage / 'system/coolant/fvSchemes'
                for stage in ('steady_cold_flow', 'steady')
            ]
            for path in solution_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(solution, encoding='utf-8')
            for path in scheme_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(schemes, encoding='utf-8')

            apply_profile(case, 'coupled_pressure_relaxation_0p05')

            for path in solution_paths:
                value = path.read_text(encoding='utf-8')
                self.assertIn('p_rgh 0.05;', value)
                self.assertIn('U 0.05;', value)
                self.assertIn('h 0.05;', value)
                self.assertIn('relTol 0;', value)
            report = json.loads((case / 'reports/steady_numerics.json').read_text())
            self.assertEqual(report['pressure_relaxation'], 0.05)
            self.assertFalse(report['acceptance_limits_changed'])
            self.assertFalse(report['physical_inputs_changed'])

    def test_equation_relaxation_follow_up_halves_coupled_equations(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            solution = _fluid_solution(conservative=True)
            schemes = _fluid_schemes()
            solution_paths = [case / 'system/coolant/fvSolution'] + [
                case / 'profiles' / stage / 'system/coolant/fvSolution'
                for stage in ('steady_cold_flow', 'steady')
            ]
            scheme_paths = [case / 'system/coolant/fvSchemes'] + [
                case / 'profiles' / stage / 'system/coolant/fvSchemes'
                for stage in ('steady_cold_flow', 'steady')
            ]
            for path in solution_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(solution, encoding='utf-8')
            for path in scheme_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(schemes, encoding='utf-8')

            apply_profile(case, 'coupled_equation_relaxation_0p025')

            for path in solution_paths:
                value = path.read_text(encoding='utf-8')
                self.assertIn('p_rgh 0.05;', value)
                self.assertIn('U 0.025;', value)
                self.assertIn('h 0.025;', value)
                self.assertIn('k 0.025;', value)
                self.assertIn('omega 0.025;', value)
                self.assertIn('relTol 0;', value)
            report = json.loads((case / 'reports/steady_numerics.json').read_text())
            self.assertEqual(report['pressure_relaxation'], 0.05)
            self.assertEqual(report['equation_relaxation']['U'], 0.025)
            self.assertFalse(report['acceptance_limits_changed'])
            self.assertFalse(report['physical_inputs_changed'])

    def test_limited_fast_profile_retains_pressure_relative_tolerance(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            solution = _fluid_solution(conservative=True)
            schemes = _fluid_schemes()
            solution_paths = [case / 'system/coolant/fvSolution'] + [
                case / 'profiles' / stage / 'system/coolant/fvSolution'
                for stage in ('steady_cold_flow', 'steady')
            ]
            scheme_paths = [case / 'system/coolant/fvSchemes'] + [
                case / 'profiles' / stage / 'system/coolant/fvSchemes'
                for stage in ('steady_cold_flow', 'steady')
            ]
            for path in solution_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(solution, encoding='utf-8')
            for path in scheme_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(schemes, encoding='utf-8')

            apply_profile(case, 'coupled_limited_reltol_0p05')

            for path in solution_paths:
                value = path.read_text(encoding='utf-8')
                self.assertIn('p_rgh 0.05;', value)
                self.assertIn('U 0.05;', value)
                self.assertIn('h 0.05;', value)
                self.assertIn('relTol 0.05;', value)
            for path in scheme_paths:
                self.assertIn(
                    'laplacian(rhorAUf,p_rgh) Gauss linear limited 0.5;',
                    path.read_text(encoding='utf-8'),
                )
            report = json.loads((case / 'reports/steady_numerics.json').read_text())
            self.assertEqual(report['pressure_relTol'], 0.05)

    def test_stable_relative_tolerance_uses_validated_relaxation(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            base_solution = _fluid_solution(conservative=True)
            base_schemes = _fluid_schemes()
            for relative in (
                'system/coolant/fvSolution',
                'profiles/steady_cold_flow/system/coolant/fvSolution',
                'profiles/steady/system/coolant/fvSolution',
            ):
                path = case / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(base_solution, encoding='utf-8')
            for relative in (
                'system/coolant/fvSchemes',
                'profiles/steady_cold_flow/system/coolant/fvSchemes',
                'profiles/steady/system/coolant/fvSchemes',
            ):
                path = case / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(base_schemes, encoding='utf-8')

            apply_profile(case, 'coupled_stable_reltol_0p005')

            solution = (case / 'system/coolant/fvSolution').read_text(encoding='utf-8')
            self.assertIn('relTol 0.005;', solution)
            self.assertIn('p_rgh 0.01;', solution)
            self.assertIn('U 0.005;', solution)
            report = json.loads((case / 'reports/steady_numerics.json').read_text(encoding='utf-8'))
            self.assertEqual(report['pressure_relTol'], 0.005)
            self.assertEqual(report['pressure_relaxation'], 0.01)
            self.assertEqual(report['equation_relaxation']['U'], 0.005)
            self.assertFalse(report['acceptance_limits_changed'])
            self.assertFalse(report['physical_inputs_changed'])

    def test_strong_equation_relaxation_follow_up_uses_measured_safe_values(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            solution = _fluid_solution(conservative=True)
            schemes = _fluid_schemes()
            solution_paths = [case / 'system/coolant/fvSolution'] + [
                case / 'profiles' / stage / 'system/coolant/fvSolution'
                for stage in ('steady_cold_flow', 'steady')
            ]
            scheme_paths = [case / 'system/coolant/fvSchemes'] + [
                case / 'profiles' / stage / 'system/coolant/fvSchemes'
                for stage in ('steady_cold_flow', 'steady')
            ]
            for path in solution_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(solution, encoding='utf-8')
            for path in scheme_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(schemes, encoding='utf-8')

            apply_profile(case, 'coupled_equation_relaxation_0p005')

            for path in solution_paths:
                value = path.read_text(encoding='utf-8')
                self.assertIn('p_rgh 0.01;', value)
                self.assertIn('U 0.005;', value)
                self.assertIn('h 0.005;', value)
                self.assertIn('k 0.005;', value)
                self.assertIn('omega 0.005;', value)
                self.assertIn('relTol 0;', value)
            report = json.loads((case / 'reports/steady_numerics.json').read_text())
            self.assertEqual(report['pressure_relaxation'], 0.01)
            self.assertEqual(report['equation_relaxation']['U'], 0.005)
            self.assertFalse(report['acceptance_limits_changed'])
            self.assertFalse(report['physical_inputs_changed'])

    def test_existing_run_or_unknown_profile_cannot_be_modified(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            (case / 'runs/steady_cold_flow').mkdir(parents=True)
            for profile in ('pressure_limited_0p5', 'pressure_reltol_zero',
                            'pressure_relaxation_0p15', 'pressure_relaxation_0p5',
                            'coupled_relaxation_0p1',
                            'coupled_pressure_relaxation_0p05',
                            'coupled_limited_reltol_0p05',
                            'coupled_stable_reltol_0p005',
                            'coupled_equation_relaxation_0p025',
                            'coupled_equation_relaxation_0p005', 'typo'):
                with self.assertRaises(ValueError):
                    apply_profile(case, profile)

    def test_default_does_not_change_existing_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp)
            apply_profile(case, None)
            self.assertEqual(list(case.iterdir()), [])
