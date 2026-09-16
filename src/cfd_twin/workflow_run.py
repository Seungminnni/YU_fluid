"""Isolated, explicit CFD jobs for validated imported STEP assemblies.

Uploading never starts Docker. A separate confirmation request freezes source
hashes and operating inputs, then a detached worker executes the existing
mesh/S0/S1/S2 gate sequence in a unique case directory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .case_builder import _docker_command
from .cad_import import STEP_ROLES, import_step_bundle, imported_config_overrides, inspect_imported_steps
from .config import load_config
from .errors import CfdTwinError
from .production_mesh import write_production_mesh_source_bundle
from .steady_convergence import continuation_decision
from .workflow_publish import publish_completed_run


SOURCE_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = SOURCE_ROOT / "configs/baseline_23cell.yaml"
THERMAL_OVERLAY = SOURCE_ROOT / "configs/ppt_bernardi_udf.yaml"
TOKEN_RE = re.compile(r"^[0-9a-f]{64}\.(?:step|stp)$")
# The completed 849k baseline currently occupies about 41 GiB. Leave enough
# headroom for a comparable isolated case and a safety margin before launch.
MIN_FREE_GIB = 55


def _lock_solver_file(stream: object) -> None:
    """Non-blocking single-job lock on both POSIX and Windows hosts."""

    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        if not stream.read(1):
            stream.seek(0)
            stream.write(b"1")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _worker_process_options(platform_name: str | None = None) -> dict[str, object]:
    """Detach the CFD worker with flags supported by the current host OS."""

    if (platform_name or os.name) == "nt":
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        }
    return {"start_new_session": True}


def _compose_service(case: Path, service: str, output: object, *, environment: dict[str, str] | None = None) -> int:
    docker = _docker_command()
    if docker is None:
        raise CfdTwinError("Docker Desktop CLI가 없어 실제 CFD 단계를 실행할 수 없습니다.")
    return subprocess.run(
        [docker, "compose", "-f", str(case / "compose.yaml"), "run", "--rm", "--build", service],
        cwd=case, env=environment, stdout=output, stderr=subprocess.STDOUT, check=False,
    ).returncode


def _run_steady_stages(case: Path, output: object) -> None:
    """Run S0/S1 with the same measured, bounded gate on Mac and Windows."""

    for stage, run_name, pilot, continuation, report_name, env_name in (
        ("cold", "steady_cold_flow", "cold-flow-pilot", "cold-flow-continue", "cold_flow_validation.json", "COLD_FLOW_ADDITIONAL_ITERATIONS"),
        ("cht", "steady_cht", "steady-pilot", "steady-continue", "steady_cht_validation.json", "STEADY_ADDITIONAL_ITERATIONS"),
    ):
        run_dir = case / "runs" / run_name
        report_path = run_dir / "reports/solver" / report_name
        decision_path = run_dir / "reports/solver/convergence_decision.json"
        if not run_dir.exists():
            status = _compose_service(case, pilot, output)
            if status and not report_path.is_file():
                raise CfdTwinError(f"{run_name}: solver stopped without validation report")
        elif not report_path.is_file():
            raise CfdTwinError(f"{run_name}: existing run has no validation report")
        while True:
            decision = continuation_decision(_read_json(report_path), stage=stage)
            _write_json(decision_path, decision)
            if decision["action"] == "pass":
                break
            if decision["action"] != "continue":
                raise CfdTwinError(f"{run_name}: {decision.get('reason', 'non-retryable gate failure')}")
            environment = os.environ.copy()
            environment[env_name] = str(decision["additional_iterations"])
            status = _compose_service(case, continuation, output, environment=environment)
            current = _read_json(report_path).get("final_iteration")
            if status and not report_path.is_file():
                raise CfdTwinError(f"{run_name}: continuation failed with {status}")
            if not isinstance(current, (int, float)) or current <= decision["iteration"]:
                raise CfdTwinError(f"{run_name}: validation did not advance past {decision['iteration']}")


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def stage_uploaded_step(root: Path, filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".step", ".stp"}:
        raise ValueError("새 CFD 실행에는 3개 영역의 STEP 파일이 필요합니다. STL은 참고용입니다.")
    if not 0 < len(data) <= 50 * 1024 * 1024 or not data[:4096].lstrip().startswith(b"ISO-10303-21"):
        raise ValueError("유효한 50 MiB 이하 STEP 파일이 필요합니다.")
    digest = hashlib.sha256(data).hexdigest()
    token = digest + suffix
    destination = root / "staging" / token
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        try:
            with destination.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            pass
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise ValueError("업로드 저장 파일의 해시가 일치하지 않습니다.")
    return token


def _source_files(root: Path, selected: object) -> dict[str, Path]:
    if not isinstance(selected, dict) or set(selected) != set(STEP_ROLES):
        raise ValueError("탱크·냉각수·배터리 STEP 역할 3개를 각각 지정해야 합니다.")
    if len(set(selected.values())) != 3:
        raise ValueError("같은 STEP 파일을 두 역할에 사용할 수 없습니다.")
    files = {}
    for role in STEP_ROLES:
        token = selected[role]
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise ValueError("알 수 없는 STEP 업로드 식별자입니다.")
        source = (root / "staging" / token).resolve()
        if not source.is_relative_to((root / "staging").resolve()) or not source.is_file():
            raise ValueError("첨부된 STEP 파일을 찾을 수 없습니다.")
        if hashlib.sha256(source.read_bytes()).hexdigest() != token.split(".")[0]:
            raise ValueError("첨부 STEP 파일의 내용이 변경됐습니다.")
        files[role] = source
    return files


def _operating_inputs(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or raw.get("coolant") != "deionized_water":
        raise ValueError("현재 검증 물성은 탈이온수만 지원합니다.")
    ranges = {
        "inlet_temperature_k": (250.0, 370.0),
        "inlet_velocity_m_s": (0.001, 5.0),
        "outlet_pressure_pa": (50_000.0, 300_000.0),
        "mesh_target_cells": (700_000.0, 900_000.0),
    }
    values: dict[str, object] = {"coolant": "deionized_water"}
    for name, (lower, upper) in ranges.items():
        try:
            value = float(raw[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{name}: 숫자가 필요합니다.") from exc
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"{name}: 허용 범위 {lower:g}–{upper:g} 밖입니다.")
        values[name] = int(value) if name == "mesh_target_cells" and value.is_integer() else value
    if values["mesh_target_cells"] not in {700_000, 800_000}:
        raise ValueError("메시 등급은 검증된 70만급 또는 80만급 중 하나여야 합니다.")
    return values


def _operation_overrides(inputs: dict[str, object]) -> tuple[str, ...]:
    return (
        f"boundary.inlet_temperature={inputs['inlet_temperature_k']:.12g} K",
        f"boundary.initial_fluid_temperature={inputs['inlet_temperature_k']:.12g} K",
        f"boundary.inlet_velocity={inputs['inlet_velocity_m_s']:.12g} m/s",
        f"boundary.outlet_absolute_pressure={inputs['outlet_pressure_pa']:.12g} Pa",
        f"boundary.pressure_reference_absolute={inputs['outlet_pressure_pa']:.12g} Pa",
    )


def prepare_imported_run(root: Path, selected: object, raw_inputs: object) -> dict[str, object]:
    files = _source_files(root, selected)
    inputs = _operating_inputs(raw_inputs)
    base = load_config(BASE_CONFIG, overlay_paths=(THERMAL_OVERLAY,))
    geometry = inspect_imported_steps(files, base)
    identity = {
        "files": {role: files[role].name for role in STEP_ROLES},
        "inputs": inputs,
        "geometry": geometry,
    }
    confirmation = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    free_gib = shutil.disk_usage(root).free / 1024**3
    return {
        "status": "READY_FOR_EXPLICIT_CONFIRMATION",
        "confirmation": confirmation,
        "geometry": geometry,
        "inputs": inputs,
        "scope": "same 23-cell tank/battery family; changed circular top/bottom ports supported",
        "mesh_note": f"{inputs['mesh_target_cells']//1000}k-calibrated profile; actual count and quality must pass after meshing",
        "disk_free_gib": round(free_gib, 1),
        "disk_minimum_gib": MIN_FREE_GIB,
        "can_start_new_solver": free_gib >= MIN_FREE_GIB,
    }


def create_imported_run(
    root: Path, selected: object, raw_inputs: object, confirmation: str
) -> dict[str, object]:
    prepared = prepare_imported_run(root, selected, raw_inputs)
    if confirmation != prepared["confirmation"]:
        raise ValueError("CAD 또는 입력값이 확인 화면 이후 변경됐습니다. 다시 확인하십시오.")
    if not prepared["can_start_new_solver"]:
        raise ValueError(
            f"새 CFD 계산에는 안전 여유를 포함해 최소 {MIN_FREE_GIB} GiB의 빈 공간이 필요합니다. "
            f"현재 {prepared['disk_free_gib']} GiB입니다. 기존 결과는 자동 삭제하지 않습니다."
        )
    files = _source_files(root, selected)
    run_id = uuid.uuid4().hex
    job_dir = root / "runs" / run_id
    job_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "id": run_id,
        "status": "queued",
        "files": {role: str(files[role]) for role in STEP_ROLES},
        "inputs": prepared["inputs"],
        "confirmation": confirmation,
        "geometry": prepared["geometry"],
    }
    _write_json(job_dir / "job.json", manifest)
    _write_json(job_dir / "status.json", {"status": "queued", "stage": "cad", "message": "검증된 STEP에서 계산 케이스를 준비합니다."})
    with (job_dir / "worker.log").open("ab") as output:
        subprocess.Popen(
            [sys.executable, "-m", "cfd_twin.workflow_run", str(job_dir / "job.json")],
            cwd=SOURCE_ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            **_worker_process_options(),
        )
    return {"id": run_id, "status": "queued", "status_url": f"/api/runs/{run_id}"}


def imported_run_status(root: Path, run_id: str) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("잘못된 실행 ID입니다.")
    job_dir = root / "runs" / run_id
    if not (job_dir / "job.json").is_file():
        raise ValueError("실행을 찾을 수 없습니다.")
    status = _read_json(job_dir / "status.json")
    status["id"] = run_id
    status["viewer_url"] = f"/runs/{run_id}/viewer/index.html" if status.get("status") == "complete" else None
    if (job_dir / "case").is_dir():
        # Read-only progress from the same validated reports/logs as the
        # baseline portal. Never manufacture a mesh/solver percentage.
        from .workflow_app import workflow_status

        status["progress"] = workflow_status(job_dir / "case", job_dir / "cad", job_dir / "viewer")
    return status


def _gate(path: Path, expected: str) -> None:
    actual = _read_json(path).get("status")
    if actual != expected:
        raise CfdTwinError(f"{path}: expected {expected}; got {actual}")


def build_imported_case(job_dir: Path) -> dict[str, object]:
    """Create the real STL/mesh/solver input bundle without running Docker."""

    job = _read_json(job_dir / "job.json")
    files = {role: Path(job["files"][role]) for role in STEP_ROLES}
    imported = import_step_bundle(files, BASE_CONFIG, THERMAL_OVERLAY, job_dir / "cad")
    inputs = job["inputs"]
    geometry_overrides = imported_config_overrides(imported["report"])
    config = load_config(
        BASE_CONFIG, overlay_paths=(THERMAL_OVERLAY,),
        overrides=geometry_overrides + _operation_overrides(inputs),
    )
    case = job_dir / "case"
    profile = f"production_{inputs['mesh_target_cells']//1000}k"
    write_production_mesh_source_bundle(config, imported["cad_dir"], case, mesh_level=profile)
    (job_dir / "resolved_config.json").write_text(config.to_json() + "\n", encoding="utf-8")
    return {"case": case, "cad": imported["cad_dir"], "config": config}


def run_imported_case(job_manifest: Path) -> None:
    job_dir = job_manifest.resolve().parent
    root = job_dir.parent.parent
    lock_path = root / "active_solver.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    def update(status: str, stage: str, message: str) -> None:
        _write_json(job_dir / "status.json", {"status": status, "stage": stage, "message": message, "pid": os.getpid()})

    try:
        update("running", "cad", "STEP 형상과 입·출구를 검증하고 STL로 내보내는 중")
        built = build_imported_case(job_dir)
        case, config = built["case"], built["config"]
        with lock_path.open("a+b") as lock:
            try:
                _lock_solver_file(lock)
            except (BlockingIOError, OSError) as exc:
                raise CfdTwinError("다른 업로드 계산이 실행 중입니다. 나중에 다시 시작하십시오.") from exc
            stages = (
                ("mesh", "production-mesh", "메시 생성 및 품질 검사"),
                ("steady", None, "S0 유동·S1 정상 CHT 계산"),
                ("transient", "frozen-flow-actual-1s", "1초 간격 S2 동적 발열 계산"),
            )
            for stage, service, message in stages:
                update("running", stage, message)
                environment = os.environ.copy()
                if stage == "transient":
                    environment.update(TRANSIENT_DELTA_T="1", ENERGY_COUPLING_ITERATIONS="200")
                with (job_dir / "worker.log").open("ab") as output:
                    if stage == "steady":
                        _run_steady_stages(case, output)
                        result_code = 0
                    else:
                        result_code = _compose_service(case, service, output, environment=environment)
                if result_code:
                    raise CfdTwinError(f"{stage}: {service} exited with {result_code}; worker.log 참조")
                if stage == "mesh":
                    _gate(case / "reports/production_mesh/production_mesh_gate.json", "PRODUCTION_MESH_GATE_PASSED")
                    composition = _read_json(case / "reports/production_mesh/mesh_target_composition.json")
                    actual = composition.get("actual", {})
                    count = actual.get("total_cells") if isinstance(actual, dict) else None
                    target = _read_json(job_dir / "job.json")["inputs"]["mesh_target_cells"]
                    if not isinstance(count, int) or abs(count - target) / target > 0.1:
                        raise CfdTwinError(f"mesh: actual {count} is outside requested {target} ±10%")
                if stage == "steady":
                    _gate(case / "runs/steady_cold_flow/reports/solver/cold_flow_validation.json", "COLD_FLOW_VALIDATION_PASSED")
                    _gate(case / "runs/steady_cht/reports/solver/steady_cht_validation.json", "STEADY_CHT_VALIDATION_PASSED")
                if stage == "transient":
                    _gate(case / "runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json", "FROZEN_FLOW_VALIDATION_PASSED")
                    _gate(case / "runs/frozen_flow_actual_1s/reports/solver/energy_balance.json", "TRANSIENT_ENERGY_BALANCE_PASSED")
            update("running", "publish", "검증된 결과를 3D 뷰어에 연결 중")
            publish_completed_run(case, job_dir / "viewer", config=config)
            update("complete", "publish", "메시·S0·S1·S2·후처리 검증을 통과했습니다. 단일 메시 검토용 결과입니다.")
    except Exception as exc:
        update("failed", "validation", str(exc))
        raise


if __name__ == "__main__":
    run_imported_case(Path(sys.argv[1]))
