from __future__ import annotations

import json
import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from cfd_twin.cad_case_drawings import write_case_drawings
from cfd_twin.multi_case_viewer import (
    add_temperature_display_variant, append_validated_run, read_viewer_model,
    refresh_four_case_manifest, write_four_case_model,
)
from cfd_twin.viewer_release import package_demo_viewer, publish_experiment_and_demo, validate_server_viewer
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
    def test_first_result_creates_publication_then_second_result_is_added(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, server = root / "source", root / "server"
            _viewer(source, 28)
            # Match the real isolated source: top-level dataset fallback and
            # no bundled reference drawings for the new CAD geometry.
            model, _ = read_viewer_model(source)
            model["interactive_cfd"] = copy.deepcopy(model["interactive_cfd_by_mesh"]["precision"])
            write_four_case_model(source, model)
            shutil.rmtree(source / "cad")
            archive = root / "demo.zip"
            first = publish_experiment_and_demo(server, archive, source_viewer=source, run_id="new_28")
            self.assertEqual(first["runs"], ["new_28"])
            self.assertFalse((server / "data/precision").exists())
            self.assertIn('cad_new_28_top.svg', (server / "cad/index.html").read_text(encoding="utf-8"))
            second = publish_experiment_and_demo(server, archive, source_viewer=source, run_id="new_31")
            self.assertEqual(second["runs"], ["new_28", "new_31"])
            with ZipFile(archive) as zipped:
                self.assertEqual(zipped.read("viewer/data/new_28/a.u16"), b"binary")
                self.assertEqual(zipped.read("viewer/data/new_31/a.u16"), b"binary")

    def test_invalid_first_result_leaves_no_publication_and_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, server = root / "source", root / "server"
            _viewer(source, 28)
            portable = source / "data/precision/a.u16.js"
            portable.unlink()
            with self.assertRaises(FileNotFoundError):
                publish_experiment_and_demo(server, root / "demo.zip", source_viewer=source, run_id="new_28")
            self.assertEqual([p.name for p in server.iterdir()], [".release.lock"])
            portable.write_text("portable", encoding="utf-8")
            result = publish_experiment_and_demo(server, root / "demo.zip", source_viewer=source, run_id="new_28")
            self.assertEqual(result["runs"], ["new_28"])
    def test_comparison_variant_uses_source_mesh_and_cad_without_duplicate_cfd_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            viewer = Path(temporary) / "viewer"
            _viewer(viewer, 25)
            model, _ = read_viewer_model(viewer)
            add_temperature_display_variant(
                model, source_id="precision", variant_id="water_phi25_compare",
                label="Ø25 mm · 물 · 비교 범례", label_en="Ø25 mm · Water · comparison scale",
                range_c=(19.5, 23.0),
            )
            write_four_case_model(viewer, model)
            manifest = refresh_four_case_manifest(viewer)
            self.assertEqual(manifest["runs"][1]["asset_source_id"], "precision")
            self.assertEqual(manifest["runs"][1]["display_variant_of"], "precision")
            self.assertEqual(manifest["runs"][1]["temperature_legend_range_c"], [19.5, 23.0])
            self.assertEqual(validate_server_viewer(viewer)["runs"], ["precision", "water_phi25_compare"])
            self.assertFalse((viewer / "mesh-data-water_phi25_compare.js").exists())
            archive = Path(temporary) / "demo.zip"
            package_demo_viewer(viewer, archive)
            with ZipFile(archive) as compressed:
                self.assertIn("viewer/mesh-data-precision.js", compressed.namelist())
                self.assertNotIn("viewer/mesh-data-water_phi25_compare.js", compressed.namelist())

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
                self.assertIn("OPEN_3D_RESULTS.cmd", archive.namelist())
                self.assertIn("바로열기_3D결과.cmd", archive.namelist())
                self.assertIn("serve-viewer.ps1", archive.namelist())
                self.assertIn("실행방법.txt", archive.namelist())
                launcher = archive.read("OPEN_3D_RESULTS.cmd").decode("utf-8")
                self.assertIn("serve-viewer.ps1", launcher)
                self.assertNotIn("python", launcher.lower())
                server_script = archive.read("serve-viewer.ps1").decode("utf-8")
                self.assertIn("TcpListener", server_script)
                self.assertIn("_cfd_twin_health", server_script)

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
