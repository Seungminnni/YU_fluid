from __future__ import annotations

from pathlib import Path
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
try:
    import resume_frame_fit_batch as runner
finally:
    sys.path.pop(0)


class FrameFitResumeTests(unittest.TestCase):
    def test_outlet_trial_changes_one_setting_and_rejects_repeat(self):
        profile = 'production_frame_fit_6um_11layers_motion'
        prior = {'mesh_profile': profile, 'steady_numerics_profile': 'pressure_reltol_zero'}
        batch = {'solver_mpi_ranks': 12, 'jobs': [{'diameter_mm': 34, 'id': 'third', 'job_dir': '/third'}]}
        def read(path):
            return prior if path.name == 'job.json' else {'status': 'failed'}
        with patch.object(runner, 'read', side_effect=read), patch.object(runner, 'pressure_failure_at_cap', return_value=True), patch.object(runner, 'configure') as configure, patch.object(runner, 'execute', return_value={'status': 'complete'}) as execute, patch.object(runner, 'write'):
            runner.resume(batch, 34, [profile], outlet_velocity_profile='pressure_backflow')
            self.assertEqual(configure.call_args.kwargs, {'profile': profile, 'outlet_velocity_profile': 'pressure_backflow'})
            execute.assert_called_once()
            with self.assertRaises(ValueError):
                runner.resume(batch, 34, [profile], outlet_velocity_profile='pressure_backflow', steady_numerics_profile='pressure_limited_0p5')
            prior['outlet_velocity_profile'] = 'pressure_backflow'
            with self.assertRaises(ValueError):
                runner.resume(batch, 34, [profile], outlet_velocity_profile='pressure_backflow')
            self.assertEqual(configure.call_count, 1)

    def test_numerical_retry_freezes_new_job_before_deleting_failed_output(self):
        import retry_frame_fit_yplus as retry
        import recover_frame_fit_mesh as recover
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs, batch_dir = root / 'runs', root / 'batch'
            previous = runs / ('a' * 32)
            previous.mkdir(parents=True)
            batch_dir.mkdir()
            completed = runs / ('b' * 32)
            completed.mkdir()
            (completed / 'result').write_text('preserved')
            source = root / (hashlib.sha256(b'frozen CAD').hexdigest() + '.step')
            source.write_bytes(b'frozen CAD')
            profile = 'production_frame_fit_6um_11layers_motion'
            retry.write(previous / 'job.json', {'files': {'cad': str(source)},
                        'inputs': {'mesh_target_cells': 600000}, 'source': {}, 'mesh_profile': profile})
            retry.write(previous / 'status.json', {'status': 'failed', 'stage': 'steady'})
            log = previous / 'case/runs/steady_cold_flow/logs/solver/trace.log'
            log.parent.mkdir(parents=True)
            log.write_text('pressure plateau')
            (previous / 'worker.log').write_text('Docker startup failed')
            (previous / 'case/reports').mkdir(parents=True)
            retry.write(previous / 'case/reports/steady_numerics.json', {'profile': 'pressure_limited_0p5'})
            entry = {'diameter_mm': 34, 'id': previous.name, 'job_dir': str(previous)}
            batch = {'jobs': [entry]}
            delete = retry.delete_failed_job
            def checked_delete(old, new):
                manifest = retry.read(new / 'job.json')
                self.assertEqual(manifest['steady_numerics_profile'], 'pressure_limited_0p5')
                self.assertEqual(manifest['mesh_profile'], profile)
                self.assertFalse((new / 'case').exists())
                delete(old, new)
            with patch.object(retry, 'ROOT', root), patch.object(retry, 'RUNS', runs), patch.object(retry, 'BATCH', batch_dir), patch.object(recover, 'BATCH', batch_dir), patch.object(retry, 'delete_failed_job', side_effect=checked_delete):
                new = recover.configure(batch, entry, 6, 11, profile=profile,
                                        steady_numerics_profile='pressure_limited_0p5')
            self.assertFalse(previous.exists())
            self.assertTrue((completed / 'result').exists())
            self.assertTrue(source.exists())
            self.assertEqual(retry.read(new / 'status.json')['status'], 'queued')
            archive = batch_dir / 'diagnostic_attempts' / previous.name
            self.assertEqual((archive / log.relative_to(previous)).read_text(), 'pressure plateau')
            self.assertEqual((archive / 'worker_tail.log').read_text(), 'Docker startup failed')
            self.assertEqual(retry.read(archive / 'steady_numerics.json')['profile'], 'pressure_limited_0p5')

    def test_pressure_retry_requires_complete_safe_evidence_at_cap(self):
        checks = dict.fromkeys([
            'battery_yplus_gate_passed', 'final_fields_finite', 'final_iteration_present',
            'final_turbulence_fields_nonnegative', 'mass_imbalance_below_0p1_percent',
            'minimum_5000_outer_iterations_completed', 'monitor_window_has_at_least_21_samples',
            'pilot_derived_residual_floors_reached', 'pressure_drop_span_below_0p5_percent', 'solver_clean_end',
        ], True)
        checks['last_200_flow_residuals_below_1e-4'] = False
        fields = dict.fromkeys(['coolant.Ux', 'coolant.Uy', 'coolant.Uz', 'coolant.k', 'coolant.omega'], True)
        fields['coolant.p_rgh'] = False
        report = {'status': 'COLD_FLOW_VALIDATION_FAILED', 'final_iteration': 9500,
                  'checks': checks, 'acceptance_residual_audit': {
                      'checks': fields, 'target': 1e-4, 'window_outer_iterations': 200}}
        with tempfile.TemporaryDirectory() as temp:
            job = Path(temp)
            entry = {'job_dir': str(job)}
            def write(relative, value):
                path = job / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding='utf-8')
            write('status.json', {'status': 'failed', 'stage': 'steady'})
            write('job.json', {})
            write('case/reports/production_mesh/production_mesh_gate.json', {'status': 'PRODUCTION_MESH_GATE_PASSED'})
            path = 'case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json'
            write(path, report)
            self.assertTrue(runner.pressure_retryable(entry, 'pressure_limited_0p5'))
            unsafe = []
            altered = copy.deepcopy(report)
            altered['checks'].pop('solver_clean_end')
            unsafe.append(altered)
            altered = copy.deepcopy(report)
            altered['checks']['battery_yplus_gate_passed'] = False
            unsafe.append(altered)
            altered = copy.deepcopy(report)
            altered['acceptance_residual_audit']['checks']['coolant.Ux'] = False
            unsafe.append(altered)
            unsafe.append({**report, 'final_iteration': 6000})
            for value in unsafe:
                write(path, value)
                self.assertFalse(runner.pressure_retryable(entry, 'pressure_limited_0p5'))
            write(path, report)
            write('job.json', {'steady_numerics_profile': 'pressure_limited_0p5'})
            self.assertFalse(runner.pressure_retryable(entry, 'pressure_limited_0p5'))
            self.assertTrue(runner.pressure_failure_at_cap(entry))

    def test_only_known_under_relaxed_divergence_can_advance_to_0p5(self):
        profile = 'production_frame_fit_6um_11layers_motion'
        with tempfile.TemporaryDirectory() as temp:
            job = Path(temp)
            entry = {'job_dir': str(job)}
            def write(relative, value):
                path = job / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding='utf-8')
            write('job.json', {'mesh_profile': profile,
                               'steady_numerics_profile': 'pressure_relaxation_0p15'})
            write('status.json', {'status': 'failed', 'stage': 'steady'})
            write('case/reports/production_mesh/production_mesh_gate.json',
                  {'status': 'PRODUCTION_MESH_GATE_PASSED'})
            log = job / 'case/runs/steady_cold_flow/logs/solver/steady_cold_flow.log'
            log.parent.mkdir(parents=True)
            log.write_text('Negative initial temperature T0: -1\nFOAM parallel run aborting\n')
            self.assertTrue(runner.under_relaxed_pressure_diverged(entry))
            self.assertTrue(runner.pressure_retryable(entry, 'pressure_relaxation_0p5'))
            self.assertFalse(runner.pressure_retryable(entry, 'pressure_relaxation_0p15'))
            log.write_text('ordinary failure')
            self.assertFalse(runner.under_relaxed_pressure_diverged(entry))
            self.assertFalse(runner.pressure_retryable(entry, 'pressure_relaxation_0p5'))

    def test_pressure_retry_cannot_change_mesh_or_repeat_candidates(self):
        profile = 'production_frame_fit_6um_11layers_motion'
        batch = {'solver_mpi_ranks': 12, 'jobs': [{'diameter_mm': 34, 'id': 'third', 'job_dir': '/third'}]}
        def read(path):
            return {'mesh_profile': profile} if path.name == 'job.json' else {'status': 'failed'}
        with patch.object(runner, 'read', side_effect=read), patch.object(runner, 'pressure_retryable', return_value=True), patch.object(runner, 'configure') as configure, patch.object(runner, 'execute', return_value={'status': 'complete'}) as execute, patch.object(runner, 'write'):
            for candidates in ([profile, profile], ['production_frame_fit_6um_12layers_motion']):
                with self.assertRaises(ValueError):
                    runner.resume(batch, 34, candidates, steady_numerics_profile='pressure_limited_0p5')
            configure.assert_not_called()
            runner.resume(batch, 34, [profile], steady_numerics_profile='pressure_limited_0p5')
            self.assertEqual(configure.call_count, 1)
            self.assertEqual(configure.call_args.kwargs['steady_numerics_profile'], 'pressure_limited_0p5')
            execute.assert_called_once()

    def test_completed_case_is_never_reconfigured(self):
        batch = {'jobs': [{'diameter_mm': 28, 'id': 'old', 'job_dir': '/completed'}]}
        with patch.object(runner, 'read', return_value={'status': 'complete'}), patch.object(runner, 'configure') as configure:
            self.assertEqual(runner.resume(batch, 28, ['production_frame_fit_6um_10layers_motion'])['status'], 'complete')
            configure.assert_not_called()

    def test_safe_pressure_plateau_can_enter_only_reviewed_800k_trial(self):
        old = 'production_frame_fit_6um_11layers_motion'
        new = 'production_frame_fit_800k_6um_11layers_motion'
        batch = {'solver_mpi_ranks': 12, 'jobs': [
            {'diameter_mm': 34, 'id': 'third', 'job_dir': '/third'}]}
        prior = {'mesh_profile': old, 'inputs': {'mesh_target_cells': 600000}}
        def read(path):
            return prior if path.name == 'job.json' else {'status': 'failed'}
        with patch.object(runner, 'read', side_effect=read), \
                patch.object(runner, 'pressure_failure_at_cap', return_value=True), \
                patch.object(runner, 'retryable', return_value=False), \
                patch.object(runner, 'configure') as configure, \
                patch.object(runner, 'execute', return_value={'status': 'complete'}), \
                patch.object(runner, 'write'):
            self.assertTrue(runner.mesh_scale_retryable(batch['jobs'][0], [new]))
            self.assertFalse(runner.mesh_scale_retryable(batch['jobs'][0], ['production_800k']))
            runner.resume(batch, 34, [new])
            self.assertEqual(configure.call_args.kwargs['profile'], new)

    def test_retries_only_selected_failed_diameter_and_stops_after_success(self):
        batch = {'solver_mpi_ranks': 12, 'jobs': [
            {'diameter_mm': 28, 'id': 'first', 'job_dir': '/first'},
            {'diameter_mm': 31, 'id': 'second', 'job_dir': '/second'},
            {'diameter_mm': 34, 'id': 'third', 'job_dir': '/third'},
        ]}
        states = {'first': {'status': 'complete'}, 'second': {'status': 'complete'},
                  'third': {'status': 'failed', 'stage': 'steady'}}
        def read(path):
            return states[path.parent.name]
        def execute(batch, entry):
            states[entry['id']] = {'status': 'complete'}
            return states[entry['id']]
        with patch.object(runner, 'read', side_effect=read), patch.object(runner, 'retryable', return_value=True), patch.object(runner, 'configure') as configure, patch.object(runner, 'execute', side_effect=execute) as run, patch.object(runner, 'write') as write:
            result = runner.resume(batch, 34, ['production_frame_fit_6um_10layers_motion', 'production_frame_fit_4um_10layers_motion'])
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(configure.call_count, 1)
            self.assertEqual(configure.call_args.args[1]['diameter_mm'], 34)
            self.assertEqual(configure.call_args.kwargs['profile'], 'production_frame_fit_6um_10layers_motion')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(write.call_args.args[1]['status'], 'complete')

    def test_running_case_is_not_retried(self):
        batch = {'jobs': [{'diameter_mm': 34, 'id': 'live', 'job_dir': '/live'}]}
        with patch.object(runner, 'read', return_value={'status': 'running'}), patch.object(runner, 'retryable', return_value=False), patch.object(runner, 'configure') as configure:
            with self.assertRaises(ValueError):
                runner.resume(batch, 34, ['production_frame_fit_6um_10layers_motion'])
            configure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
