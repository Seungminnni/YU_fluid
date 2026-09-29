import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfd_twin.workflow_run import _run_wall_diagnostic
from cfd_twin.errors import CfdTwinError


class WallDiagnosticWorkflowTests(unittest.TestCase):
    def test_short_yplus_failure_is_diagnostic_and_full_iteration_limit_remains(self):
        with tempfile.TemporaryDirectory() as temp:
            case=Path(temp)
            profile=case/'profiles/steady_cold_flow/system/controlDict'
            profile.parent.mkdir(parents=True)
            original='endTime 6000;\n'
            profile.write_text(original)
            report=case/'runs/steady_cold_flow/reports/solver/cold_flow_validation.json'
            report.parent.mkdir(parents=True)
            (case/'diagnostics/wall/1500').mkdir(parents=True)
            calls=[]
            def service(case, name, output, **kwargs):
                calls.append(name)
                if name=='cold-flow-pilot':
                    self.assertIn('endTime 1500;', profile.read_text())
                    data={'final_iteration':1500,'checks':{'solver_clean_end':True,'final_fields_finite':True,'final_turbulence_fields_nonnegative':True,'battery_yplus_gate_passed':False}}
                    report.with_name('yplus.json').write_text('{}')
                else:
                    self.assertEqual(kwargs['environment']['COLD_FLOW_ADDITIONAL_ITERATIONS'],'4500')
                    self.assertEqual(profile.read_text(),original)
                    data={'final_iteration':6000,'status':'COLD_FLOW_VALIDATION_FAILED'}
                report.write_text(json.dumps(data))
                return 2
            with patch('cfd_twin.workflow_run._compose_service',side_effect=service), patch('cfd_twin.wall_diagnostics.compare') as compare:
                _run_wall_diagnostic(case,io.BytesIO(),1500,cancel_path=case/'cancel')
                compare.assert_called_once_with(case,'1500',diagnostic_only=True)
            self.assertEqual(calls,['cold-flow-pilot','cold-flow-continue'])
            self.assertEqual(json.loads(report.read_text())['status'],'COLD_FLOW_VALIDATION_FAILED')
            self.assertEqual(profile.read_text(),original)

    def test_invalid_or_missing_checkpoint_never_continues(self):
        with tempfile.TemporaryDirectory() as temp:
            case=Path(temp)
            profile=case/'profiles/steady_cold_flow/system/controlDict'
            profile.parent.mkdir(parents=True)
            profile.write_text('endTime 6000;\n')
            with patch('cfd_twin.workflow_run._compose_service',return_value=2) as service:
                with self.assertRaises(CfdTwinError):
                    _run_wall_diagnostic(case,io.BytesIO(),1500,cancel_path=case/'cancel')
                self.assertEqual(service.call_count,1)
            self.assertEqual(profile.read_text(),'endTime 6000;\n')
