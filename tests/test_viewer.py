from __future__ import annotations

import base64
import gzip
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin import viewer as viewer_module
from cfd_twin.config import load_config
from cfd_twin.viewer import (
    JAVASCRIPT,
    _convergence_payload,
    _energy_balance_payload,
    _mesh_runs_payload,
    _portable_dataset_javascript,
    _regional_initial_conditions_match,
    write_viewer_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


class ViewerTests(unittest.TestCase):
    def test_coolant_only_sections_allow_no_solid_temperature_samples(self) -> None:
        source = inspect.getsource(viewer_module._interactive_actual_cfd_payload)
        self.assertIn("if valid_solid_temperature", source)
        self.assertIn("else None", source)

    def test_temperature_display_ranges_follow_water_and_oil_legend_contract(self) -> None:
        self.assertIn("const selectedRange=activeMeshRun()?.temperature_legend_range_c;", JAVASCRIPT)
        self.assertIn("return dielectric?[19.500,23.000]:[19.908,21.044]", JAVASCRIPT)
        self.assertIn("[21.044,20.930,20.817,20.703,20.590,20.476,20.362,20.249,20.135,20.022,19.908]", JAVASCRIPT)
        self.assertIn("const temperaturePaletteStops=[", JAVASCRIPT)
        self.assertIn("[.52,[192,243,84]]", JAVASCRIPT)
        self.assertIn('const paletteStops=unit==="C"?temperaturePaletteStops', JAVASCRIPT)
        self.assertIn('function scalarPalette(value,field){return field==="temperature"?temperaturePalette(value):jet(value);}', JAVASCRIPT)
        self.assertIn('if(isPort&&visualMode==="cell-temperature")continue;', JAVASCRIPT)
        self.assertIn('visibility.ports&&!locationGuideMode&&visualMode!=="xy-velocity"&&visualMode!=="cell-temperature"', JAVASCRIPT)
        self.assertNotIn('if(isPort&&thermalDisplay){ctx.fillStyle=`rgba(182,198,207,', JAVASCRIPT)
        self.assertIn('fluentSurfaceShadow(item.face,basis)+(thermalCells&&!isPort?.19:0)', JAVASCRIPT)
        self.assertIn('function cellSurfacePalette(value)', JAVASCRIPT)
        self.assertIn('return temperaturePalette(value);', JAVASCRIPT)
        self.assertIn('const [r,g,b]=cellSurfacePalette(normalised)', JAVASCRIPT)
        self.assertIn('if(visualMode==="cell-temperature")drawTemperaturePortOutlines(basis,scale,cx,cy)', JAVASCRIPT)
        self.assertIn('function drawTemperaturePortOutlines(basis,scale,cx,cy)', JAVASCRIPT)
        self.assertIn('const geometryFrameStroke="rgba(164,195,216,.67)",geometryFrameLineWidth=1.25', JAVASCRIPT)
        self.assertIn('ctx.save();ctx.strokeStyle=geometryFrameStroke;ctx.lineWidth=geometryFrameLineWidth;', JAVASCRIPT)
        self.assertIn('ctx.lineWidth=geometryFrameLineWidth; ctx.strokeStyle=geometryFrameStroke;', JAVASCRIPT)
        self.assertNotIn("const portT=portTemperature(item.face.id),range=sharedTemperatureRange()", JAVASCRIPT)
        self.assertIn("const t=clamp(value)", JAVASCRIPT)
        self.assertIn("return sharedTemperatureRange()", JAVASCRIPT)

    def test_archived_result_with_different_regional_initials_is_excluded(self) -> None:
        config = load_config(
            BASELINE,
            overlay_paths=(ROOT / "configs" / "ppt_bernardi_udf.yaml",),
        )
        self.assertTrue(
            _regional_initial_conditions_match(
                config,
                {
                    "initial_temperature_k": 293.15,
                    "initial_fluid_temperature_k": 293.0,
                    "initial_solid_temperature_k": 293.15,
                },
            )
        )
        self.assertFalse(
            _regional_initial_conditions_match(
                config,
                {"initial_temperature_k": 293.15},
            )
        )

    def test_precision_mesh_card_uses_executed_800k_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            report = (
                case
                / "production/L3/reports/production_mesh/mesh_target_composition.json"
            )
            report.parent.mkdir(parents=True)
            report.write_text(
                json.dumps(
                    {
                        "status": "MESH_TARGET_VALIDATION_PASSED",
                        "actual": {
                            "total_cells": 849_284,
                            "fluid_cells": 557_402,
                            "solid_cells": 291_882,
                            "points": 945_483,
                            "hexahedron_fraction": 0.902227,
                            "cell_types": {"hexahedra": 766_247},
                        },
                    }
                ),
                encoding="utf-8",
            )

            precision = _mesh_runs_payload(
                case, precision_data_ready=True
            )[0]

            self.assertEqual(precision["label"], "80만급 생산 결과 · 849,284셀")
            self.assertEqual(precision["counts"]["points"], 945_483)
            self.assertIn("90.223% hexahedra", precision["element_family"])

    def test_comparison_manifest_overrides_run_labels_and_port_diameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            (case / "comparison_manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runs": [
                            {
                                "id": "precision",
                                "label": "Ø19 비교 계산 · 603,634셀",
                                "label_en": "Ø19 Comparison · 603,634 cells",
                                "inlet_diameter_mm": 19,
                                "outlet_diameter_mm": 19,
                            },
                            {
                                "id": "single",
                                "label": "Ø23 비교 계산 · 611,869셀",
                                "label_en": "Ø23 Comparison · 611,869 cells",
                                "inlet_diameter_mm": 23,
                                "outlet_diameter_mm": 23,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            runs = _mesh_runs_payload(case, precision_data_ready=True)
            self.assertEqual(runs[0]["label_en"], "Ø19 Comparison · 603,634 cells")
            self.assertEqual(runs[0]["inlet_diameter_mm"], 19)
            self.assertEqual(runs[1]["label"], "Ø23 비교 계산 · 611,869셀")
            self.assertEqual(runs[1]["outlet_diameter_mm"], 23)

    def test_energy_balance_payload_preserves_persisted_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            results = case / "results"
            results.mkdir()
            (results / "energy_balance.csv").write_text(
                "time_s,generated_W,removed_W,stored_W,imbalance_fraction\n"
                "15,7.2,3.4,3.7,0.0138888889\n"
                "30,7.2,5.5,1.69,0.0013888889\n",
                encoding="utf-8",
            )

            payload = _energy_balance_payload(case)

            self.assertTrue(payload["available"])
            self.assertEqual(payload["times_s"], [15.0, 30.0])
            self.assertEqual(payload["generated_w"], [7.2, 7.2])
            self.assertAlmostEqual(payload["imbalance_percent"][-1], 0.13888889)
            self.assertEqual(payload["final"]["time_s"], 30.0)

    def test_convergence_payload_includes_saved_solver_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            transient_dir = (
                case
                / "production/single/runs/frozen_flow_actual_1s/reports/solver"
            )
            transient_dir.mkdir(parents=True)
            (transient_dir / "transient_residuals.csv").write_text(
                "global_iteration,time_s,time_step_index,outer_corrector,region,field,initial_residual,final_residual,linear_iterations,residual_definition\n"
                "1,1,1,1,batterySolid,h,0.1,0.01,2,OpenFOAM raw\n",
                encoding="utf-8",
            )
            solver_log = "\n".join(
                (
                    "Time = 1",
                    "Solving for fluid region coolant",
                    "smoothSolver: Solving for Ux, Initial residual = 0.2, Final residual = 0.02, No Iterations 3",
                    "Solving for solid region batterySolid",
                    "smoothSolver: Solving for h, Initial residual = 0.3, Final residual = 0.03, No Iterations 4",
                    "Time = 2",
                    "Solving for fluid region coolant",
                    "smoothSolver: Solving for Ux, Initial residual = 0.02, Final residual = 0.002, No Iterations 2",
                    "Solving for solid region batterySolid",
                    "smoothSolver: Solving for h, Initial residual = 0.03, Final residual = 0.003, No Iterations 3",
                    "End",
                )
            )
            for run_name in ("steady_cold_flow", "steady_cht"):
                log_dir = case / f"production/single/runs/{run_name}/logs/solver"
                log_dir.mkdir(parents=True)
                (log_dir / f"{run_name}.log").write_text(
                    solver_log, encoding="utf-8"
                )

            payload = _convergence_payload(case)

            histories = {history["id"]: history for history in payload["histories"]}
            self.assertEqual(payload["default_history_id"], "transient_energy")
            self.assertEqual(
                set(histories), {"cold_flow", "steady_cht", "transient_energy"}
            )
            self.assertEqual(histories["cold_flow"]["last_iteration"], 2)
            self.assertEqual(len(histories["steady_cht"]["series"]), 2)

    def test_transient_convergence_prefers_cumulative_pimple_axis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            report_dir = (
                case
                / "production/single/runs/full_transient_actual_1s/reports/solver"
            )
            report_dir.mkdir(parents=True)
            (report_dir / "transient_residuals.csv").write_text(
                "global_iteration,time_s,time_step_index,outer_corrector,region,field,initial_residual,final_residual,linear_iterations,residual_definition\n"
                "1,0.1,1,1,coolant,Ux,0.1,0.01,3,OpenFOAM raw\n"
                "2,0.1,1,2,coolant,Ux,0.02,0.001,2,OpenFOAM raw\n"
                "3,0.1,1,3,coolant,Ux,0.004,0.0001,1,OpenFOAM raw\n",
                encoding="utf-8",
            )
            (report_dir / "transient_residuals_summary.json").write_text(
                json.dumps(
                    {
                        "axis": "cumulative_pimple_outer_iteration",
                        "time_step_count": 1,
                        "first_time_s": 0.1,
                        "last_time_s": 0.1,
                    }
                ),
                encoding="utf-8",
            )

            payload = _convergence_payload(case)

            self.assertTrue(payload["available"])
            self.assertEqual(
                payload["axis_mode"], "cumulative_pimple_outer_iteration"
            )
            self.assertEqual(payload["scope"], "새 표준 결과 · 전체 과도 CHT 계산")
            self.assertEqual(payload["last_iteration"], 3)
            self.assertEqual(payload["linear_iteration_total"], 6)
            self.assertEqual(payload["series"][0]["points"][-1][0], 3)

    def test_convergence_payload_discovers_800k_l3_steady_histories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            transient_dir = (
                case
                / "production/L3/runs/frozen_flow_actual_1s/reports/solver"
            )
            transient_dir.mkdir(parents=True)
            (transient_dir / "transient_residuals.csv").write_text(
                "global_iteration,time_s,time_step_index,outer_corrector,region,field,initial_residual,final_residual,linear_iterations,residual_definition\n"
                "1,1,1,1,batterySolid,h,0.1,0.01,2,OpenFOAM raw\n",
                encoding="utf-8",
            )
            solver_log = "\n".join(
                (
                    "Time = 1",
                    "Solving for fluid region coolant",
                    "smoothSolver: Solving for Ux, Initial residual = 0.2, Final residual = 0.02, No Iterations 3",
                    "smoothSolver: Solving for k, Initial residual = 0.1, Final residual = 0.01, No Iterations 2",
                    "Solving for solid region batterySolid",
                    "smoothSolver: Solving for h, Initial residual = 0.3, Final residual = 0.03, No Iterations 4",
                    "End",
                )
            )
            for run_name in ("steady_cold_flow", "steady_cht"):
                log_dir = case / f"production/L3/runs/{run_name}/logs/solver"
                log_dir.mkdir(parents=True)
                (log_dir / f"{run_name}.log").write_text(
                    solver_log, encoding="utf-8"
                )

            payload = _convergence_payload(case)

            histories = {history["id"]: history for history in payload["histories"]}
            self.assertEqual(
                set(histories), {"cold_flow", "steady_cht", "transient_energy"}
            )
            self.assertEqual(histories["cold_flow"]["production_profile"], "L3")
            self.assertIn("80만급/정밀 결과", histories["cold_flow"]["scope"])
            self.assertEqual(
                {series["key"] for series in histories["cold_flow"]["series"]},
                {"coolant.Ux", "coolant.k"},
            )
            self.assertEqual(
                {series["key"] for series in histories["steady_cht"]["series"]},
                {"coolant.Ux", "coolant.k", "batterySolid.h"},
            )

    def test_convergence_payload_renders_preserved_failed_cold_flow_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            run = case / "runs/steady_cold_flow"
            log_dir = run / "logs/solver"
            report_dir = run / "reports/solver"
            log_dir.mkdir(parents=True)
            report_dir.mkdir(parents=True)
            (log_dir / "steady_cold_flow.log").write_text(
                "\n".join(
                    (
                        "Time = 1",
                        "Solving for fluid region coolant",
                        "GAMG: Solving for p_rgh, Initial residual = 0.01, Final residual = 0.001, No Iterations 3",
                        "Time = 2",
                        "Solving for fluid region coolant",
                        "GAMG: Solving for p_rgh, Initial residual = 0.0009, Final residual = 0.0002, No Iterations 2",
                        "End",
                    )
                ),
                encoding="utf-8",
            )
            (report_dir / "cold_flow_validation.json").write_text(
                json.dumps(
                    {
                        "status": "COLD_FLOW_VALIDATION_FAILED",
                        "residual_floors": {"coolant.p_rgh": 1e-4},
                        "residual_checks": {"coolant.p_rgh": False},
                    }
                ),
                encoding="utf-8",
            )

            payload = _convergence_payload(case)

            self.assertTrue(payload["available"])
            self.assertEqual(payload["id"], "cold_flow")
            self.assertEqual(payload["last_iteration"], 2)
            self.assertEqual(payload["validation_status"], "COLD_FLOW_VALIDATION_FAILED")
            self.assertFalse(payload["configured_residual_checks_passed"])
            self.assertEqual(payload["series"][0]["configured_gate"], 1e-4)

    def test_dependency_free_viewer_is_truthfully_labelled(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            model = write_viewer_bundle(config, directory)
            output = Path(directory)
            for name in (
                "index.html",
                "results.html",
                "style.css",
                "app.js",
                "model.js",
                "viewer_manifest.json",
                "README_VIEWER.txt",
            ):
                self.assertTrue((output / name).is_file())
            landing = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn('href="results.html"', landing)
            self.assertIn('id="go-start"', landing)
            self.assertIn('id="go-graphics"', landing)
            self.assertNotIn('id="menu-view"', landing)
            self.assertIn('id="cad-files"', landing)
            self.assertIn('href="cad/index.html"', landing)
            self.assertTrue((output / "cad/index.html").is_file())
            self.assertTrue((output / "cad/dimensioned_top.svg").is_file())
            self.assertTrue((output / "cad/dimensioned_side.svg").is_file())
            self.assertTrue((output / "cad/step_top.png").is_file())
            self.assertTrue((output / "cad/step_side.png").is_file())
            self.assertIn("new URL(location.href)", (output / "offline-app.js").read_text(encoding="utf-8"))
            html = (output / "results.html").read_text(encoding="utf-8")
            script = (output / "app.js").read_text(encoding="utf-8")
            self.assertRegex(html, r'href="style\.[0-9a-f]{12}\.css"')
            self.assertRegex(html, r'src="model\.[0-9a-f]{12}\.js"')
            self.assertRegex(html, r'src="app\.[0-9a-f]{12}\.js"')
            self.assertNotIn("__STYLE_ASSET__", html)
            self.assertNotIn("__MODEL_ASSET__", html)
            self.assertNotIn("__APP_ASSET__", html)
            self.assertIn("CFD 미실행", html)
            self.assertIn('data-view="px"', html)
            self.assertIn('data-view="nz"', html)
            self.assertNotIn("포트는 A-04", html)
            self.assertIn("CFD 결과 뷰어", html)
            self.assertIn("DIGITAL TWIN · SIMULATION REPLAY", html)
            self.assertIn('id="cad-tab" href="cad/index.html"', html)
            self.assertIn("현재 결과", html)
            self.assertIn("주요 CFD 결과", html)
            self.assertIn("경계 조건", html)
            self.assertIn("계산 메시 선택", html)
            self.assertIn('id="mesh-run-select"', html)
            self.assertIn(
                "const selectableMeshRuns=meshRuns.filter(run=>run.mesh_ready||run.data_ready)",
                script,
            )
            self.assertIn("meshRunSelect.innerHTML=selectableMeshRuns.map", script)
            self.assertIn("const meshRunText=(run,key)=>", script)
            self.assertIn("model.convergence_by_mesh", script)
            self.assertIn("model.energy_balance_by_mesh", script)
            self.assertIn('context.fillStyle="#ffffff";context.fillRect(0,0,width,height)', script)
            self.assertIn('"#c62828","#1565c0","#2e7d32","#7b1fa2","#00838f"', script)
            self.assertIn("CFD 수렴 모니터", html)
            self.assertIn('id="convergence-chart-open"', html)
            self.assertIn('id="failed-convergence-card"', html)
            self.assertIn('id="failed-convergence-chart-open"', html)
            self.assertIn("model.failed_convergence_histories", script)
            self.assertIn('context.fillText(`${tr("합격 기준")}', script)
            self.assertIn('<option value="energy-balance">6. 에너지 수지</option>', html)
            self.assertIn('<option value="key-cells">7. 주요 배터리 5셀 온도</option>', html)
            self.assertIn("해석 세부정보", html)
            self.assertIn('id="kpi-outlet-temperature"', html)
            self.assertIn('id="kpi-cell-maximum"', html)
            self.assertIn('id="kpi-cell-hotspot"', html)
            self.assertIn('id="kpi-cell-hotspot-location"', html)
            self.assertNotIn("Figure 1 온도 처리", script)
            self.assertIn("논문 Figure", html)
            self.assertNotIn("필요 데이터:", script)
            self.assertIn("function figureSummary", script)
            self.assertIn("OpenFOAM 외부 반복 횟수", script)
            for mode_label in (
                "종합 화면",
                "셀 온도",
                "유체 유선",
                "가로 단면 위치",
                "가로 단면 온도",
                "가로 단면 유속",
                "가로 단면 압력",
                "배터리 표면 열전달계수",
                "세로 단면 위치",
                "세로 단면 온도",
                "세로 단면 유속",
                "세로 단면 압력",
            ):
                self.assertIn(mode_label, html)

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
            self.assertEqual(html.count('name="viz-mode"'), 15)
            self.assertIn('value="xy-location"', html)
            self.assertIn('value="xz-location"', html)
            self.assertIn('value="xy-temperature"', html)
            self.assertIn('value="xz-velocity"', html)
            self.assertIn('value="xy-mass-flow"', html)
            self.assertIn('value="xz-mass-flow"', html)
            self.assertNotIn('value="xy-mass-flux"', html)
            self.assertNotIn('value="xz-mass-flux"', html)
            self.assertIn('value="xy-pressure"', html)
            self.assertIn('value="xz-pressure"', html)
            self.assertIn('value="surface-heat-transfer"', html)
            self.assertNotIn('value="xy-heat-transfer"', html)
            self.assertNotIn('value="xz-heat-transfer"', html)
            self.assertIn('id="show-cells"', html)
            self.assertIn('id="show-flow"', html)
            self.assertIn('id="show-grid"', html)
            self.assertIn('id="smooth-section"', html)
            self.assertIn('id="section-3d"', html)
            self.assertIn("가로·세로 단면을 회전·확대 가능한 3D 평면", html)
            self.assertIn('id="head-rate"', html)
            self.assertIn('<option value="0.5" selected>0.5× 보기</option>', html)
            self.assertIn('id="cell-toast"', html)
            self.assertIn('id="scene-overlay"', html)
            self.assertIn("contain:strict", (output / "style.css").read_text(encoding="utf-8"))
            self.assertIn('ctx.clearRect(0,0,canvas.width,canvas.height)', script)
            self.assertIn('new ResizeObserver', script)
            self.assertIn('id="horizontal-sections"', html)
            self.assertEqual(html.count('name="horizontal-section"'), 7)
            self.assertIn('value="xy04"', html)
            self.assertIn('value="xy96"', html)
            self.assertIn("하부 간극 3.5 mm", html)
            self.assertIn("상부 간극 78.5 mm", html)
            self.assertIn('id="horizontal-centre"', html)
            self.assertEqual(model["mesh_runs"][0]["counts"]["total_cells"], 1_316_854)
            self.assertEqual(model["mesh_runs"][1]["counts"]["total_cells"], 207_390)
            self.assertFalse(model["mesh_runs"][1]["data_ready"])
            self.assertFalse(model["convergence"]["available"])
            self.assertFalse(model["energy_balance"]["available"])
            self.assertIn('id="vertical-sections"', html)
            self.assertEqual(html.count('name="vertical-section"'), 5)
            self.assertEqual(
                html.count('type="radio" name="vertical-section"'), 5
            )
            self.assertIn('id="vertical-section-plane"', html)
            self.assertIn('YZ · X 고정 (5/4열)', html)
            self.assertNotIn('id="vertical-all"', html)
            self.assertIn('id="vertical-centre"', html)
            self.assertIn('class="card mesh-run-card"', html)
            self.assertIn('class="card convergence-card"', html)
            self.assertIn('class="card temperature-export-card"', html)
            self.assertNotIn('class="card mesh-run-card" hidden', html)
            self.assertNotIn('class="card convergence-card" hidden', html)
            self.assertNotIn('class="card temperature-export-card" hidden', html)
            self.assertEqual(html.count('class="card detail-card"'), 4)
            self.assertNotIn('class="card detail-card" hidden', html)
            self.assertIn('id="mesh-run-note" hidden', html)
            self.assertNotIn(
                "과도 단계는 고정 유동이므로 에너지만 계산합니다.", html
            )
            self.assertNotIn("PNG/CSV 저장</p>", html)
            self.assertIn('id="flow-only"', html)
            self.assertIn('id="port-state"', html)
            self.assertIn('id="temperature-chart-open"', html)
            self.assertIn('id="mesh-figure-panel"', html)
            self.assertIn('id="mesh-figure-canvas"', html)
            self.assertIn('id="mesh-figure-image-grid"', html)
            self.assertIn('id="mesh-figure-png"', html)
            self.assertIn('id="temperature-chart-metric"', html)
            self.assertIn('value="battery-maximum"', html)
            self.assertIn('value="surface-average"', html)
            self.assertIn('value="internal-average"', html)
            self.assertIn('value="per-cell"', html)
            self.assertIn('value="cell-extrema"', html)
            self.assertIn('id="cell-extrema-cell"', html)
            self.assertIn('if(metric==="key-cells")', script)
            self.assertNotIn('function residualLineDash(key)', script)
            self.assertNotIn('context.setLineDash(residualLineDash', script)
            self.assertIn('id="temperature-chart-png"', html)
            self.assertIn('id="temperature-chart-csv"', html)
            self.assertIn('id="temperature-chart-all-csv"', html)
            self.assertIn("1×", html)
            self.assertIn("180×", html)
            self.assertEqual(model["enclosure_mm"]["notch_depth"], 11.25)
            self.assertEqual(len(model["cells"]), 23)
            self.assertEqual(
                [cell["id"] for cell in model["featured_temperature_cells"]],
                ["C01", "C05", "C19", "C23", "C12"],
            )
            self.assertEqual(len(model["outline_mm"]), 22)
            self.assertEqual([port["id"] for port in model["ports"]], ["IN", "OUT"])
            self.assertEqual([item["id"] for item in model["figures"]], [1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14])
            self.assertEqual(model["inputs"]["inlet_velocity_m_s"], 0.4)
            self.assertFalse(model["production_cad_ready"])
            self.assertIn("pointermove", script)
            self.assertIn("function advanceVideoPlayback", script)
            self.assertIn("simulationTime+elapsed*Number(flowRate.value)", script)
            self.assertIn("function updateFlowClock", script)
            self.assertIn("flowSeek.value=String(second)", script)
            self.assertIn("videoSimulationTime+elapsed*rate", script)
            self.assertIn("function stepVideo", script)
            self.assertIn("시뮬레이션 t =", script)
            self.assertIn("Fig 6 실제 냉각수 T(x,t) + 고정 U(x)", script)
            self.assertIn("고정 U(x)의 긴 유선을 따라 이동 헤드가 방향 표시 · 과도속도 아님", script)
            self.assertIn("drawFlow(flow.streamlines", script)
            self.assertIn("ctx.lineWidth=2", script)
            self.assertIn("const streamlineDisplayLimit=260", script)
            self.assertIn("displayedFlowPaths(paths)", script)
            self.assertIn('window.CFDTwinI18n?.getLanguage()||new URLSearchParams(window.location.search).get("lang")', script)
            self.assertIn("const section=horizontalSection()", script)
            self.assertIn("function horizontalSection", script)
            self.assertIn("function updateHorizontalSection", script)
            self.assertIn('sections[input.value]', script)
            self.assertIn('function verticalSelection', script)
            self.assertIn('function verticalSectionKey', script)
            self.assertIn('function verticalViewName', script)
            self.assertIn('verticalSectionPlane.addEventListener', script)
            self.assertIn('for(const section of verticalSelection())drawSection', script)
            self.assertIn('function surfaceThermalValue', script)
            self.assertIn('function surfaceCornerTemperatures', script)
            self.assertIn('function cellSectionTemperature', script)
            self.assertIn('function temperatureSectionRange', script)
            self.assertIn('function drawDirectXYCellCuts', script)
            self.assertIn('function drawCellSectionCuts', script)
            self.assertIn('BATTERY HOTTEST POINT', script)
            self.assertIn('cellSurfaceExtrema()?.maximum', script)
            self.assertIn('function drawHorizontalCellCuts', script)
            self.assertIn('function drawVerticalCellCuts', script)
            self.assertIn('function drawSectionLocationGuide', script)
            self.assertIn('function sectionLocationMetrics', script)
            self.assertIn('"축 전체 길이"', script)
            self.assertIn('"절단 축"', script)
            self.assertIn('"원점부터 거리"', script)
            self.assertIn('"위치 비율"', script)
            self.assertIn('function vertical2DView', script)
            self.assertIn('function exactSolidSectionActive', script)
            self.assertIn('solid_temperature_1800_c', script)
            self.assertIn(
                'const thermalCells=["combined","cell-temperature"].includes(visualMode)',
                script,
            )
            self.assertNotIn(
                'const thermalCells=["combined","cell-temperature","xy-temperature","xz-temperature"]',
                script,
            )
            self.assertIn('셀 전체표면 숨김 · 실제 교차 단면만 표시', script)
            self.assertIn('batterySolid 체적평균', script)
            self.assertIn('1,800 s batterySolid 저장 단면값', script)
            self.assertIn('function bilinear', script)
            self.assertIn('function bilinearPoint', script)
            self.assertIn('subdivisions=3', script)
            self.assertIn('verticalSubdivisions=selectedVertical.length>1?2:3', script)
            self.assertIn('cellSurface?.temperature_range_c', script)
            self.assertIn("function selectVisualMode", script)
            self.assertIn("function setLayerVisibility", script)
            self.assertIn("portTemperature", script)
            self.assertIn("function portMetrics", script)
            self.assertIn("function updateDashboardMetrics", script)
            self.assertIn("function drawPortCallout", script)
            self.assertIn("function fluentSurfaceShadow", script)
            self.assertIn("meshRuns.filter(run=>run.mesh_ready||run.data_ready)", script)
            self.assertIn("[activeMeshRunId]||{enabled:false}", script)
            self.assertIn("Shade the rendered solid like Fluent", script)
            self.assertIn("area_average_pressure_pa", script)
            self.assertIn("pressure_drop_pa", script)
            self.assertIn("temperature_display_offset_c", script)
            self.assertIn("T 표시보정", script)
            self.assertIn("function loadActualDataset", script)
            self.assertIn('location.protocol==="file:"', script)
            self.assertIn("function portableDatasetBuffer", script)
            self.assertIn("DecompressionStream", script)
            self.assertIn("data-load-status", html)
            self.assertIn("function actualDatasetValue", script)
            self.assertIn("function drawDirectSampleTiles", script)
            self.assertIn("function prepareFlowPathPhysics", script)
            self.assertIn("function physicalFlowPoint", script)
            self.assertIn("Math.expm1", script)
            self.assertNotIn("const phase=simulationTime/30", script)
            self.assertIn("simulationTime*headDisplayScale()", script)
            self.assertIn("function sectionMarkerPhase", script)
            self.assertNotIn("function hatchUnavailableCellCut", script)
            self.assertIn("고체 내부 공간장 미저장 · 직접 표면+체적평균 제약 투영(표시 보간, 내부 CFD 셀 아님)", script)
            self.assertIn("const horizontalCutAngularSubdivisions=4,horizontalCutRadialBands=8", script)
            self.assertIn("const verticalCutAxialSubdivisions=4,verticalCutTransverseBands=16", script)
            self.assertIn("function interpolatedCellSurfaceAtCut", script)
            self.assertIn("function projectedHorizontalCellTemperature", script)
            self.assertIn("function interpolatedCellSurfaceAtAxial", script)
            self.assertIn("function projectedVerticalCellTemperature", script)
            self.assertIn("직접 표면+체적평균 제약 투영(표시 보간, 내부 CFD 셀 아님)", script)
            self.assertIn("방향 × ${horizontalCutRadialBands}반경 표시분할(보간)", script)
            self.assertIn("not 1,024 independent CFD cells", script)
            self.assertIn("const positions=[left,right],angularIndices=positions.map", script)
            self.assertIn("높이 × ${verticalCutTransverseBands}가로 표시분할(보간)", script)
            self.assertNotIn("셀 면색=해당 시각 체적평균", script)
            self.assertIn("function velocityDisplayRange", script)
            self.assertIn("function velocityDisplayMaximum", script)
            self.assertNotIn("ACTUAL CFD · GLOBAL U SCALE", script)
            self.assertIn("전체 유동장 공통 색상범위", script)
            self.assertNotIn(
                'const limit=Math.max(1e-9,section.velocity_range_m_s[1])',
                script,
            )
            self.assertNotIn("simulationTime/210", script)
            self.assertNotIn("simulationTime/8", script)
            self.assertIn("DISPLAY SMOOTH", script)
            self.assertIn("NO INVALID PADDING", script)
            self.assertIn("직접 셀값 · 시간보간/패딩 없음", script)
            self.assertIn("function drawDynamicOverlay", script)
            self.assertIn("function paintMeshStructureFigure", script)
            self.assertIn("function paintTwoResultFigure", script)
            self.assertIn("function meshFinalMetrics", script)
            self.assertNotIn(
                "if([2,3].includes(item.id)&&meshRuns.length>=2)", script
            )
            self.assertIn(
                "const visibleFigures=model.figures.filter(item=>![2,3].includes(Number(item.id)))",
                script,
            )
            self.assertIn("function drawActualMeshWireframe", script)
            self.assertIn('id="show-mesh"', html)
            self.assertIn('id="show-temperature-legend"', html)
            self.assertIn('id="show-velocity-legend"', html)
            self.assertIn('id="show-mass-flow-legend"', html)
            self.assertIn('id="show-pressure-legend"', html)
            self.assertIn('function massFlowDisplayRange', script)
            self.assertIn('field==="mass-flow"', script)
            self.assertIn('Mass flow rate', script)
            self.assertIn('normalVolumeFlow', script)
            self.assertIn('id="show-heat-transfer-legend"', html)
            self.assertNotIn('id="show-legend"', html)
            self.assertIn("배터리 최고 온도 상승", script)
            self.assertIn("표준 − 정밀", script)
            self.assertIn("각 계산에 사용된 실제 polyMesh", script)
            self.assertIn("const groups=new Map()", script)
            self.assertNotIn("function eraseOverlayInsideCells", script)
            self.assertIn(
                'if(["combined","cell-temperature"].includes(visualMode))drawFlow',
                script,
            )
            self.assertIn(
                'if(visualMode==="velocity-streamlines")drawFlow', script
            )
            self.assertIn(
                'if(!["combined","cell-temperature","velocity-streamlines"].includes(visualMode))return',
                script,
            )
            self.assertIn(
                'visibility.velocityLegend&&visualMode==="cell-temperature"&&visibility.flow',
                script,
            )
            self.assertIn("function rayHitsCell", script)
            self.assertIn("function flowPointOccluded", script)
            self.assertIn("if(flowPointOccluded(midpoint,basis))continue", script)
            self.assertIn("if(flowPointOccluded(sample.point,basis))continue", script)
            self.assertNotIn('globalCompositeOperation="destination-out"', script)
            self.assertIn('ctx.strokeStyle=`rgba(${r},${g},${bColor},.86)`', script)
            self.assertIn('"velocity-legend","left"', script)
            self.assertIn('"temperature-legend","right"', script)
            self.assertIn("function sharedTemperatureRange", script)
            self.assertIn("return dielectric?[19.500,23.000]:[19.908,21.044]", script)
            self.assertIn('preferredIntervals=unit==="C"?10:legendId==="mass-flow-legend"?6:9', script)
            self.assertIn('unit==="C"?"°C":unit', script)
            self.assertIn('unit==="m/s"?"Velocity [m/s]":unit==="C"?"Temperature [°C]"', script)
            self.assertNotIn("RAW SAMPLES · DISPLAY SMOOTH", script)
            self.assertNotIn('ctx.fillText(`[${unit}]`', script)
            self.assertIn("const activeGeometry=(model.geometry_by_mesh||{})", script)
            self.assertIn('visibility.temperatureLegend', script)
            self.assertIn('visibility.velocityLegend', script)
            self.assertIn('smoothSectionInput.checked?thermalFaceGradient', script)
            self.assertNotIn('ctx.strokeStyle="rgba(4,12,31,.78)"', script)
            self.assertNotIn('ctx.strokeStyle="rgba(139,174,255,.94)"', script)
            self.assertIn("const sectionRasterCache=new WeakMap()", script)
            self.assertIn("const flipRow=section.row_step_mm[1]<0", script)
            self.assertIn("const flipColumn=section.column_step_mm[0]>0", script)
            self.assertIn("model.spatial_transform_by_mesh", script)
            self.assertIn("Math.sqrt(3200000/cssPixels)", script)
            self.assertIn("const overlayTargetFps=90", script)
            self.assertIn(
                "overlayRenderAccumulator>=1/overlayTargetFps-.001", script
            )
            self.assertIn("헤드 최대 90 FPS", script)
            self.assertIn("const timeVaryingScene=", script)
            self.assertNotIn("sceneBaseCache", script)
            self.assertIn("function paintTemperatureChart", script)
            self.assertIn("function activeTemperatureGraph", script)
            self.assertIn("function buildCellTemperatureExtrema", script)
            self.assertIn("function ensureCellTemperatureExtrema", script)
            self.assertIn('value="cell-extrema-map"', html)
            self.assertIn("function drawCellExtremaMap", script)
            self.assertIn("function cellTemperatureExtremaAtTime", script)
            self.assertIn('`▲${values.maximum.toFixed(2)}`', script)
            self.assertIn('`▼${values.minimum.toFixed(2)}`', script)
            self.assertIn('ctx.strokeStyle="rgba(255,255,255,.96)";ctx.strokeText', script)
            self.assertIn('"#8b1028",valueSize', script)
            self.assertIn('"#003f73",valueSize', script)
            self.assertIn("rotatedWidth=box.y,rotatedHeight=box.x", script)
            self.assertIn("x:left+(box.y-y)*scale,y:layoutTop+(box.x-x)*scale", script)
            self.assertIn('`${Number(box.y).toFixed(0)} mm`', script)
            self.assertIn('"cell-temperature","cell-extrema-map"', script)
            self.assertIn('metric==="cell-extrema"', script)
            self.assertIn('`${cellId} 최고온도`', script)
            self.assertIn('`${cellId} 최저온도`', script)
            self.assertIn("function downloadCsv", script)
            self.assertIn("battery_mesh_maximum_c", script)
            self.assertIn("battery_surface_area_average_c", script)
            self.assertIn("battery_internal_volume_average_c", script)
            self.assertIn("function directXYLayout", script)
            self.assertIn("plotLeft+(plotWidth-width)/2", script)
            self.assertIn("function drawDirectXYVelocityVectors", script)
            self.assertIn('visualMode.startsWith("xy-")&&!locationGuideMode&&!section3DInput.checked&&horizontalSection()', script)
            self.assertIn('visualMode.startsWith("xz-")&&!locationGuideMode&&!section3DInput.checked&&verticalSection()', script)
            self.assertIn('section3DOption.hidden=mode.endsWith("-location")||!(mode.startsWith("xy-")||mode.startsWith("xz-"))', script)
            self.assertIn("FROZEN-FLOW U(x) · 전체 유동장 공통 색상범위", script)
            self.assertIn("function drawXYPlaneTexture", script)
            self.assertIn("function drawDirectXYVelocity3D", script)
            self.assertIn("function surfaceCornerTemperatures", script)
            self.assertIn("function thermalFaceGradient", script)
            self.assertIn(
                'ctx.globalAlpha=1;ctx.globalCompositeOperation="source-over"',
                script,
            )
            self.assertIn(
                'const fallback=`rgb(${r},${g},${b})`',
                script,
            )
            self.assertIn(
                'gradient.addColorStop(position,`rgb(${colour[0]},${colour[1]},${colour[2]})`)',
                script,
            )
            self.assertIn('ctx.fillStyle="rgb(32,112,145)"', script)
            self.assertNotIn(
                'const fallback=`rgba(${r},${g},${b},.95)`',
                script,
            )
            self.assertIn("function cellSurfaceExtrema", script)
            self.assertIn("BATTERY HOTTEST POINT", script)
            self.assertIn("cellSurfaceExtrema()?.maximum", script)
            self.assertIn("data-cell-id", script)
            self.assertIn("showCellNotification", script)
            self.assertIn('6:"xy-temperature"', script)
            self.assertIn('11:"xy-velocity"', script)
            self.assertIn('preferredIntervals=unit==="C"?10:legendId==="mass-flow-legend"?6:9', script)
            self.assertIn('Math.floor(height/Math.max(18,tickFontPx*1.7))', script)
            self.assertNotIn("PAPER REF", script)
            self.assertIn("requestAnimationFrame(animate)", script)
            self.assertIn("function surfaceHeatTransferValue", script)
            self.assertIn("function heatTransferFaceGradient", script)
            self.assertNotIn("function effectiveHeatTransferCoefficient", script)
            self.assertIn("q''=OpenFOAM wallHeatFlux", script)
            self.assertIn('kpi-inlet-mass-flow', html)
            self.assertIn('kpi-outlet-mass-flow', html)
            self.assertEqual(
                model["surface_heat_transfer_coefficient"]["units"],
                "W/(m^2*K)",
            )
            self.assertEqual(
                model["surface_heat_transfer_coefficient"]["scope"],
                "coolant-facing surfaces of batteries C01-C23 only",
            )
            self.assertIn("let yaw = -0.96, pitch = 0.52", script)
            self.assertFalse(model["interactive_cfd"]["enabled"])

    def test_imported_cad_result_does_not_show_baseline_drawings(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_viewer_bundle(config, output, include_reference_cad=False)
            self.assertFalse((output / "cad/index.html").exists())
            html = (output / "results.html").read_text(encoding="utf-8")
            self.assertIn('id="cad-tab" href="cad/index.html" hidden', html)
            self.assertIn('"reference_cad_ready": false', (output / "offline-data.js").read_text(encoding="utf-8"))

    def test_ppt_bernardi_heat_range_is_exposed_to_the_ui(self) -> None:
        config = load_config(
            BASELINE,
            overlay_paths=(ROOT / "configs" / "ppt_bernardi_udf.yaml",),
        )
        with tempfile.TemporaryDirectory() as directory:
            model = write_viewer_bundle(config, directory)

        heat = model["heat_source"]
        self.assertEqual(heat["mode"], "peneklioglu_bernardi_2c_293p15")
        self.assertEqual(heat["label"], "PPT Bernardi · 2C · 293.15 K")
        self.assertTrue(heat["time_varying"])
        self.assertAlmostEqual(heat["cell_heat_start_w"], 1.5173195359225133)
        self.assertAlmostEqual(heat["cell_heat_end_w"], 2.4228902680554714)
        self.assertAlmostEqual(heat["total_heat_end_w"], 55.72647616527584)
        self.assertAlmostEqual(model["inputs"]["inlet_temperature_K"], 293.0)
        self.assertAlmostEqual(model["inputs"]["initial_fluid_temperature_K"], 293.0)
        self.assertAlmostEqual(model["inputs"]["initial_solid_temperature_K"], 293.15)

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

    def test_viewer_labels_archived_validated_result_without_publication_claim(self) -> None:
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
            self.assertEqual(model["status"], "PRECISION_RESULT_READY")
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

    def test_viewer_preserves_completed_result_but_surfaces_quality_warning(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "case" / "results"
            results.mkdir(parents=True)
            (results / "result_manifest.json").write_text(
                '{"data_status":"L0_CFD_RESULTS_READY","actual_solver_data":true,'
                '"quality_status":"COMPLETED_WITH_ENERGY_BALANCE_WARNING",'
                '"validation_status":"FROZEN_FLOW_VALIDATION_FAILED",'
                '"validation_warnings":["energy target missed"],'
                '"final_summary":{"battery_tmax_k":294.214098934,'
                '"complete_energy_balance_mean_fraction":0.038995509}}',
                encoding="utf-8",
            )
            model = write_viewer_bundle(
                config, root / "viewer", case_dir=root / "case"
            )
            self.assertTrue(model["actual_solver_data"])
            self.assertTrue(model["quality_warning"])
            self.assertEqual(
                model["quality_status"],
                "COMPLETED_WITH_ENERGY_BALANCE_WARNING",
            )
            self.assertEqual(model["validation_warnings"], ["energy target missed"])
            self.assertIn("품질 경고", model["calculation_summary"])
            self.assertIn("3.8996%", model["calculation_summary"])
            css = next((root / "viewer").glob("style.*.css")).read_text(
                encoding="utf-8"
            )
            app = next((root / "viewer").glob("app.*.js")).read_text(
                encoding="utf-8"
            )
            self.assertIn(".quality-warning .result-badge", css)
            self.assertIn('"계산 완료 · 경고"', app)


if __name__ == "__main__":
    unittest.main()
