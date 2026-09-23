"""Cancellation is scoped to one imported job and preserves partial files."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfd_twin.workflow_run import (
    RunCancelled,
    _compose_service,
    imported_run_status,
    request_imported_run_cancel,
    run_imported_case,
)


class WorkflowCancelTests(unittest.TestCase):
    def make_job(self, root: Path, state: str = "queued") -> tuple[str, Path]:
        run_id = "a" * 32
        job = root / "runs" / run_id
        job.mkdir(parents=True)
        (job / "job.json").write_text("{}", encoding="utf-8")
        (job / "status.json").write_text(
            json.dumps({"status": state, "stage": "mesh", "message": "work"}),
            encoding="utf-8",
        )
        return run_id, job

    def test_request_is_idempotent_and_terminal_jobs_are_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id, job = self.make_job(root)
            first = request_imported_run_cancel(root, run_id)
            self.assertEqual(first["status"], "stopping")
            self.assertTrue((job / "cancel.request").is_file())
            self.assertEqual(request_imported_run_cancel(root, run_id)["status"], "stopping")
            (job / "cancel.request").unlink()
            (job / "status.json").write_text(json.dumps({"status": "failed"}), encoding="utf-8")
            self.assertEqual(request_imported_run_cancel(root, run_id)["status"], "failed")
            self.assertFalse((job / "cancel.request").exists())

    def test_queued_worker_stops_before_building_cad(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id, job = self.make_job(root)
            request_imported_run_cancel(root, run_id)
            with patch("cfd_twin.workflow_run.build_imported_case") as build:
                run_imported_case(job / "job.json")
            build.assert_not_called()
            self.assertEqual(imported_run_status(root, run_id)["status"], "cancelled")
            self.assertTrue((job / "job.json").is_file())

    def test_docker_stop_targets_only_this_job_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = root / "case"
            case.mkdir()
            cancel = root / ("b" * 32) / "cancel.request"
            cancel.parent.mkdir()

            class FakeProcess:
                terminated = False
                first_wait = True

                def wait(self, timeout=None):
                    if self.first_wait:
                        self.first_wait = False
                        cancel.write_text("stop", encoding="utf-8")
                        raise subprocess.TimeoutExpired("docker compose", timeout)
                    return 143

                def poll(self):
                    return 143 if self.terminated else None

                def terminate(self):
                    self.terminated = True

            process = FakeProcess()
            with patch("cfd_twin.workflow_run._docker_command", return_value="docker"), \
                 patch("cfd_twin.workflow_run.subprocess.Popen", return_value=process) as popen, \
                 patch("cfd_twin.workflow_run.subprocess.run") as command:
                with self.assertRaises(RunCancelled):
                    _compose_service(case, "production-mesh", io.BytesIO(), cancel_path=cancel)
            docker_command = popen.call_args.args[0]
            self.assertIn("--name", docker_command)
            name = docker_command[docker_command.index("--name") + 1]
            self.assertTrue(name.startswith("cfd-twin-bbbbbbbbbbbb-"))
            self.assertEqual(command.call_args_list[0].args[0][:4], ["docker", "stop", "--time", "10"])
            self.assertEqual(command.call_args_list[0].args[0][-1], name)
            self.assertEqual(command.call_args_list[1].args[0], ["docker", "rm", "-f", name])
            self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
