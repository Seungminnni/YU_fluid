from __future__ import annotations

import base64
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from mdpi_cfd.config import load_config
from mdpi_cfd.viewer import _portable_dataset_javascript, write_viewer_bundle


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


class ViewerTests(unittest.TestCase):
    def test_dependency_free_viewer_is_truthfully_labelled(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            model = write_viewer_bundle(config, directory)
            output = Path(directory)
            for name in (
                "index.html",
                "style.css",
                "app.js",
                "model.js",
                "viewer_manifest.json",
                "README_VIEWER.txt",
            ):
                self.assertTrue((output / name).is_file())
            html = (output / "index.html").read_text(encoding="utf-8")
            script = (output / "app.js").read_text(encoding="utf-8")
            self.assertIn("CFD 미실행", html)
            self.assertIn('data-view="px"', html)
            self.assertIn('data-view="nz"', html)
            self.assertIn("포트는 A-04", html)
            self.assertIn("온도장이 아닙니다", html)
            self.assertIn("Figure 출력 계약", html)
            self.assertIn('id="playback-rate"', html)
            self.assertIn('id="video-seek"', html)
            self.assertIn('id="video-prev"', html)
            self.assertIn('id="video-next"', html)
            self.assertIn('id="video-play"', html)
            self.assertIn('id="flow-duration"', html)
            self.assertIn('id="video-duration"', html)
            self.assertIn('id="flow-play"', html)
            self.assertIn('id="flow-seek"', html)
            self.assertIn('id="flow-seek" type="range" min="0" max="1800" step="1"', html)
            self.assertIn('id="flow-sampling"', html)
            self.assertIn('id="flow-sampling"', html)
            self.assertEqual(html.count('name="viz-mode"'), 7)
            self.assertIn('value="xy-temperature"', html)
            self.assertIn('value="xz-velocity"', html)
            self.assertIn('id="show-cells"', html)
            self.assertIn('id="show-flow"', html)
            self.assertIn('id="show-grid"', html)
            self.assertIn('id="horizontal-sections"', html)
            self.assertEqual(html.count('name="horizontal-section"'), 5)
            self.assertIn('id="horizontal-centre"', html)
            self.assertIn('id="vertical-sections"', html)
            self.assertEqual(html.count('name="vertical-section"'), 5)
            self.assertIn('id="vertical-all"', html)
            self.assertIn('id="vertical-centre"', html)
            self.assertIn('id="flow-only"', html)
            self.assertIn('id="port-state"', html)
            self.assertIn('id="temperature-chart-open"', html)
            self.assertIn('id="temperature-chart-metric"', html)
            self.assertIn('value="battery-maximum"', html)
            self.assertIn('value="surface-average"', html)
            self.assertIn('value="internal-average"', html)
            self.assertIn('value="per-cell"', html)
            self.assertIn('id="temperature-chart-png"', html)
            self.assertIn('id="temperature-chart-csv"', html)
            self.assertIn('id="temperature-chart-all-csv"', html)
            self.assertIn("1×", html)
            self.assertIn("180×", html)
            self.assertEqual(model["enclosure_mm"]["notch_depth"], 11.25)
            self.assertEqual(len(model["cells"]), 23)
            self.assertEqual(len(model["outline_mm"]), 22)
            self.assertEqual([port["id"] for port in model["ports"]], ["IN", "OUT"])
            self.assertEqual([item["id"] for item in model["figures"]], [1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14])
            self.assertEqual(model["inputs"]["inlet_velocity_m_s"], 0.4)
            self.assertFalse(model["production_cad_ready"])
            self.assertIn("pointermove", script)
            self.assertIn("function advanceVideoPlayback", script)
            self.assertIn("simulationTime+elapsed*Number(flowRate.value)", script)
            self.assertIn("flowSeek.value=String(Math.round(simulationTime))", script)
            self.assertIn("videoSimulationTime+elapsed*rate", script)
            self.assertIn("function stepVideo", script)
            self.assertIn("시뮬레이션 t =", script)
            self.assertIn("Fig 6 실제 냉각수 T(x,t) + 고정 U(x)", script)
            self.assertIn("고정 U(x)의 긴 유선을 따라 이동 헤드가 방향 표시 · 과도속도 아님", script)
            self.assertIn("drawFlow(flow.streamlines", script)
            self.assertIn("drawSection(horizontalSection()", script)
            self.assertIn("function horizontalSection", script)
            self.assertIn("function updateHorizontalSection", script)
            self.assertIn('sections[input.value]', script)
            self.assertIn('function verticalSelection', script)
            self.assertIn('for(const section of verticalSelection())drawSection', script)
            self.assertIn('function surfaceThermalValue', script)
            self.assertIn('function surfaceBoundaryTemperatures', script)
            self.assertIn('function bilinear', script)
            self.assertIn('function bilinearPoint', script)
            self.assertIn('subdivisions=3', script)
            self.assertIn('verticalSubdivisions=selectedVertical.length>1?2:3', script)
            self.assertIn('cellSurface?.temperature_range_c', script)
            self.assertIn("function selectVisualMode", script)
            self.assertIn("function setLayerVisibility", script)
            self.assertIn("portTemperature", script)
            self.assertIn("function portMetrics", script)
            self.assertIn("function drawPortCallout", script)
            self.assertIn("area_average_pressure_pa", script)
            self.assertIn("pressure_drop_pa", script)
            self.assertIn("temperature_display_offset_c", script)
            self.assertIn("DISPLAY CAL.", script)
            self.assertIn("T 표시보정", script)
            self.assertIn("function loadActualDataset", script)
            self.assertIn('location.protocol==="file:"', script)
            self.assertIn("function portableDatasetBuffer", script)
            self.assertIn("DecompressionStream", script)
            self.assertIn("data-load-status", html)
            self.assertIn("function actualDatasetValue", script)
            self.assertIn("function drawActualSection", script)
            self.assertIn("직접 셀값 · 시간보간/패딩 없음", script)
            self.assertIn("function drawDynamicOverlay", script)
            self.assertIn("function paintTemperatureChart", script)
            self.assertIn("function activeTemperatureGraph", script)
            self.assertIn("function downloadCsv", script)
            self.assertIn("battery_mesh_maximum_c", script)
            self.assertIn("battery_surface_area_average_c", script)
            self.assertIn("battery_internal_volume_average_c", script)
            self.assertIn("function directXYLayout", script)
            self.assertIn("plotLeft+(plotWidth-width)/2", script)
            self.assertIn("function drawDirectXYVelocityVectors", script)
            self.assertIn('visualMode.startsWith("xy-")&&horizontalSection()', script)
            self.assertIn("FROZEN-FLOW U(x): 색상장은 고정", script)
            self.assertIn('6:"xy-temperature"', script)
            self.assertIn('11:"xy-velocity"', script)
            self.assertIn('const intervals=unit==="C"?18:9', script)
            self.assertIn("PAPER REF", script)
            self.assertIn("requestAnimationFrame(animate)", script)
            self.assertFalse(model["interactive_cfd"]["enabled"])

    def test_portable_dataset_script_round_trips_binary_values(self) -> None:
        raw = bytes(range(256)) * 7
        script = _portable_dataset_javascript("xz50", raw)
        assignment = script.split(" = ", 2)[-1].removesuffix(";\n")
        packed = json.loads(assignment)
        self.assertEqual(packed["encoding"], "gzip-base64")
        self.assertEqual(packed["byte_length"], len(raw))
        self.assertEqual(gzip.decompress(base64.b64decode(packed["payload"])), raw)

    def test_viewer_loads_only_persisted_real_png_artifacts(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = root / "case"
            figure_dir = case / "results" / "figures"
            preview_dir = case / "mesh" / "previews"
            figure_dir.mkdir(parents=True)
            preview_dir.mkdir(parents=True)
            (preview_dir / "mesh_xy.png").write_bytes(b"fixture")
            (figure_dir / "figure_02.json").write_text(
                '{"id":2,"title":"Mesh structure","required":["volume_mesh"],'
                '"status":"MESH_READY","synthetic_data":false,'
                '"artifacts":["mesh/previews/mesh_xy.png"]}'
            )
            model = write_viewer_bundle(config, root / "viewer", case_dir=case)
            figure2 = next(item for item in model["figures"] if item["id"] == 2)
            self.assertEqual(figure2["status"], "MESH_READY")
            self.assertRegex(
                figure2["viewer_artifacts"][0],
                r"^artifacts/fig02_mesh_xy\.png\?v=[0-9a-f]{12}$",
            )
            self.assertTrue((root / "viewer" / "artifacts" / "fig02_mesh_xy.png").is_file())

    def test_viewer_labels_validated_l0_without_publication_claim(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "case" / "results"
            results.mkdir(parents=True)
            (results / "result_manifest.json").write_text(
                '{"data_status":"L0_CFD_RESULTS_READY","actual_solver_data":true,'
                '"publication_results_authorized":false,'
                '"final_summary":{"battery_tmax_k":293.17382559}}',
                encoding="utf-8",
            )
            model = write_viewer_bundle(
                config, root / "viewer", case_dir=root / "case"
            )
            self.assertEqual(model["status"], "L0_CFD_RESULTS_READY")
            self.assertTrue(model["actual_solver_data"])
            self.assertIn("논문 최종 아님", model["status_label"])
            self.assertIn("293.173826 K", model["calculation_note"])

    def test_viewer_reports_completed_mesh_independence_without_claiming_transient_u(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "case" / "results"
            results.mkdir(parents=True)
            (results / "result_manifest.json").write_text(
                '{"data_status":"L0_CFD_RESULTS_READY","actual_solver_data":true,'
                '"final_summary":{"battery_tmax_k":293.17382559}}',
                encoding="utf-8",
            )
            (results / "mesh_independence.json").write_text(
                '{"status":"MESH_INDEPENDENCE_PASSED","comparisons":{"L2_to_L3":'
                '{"battery_tmax_difference_k":0.004321}}}',
                encoding="utf-8",
            )
            model = write_viewer_bundle(config, root / "viewer", case_dir=root / "case")
            self.assertIn("격자 독립성 PASS", model["status_label"])
            self.assertIn("0.004321 K", model["calculation_note"])
            self.assertIn("과도속도장이 아닙니다", model["calculation_note"])


if __name__ == "__main__":
    unittest.main()
