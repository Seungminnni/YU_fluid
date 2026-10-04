"""Start the workflow from a source checkout, including one with no results."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from .workflow_app import CAD_ROLES, baseline_geometry, serve_workflow


def verified_reference_cad(source: Path, runtime: Path) -> Path:
    """Recover only Git CRLF conversion that reproduces the recorded SHA-256."""
    if baseline_geometry(source)["verified"]:
        return source
    result = json.loads((source / "cad_result.json").read_text(encoding="utf-8"))
    canonical = {}
    for name in CAD_ROLES:
        data = (source / name).read_bytes()
        expected = result.get("file_sha256", {}).get(name)
        if hashlib.sha256(data).hexdigest() != expected:
            data = data.replace(b"\r\n", b"\n")
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Reference CAD content differs from its verified hash: {name}")
        canonical[name] = data
    runtime.mkdir(parents=True, exist_ok=True)
    for name, data in canonical.items():
        (runtime / name).write_bytes(data)
    for name in ("cad_manifest.json", "cad_result.json"):
        shutil.copy2(source / name, runtime / name)
    if not baseline_geometry(runtime)["verified"]:
        raise ValueError("Reference CAD reports do not confirm verified geometry")
    return runtime


def prepare_workspace(root: Path) -> dict[str, Path | int]:
    """Resolve real inputs; an empty checkout has zero published results."""
    from .viewer_release import validate_server_viewer

    root = root.resolve()
    cad = root / "src/cfd_twin/assets/verified_cad_phi23"
    if not cad.is_dir():
        cad = root / "data/cad"
    cad = verified_reference_cad(cad, root / "build/reference_cad")
    viewer = root / "viewer"
    # Previews or a lock file alone are not a publication. Partial/corrupt
    # publications must still fail validation rather than appear as zero runs.
    markers = ("results.html", "model.js", "four_case_manifest.json", "data")
    count = 0
    if any((viewer / name).exists() for name in markers):
        count = len(validate_server_viewer(viewer)["runs"])
    jobs = root / "build/imported_runs"
    jobs.mkdir(parents=True, exist_ok=True)
    return {"case_dir": root / "build/published_viewer_only", "cad_dir": cad,
            "viewer_dir": viewer, "imported_root": jobs, "result_count": count}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--check", action="store_true", help="Verify startup inputs without starting a server")
    args = parser.parse_args(argv)
    workspace = prepare_workspace(args.root)
    count = workspace.pop("result_count")
    print(f"Verified reference CAD; {count} published results. Python requirement: 3.12 or higher.", flush=True)
    if not args.check:
        print(f"Open http://127.0.0.1:{args.port}/ to configure a new experiment.", flush=True)
        serve_workflow(**workspace, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
