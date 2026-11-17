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
import time
import uuid
from pathlib import Path

from .case_builder import _docker_command
from .cad_import import STEP_ROLES, import_step_bundle, imported_config_overrides, inspect_imported_steps
from .config import load_config
from .errors import CfdTwinError
from .production_mesh import MESH_PRESETS, write_production_mesh_source_bundle
from .steady_convergence import continuation_decision, validate_maximum_iterations
from .steady_numerics import apply_profile as apply_steady_numerics, validate_profile as validate_steady_numerics
from .outlet_boundary import apply_profile as apply_outlet_boundary, validate_profile as validate_outlet_boundary, record_outlet
from .workflow_publish import publish_completed_run


SOURCE_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = SOURCE_ROOT / "configs/baseline_23cell.yaml"
THERMAL_OVERLAY = SOURCE_ROOT / "configs/ppt_bernardi_udf.yaml"
TOKEN_RE = re.compile(r"^[0-9a-f]{64}\.(?:step|stp)$")
# Reserve enough free space for one isolated CFD case and its publication.
MIN_FREE_GIB = 55


class RunCancelled(CfdTwinError):
    """A user requested that this isolated CFD job stop without publication."""


def _check_cancel(cancel_path: Path | None) -> None:
    if cancel_path is not None and cancel_path.is_file():
        raise RunCancelled("사용자 요청으로 계산을 중지했습니다. 부분 결과는 작업 폴더에 남겨뒀습니다.")


def _lock_solver_file(stream: object) -> None:
    """Non-blocking single-job lock on both POSIX and Windows hosts."""

    from .file_lock import lock_file

    lock_file(stream)


def _worker_process_options(platform_name: str | None = None) -> dict[str, object]:
    """Detach the CFD worker with flags supported by the current host OS."""

    if (platform_name or os.name) == "nt":
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        }
    return {"start_new_session": True}


