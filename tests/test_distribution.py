"""Release archive must contain executable paths and traceable source evidence."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from cfd_twin.distribution import CAD_FILES, CASE_REPORTS, LOG_RELATIVE, PREFIX, build_distribution


class DistributionTest(unittest.TestCase):
    def test_integrated_archive_keeps_initial_ui_and_real_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case, cad, viewer = root / "case", root / "cad", root / "viewer"
            for name in CASE_REPORTS:
                file = case / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('{"status":"passed"}', encoding="utf-8")
            for name in CAD_FILES:
                file = cad / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(name, encoding="utf-8")
            log = case / LOG_RELATIVE
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("Time = 1800\nEnd\n", encoding="utf-8")
            (viewer / "index.html").parent.mkdir(parents=True, exist_ok=True)
            (viewer / "index.html").write_text("<head></head><button>결과 보기</button>", encoding="utf-8")
            (viewer / "mesh-volume-data.js").write_text("real edges", encoding="utf-8")
            (viewer / "viewer_manifest.json").write_text(json.dumps({
                "execution_variant": "adaptive_energy_coupling_trial",
                "actual_solver_data": True,
                "validation_status": "FROZEN_FLOW_VALIDATION_PASSED",
            }), encoding="utf-8")
            source = root / "src/cfd_twin"
            source.mkdir(parents=True)
            (source / "__init__.py").write_text("", encoding="utf-8")
            (root / "configs").mkdir()
            (root / "configs/baseline_23cell.yaml").write_text("case: baseline", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='test'", encoding="utf-8")
            (root / "distribution").mkdir()
            for name in ("run_mac.command", "run_windows.cmd", "README_시작하기.txt"):
                (root / "distribution" / name).write_text(name, encoding="utf-8")
            output = root / "release.zip"
            status = {"viewer_ready": True, "mesh_cells": 849284,
                      "stages": [{"status": "passed"}]}
            with patch("cfd_twin.distribution.workflow_status", return_value=status):
                report = build_distribution(output, root=root, case_dir=case, cad_dir=cad, viewer_dir=viewer)
            self.assertEqual(report["mesh_cells"], 849284)
            with ZipFile(output) as archive:
                self.assertIn('<base href="viewer/">', archive.read(f"{PREFIX}/index.html").decode())
                self.assertEqual(archive.read(f"{PREFIX}/data/case/{LOG_RELATIVE}"), b"Time = 1800\nEnd\n")
                self.assertEqual(archive.read(f"{PREFIX}/data/cad/coolant.step"), b"coolant.step")
                self.assertIn(f"{PREFIX}/src/cfd_twin/__init__.py", archive.namelist())
                self.assertIn(f"{PREFIX}/run_windows.cmd", archive.namelist())
            with self.assertRaises(FileExistsError):
                build_distribution(output, root=root, case_dir=case, cad_dir=cad, viewer_dir=viewer)


if __name__ == "__main__":
    unittest.main()
