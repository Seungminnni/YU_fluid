from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from cfd_twin.cad_case_drawings import write_case_drawings
from cfd_twin.multi_case_viewer import append_validated_run, read_viewer_model, refresh_four_case_manifest
from cfd_twin.viewer_release import package_demo_viewer, validate_server_viewer
from cfd_twin.workflow_app import workflow_status


def _model(diameter: int) -> dict[str, object]:
    geometry = {
        "cells": [{"id": "C01", "x": 20, "y": 20, "z": 41, "radius": 9.5, "length": 68}],
        "ports": [
            {"id": "IN", "x": 20, "y": 105, "z": 98, "radius": diameter / 2},
            {"id": "OUT", "x": 20, "y": 15, "z": -16, "radius": diameter / 2},
        ],
        "outline_mm": [[0, 0], [114, 0], [114, 121], [0, 121]],
    }
    return {
        **geometry,
        "geometry_by_mesh": {"precision": geometry},
        "actual_solver_data": True,
        "validation_status": "FROZEN_FLOW_VALIDATION_PASSED",
        "mesh_runs": [{
            "id": "precision", "label": f"Ø{diameter} mm", "label_en": f"Ø{diameter} mm",
            "inlet_diameter_mm": diameter, "outlet_diameter_mm": diameter,
            "counts": {"total_cells": 600_000}, "mesh_ready": True, "data_ready": True,
        }],
        "interactive_cfd_by_mesh": {
            "precision": {"actual_one_second": {"datasets": {"cell_surface": {
                "file": "data/precision/a.u16", "portable_file": "data/precision/a.u16.js"
            }}}}
        },
        "convergence_by_mesh": {"precision": {"available": True}},
        "energy_balance_by_mesh": {"precision": {"available": True, "status": "passed"}},
        "mesh_wireframes": {"precision": {"point_count": 1}},
    }


def _viewer(path: Path, diameter: int) -> None:
    path.mkdir()
    model = _model(diameter)
    (path / "results.html").write_text('<script src="model.js"></script><script src="app.js"></script>', encoding="utf-8")
    (path / "model.js").write_text("window.CFD_TWIN_MODEL = " + json.dumps(model) + ";\n", encoding="utf-8")
    (path / "app.js").write_text("// viewer\n", encoding="utf-8")
    (path / "index.html").write_text('<script src="offline-app.js"></script>', encoding="utf-8")
    (path / "offline-app.js").write_text("// portal\n", encoding="utf-8")
    (path / "mesh.html").write_text('<script src="four_case_data.js"></script>', encoding="utf-8")
    (path / "mesh-data.js").write_text("mesh", encoding="utf-8")
    (path / "mesh-volume-data.js").write_text("volume", encoding="utf-8")
    (path / "mesh-data-precision.js").write_text("mesh", encoding="utf-8")
    (path / "mesh-volume-data-precision.js").write_text("volume", encoding="utf-8")
    data = path / "data" / "precision"
    data.mkdir(parents=True)
    (data / "a.u16").write_bytes(b"binary")
    (data / "a.u16.js").write_text("portable", encoding="utf-8")
    (path / "cad").mkdir()
    (path / "cad" / "index.html").write_text('<script src="../four_case_data.js"></script>', encoding="utf-8")
    write_case_drawings(model, path)
    refresh_four_case_manifest(path)


class ViewerReleaseTests(unittest.TestCase):
    def test_new_validated_run_appears_in_server_and_identical_demo_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, source = root / "server", root / "source"
            _viewer(server, 19)
            _viewer(source, 23)
            source_model = _model(23)
            source_model["mesh_runs"][0].pop("inlet_diameter_mm")
            source_model["mesh_runs"][0].pop("outlet_diameter_mm")
            source_model["ports"][1]["radius"] = 12.5
            source_dataset = source_model["interactive_cfd_by_mesh"]["precision"]["actual_one_second"]["datasets"]["cell_surface"]
            source_dataset["file"] += "?v=verified"
            source_dataset["portable_file"] += "?v=verified"
            (source / "model.js").write_text(
                "window.CFD_TWIN_MODEL = " + json.dumps(source_model) + ";\n",
                encoding="utf-8",
            )
            manifest = append_validated_run(server, source, run_id="experiment_01", label="Ø23 experiment")
            self.assertEqual([run["id"] for run in manifest["runs"]], ["precision", "experiment_01"])
            self.assertEqual([run["id"] for run in read_viewer_model(server)[0]["mesh_runs"]], ["precision", "experiment_01"])
            self.assertEqual(manifest["runs"][-1]["diameter_mm"], 23)
            self.assertEqual(manifest["runs"][-1]["outlet_diameter_mm"], 25)
            self.assertIn("OUT Ø25", (server / "cad/cad_experiment_01_top.svg").read_text(encoding="utf-8"))
            status = workflow_status(root / "archived_case", root / "cad", server)
            self.assertTrue(status["viewer_ready"])
            self.assertEqual(len(status["result_slots"]), 2)
            self.assertEqual(validate_server_viewer(server)["dataset_files"], 4)
            output = root / "demo.zip"
            result = package_demo_viewer(server, output)
            self.assertEqual(result["runs"], ["precision", "experiment_01"])
            with ZipFile(output) as archive:
                self.assertEqual(archive.read("viewer/data/experiment_01/a.u16"), b"binary")
                self.assertEqual(
                    archive.read("viewer/results.html"), (server / "results.html").read_bytes()
                )

    def test_rejects_incomplete_source_without_changing_existing_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, source = root / "server", root / "source"
            _viewer(server, 19)
            _viewer(source, 23)
            model = _model(23)
            model["validation_status"] = "INCOMPLETE"
            (source / "model.js").write_text("window.CFD_TWIN_MODEL = " + json.dumps(model) + ";\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                append_validated_run(server, source, run_id="experiment_01")
            self.assertEqual([run["id"] for run in read_viewer_model(server)[0]["mesh_runs"]], ["precision"])

    def test_rejects_missing_portable_data_before_copying(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, source = root / "server", root / "source"
            _viewer(server, 19)
            _viewer(source, 23)
            (source / "data/precision/a.u16.js").unlink()
            with self.assertRaises(FileNotFoundError):
                append_validated_run(server, source, run_id="experiment_01")
            self.assertFalse((server / "data/experiment_01").exists())
            self.assertEqual([run["id"] for run in read_viewer_model(server)[0]["mesh_runs"]], ["precision"])


if __name__ == "__main__":
    unittest.main()
