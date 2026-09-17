"""Local baseline viewer and explicitly confirmed imported-STEP CFD jobs.

Uploads are inert until the user reviews detected geometry and operating
conditions, then explicitly starts an isolated mesh/S0/S1/S2 worker.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import re
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import CfdTwinError
from .workflow_run import (
    create_imported_run,
    imported_run_status,
    prepare_imported_run,
    stage_uploaded_step,
)


ASSETS = Path(__file__).with_name("workflow_assets")
MAX_CAD_BYTES = 50 * 1024 * 1024
END_TIME_S = 1800.0
BASELINE_INPUTS = {
    "coolant": "deionized_water",
    "inlet_temperature_k": 293.0,
    "inlet_velocity_m_s": 0.4,
    "outlet_pressure_pa": 101325.0,
    "mesh_target_cells": 800000,
}
CAD_ROLES = {
    "enclosure_with_ports.step": "탱크·포트 외형",
    "coolant.step": "냉각수 유체 영역",
    "battery_cells.step": "23개 배터리 고체 영역",
}


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _log_tail(path: Path, size: int = 512 * 1024) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - size))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def inspect_cad_bytes(name: str, data: bytes, cad_dir: Path) -> dict[str, object]:
    """Recognize only *exact* validated baseline CAD; never infer patch roles.

    Arbitrary STEP/STL geometry can be uploaded for preliminary inspection but
    requires geometric/semantic validation before meshing.  A file name alone
    is never proof that the face marked `inlet` is the inlet.
    """

    filename = Path(name).name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".step", ".stp", ".stl"}:
        raise ValueError("STEP(.step/.stp) 또는 STL(.stl) 파일만 받습니다.")
    if not data or len(data) > MAX_CAD_BYTES:
        raise ValueError("CAD 파일은 1 byte 이상, 50 MiB 이하여야 합니다.")
    if suffix in {".step", ".stp"} and not data[:4096].lstrip().startswith(
        b"ISO-10303-21"
    ):
        raise ValueError("STEP 헤더(ISO-10303-21)를 확인할 수 없습니다.")
    if suffix == ".stl" and len(data) < 84 and not data.lstrip().startswith(b"solid"):
        raise ValueError("STL 형식으로 보이지 않습니다.")

    digest = hashlib.sha256(data).hexdigest()
    manifest = _read_json(cad_dir / "cad_result.json")
    hashes = manifest.get("file_sha256", {})
    if not isinstance(hashes, dict):
        hashes = {}
    matched = next(
        (role for role in CAD_ROLES if hashes.get(role) == digest), None
    )
    return {
        "filename": filename,
        "bytes": len(data),
        "sha256": digest,
        "format": "STEP" if suffix in {".step", ".stp"} else "STL",
        "recognized_baseline_role": matched,
        "role_label": CAD_ROLES.get(matched, "미확정 CAD"),
        "verification": "EXACT_BASELINE_MATCH" if matched else "GEOMETRY_REVIEW_REQUIRED",
        "usable_for_current_mesh": bool(matched),
        "message": (
            "검증된 기준 CAD와 내용이 일치합니다."
            if matched
            else "파일 형식만 확인했습니다. 새 계산 전 3영역 STEP의 닫힘·치수·배터리·입출구를 별도로 검사합니다."
        ),
    }


def baseline_geometry(cad_dir: Path) -> dict[str, object]:
    manifest = _read_json(cad_dir / "cad_manifest.json")
    result = _read_json(cad_dir / "cad_result.json")
    measured = result.get("measured", {})
    if not isinstance(measured, dict):
        measured = {}
    hashes = result.get("file_sha256", {})
    if not isinstance(hashes, dict):
        hashes = {}
    files_match_report = all(
        (cad_dir / name).is_file()
        and hashes.get(name) == hashlib.sha256((cad_dir / name).read_bytes()).hexdigest()
        for name in CAD_ROLES
    )
    verified = (
        manifest.get("status") == "CAD_EXPORTED_GEOMETRY_CHECKS_PASSED"
        and result.get("status") == "CAD_EXPORTED_GEOMETRY_CHECKS_PASSED"
        and files_match_report
    )
    return {
        "verified": verified,
        "cad_source": "검증된 23셀 기준 CAD",
        "tank_mm": [114, 121, 82],
        "cell_count": measured.get("battery_solid_count", 23),
        "arrangement": "5–4–5–4–5",
        "inlet_diameter_mm": manifest.get("inlet_diameter_mm", manifest.get("port_diameter_mm")),
        "outlet_diameter_mm": manifest.get("outlet_diameter_mm", manifest.get("port_diameter_mm")),
        "inlet_area_mm2": measured.get("inlet_area_mm2"),
        "outlet_area_mm2": measured.get("outlet_area_mm2"),
        "port_source": manifest.get("port_source"),
        "role_files": list(CAD_ROLES),
        "geometry_sha256": manifest.get("geometry_sha256"),
        "note": (
            "입·출구 구경과 중심을 첨부 STEP에서 검증했습니다."
            if result.get("imported_step") else
            "포트 중심은 A-04 기준 가정입니다. 업로드된 새 CAD의 포트 위치를 자동 확정하지 않습니다."
        ),
    }


def prepare_inputs(value: object, *, run_active: bool) -> dict[str, object]:
    """Validate a confirmation sheet, without claiming to launch a new run."""

    if not isinstance(value, dict):
        raise ValueError("입력은 JSON 객체여야 합니다.")
    coolant = value.get("coolant")
    if coolant != "deionized_water":
        raise ValueError("현재 물성 검증값은 탈이온수만 지원합니다.")
    numeric: dict[str, float] = {}
    ranges = {
        "inlet_temperature_k": (250.0, 370.0),
        "inlet_velocity_m_s": (0.001, 5.0),
        "outlet_pressure_pa": (50_000.0, 300_000.0),
        "mesh_target_cells": (700_000.0, 900_000.0),
    }
    for key, (lower, upper) in ranges.items():
        try:
            number = float(value[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{key}: 숫자가 필요합니다.") from exc
        if not math.isfinite(number) or not lower <= number <= upper:
            raise ValueError(f"{key}: 허용 범위 {lower:g}–{upper:g} 밖입니다.")
        numeric[key] = number
    if numeric["mesh_target_cells"] not in {700_000, 800_000}:
        raise ValueError("메시 등급은 검증된 70만급 또는 80만급 중 하나여야 합니다.")
    cad_mode = value.get("cad_mode")
    if cad_mode not in {"baseline", "exact_baseline_upload"}:
        raise ValueError("검증되지 않은 CAD는 아직 계산에 연결할 수 없습니다.")
    unchanged = all(
        math.isclose(numeric[key], float(BASELINE_INPUTS[key]), abs_tol=1e-9)
        for key in numeric
    )
    return {
        "status": "EXISTING_RUN_ATTACHABLE" if unchanged and run_active else "DRAFT_ONLY",
        "cad_mode": cad_mode,
        "inputs": {"coolant": coolant, **numeric},
        "matches_current_baseline": unchanged,
        "can_start_new_solver": False,
        "can_view_current_run": unchanged and run_active,
        "message": (
            "동일한 기준 조건의 기존 계산 또는 검증된 결과를 연결합니다. 새 계산은 시작하지 않습니다."
            if unchanged and run_active
            else "기준 조건과 달라 새 CFD 계산이 필요합니다. CAD·운전조건 확인과 저장공간 검사를 이어서 진행합니다."
        ),
    }


def workflow_status(case_dir: Path, cad_dir: Path, viewer_dir: Path) -> dict[str, object]:
    mesh = _read_json(case_dir / "reports/production_mesh/production_mesh_gate.json")
    composition = _read_json(
        case_dir / "reports/production_mesh/mesh_target_composition.json"
    )
    cold = _read_json(
        case_dir / "runs/steady_cold_flow/reports/solver/cold_flow_validation.json"
    )
    steady = _read_json(
        case_dir / "runs/steady_cht/reports/solver/steady_cht_validation.json"
    )
    transient_report = _read_json(
        case_dir / "runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json"
    )
    energy_report = _read_json(
        case_dir / "runs/frozen_flow_actual_1s/reports/solver/energy_balance.json"
    )
    coupling_audit = _read_json(
        case_dir / "runs/frozen_flow_actual_1s/reports/solver/energy_coupling_audit.json"
    )
    log = case_dir / "runs/frozen_flow_actual_1s/logs/solver/frozen_flow_actual_1s.log"
    tail = _log_tail(log)
    times = re.findall(r"(?m)^Time\s*=\s*([\d.]+)\s*$", tail)
    time_s = float(times[-1]) if times else None
    coupling = re.findall(r"(?m)^energyCoupling:\s*(\d+)/(\d+)", tail)
    coupling_current = int(coupling[-1][0]) if coupling else None
    coupling_limit = int(coupling[-1][1]) if coupling else None
    try:
        log_age_s = max(0.0, datetime.now(timezone.utc).timestamp() - log.stat().st_mtime)
    except OSError:
        log_age_s = None
    log_recent = log_age_s is not None and log_age_s < 120
    clean_end = bool(re.search(r"(?m)^End\s*$", tail))
    transient_passed = str(transient_report.get("status", "")).endswith("_PASSED")
    energy_passed = energy_report.get("status") == "TRANSIENT_ENERGY_BALANCE_PASSED"
    final_summary = transient_report.get("final_summary", {})
    if not isinstance(final_summary, dict):
        final_summary = {}
    final_window = transient_report.get("final_window_rows", [])
    final_row = final_window[-1] if isinstance(final_window, list) and final_window else {}
    if not isinstance(final_row, dict):
        final_row = {}
    manifest = _read_json(case_dir / "results/result_manifest.json")
    postprocess = _read_json(case_dir / "reports/workflow_postprocess.json")
    viewer_ready = (
        viewer_dir.joinpath("index.html").is_file()
        and viewer_dir.joinpath("results.html").is_file()
        and manifest.get("data_status")
        in {"L0_CFD_RESULTS_READY", "CFD_PILOT_RESULTS_READY"}
        and manifest.get("actual_solver_data") is True
        and str(manifest.get("validation_status", "")).endswith("_PASSED")
        and (
            manifest.get("data_status") != "CFD_PILOT_RESULTS_READY"
            or postprocess.get("stage") == "complete"
        )
    )
    actual = composition.get("actual", {})
    if not isinstance(actual, dict):
        actual = {}
    stages = [
        {"id": "cad", "label": "CAD 확인", "status": "passed" if baseline_geometry(cad_dir)["verified"] else "pending"},
        {"id": "mesh", "label": "메시 생성·품질", "status": "passed" if mesh.get("status") == "PRODUCTION_MESH_GATE_PASSED" else "pending"},
        {"id": "cold_flow", "label": "S0 냉간 유동", "status": "passed" if cold.get("status") == "COLD_FLOW_VALIDATION_PASSED" else "pending"},
        {"id": "steady_cht", "label": "S1 정상 CHT", "status": "passed" if steady.get("status") == "STEADY_CHT_VALIDATION_PASSED" else "pending"},
        {"id": "transient", "label": "S2 과도 열해석", "status": "passed" if transient_passed else "needs_check" if clean_end and time_s is not None else "running" if log_recent and time_s is not None else "needs_check" if time_s is not None else "pending"},
        {"id": "publish", "label": "결과 후처리·3D 뷰어", "status": "passed" if viewer_ready else "pending"},
    ]
    adaptive_trial = manifest.get("execution_variant") == "adaptive_energy_coupling_trial"
    return {
        "case_name": case_dir.name,
        "mesh_cells": actual.get("total_cells"),
        "stages": stages,
        "transient": {
            "time_s": time_s,
            "end_time_s": END_TIME_S,
            "physical_time_percent": min(100.0, 100.0 * time_s / END_TIME_S) if time_s is not None else 0.0,
            "energy_coupling_iteration": coupling_current,
            "energy_coupling_limit": coupling_limit,
            "energy_coupling_audit": {
                key: coupling_audit.get(key)
                for key in ("target", "cap", "time_step_count", "early_exit_count", "cap_reached_count", "coupling_not_entered_count")
            } if coupling_audit else None,
            "log_age_s": log_age_s,
            "solver_clean_end": clean_end,
            "validation_status": transient_report.get("status"),
            "note": "퍼센트는 완료 물리시간 비율이며 수렴률이나 남은 시간 예측이 아닙니다.",
        },
        "viewer_ready": viewer_ready,
        "postprocess": postprocess,
        "result_summary": {
            "solver_validation_passed": transient_passed,
            "energy_balance_passed": energy_passed,
            "battery_tmax_k": final_summary.get("battery_tmax_k"),
            "battery_tavg_k": final_summary.get("battery_tavg_k"),
            "outlet_temperature_k": final_row.get("outlet_mass_weighted_temperature_k"),
            "complete_energy_balance_final_fraction": (
                energy_report.get("final", {}).get("imbalance_fraction")
                if isinstance(energy_report.get("final"), dict)
                else None
            ),
            "publication_authorized": transient_report.get(
                "publication_results_authorized", False
            ),
        },
        "result_slots": [
            {"name": "적응형 84.9만셀 결과" if adaptive_trial else "현재 84.9만셀 결과", "ready": viewer_ready,
             "battery_tmax_k": final_summary.get("battery_tmax_k") if transient_passed else None,
             "outlet_temperature_k": final_row.get("outlet_mass_weighted_temperature_k") if transient_passed else None},
            {"name": "비교 결과 2", "ready": False},
            {"name": "비교 결과 3", "ready": False},
        ],
    }


def serve_workflow(
    *, case_dir: Path, cad_dir: Path, viewer_dir: Path, port: int = 8765
) -> None:
    """Serve baseline status and user-confirmed isolated CAD jobs on loopback."""

    case_dir, cad_dir, viewer_dir = (
        case_dir.resolve(), cad_dir.resolve(), viewer_dir.resolve()
    )
    imported_root = case_dir.parent / "imported_runs"
    monitor_stop = threading.Event()

    def monitor_completed_run() -> None:
        """Postprocess once, only after independently validated solver completion."""

        while not monitor_stop.is_set():
            current = workflow_status(case_dir, cad_dir, viewer_dir)
            if current["viewer_ready"]:
                return
            if (
                current["transient"]["solver_clean_end"]
                and current["transient"]["time_s"] == END_TIME_S
                and all(
                    stage["status"] == "passed"
                    for stage in current["stages"][:5]
                )
                and current["result_summary"]["energy_balance_passed"]
            ):
                from .workflow_publish import _write_json, publish_completed_run

                try:
                    publish_completed_run(case_dir, viewer_dir)
                except Exception as exc:
                    _write_json(
                        case_dir / "reports/workflow_postprocess.json",
                        {"stage": "failed", "message": str(exc)},
                    )
                return
            monitor_stop.wait(10)

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: HTTPStatus, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, root: Path, relative: str) -> None:
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (HTTP interface)
            path = unquote(urlsplit(self.path).path)
            if path == "/api/status":
                self._json(HTTPStatus.OK, workflow_status(case_dir, cad_dir, viewer_dir))
            elif path == "/api/geometry":
                self._json(HTTPStatus.OK, baseline_geometry(cad_dir))
            elif path.startswith("/api/runs/"):
                try:
                    self._json(HTTPStatus.OK, imported_run_status(imported_root, path[len("/api/runs/"):]))
                except ValueError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            elif path in {"/", "/index.html"}:
                self._file(ASSETS, "index.html")
            elif path in {"/app.js", "/style.css"}:
                self._file(ASSETS, path.lstrip("/"))
            elif path.startswith("/viewer/") and workflow_status(case_dir, cad_dir, viewer_dir)["viewer_ready"]:
                self._file(viewer_dir, path[len("/viewer/"):])
            elif path.startswith("/runs/"):
                parts = path.split("/", 4)
                if len(parts) == 5 and parts[3] == "viewer":
                    try:
                        status = imported_run_status(imported_root, parts[2])
                    except ValueError:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    if status.get("status") == "complete":
                        self._file(imported_root / "runs" / parts[2] / "viewer", parts[4])
                        return
                self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802 (HTTP interface)
            path = urlsplit(self.path).path
            try:
                origin = self.headers.get("Origin")
                host = self.headers.get("Host", "")
                allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
                if host not in allowed_hosts or (origin and origin not in {f"http://{name}" for name in allowed_hosts}):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "다른 출처의 로컬 계산 요청은 허용하지 않습니다."})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_CAD_BYTES + 4096:
                    raise ValueError("요청 본문이 비었거나 50 MiB를 초과했습니다.")
                data = self.rfile.read(length)
                if path == "/api/cad/inspect":
                    name = unquote(self.headers.get("X-CAD-Filename", ""))
                    result = inspect_cad_bytes(name, data, cad_dir)
                    if result["format"] == "STEP":
                        result["upload_token"] = stage_uploaded_step(imported_root, name, data)
                elif path == "/api/prepare":
                    payload = json.loads(data.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("입력은 JSON 객체여야 합니다.")
                    status = workflow_status(case_dir, cad_dir, viewer_dir)
                    active = status["viewer_ready"] or next(s for s in status["stages"] if s["id"] == "transient")["status"] == "running"
                    result = prepare_inputs(payload, run_active=active)
                elif path == "/api/import/prepare":
                    payload = json.loads(data.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("입력은 JSON 객체여야 합니다.")
                    result = prepare_imported_run(imported_root, payload.get("files"), payload.get("inputs"))
                elif path == "/api/baseline/prepare":
                    payload = json.loads(data.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("입력은 JSON 객체여야 합니다.")
                    if not baseline_geometry(cad_dir)["verified"]:
                        raise ValueError("기준 CAD의 원본 해시 검증이 필요합니다.")
                    selected = {
                        role: stage_uploaded_step(imported_root, role, (cad_dir / role).read_bytes())
                        for role in CAD_ROLES
                    }
                    result = prepare_imported_run(imported_root, selected, payload)
                    result["files"] = selected
                    result["built_in_baseline"] = True
                elif path == "/api/import/start":
                    payload = json.loads(data.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("입력은 JSON 객체여야 합니다.")
                    if payload.get("geometry_confirmed") is not True:
                        raise ValueError("검증된 형상·운전 조건을 확인해야 새 계산을 시작할 수 있습니다.")
                    current = workflow_status(case_dir, cad_dir, viewer_dir)
                    if any(stage["id"] == "transient" and stage["status"] == "running" for stage in current["stages"]):
                        raise ValueError("현재 기준 CFD 계산 중입니다. 자원 충돌을 막기 위해 완료 후 새 계산을 시작하십시오.")
                    result = create_imported_run(
                        imported_root, payload.get("files"), payload.get("inputs"),
                        payload.get("confirmation", ""),
                    )
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, CfdTwinError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, result)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"CFD workflow portal: http://127.0.0.1:{server.server_port}/", flush=True)
    monitor = threading.Thread(
        target=monitor_completed_run, name="cfd-postprocess", daemon=True
    )
    monitor.start()
    try:
        server.serve_forever()
    finally:
        monitor_stop.set()
        server.server_close()