def _compose_service(
    case: Path, service: str, output: object, *, environment: dict[str, str] | None = None,
    cancel_path: Path | None = None,
) -> int:
    _check_cancel(cancel_path)
    docker = _docker_command()
    if docker is None:
        raise CfdTwinError("Docker Desktop CLI가 없어 실제 CFD 단계를 실행할 수 없습니다.")
    command = [docker, "compose", "-f", str(case / "compose.yaml"), "run", "--rm", "--build"]
    if cancel_path is None:
        return subprocess.run(
            [*command, service], cwd=case, env=environment,
            stdout=output, stderr=subprocess.STDOUT, check=False,
        ).returncode

    # A unique container name lets cancellation target this job, never a
    # baseline run or a different Docker Compose project on the same host.
    container = f"cfd-twin-{cancel_path.parent.name[:12]}-{uuid.uuid4().hex[:8]}"
    process = subprocess.Popen(
        [*command, "--name", container, service], cwd=case, env=environment,
        stdout=output, stderr=subprocess.STDOUT,
    )
    while True:
        try:
            result = process.wait(timeout=0.5)
            _check_cancel(cancel_path)
            return result
        except subprocess.TimeoutExpired:
            if not cancel_path.is_file():
                continue
            # Stop only the container started for this particular service. If
            # Docker is still building, terminate its client below instead.
            try:
                subprocess.run(
                    [docker, "stop", "--time", "10", container],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False, timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            # Handles a container created just after the first stop attempt.
            try:
                subprocess.run(
                    [docker, "rm", "-f", container],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False, timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            _check_cancel(cancel_path)


def _run_steady_stages(
    case: Path, output: object, *, cancel_path: Path | None = None,
    cold_flow_maximum_iterations: int | None = None,
) -> None:
    """Run S0/S1 with the same measured, bounded gate on Mac and Windows."""

    cold_flow_maximum = validate_maximum_iterations(cold_flow_maximum_iterations)

    for stage, run_name, pilot, continuation, report_name, env_name in (
        ("cold", "steady_cold_flow", "cold-flow-pilot", "cold-flow-continue", "cold_flow_validation.json", "COLD_FLOW_ADDITIONAL_ITERATIONS"),
        ("cht", "steady_cht", "steady-pilot", "steady-continue", "steady_cht_validation.json", "STEADY_ADDITIONAL_ITERATIONS"),
    ):
        _check_cancel(cancel_path)
        run_dir = case / "runs" / run_name
        report_path = run_dir / "reports/solver" / report_name
        decision_path = run_dir / "reports/solver/convergence_decision.json"
        if not run_dir.exists():
            status = _compose_service(case, pilot, output, **({"cancel_path": cancel_path} if cancel_path else {}))
            if status and not report_path.is_file():
                raise CfdTwinError(f"{run_name}: solver stopped without validation report")
        elif not report_path.is_file():
            raise CfdTwinError(f"{run_name}: existing run has no validation report")
        while True:
            _check_cancel(cancel_path)
            if stage == "cold" and (case / "reports/outlet_boundary.json").is_file():
                measured = _read_json(report_path).get("final_iteration")
                if measured is not None:
                    record_outlet(case, f"{measured:g}")
            if stage == "cold" and (case / "diagnostics/wall/layer_faces.json.gz").is_file():
                from .wall_diagnostics import compare
                measured = _read_json(report_path).get("final_iteration")
                if measured is not None:
                    # Preserve correspondence even when the eventual y+ gate
                    # fails. Keep diagnostic_only until the full gate passes.
                    compare(case, f"{measured:g}", diagnostic_only=_read_json(report_path).get("status") != "COLD_FLOW_VALIDATION_PASSED")
            decision = continuation_decision(
                _read_json(report_path), stage=stage,
                maximum=cold_flow_maximum if stage == "cold" else 9500,
            )
            _write_json(decision_path, decision)
            if decision["action"] == "pass":
                break
            if decision["action"] != "continue":
                raise CfdTwinError(f"{run_name}: {decision.get('reason', 'non-retryable gate failure')}")
            environment = os.environ.copy()
            environment[env_name] = str(decision["additional_iterations"])
            status = _compose_service(
                case, continuation, output, environment=environment,
                **({"cancel_path": cancel_path} if cancel_path else {}),
            )
            current = _read_json(report_path).get("final_iteration")
            if status and not report_path.is_file():
                raise CfdTwinError(f"{run_name}: continuation failed with {status}")
            if not isinstance(current, (int, float)) or current <= decision["iteration"]:
                raise CfdTwinError(f"{run_name}: validation did not advance past {decision['iteration']}")


def _run_wall_diagnostic(case: Path, output: object, iterations: int, *, cancel_path: Path) -> None:
    """A short S0 checkpoint is diagnostic, never an acceptance shortcut."""
    from .wall_diagnostics import compare

    if not isinstance(iterations, int) or not 100 <= iterations < 5000:
        raise CfdTwinError("Wall diagnostic requires 100--4999 iterations")
    profile = case / "profiles/steady_cold_flow/system/controlDict"
    original = profile.read_text()
    maximum = int(re.search(r"\bendTime\s+(\d+)\s*;", original).group(1))
    shortened, count = re.subn(r"\bendTime\s+\d+\s*;", f"endTime {iterations};", original)
    if count != 1 or maximum < 5000:
        raise CfdTwinError("Invalid cold-flow control dictionary")
    profile.write_text(shortened, encoding="utf-8", newline="\n")
    try:
        _compose_service(case, "cold-flow-pilot", output, cancel_path=cancel_path)
    finally:
        profile.write_text(original, encoding="utf-8", newline="\n")
    report_path = case / "runs/steady_cold_flow/reports/solver/cold_flow_validation.json"
    report = _read_json(report_path)
    if report.get("final_iteration") != iterations:
        raise CfdTwinError("Short wall diagnostic did not finish")
    compare(case, str(iterations), diagnostic_only=True)
    if (case / "reports/outlet_boundary.json").is_file():
        record_outlet(case, str(iterations))
    destination = case / "diagnostics/wall" / str(iterations)
    shutil.copy2(report_path, destination / "cold_flow_validation.json")
    shutil.copy2(report_path.with_name("yplus.json"), destination / "yplus.json")
    for check in ("solver_clean_end", "final_fields_finite", "final_turbulence_fields_nonnegative"):
        if report.get("checks", {}).get(check) is not True:
            raise CfdTwinError(f"Unsafe diagnostic field: {check}")
    # A developing y+ distribution at 1500 iterations is not the final result.
    # Continue to the original 6000-iteration checkpoint before normal gates.
    environment = os.environ.copy()
    environment["COLD_FLOW_ADDITIONAL_ITERATIONS"] = str(maximum - iterations)
    _compose_service(case, "cold-flow-continue", output, environment=environment, cancel_path=cancel_path)
    if _read_json(report_path).get("final_iteration") != maximum:
        raise CfdTwinError("Cold flow did not reach the original checkpoint")


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
    supported_coolants = {
        "deionized_water",
        "dynalene_pao2_20c",
        "ampcool_ac110_20c_interpolated",
        "shell_s3x_20c_approximate",
    }
    if not isinstance(raw, dict) or raw.get("coolant") not in supported_coolants:
        raise ValueError("지원되는 검증·비교 냉각수 식별자가 필요합니다.")
    ranges = {
        "inlet_temperature_k": (250.0, 370.0),
        "inlet_velocity_m_s": (0.001, 5.0),
        "outlet_pressure_pa": (50_000.0, 300_000.0),
        "mesh_target_cells": (400_000.0, 1_000_000.0),
    }
    values: dict[str, object] = {"coolant": str(raw["coolant"])}
    for name, (lower, upper) in ranges.items():
        try:
            value = float(raw[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{name}: 숫자가 필요합니다.") from exc
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"{name}: 허용 범위 {lower:g}–{upper:g} 밖입니다.")
        values[name] = int(value) if name == "mesh_target_cells" and value.is_integer() else value
    if values["mesh_target_cells"] not in {400_000, 600_000, 700_000, 800_000, 1_000_000}:
        raise ValueError("메시 등급은 40만급, 60만급, 70만급, 80만급 또는 100만급 중 하나여야 합니다.")
    return values


def _operation_overrides(inputs: dict[str, object]) -> tuple[str, ...]:
    return (
        f"boundary.inlet_temperature={inputs['inlet_temperature_k']:.12g} K",
        f"boundary.initial_fluid_temperature={inputs['inlet_temperature_k']:.12g} K",
        f"boundary.inlet_velocity={inputs['inlet_velocity_m_s']:.12g} m/s",
        f"boundary.outlet_absolute_pressure={inputs['outlet_pressure_pa']:.12g} Pa",
        f"boundary.pressure_reference_absolute={inputs['outlet_pressure_pa']:.12g} Pa",
    )


def prepare_solver_inputs(root: Path, raw_inputs: object) -> dict[str, object]:
    """Validate operating inputs and storage before any expensive CAD work."""

    inputs = _operating_inputs(raw_inputs)
    free_gib = shutil.disk_usage(root).free / 1024**3
    return {
        "inputs": inputs,
        "disk_free_gib": round(free_gib, 1),
        "disk_minimum_gib": MIN_FREE_GIB,
        "can_start_new_solver": free_gib >= MIN_FREE_GIB,
    }


def _validated_geometry_overrides(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and 0 < len(item) <= 4096 for item in value
    ):
        raise ValueError("형상 설정 재현 정보가 올바르지 않습니다.")
    return tuple(value)


def prepare_imported_run(
    root: Path, selected: object, raw_inputs: object, *,
    geometry_overrides: object = None,
) -> dict[str, object]:
    files = _source_files(root, selected)
    preflight = prepare_solver_inputs(root, raw_inputs)
    inputs = preflight["inputs"]
    overrides = _validated_geometry_overrides(geometry_overrides)
    base = load_config(
        BASE_CONFIG, overlay_paths=(THERMAL_OVERLAY,), overrides=overrides,
    )
    geometry = inspect_imported_steps(files, base)
    identity = {
        "files": {role: files[role].name for role in STEP_ROLES},
        "inputs": inputs,
        "geometry": geometry,
        "geometry_overrides": list(overrides),
    }
    confirmation = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {
        "status": "READY_FOR_EXPLICIT_CONFIRMATION",
        "confirmation": confirmation,
        "geometry": geometry,
        "inputs": inputs,
        "scope": "same 23-cell tank/battery family; changed circular top/bottom ports supported",
        "mesh_note": f"{inputs['mesh_target_cells']//1000}k-calibrated profile; actual count and quality must pass after meshing",
        "disk_free_gib": preflight["disk_free_gib"],
        "disk_minimum_gib": preflight["disk_minimum_gib"],
        "can_start_new_solver": preflight["can_start_new_solver"],
    }


def create_imported_run(
    root: Path, selected: object, raw_inputs: object, confirmation: str, *,
    geometry_overrides: object = None,
    source_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    overrides = _validated_geometry_overrides(geometry_overrides)
    prepared = prepare_imported_run(
        root, selected, raw_inputs, geometry_overrides=overrides,
    )
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
        "geometry_overrides": list(overrides),
    }
    if source_metadata:
        manifest["source"] = source_metadata
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
    if (job_dir / "cancel.request").is_file() and status.get("status") in {"queued", "running"}:
        status["status"] = "stopping"
        status["message"] = "중지 요청을 받았습니다. 현재 작업을 안전하게 종료하고 있습니다."
    status["id"] = run_id
    status["viewer_url"] = f"/runs/{run_id}/viewer/results.html" if status.get("status") == "complete" else None
    if status.get("status") == "failed":
        layer_report = _read_json(job_dir / "case/reports/production_mesh/layer_coverage.json")
        layer_error = layer_report.get("error")
        if isinstance(layer_error, str) and layer_error:
            compressed_owner = job_dir / "case/constant/polyMesh/owner.gz"
            if "polyMesh/owner'" in layer_error and compressed_owner.is_file():
                status["diagnostic"] = (
                    "메시 생성 후 격자가 압축된 owner.gz로 저장됐지만 검증기가 "
                    "비압축 owner 파일을 찾았습니다. 메시 품질 합격 여부는 아직 확인 전입니다."
                )
            else:
                status["diagnostic"] = f"메시 검증 오류: {layer_error}"
    if (job_dir / "case").is_dir():
        # Read-only progress from the same validated reports/logs as the
        # baseline portal. Never manufacture a mesh/solver percentage.
        from .workflow_app import workflow_status

        status["progress"] = workflow_status(job_dir / "case", job_dir / "cad", job_dir / "viewer")
    return status


def request_imported_run_cancel(root: Path, run_id: str) -> dict[str, object]:
    """Request cooperative cancellation of one known, unfinished job."""

    current = imported_run_status(root, run_id)
    if current.get("status") in {"queued", "running", "stopping"}:
        marker = root / "runs" / run_id / "cancel.request"
        try:
            with marker.open("xb") as stream:
                stream.write(b"user requested stop\n")
        except FileExistsError:
            pass
    return imported_run_status(root, run_id)


def _gate(path: Path, expected: str) -> None:
    actual = _read_json(path).get("status")
    if actual != expected:
        raise CfdTwinError(f"{path}: expected {expected}; got {actual}")


def build_imported_case(job_dir: Path) -> dict[str, object]:
    """Create the real STL/mesh/solver input bundle without running Docker."""

    job = _read_json(job_dir / "job.json")
    validate_steady_numerics(job.get("steady_numerics_profile"))
    validate_outlet_boundary(job.get("outlet_velocity_profile"))
    validate_maximum_iterations(job.get("cold_flow_maximum_iterations"))
    files = {role: Path(job["files"][role]) for role in STEP_ROLES}
    base_overrides = _validated_geometry_overrides(job.get("geometry_overrides"))
    inputs = job["inputs"]
    frozen_cad = job.get("prevalidated_cad")
    external_mesh = job.get("external_structured_mesh_import")
    external_mesh_authoritative = external_mesh is not None
    if external_mesh_authoritative:
        if not isinstance(external_mesh, dict) or set(external_mesh) != {"source", "file_sha256"}:
            raise CfdTwinError("external structured mesh reference is malformed")
        mesh_source = Path(str(external_mesh["source"])).resolve()
        mesh_hashes = external_mesh["file_sha256"]
        if mesh_source.is_relative_to(job_dir.resolve()) or not mesh_source.is_dir():
            raise CfdTwinError("external structured mesh must be an external shared input")
        if not isinstance(mesh_hashes, dict) or not mesh_hashes:
            raise CfdTwinError("external structured mesh hash inventory is missing")
        for relative, digest in mesh_hashes.items():
            candidate = (mesh_source / str(relative)).resolve()
            if not candidate.is_relative_to(mesh_source) or not candidate.is_file():
                raise CfdTwinError(f"external structured mesh evidence is missing: {relative}")
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
                raise CfdTwinError(f"external structured mesh evidence hash mismatch: {relative}")
    if frozen_cad is None:
        imported = import_step_bundle(
            files, BASE_CONFIG, THERMAL_OVERLAY, job_dir / "cad",
            base_overrides=base_overrides,
        )
        geometry_overrides = imported_config_overrides(imported["report"])
        config = load_config(
            BASE_CONFIG, overlay_paths=(THERMAL_OVERLAY,),
            overrides=base_overrides + geometry_overrides + _operation_overrides(inputs),
        )
        cad_dir = imported["cad_dir"]
    else:
        if not isinstance(frozen_cad, dict) or set(frozen_cad) != {"path", "file_sha256"}:
            raise CfdTwinError("prevalidated CAD reference is malformed")
        source = Path(str(frozen_cad["path"])).resolve()
        expected = frozen_cad["file_sha256"]
        if not isinstance(expected, dict) or not expected:
            raise CfdTwinError("prevalidated CAD hash inventory is missing")
        if source.is_relative_to(job_dir.resolve()) or not source.is_dir():
            raise CfdTwinError("prevalidated CAD must be an external shared input")
        for relative, digest in expected.items():
            if not isinstance(relative, str) or not isinstance(digest, str):
                raise CfdTwinError("prevalidated CAD hash inventory is malformed")
            candidate = (source / relative).resolve()
            if not candidate.is_relative_to(source) or not candidate.is_file():
                raise CfdTwinError(f"prevalidated CAD file is missing: {relative}")
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
                raise CfdTwinError(f"prevalidated CAD hash mismatch: {relative}")
        cad_dir = job_dir / "cad"
        if cad_dir.exists():
            raise CfdTwinError(f"fresh job CAD directory must not exist: {cad_dir}")
        shutil.copytree(source, cad_dir)
        # The shared surfaces were generated directly from these frozen
        # geometry overrides, so retain that exact configuration identity.
        config = load_config(
            BASE_CONFIG, overlay_paths=(THERMAL_OVERLAY,),
            overrides=base_overrides + _operation_overrides(inputs),
        )
    case = job_dir / "case"
    profile = job.get("mesh_profile", f"production_{inputs['mesh_target_cells']//1000}k")
    if profile not in MESH_PRESETS or MESH_PRESETS[profile].target_cells != inputs['mesh_target_cells']:
        raise CfdTwinError("Mesh profile must match the requested cell-count target")
    write_production_mesh_source_bundle(
        config,
        cad_dir,
        case,
        mesh_level=profile,
        external_structured_mesh_authoritative=external_mesh_authoritative,
    )
    apply_steady_numerics(case, job.get("steady_numerics_profile"))
    apply_outlet_boundary(case, job.get("outlet_velocity_profile"))
    (job_dir / "resolved_config.json").write_text(config.to_json() + "\n", encoding="utf-8")
    return {"case": case, "cad": cad_dir, "config": config}


def run_imported_case(job_manifest: Path) -> None:
    job_dir = job_manifest.resolve().parent
    cancel_path = job_dir / "cancel.request"
    root = job_dir.parent.parent
    lock_path = root / "active_solver.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    current_stage = "cad"
    run_started = time.time()
    stage_started = run_started
    runtime_report: dict[str, object] = {
        "schema_version": 1,
        "status": "running",
        "started_at_epoch_s": run_started,
        "solver_mpi_ranks": int(os.environ.get("OPENFOAM_MPI_RANKS", "1")),
        "mesh_mpi_ranks": int(os.environ.get("OPENFOAM_MESH_MPI_RANKS", "1")),
        "stage_wall_seconds": {},
        "semantics": "measured host wall-clock seconds for this fresh isolated job",
    }
    runtime_path = job_dir / "execution_runtime.json"
    case: Path | None = None

    def write_runtime() -> None:
        _write_json(runtime_path, runtime_report)
        if case is not None:
            _write_json(case / "reports/execution_runtime.json", runtime_report)

    def finish_stage(stage: str) -> None:
        nonlocal stage_started
        now = time.time()
        durations = runtime_report["stage_wall_seconds"]
        assert isinstance(durations, dict)
        durations[stage] = float(now - stage_started)
        stage_started = now
        runtime_report["last_completed_stage"] = stage
        runtime_report["elapsed_wall_seconds"] = float(now - run_started)
        write_runtime()

    def update(status: str, stage: str, message: str) -> None:
        _write_json(job_dir / "status.json", {"status": status, "stage": stage, "message": message, "pid": os.getpid()})

    try:
        write_runtime()
        _check_cancel(cancel_path)
        update("running", "cad", "STEP 형상과 입·출구를 검증하고 STL로 내보내는 중")
        built = build_imported_case(job_dir)
        case = built["case"]
        finish_stage("cad")
        _check_cancel(cancel_path)
        config = built["config"]
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
                _check_cancel(cancel_path)
                current_stage = stage
                stage_started = time.time()
                update("running", stage, message)
                environment = os.environ.copy()
                if stage == "transient":
                    environment.update(TRANSIENT_DELTA_T="1", ENERGY_COUPLING_ITERATIONS="200")
                with (job_dir / "worker.log").open("ab") as output:
                    if stage == "steady":
                        job_options = _read_json(job_manifest)
                        diagnostic_iterations = job_options.get("wall_diagnostic_iterations")
                        if diagnostic_iterations is not None:
                            _run_wall_diagnostic(case, output, diagnostic_iterations, cancel_path=cancel_path)
                        _run_steady_stages(
                            case, output, cancel_path=cancel_path,
                            cold_flow_maximum_iterations=job_options.get(
                                "cold_flow_maximum_iterations"
                            ),
                        )
                        result_code = 0
                    else:
                        result_code = _compose_service(
                            case, service, output, environment=environment,
                            cancel_path=cancel_path,
                        )
                _check_cancel(cancel_path)
                if result_code:
                    detail = _read_json(case / "reports/production_mesh/layer_coverage.json") if stage == "mesh" else {}
                    reason = detail.get("error") if detail.get("status") == "LAYER_VALIDATION_FAILED" else None
                    suffix = f"; {reason}" if reason else "; worker.log 참조"
                    raise CfdTwinError(f"{stage}: {service} exited with {result_code}{suffix}")
                if stage == "mesh":
                    _gate(case / "reports/production_mesh/production_mesh_gate.json", "PRODUCTION_MESH_GATE_PASSED")
                    composition = _read_json(case / "reports/production_mesh/mesh_target_composition.json")
                    actual = composition.get("actual", {})
                    count = actual.get("total_cells") if isinstance(actual, dict) else None
                    target = _read_json(job_dir / "job.json")["inputs"]["mesh_target_cells"]
                    job = _read_json(job_manifest)
                    preset = MESH_PRESETS[job.get("mesh_profile", f"production_{target//1000}k")]
                    if not isinstance(count, int) or abs(count - target) / target > preset.total_tolerance_fraction or (preset.minimum_total_cells is not None and count < preset.minimum_total_cells):
                        raise CfdTwinError(f"mesh: actual {count} is outside profile {target} ±{100*preset.total_tolerance_fraction:g}%")
                if stage == "steady":
                    _gate(case / "runs/steady_cold_flow/reports/solver/cold_flow_validation.json", "COLD_FLOW_VALIDATION_PASSED")
                    _gate(case / "runs/steady_cht/reports/solver/steady_cht_validation.json", "STEADY_CHT_VALIDATION_PASSED")
                if stage == "transient":
                    _gate(case / "runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json", "FROZEN_FLOW_VALIDATION_PASSED")
                    _gate(case / "runs/frozen_flow_actual_1s/reports/solver/energy_balance.json", "TRANSIENT_ENERGY_BALANCE_PASSED")
                finish_stage(stage)
            _check_cancel(cancel_path)
            runtime_report["status"] = "analysis_complete"
            runtime_report["analysis_wall_seconds"] = float(time.time() - run_started)
            runtime_report["completed_analysis_at_epoch_s"] = time.time()
            write_runtime()
            current_stage = "publish"
            stage_started = time.time()
            update("running", "publish", "검증된 결과를 3D 뷰어에 연결 중")
            publish_completed_run(case, job_dir / "viewer", config=config, include_reference_cad=False)
            finish_stage("publish")
            _check_cancel(cancel_path)
            current_stage = "release"
            stage_started = time.time()
            update("running", "release", "서버 뷰어와 오프라인 데모 ZIP을 함께 갱신 중")
            try:
                from .mesh_viewer import write_mesh_only_viewer
                from .viewer_release import publish_experiment_and_demo

                write_mesh_only_viewer(case, job_dir / "viewer")
                project_root = Path(__file__).resolve().parents[2]
                labels = {}
                if (case / "reports/outlet_boundary.json").is_file():
                    diameter = config.geometry.ports.inlet_diameter_m * 1000
                    labels = {"label": f"Ø{diameter:g} mm · 출구 역류 대응", "label_en": f"Ø{diameter:g} mm · pressure-outlet backflow"}
                publish_experiment_and_demo(
                    project_root / "viewer",
                    project_root / "YU_fluid_데모.zip",
                    source_viewer=job_dir / "viewer",
                    run_id=f"run_{job_dir.name}",
                    **labels,
                )
            except Exception as exc:
                finish_stage("release")
                runtime_report["status"] = "release_failed"
                runtime_report["total_wall_seconds"] = float(time.time() - run_started)
                runtime_report["finished_at_epoch_s"] = time.time()
                runtime_report["release_error"] = str(exc)
                write_runtime()
                _write_json(job_dir / "status.json", {
                    "status": "complete",
                    "stage": "release",
                    "message": "CFD 계산·검증은 완료됐지만 서버/ZIP 동시 배포에 실패했습니다.",
                    "release_error": str(exc),
                    "pid": os.getpid(),
                })
                return
            finish_stage("release")
            runtime_report["status"] = "complete"
            runtime_report["total_wall_seconds"] = float(time.time() - run_started)
            runtime_report["finished_at_epoch_s"] = time.time()
            write_runtime()
            update("complete", "release", "CFD 검증 완료 · 서버 뷰어와 데모 ZIP 동시 갱신 완료")
    except RunCancelled as exc:
        runtime_report.update(status="cancelled", failed_or_stopped_stage=current_stage,
                              elapsed_wall_seconds=float(time.time() - run_started))
        write_runtime()
        update("cancelled", "stopped", str(exc))
    except Exception as exc:
        runtime_report.update(status="failed", failed_or_stopped_stage=current_stage,
                              elapsed_wall_seconds=float(time.time() - run_started), error=str(exc))
        write_runtime()
        update("failed", current_stage, str(exc))
        raise


if __name__ == "__main__":
    run_imported_case(Path(sys.argv[1]))
