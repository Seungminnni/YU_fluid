from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("launch_workflow", Path(__file__).resolve().parents[1] / "tools/launch_workflow.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_accepts_312_and_newer_without_pinning_an_interpreter(self):
        for version in ((3, 12), (3, 13), (3, 14), (3, 15)):
            with self.subTest(version=version), patch.object(launcher.sys, "version_info", version), patch.object(launcher.Path, "exists", return_value=False), patch.object(launcher.subprocess, "run") as run, patch.object(launcher.subprocess, "call", return_value=0) as call:
                self.assertEqual(launcher.main(), 0)
                self.assertEqual(run.call_args_list[0].args[0][:3], [launcher.sys.executable, "-m", "venv"])
                self.assertIn("cfd_twin.bootstrap", call.call_args.args[0])

    def test_rejects_old_python_before_creating_environment(self):
        with patch.object(launcher.sys, "version_info", (3, 11)), patch.object(launcher.subprocess, "run") as run:
            with self.assertRaisesRegex(SystemExit, "3.12 or higher"):
                launcher.main()
            run.assert_not_called()
