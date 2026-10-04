from __future__ import annotations

import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cfd_twin.bootstrap import prepare_workspace, verified_reference_cad
from cfd_twin.workflow_app import CAD_ROLES, baseline_geometry, workflow_status

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "src/cfd_twin/assets/verified_cad_phi23"


class BootstrapTests(unittest.TestCase):
    def test_fresh_checkout_has_verified_cad_and_no_fabricated_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(REFERENCE, root / "src/cfd_twin/assets/verified_cad_phi23")
            workspace = prepare_workspace(root)
            self.assertEqual(workspace.pop("result_count"), 0)
            self.assertTrue(baseline_geometry(workspace["cad_dir"])["verified"])
            status = workflow_status(workspace["case_dir"], workspace["cad_dir"], workspace["viewer_dir"])
            self.assertFalse(status["viewer_ready"])
            self.assertTrue(workspace["imported_root"].is_dir())
            self.assertFalse(workspace["viewer_dir"].exists())

    def test_crlf_recovery_requires_exact_recorded_hash_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, runtime = Path(directory) / "source", Path(directory) / "runtime"
            shutil.copytree(REFERENCE, source)
            for name in CAD_ROLES:
                path = source / name
                path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
            self.assertFalse(baseline_geometry(source)["verified"])
            self.assertEqual(verified_reference_cad(source, runtime), runtime)
            self.assertTrue(baseline_geometry(runtime)["verified"])
            self.assertIn(b"\r\n", (source / "coolant.step").read_bytes())
            (source / "coolant.step").write_bytes((source / "coolant.step").read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "verified hash"):
                verified_reference_cad(source, runtime)

    def test_partial_publication_is_not_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(REFERENCE, root / "data/cad")
            (root / "viewer").mkdir()
            (root / "viewer/results.html").write_text("broken", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                prepare_workspace(root)

    def test_windows_entrypoint_imports_without_fcntl(self):
        code = "import sys; sys.modules['fcntl'] = None; import cfd_twin.bootstrap, cfd_twin.experiments; import run_wall_diagnostic_batch, run_frame_fit_batch, run_local_refinement_batch"
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "tools", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
