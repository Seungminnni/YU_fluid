"""Build a portable, evidence-backed CFD portal without copying the 41 GiB case.

The archive contains the adaptive result viewer, original validation reports,
the baseline STEP triplet, and the real CAD/mesh/S0/S1/S2 worker code.  A fresh
calculation requires launching the local Python server and having Docker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .workflow_app import workflow_status


CASE_REPORTS = (
    "reports/production_mesh/production_mesh_gate.json",
    "reports/production_mesh/mesh_target_composition.json",
    "runs/steady_cold_flow/reports/solver/cold_flow_validation.json",
    "runs/steady_cht/reports/solver/steady_cht_validation.json",
    "runs/frozen_flow_actual_1s/reports/solver/frozen_flow_validation.json",
    "runs/frozen_flow_actual_1s/reports/solver/energy_balance.json",
    "runs/frozen_flow_actual_1s/reports/solver/energy_coupling_audit.json",
    "results/result_manifest.json",
    "reports/workflow_postprocess.json",
)
CAD_FILES = (
    "enclosure_with_ports.step",
    "coolant.step",
    "battery_cells.step",
    "cad_manifest.json",
    "cad_result.json",
)
LOG_RELATIVE = "runs/frozen_flow_actual_1s/logs/solver/frozen_flow_actual_1s.log"
PREFIX = "YU_fluid_통합CFD_적응형결과"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_distribution(
    output_path: str | Path,
    *,
    root: str | Path,
    case_dir: str | Path,
    cad_dir: str | Path,
    viewer_dir: str | Path,
) -> dict[str, object]:
    """Write a new archive only after checking actual reports and viewer fields."""

    root, case, cad, viewer, destination = map(
        lambda value: Path(value).resolve(),
        (root, case_dir, cad_dir, viewer_dir, output_path),
    )
    if destination.exists():
        raise FileExistsError(f"기존 배포 파일을 덮어쓰지 않습니다: {destination}")
    status = workflow_status(case, cad, viewer)
    if not status["viewer_ready"] or any(
        stage["status"] != "passed" for stage in status["stages"]
    ):
        raise ValueError("CAD·메시·S0/S1/S2·후처리의 실제 검증 기록이 필요합니다.")
    manifest_path = viewer / "viewer_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("execution_variant") != "adaptive_energy_coupling_trial"
        or not manifest.get("actual_solver_data")
        or not (viewer / "mesh-volume-data.js").is_file()
    ):
        raise ValueError("검증된 적응형 3D 결과와 전체 체적 메시 파일이 필요합니다.")
    source = root / "src/cfd_twin"
    templates = root / "distribution"
    required = [
        *(case / name for name in CASE_REPORTS),
        *(cad / name for name in CAD_FILES),
        case / LOG_RELATIVE,
        *(templates / name for name in ("run_mac.command", "run_windows.cmd", "README_시작하기.txt")),
        root / "pyproject.toml",
    ]
    if missing := [str(path) for path in required if not path.is_file()]:
        raise FileNotFoundError("배포 필수 파일이 없습니다: " + ", ".join(missing))
    destination.parent.mkdir(parents=True, exist_ok=True)
    identity = {
        "schema_version": 1,
        "viewer_execution_variant": manifest["execution_variant"],
        "mesh_cells": status["mesh_cells"],
        "validation_status": manifest["validation_status"],
        "original_reports_sha256": {name: _sha256(case / name) for name in CASE_REPORTS},
        "original_cad_sha256": {name: _sha256(cad / name) for name in CAD_FILES},
        "original_viewer_manifest_sha256": _sha256(manifest_path),
        "log": "원본 솔버 로그의 마지막 512 KiB만 포함합니다. 전체 로그는 원본 계산 디렉터리에 보존합니다.",
        "baseline_3d_excluded": True,
        "calculation_requires": "Python >=3.12, package dependencies, Docker Desktop, >=55 GiB free disk",
    }
    # The initial page uses exactly the same markup/styles as the live portal,
    # with an explicit offline JS handler. Relative assets resolve in viewer/.
    first_page = (viewer / "index.html").read_text(encoding="utf-8").replace(
        "<head>", '<head>\n  <base href="viewer/">', 1
    )
    count = 0
    try:
        with ZipFile(destination, mode="x", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            def add(path: Path, relative: str) -> None:
                nonlocal count
                archive.write(path, f"{PREFIX}/{relative}")
                count += 1

            archive.writestr(f"{PREFIX}/index.html", first_page)
            count += 1
            for path in sorted(viewer.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    add(path, f"viewer/{path.relative_to(viewer).as_posix()}")
            for path in sorted(source.rglob("*")):
                if path.is_file() and (
                    path.suffix == ".py"
                    or (path.parent.name == "workflow_assets" and path.suffix in {".html", ".css", ".js"})
                ):
                    add(path, f"src/cfd_twin/{path.relative_to(source).as_posix()}")
            for path in sorted((root / "configs").glob("*.yaml")):
                add(path, f"configs/{path.name}")
            add(root / "pyproject.toml", "pyproject.toml")
            for name in CASE_REPORTS:
                add(case / name, f"data/case/{name}")
            for name in CAD_FILES:
                add(cad / name, f"data/cad/{name}")
            with (case / LOG_RELATIVE).open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 512 * 1024))
                tail = stream.read()
            archive.writestr(f"{PREFIX}/data/case/{LOG_RELATIVE}", tail)
            count += 1
            for name in ("run_mac.command", "run_windows.cmd", "README_시작하기.txt"):
                add(templates / name, name)
            archive.writestr(f"{PREFIX}/distribution_identity.json", json.dumps(identity, ensure_ascii=False, indent=2) + "\n")
            count += 1
    except BaseException:
        # Preserve an interrupted archive for inspection; never remove a
        # concurrent user-created path after an exclusive-open race.
        raise
    return {"archive": str(destination), "files": count, "bytes": destination.stat().st_size, **identity}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--cad-dir", type=Path, required=True)
    parser.add_argument("--viewer-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_distribution(args.output, root=args.root, case_dir=args.case_dir,
                                        cad_dir=args.cad_dir, viewer_dir=args.viewer_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
