from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
try:
    import resume_frame_fit_batch as runner
finally:
    sys.path.pop(0)


class FrameFitResumeTests(unittest.TestCase):
    def test_completed_case_is_never_reconfigured(self):
        batch = {'jobs': [{'diameter_mm': 28, 'id': 'old', 'job_dir': '/completed'}]}
        with patch.object(runner, 'read', return_value={'status': 'complete'}), patch.object(runner, 'configure') as configure:
            self.assertEqual(runner.resume(batch, 28, ['production_frame_fit_6um_10layers_motion'])['status'], 'complete')
            configure.assert_not_called()

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
