"""Validate the server viewer and publish the same files as an offline ZIP.

The project-root ``viewer/`` is the only editable publication. A ZIP is a
verified snapshot of that directory, not a second viewer to maintain by hand.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from .multi_case_viewer import append_validated_run, dataset_paths, read_viewer_model


class _PageAssets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"script", "link", "img"}:
            return
        values = dict(attrs)
        path = values.get("src") or values.get("href")
        if path:
            self.paths.append(path)


def _local_file(viewer: Path, parent: Path, raw: str) -> Path | None:
    url = urlsplit(raw)
    if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
        return None
    target = (parent / unquote(url.path)).resolve()
    if not target.is_relative_to(viewer):
        raise ValueError(f"viewer asset escapes publication: {raw}")
    return target


def validate_server_viewer(viewer_dir: str | Path) -> dict[str, object]:
    """Check active results, portable data, CAD, mesh and HTML dependencies."""
    viewer = Path(viewer_dir).resolve()
    required = ("index.html", "results.html", "mesh.html", "cad/index.html",
                "four_case_manifest.json", "four_case_data.js", "model.js")
    for name in required:
        if not (viewer / name).is_file():
            raise FileNotFoundError(viewer / name)
    model, _ = read_viewer_model(viewer)
    manifest = json.loads((viewer / "four_case_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("solver_validation_passed") is not True or manifest.get("energy_balance_passed") is not True:
        raise ValueError("published viewer is missing solver or energy-balance validation")
    runs = model.get("mesh_runs", [])
    model_ids = [str(run["id"]) for run in runs if run.get("data_ready")]
    manifest_ids = [str(run["id"]) for run in manifest.get("runs", []) if run.get("data_ready")]
    if not model_ids or model_ids != manifest_ids:
        raise ValueError(f"server result selection differs from portal: {model_ids} != {manifest_ids}")
    declared = {path.split("/", 2)[1] for path in dataset_paths(model) if path.count("/") >= 2}
    unexpected = declared - set(model_ids)
    if unexpected:
        raise ValueError(f"model references data for an inactive result: {sorted(unexpected)}")
    for raw in dataset_paths(model):
        target = _local_file(viewer, viewer, raw)
        if target is None or not target.is_file() or target.stat().st_size == 0:
            raise FileNotFoundError(viewer / raw)
    for run in runs:
        if not run.get("data_ready"):
            continue
        run_id = str(run["id"])
        if not re.fullmatch(r"[a-z][a-z0-9_]*", run_id):
            raise ValueError(f"unsafe run id: {run_id}")
        asset_id = str(run.get("asset_source_id") or run_id)
        if asset_id not in model_ids or not re.fullmatch(r"[a-z][a-z0-9_]*", asset_id):
            raise ValueError(f"invalid asset source for {run_id}: {asset_id}")
        if run.get("display_variant_of") and run["display_variant_of"] != asset_id:
            raise ValueError(f"display variant source differs from assets for {run_id}")
        for stem in ("mesh-data", "mesh-volume-data"):
            target = viewer / f"{stem}-{asset_id}.js"
            if not target.is_file() or target.stat().st_size == 0:
                raise FileNotFoundError(target)
        for view in ("top", "side"):
            for suffix in (".svg", ".en.svg"):
                target = viewer / "cad" / f"cad_{asset_id}_{view}{suffix}"
                if not target.is_file():
                    raise FileNotFoundError(target)
    for page in (viewer / "index.html", viewer / "results.html", viewer / "mesh.html", viewer / "cad/index.html"):
        assets = _PageAssets()
        assets.feed(page.read_text(encoding="utf-8"))
        for raw in assets.paths:
            target = _local_file(viewer, page.parent, raw)
            if target is not None and not target.is_file():
                raise FileNotFoundError(target)
    return {"runs": model_ids, "dataset_files": len(dataset_paths(model))}


def prune_old_viewer_assets(viewer_dir: str | Path) -> list[str]:
    """Remove only old content-hashed UI snapshots, never CFD payloads."""
    viewer = Path(viewer_dir).resolve()
    html = (viewer / "results.html").read_text(encoding="utf-8")
    active = set(re.findall(r'["\']([^"\']+\.(?:js|css))["\']', html))
    removed: list[str] = []
    for pattern in ("app.*.js", "model.*.js", "style.*.css"):
        for path in viewer.glob(pattern):
            if path.name in active:
                continue
            if not path.is_file():
                continue
            path.unlink()
            removed.append(path.name)
    return sorted(removed)


@contextmanager
def release_lock(viewer_dir: str | Path):
    viewer = Path(viewer_dir).resolve()
    viewer.mkdir(parents=True, exist_ok=True)
    with (viewer / ".release.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def package_demo_viewer(viewer_dir: str | Path, zip_path: str | Path) -> dict[str, object]:
    """Atomically replace one ZIP after validating its exact server source."""
    viewer = Path(viewer_dir).resolve()
    destination = Path(zip_path).resolve()
    if destination.is_relative_to(viewer):
        raise ValueError("ZIP output must be outside the server viewer")
    report = validate_server_viewer(viewer)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    count = 0
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            archive.writestr(
                "index.html",
                '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=viewer/index.html">'
                '<a href="viewer/index.html">Open CFD viewer</a>\n',
            )
            for path in sorted(viewer.rglob("*")):
                if not path.is_file() or path.name.startswith(".") or path.name.endswith(".tmp"):
                    continue
                if path.is_symlink():
                    raise ValueError(f"viewer contains a symlink: {path}")
                archive.write(path, "viewer/" + path.relative_to(viewer).as_posix())
                count += 1
        with ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("ZIP integrity check failed")
            if "viewer/results.html" not in archive.namelist():
                raise ValueError("ZIP has no results page")
        os.replace(temporary, destination)
        os.chmod(destination, 0o644)
    finally:
        temporary.unlink(missing_ok=True)
    return {**report, "zip": str(destination), "files": count, "bytes": destination.stat().st_size}


def publish_experiment_and_demo(
    viewer_dir: str | Path,
    zip_path: str | Path,
    *,
    source_viewer: str | Path | None = None,
    run_id: str | None = None,
    label: str | None = None,
    label_en: str | None = None,
) -> dict[str, object]:
    """Synchronize one verified server publication and its offline demo."""
    viewer = Path(viewer_dir).resolve()
    with release_lock(viewer):
        if source_viewer is not None:
            if not run_id:
                raise ValueError("run_id is required when adding a source viewer")
            current, _ = read_viewer_model(viewer)
            if not any(str(run.get("id")) == run_id for run in current.get("mesh_runs", [])):
                append_validated_run(
                    viewer, Path(source_viewer), run_id=run_id, label=label, label_en=label_en
                )
        elif run_id is not None:
            raise ValueError("run_id requires a source viewer")
        report = validate_server_viewer(viewer)
        removed = prune_old_viewer_assets(viewer)
        packaged = package_demo_viewer(viewer, zip_path)
        return {**report, **packaged, "removed_old_assets": removed}
