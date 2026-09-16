from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from cfd_twin.case_builder import _docker_engine_version, dependency_report


class DependencyReportTests(unittest.TestCase):
    def test_docker_desktop_is_valid_without_host_openfoam(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="28.4.0\n", stderr="")
        with patch("cfd_twin.case_builder._docker_command", return_value="/path/to/docker"), \
             patch("cfd_twin.case_builder.subprocess.run", return_value=completed), \
             patch("cfd_twin.case_builder.shutil.which", return_value=None), \
             patch("cfd_twin.case_builder.platform.platform", return_value="Windows-test"), \
             patch("cfd_twin.case_builder.importlib.util.find_spec", return_value=object()):
            report = dependency_report()
        self.assertFalse(report["openfoam_ready"])
        self.assertTrue(report["docker_engine_ready"])
        self.assertTrue(report["workflow_host_ready"])
        self.assertEqual(report["docker_server_version"], "28.4.0")

    def test_unreachable_docker_is_not_reported_as_ready(self) -> None:
        with patch("cfd_twin.case_builder.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 5)):
            self.assertIsNone(_docker_engine_version("/path/to/docker"))
        self.assertIsNone(_docker_engine_version(None))


if __name__ == "__main__":
    unittest.main()
