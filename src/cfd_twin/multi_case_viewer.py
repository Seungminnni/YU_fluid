"""Assemble preserved legacy and newly recomputed CFD viewers.

The merger only copies published, independently validated viewer payloads.  It
does not alter solver cases or synthesize fields.  Each selectable run keeps
its own geometry, mesh summary, convergence history, energy balance, and
packed one-second datasets.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any


MODEL_PATTERN = re.compile(r'<script src="([^"?]*model[^"?]*\.js)(?:\?[^"}]*)?"')
SCRIPT_PATTERN = re.compile(r'<script src="([^"?]*app[^"?]*\.js)(?:\?[^"}]*)?"')


def _viewer_asset(html: str, pattern: re.Pattern[str], viewer: Path) -> Path:
    match = pattern.search(html)
    if not match:
        raise ValueError(f"viewer HTML has no matching script: {viewer / 'results.html'}")
    path = viewer / match.group(1)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def read_viewer_model(viewer: Path) -> tuple[dict[str, Any], Path]:
    viewer = viewer.resolve()
    html = (viewer / "results.html").read_text(encoding="utf-8")
    model_path = _viewer_asset(html, MODEL_PATTERN, viewer)
    source = model_path.read_text(encoding="utf-8")
    match = re.search(r"^.*?=\s*(\{.*\})\s*;?\s*$", source, re.DOTALL)
    if not match:
        raise ValueError(f"cannot parse viewer model: {model_path}")
    value = json.loads(match.group(1))
    if not isinstance(value, dict):
        raise ValueError(f"viewer model is not an object: {model_path}")
    return value, model_path


def four_case_manifest(model: dict[str, Any], viewer: Path) -> dict[str, Any]:
    """Build the small, portal-readable index from the published CFD data."""

    def result_run(run: dict[str, Any]) -> dict[str, Any]:
        run_id = str(run["id"])
        graphs = model.get("interactive_cfd_by_mesh", {}).get(run_id, {}).get("temperature_graphs", {})
        diameter = run.get("inlet_diameter_mm")
        geometry = model.get("geometry_by_mesh", {}).get(run_id, {})
        enclosure = geometry.get("enclosure_mm") or model.get("enclosure_mm", {})
        cells = geometry.get("cells", [])
        ports = geometry.get("ports", [])
        if run_id == "precision" or run_id.startswith("exact_phi"):
            projection_stem = f"actual_phi{int(diameter)}" if diameter is not None else ""
        else:
            projection_stem = f"actual_{run_id}"

        def final_kelvin(key: str) -> float | None:
            series = graphs.get(key)
            if not isinstance(series, list) or not series or not isinstance(series[-1], (int, float)):
                return None
            return float(series[-1]) + 273.15

        return {
            "id": run_id,
            "label": run["label"],
            "label_en": run.get("label_en", run["label"]),
            "diameter_mm": diameter,
            "outlet_diameter_mm": run.get("outlet_diameter_mm", diameter),
            "tank_mm": [enclosure.get(axis) for axis in ("x", "y", "z")],
            "battery_count": len(cells),
            "battery_diameter_mm": 2 * float(cells[0]["radius"]) if cells else None,
            "battery_height_mm": float(cells[0]["length"]) if cells else None,
            "port_length_mm": float(ports[0].get("length", 32)) if ports else None,
            "cells": run.get("counts", {}).get("total_cells"),
            "data_ready": run.get("data_ready"),
            "battery_tmax_k": final_kelvin("battery_mesh_maximum_c"),
            "outlet_temperature_k": final_kelvin("outlet_mass_weighted_average_c"),
            "projection_top": (
                f"{projection_stem}_top_XY.png"
                if projection_stem and (viewer / "cad" / f"{projection_stem}_top_XY.png").is_file()
                else None
            ),
            "projection_side": (
                f"{projection_stem}_side_XZ.png"
                if projection_stem and (viewer / "cad" / f"{projection_stem}_side_XZ.png").is_file()
                else None
            ),
        }

    count = len(model["mesh_runs"])
    ready_ids = [str(run["id"]) for run in model["mesh_runs"] if run.get("data_ready")]
    solver_validation_passed = bool(
        model.get("actual_solver_data")
        and str(model.get("validation_status", "")).endswith("_PASSED")
        and ready_ids
        and all(
            model.get("interactive_cfd_by_mesh", {}).get(run_id, {}).get("actual_one_second")
            for run_id in ready_ids
        )
    )
    energy_balance_passed = bool(
        ready_ids
        and all(
            model.get("energy_balance_by_mesh", {}).get(run_id, {}).get("available") is True
            for run_id in ready_ids
        )
    )
    return {
        "status": (
            "FOUR_CASE_VIEWER_ASSEMBLED" if count == 4 else
            "THREE_CASE_VIEWER_ASSEMBLED" if count == 3 else
            "MULTI_CASE_VIEWER_ASSEMBLED"
        ),
        "viewer": str(viewer / "results.html"),
        "runs": [result_run(run) for run in model["mesh_runs"]],
        "solver_validation_passed": solver_validation_passed,
        "energy_balance_passed": energy_balance_passed,
        "legacy_results_preserved": bool(model.get("archived_mesh_runs")),
    }


def refresh_four_case_manifest(viewer: Path) -> dict[str, Any]:
    """Refresh the portal index of an already assembled four-case viewer."""
    model, _ = read_viewer_model(viewer)
    manifest = four_case_manifest(model, viewer)
    write_four_case_manifest(viewer, manifest)
    return manifest


def prune_archived_diameter_results(viewer: Path) -> dict[str, Any]:
    """Remove the hidden old Ø23 run after the four selected runs are ready.

    This rewrites only the published model and its small index. The caller may
    then remove the no-longer-referenced ``data/single`` files separately.
    """
    viewer = viewer.resolve()
    model, _ = read_viewer_model(viewer)
    expected = ["precision", "exact_phi21", "exact_phi23", "exact_phi25"]
    active = [str(run.get("id")) for run in model.get("mesh_runs", [])]
    if active != expected:
        raise ValueError(f"expected the four current diameter results {expected}, got {active}")

    model.pop("archived_mesh_runs", None)
    for key in (
        "calculation_note_by_mesh",
        "calculation_summary_by_mesh",
        "convergence_by_mesh",
        "derived_by_mesh",
        "energy_balance_by_mesh",
        "geometry_by_mesh",
        "inputs_by_mesh",
        "interactive_cfd_by_mesh",
        "mesh_wireframes",
        "spatial_transform_by_mesh",
    ):
        bucket = model.get(key)
        if isinstance(bucket, dict):
            bucket.pop("single", None)
    comparison = model.get("four_case_comparison")
    if isinstance(comparison, dict):
        comparison["legacy_results_preserved"] = False
        comparison["archived_run_ids"] = []
        comparison["run_ids"] = expected

    write_four_case_model(viewer, model)
    manifest = four_case_manifest(model, viewer)
    write_four_case_manifest(viewer, manifest)
    return manifest


def write_four_case_manifest(viewer: Path, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    (viewer / "four_case_manifest.json").write_text(payload + "\n", encoding="utf-8")
    (viewer / "four_case_data.js").write_text(
        "window.CFD_FOUR_CASE_MANIFEST = " + payload + ";\n", encoding="utf-8"
    )


def _rewrite_data_path(value: Any, old_id: str, new_id: str) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_data_path(item, old_id, new_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_data_path(item, old_id, new_id) for item in value]
    if isinstance(value, str):
        return value.replace(f"data/{old_id}/", f"data/{new_id}/")
    return value


def dataset_paths(value: Any) -> set[str]:
    """List published binary and portable files referenced by a CFD payload."""
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"file", "portable_file"} and isinstance(item, str) and item.startswith("data/"):
                paths.add(item)
            else:
                paths.update(dataset_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.update(dataset_paths(item))
    return paths


def _ready_run(model: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    runs = model.get("mesh_runs", [])
    if not isinstance(runs, list):
        raise ValueError("viewer mesh_runs is not a list")
    for run in runs:
        if not isinstance(run, dict) or not run.get("data_ready"):
            continue
        run_id = str(run.get("id", ""))
        payload = model.get("interactive_cfd_by_mesh", {}).get(run_id, {})
        if isinstance(payload, dict) and payload.get("actual_one_second"):
            return run_id, run
    raise ValueError("viewer has no validated data-ready run")


def _geometry(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "cells": copy.deepcopy(model["cells"]),
        "ports": copy.deepcopy(model["ports"]),
        "outline_mm": copy.deepcopy(model["outline_mm"]),
        "enclosure_mm": copy.deepcopy(model.get("enclosure_mm", {})),
    }


def _diameter_label(run: dict[str, Any], diameter: int) -> None:
    count = int(run.get("counts", {}).get("total_cells", 0))
    run["label"] = f"Ø{diameter} mm · {count:,}셀"
    run["label_en"] = f"Ø{diameter} mm · {count:,} cells"


def combine_models(
    legacy: dict[str, Any],
    exact_phi23: dict[str, Any],
    exact_phi25: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return a four-run model and its source-run mapping."""

    combined = copy.deepcopy(legacy)
    legacy_runs = combined.get("mesh_runs", [])
    if not isinstance(legacy_runs, list) or len(legacy_runs) < 2:
        raise ValueError("legacy viewer must contain the preserved Ø19 and Ø23 runs")

    legacy_specs = {"precision": 19, "single": 23}
    for run in legacy_runs:
        run_id = str(run.get("id", ""))
        if run_id not in legacy_specs:
            continue
        diameter = legacy_specs[run_id]
        run.update(
            {
                "comparison_key": f"legacy_reflected_phi{diameter}",
                "note": "보존된 기존 계산장을 강체 반사해 표시한 결과입니다.",
                "note_en": "Preserved legacy solution displayed with the documented rigid reflection.",
            }
        )
        _diameter_label(run, diameter)

    geometry_by_mesh = {
        "precision": _geometry(legacy),
        "single": _geometry(legacy),
    }
    spatial_transform_by_mesh: dict[str, Any] = {
        "precision": copy.deepcopy(legacy.get("spatial_transform")),
        "single": copy.deepcopy(legacy.get("spatial_transform")),
    }
    summary_by_mesh = {key: legacy.get("calculation_summary", "") for key in legacy_specs}
    note_by_mesh = {key: legacy.get("calculation_note", "") for key in legacy_specs}
    derived_by_mesh = {key: copy.deepcopy(legacy.get("derived", {})) for key in legacy_specs}
    inputs_by_mesh = {key: copy.deepcopy(legacy.get("inputs", {})) for key in legacy_specs}
    if isinstance(derived_by_mesh["single"].get("reynolds"), (int, float)):
        derived_by_mesh["single"]["reynolds"] *= 23.0 / 19.0
    # The preserved comparison model uses one global geometry object, which
    # was authored from the Ø19 reference.  Restore the old Ø23 port radius in
    # its per-run geometry without touching its coordinates or field values.
    for port in geometry_by_mesh["single"]["ports"]:
        port["radius"] = 23.0 / 2.0
    source_ids: dict[str, str] = {}
    additions = (
        ("exact_phi23", 23, exact_phi23),
        ("exact_phi25", 25, exact_phi25),
    )
    for new_id, diameter, source_model in additions:
        source_id, source_run = _ready_run(source_model)
        source_ids[new_id] = source_id
        run = copy.deepcopy(source_run)
        run.update(
            {
                "id": new_id,
                "comparison_key": f"c05_inlet_c19_outlet_phi{diameter}",
                "state_label": "계산·검증 완료",
                "state_label_en": "Calculation and Validation Complete",
                "note": "C05 상부 입수구·C19 하부 출수구의 정확 CAD로 새로 계산한 결과입니다.",
                "note_en": "Newly recomputed on the exact CAD with the inlet above C05 and outlet below C19.",
                "inlet_diameter_mm": diameter,
                "outlet_diameter_mm": diameter,
                "mesh_ready": True,
                "data_ready": True,
            }
        )
        _diameter_label(run, diameter)
        combined["mesh_runs"].append(run)
        for key in ("interactive_cfd_by_mesh", "convergence_by_mesh", "energy_balance_by_mesh", "mesh_wireframes"):
            source_bucket = source_model.get(key, {})
            if source_id not in source_bucket:
                raise ValueError(f"source viewer is missing {key}[{source_id!r}]")
            combined.setdefault(key, {})[new_id] = _rewrite_data_path(
                copy.deepcopy(source_bucket[source_id]), source_id, new_id
            )
        geometry_by_mesh[new_id] = _geometry(source_model)
        spatial_transform_by_mesh[new_id] = copy.deepcopy(source_model.get("spatial_transform"))
        summary_by_mesh[new_id] = source_model.get("calculation_summary", "")
        note_by_mesh[new_id] = source_model.get("calculation_note", "")
        derived_by_mesh[new_id] = copy.deepcopy(source_model.get("derived", {}))
        inputs_by_mesh[new_id] = copy.deepcopy(source_model.get("inputs", {}))

    combined["geometry_by_mesh"] = geometry_by_mesh
    combined["spatial_transform_by_mesh"] = spatial_transform_by_mesh
    combined["calculation_summary_by_mesh"] = summary_by_mesh
    combined["calculation_note_by_mesh"] = note_by_mesh
    combined["derived_by_mesh"] = derived_by_mesh
    combined["inputs_by_mesh"] = inputs_by_mesh
    combined["four_case_comparison"] = {
        "status": "FOUR_INDEPENDENT_RESULT_PAYLOADS_READY",
        "run_ids": ["precision", "single", "exact_phi23", "exact_phi25"],
        "legacy_results_preserved": True,
        "exact_geometry": "inlet above C05; outlet below C19",
    }
    return combined, source_ids


def _copy_published_data(source: Path, source_id: str, output: Path, target_id: str) -> None:
    source_data = source / "data" / source_id
    if not source_data.is_dir():
        raise FileNotFoundError(source_data)
    shutil.copytree(source_data, output / "data" / target_id)


def append_validated_run(
    viewer: Path,
    source_viewer: Path,
    *,
    run_id: str,
    label: str | None = None,
    label_en: str | None = None,
) -> dict[str, Any]:
    """Add an independently validated experiment without replacing existing runs.

    The source must already be published from a completed 1800 s calculation.
    CFD data and mesh assets are copied before the model is made visible.
    """
    from .cad_case_drawings import write_case_drawings

    if not re.fullmatch(r"[a-z][a-z0-9_]*", run_id):
        raise ValueError("run_id must be a lowercase filesystem-safe identifier")
    viewer = viewer.resolve()
    source_viewer = source_viewer.resolve()
    if viewer == source_viewer:
        raise ValueError("source and server viewer must be separate")
    model, _ = read_viewer_model(viewer)
    source_model, _ = read_viewer_model(source_viewer)
    if any(str(run.get("id")) == run_id for run in model.get("mesh_runs", [])):
        raise ValueError(f"run already exists: {run_id}")
    if not source_model.get("actual_solver_data") or source_model.get("validation_status") != "FROZEN_FLOW_VALIDATION_PASSED":
        raise ValueError("source viewer is not a validated CFD result")
    source_id, source_run = _ready_run(source_model)
    source_data = source_viewer / "data" / source_id
    if not source_data.is_dir() or not any(source_data.iterdir()):
        raise FileNotFoundError(source_data)
    referenced = dataset_paths(source_model["interactive_cfd_by_mesh"][source_id])
    if not referenced:
        raise ValueError("source CFD result has no published dataset files")
    for relative in referenced:
        path = (source_viewer / relative).resolve()
        if not path.is_relative_to(source_data) or not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(source_viewer / relative)
    for stem in ("mesh-data", "mesh-volume-data"):
        if not (source_viewer / f"{stem}.js").is_file():
            raise FileNotFoundError(source_viewer / f"{stem}.js")
    if (viewer / "data" / run_id).exists():
        raise FileExistsError(viewer / "data" / run_id)

    geometry = _geometry(source_model)
    ports = {str(port.get("id", "")).upper(): port for port in geometry["ports"]}
    if "IN" not in ports or "OUT" not in ports:
        raise ValueError("source geometry must identify IN and OUT ports")
    inlet_diameter = 2 * float(ports["IN"]["radius"])
    outlet_diameter = 2 * float(ports["OUT"]["radius"])
    if inlet_diameter <= 0 or outlet_diameter <= 0:
        raise ValueError("source geometry has an invalid port diameter")
    run = copy.deepcopy(source_run)
    run["id"] = run_id
    run["comparison_key"] = f"validated_{run_id}"
    run["mesh_ready"] = True
    run["data_ready"] = True
    run["inlet_diameter_mm"] = inlet_diameter
    run["outlet_diameter_mm"] = outlet_diameter
    diameter = inlet_diameter
    diameter_label = (
        f"Ø{inlet_diameter:g} / Ø{outlet_diameter:g} mm"
        if abs(inlet_diameter - outlet_diameter) > 1e-6
        else f"Ø{diameter:g} mm"
    )
    cells = int(run["counts"]["total_cells"])
    run["label"] = label or f"{diameter_label} · {cells:,}셀 · {run_id[-8:]}"
    run["label_en"] = label_en or (label if label is not None else f"{diameter_label} · {cells:,} cells · {run_id[-8:]}")
    model["mesh_runs"].append(run)

    for key in ("interactive_cfd_by_mesh", "convergence_by_mesh", "energy_balance_by_mesh", "mesh_wireframes"):
        source_bucket = source_model.get(key, {})
        if source_id not in source_bucket:
            raise ValueError(f"source viewer is missing {key}[{source_id!r}]")
        model.setdefault(key, {})[run_id] = _rewrite_data_path(
            copy.deepcopy(source_bucket[source_id]), source_id, run_id
        )
    for key, value in (
        ("geometry_by_mesh", geometry),
        ("spatial_transform_by_mesh", copy.deepcopy(source_model.get("spatial_transform"))),
        ("calculation_summary_by_mesh", source_model.get("calculation_summary", "")),
        ("calculation_note_by_mesh", source_model.get("calculation_note", "")),
        ("derived_by_mesh", copy.deepcopy(source_model.get("derived", {}))),
        ("inputs_by_mesh", copy.deepcopy(source_model.get("inputs", {}))),
    ):
        model.setdefault(key, {})[run_id] = value
    model.setdefault("four_case_comparison", {})["run_ids"] = [str(item["id"]) for item in model["mesh_runs"]]
    model["four_case_comparison"]["status"] = "VALIDATED_RESULT_PAYLOADS_READY"

    _copy_published_data(source_viewer, source_id, viewer, run_id)
    for stem in ("mesh-data", "mesh-volume-data"):
        shutil.copy2(source_viewer / f"{stem}.js", viewer / f"{stem}-{run_id}.js")
    source_cad = source_viewer / "cad"
    for view in ("top_XY", "side_XZ"):
        projection = source_cad / f"actual_CAD_{view}.png"
        if projection.is_file():
            shutil.copy2(projection, viewer / "cad" / f"actual_{run_id}_{view}.png")
    write_case_drawings(
        {"mesh_runs": [run], "geometry_by_mesh": {run_id: model["geometry_by_mesh"][run_id]}},
        viewer,
    )
    write_four_case_model(viewer, model)
    manifest = four_case_manifest(model, viewer)
    write_four_case_manifest(viewer, manifest)
    return manifest


def refresh_four_case_app(output: Path) -> str:
    """Write the current viewer code under a new cache-safe asset name."""

    from .viewer import JAVASCRIPT

    output = output.resolve()
    html_path = output / "results.html"
    app_asset = f"app.four-case-{hashlib.sha256(JAVASCRIPT.encode()).hexdigest()[:12]}.js"
    (output / app_asset).write_text(JAVASCRIPT, encoding="utf-8")
    (output / "app.js").write_text(JAVASCRIPT, encoding="utf-8")
    html, replacements = re.subn(
        r'<script src="[^"?]*app[^"?]*\.js(?:\?[^"}]*)?"></script>',
        f'<script src="{app_asset}"></script>',
        html_path.read_text(encoding="utf-8"),
        count=1,
    )
    if replacements != 1:
        raise ValueError("failed to update viewer app reference")
    html_path.write_text(html, encoding="utf-8")
    return app_asset


def publish_diameter_mesh_viewer(viewer: Path, mesh_sources: dict[str, Path]) -> None:
    """Attach each selected run's actual boundary and full-volume polyMesh.

    The Ø19 boundary and volume assets are kept intact; the mesh page alone
    applies its documented Y reflection. Newly solved diameter cases retain
    their native C05-top/C19-bottom coordinates.
    """

    from .mesh_viewer import CSS as MESH_CSS, JAVASCRIPT as MESH_JAVASCRIPT

    viewer = viewer.resolve()
    manifest = json.loads((viewer / "four_case_manifest.json").read_text(encoding="utf-8"))
    active_ids = [str(run["id"]) for run in manifest["runs"] if run.get("data_ready")]
    expected = ["precision", "exact_phi21", "exact_phi23", "exact_phi25"]
    if active_ids != expected or set(mesh_sources) != set(expected):
        raise ValueError(f"expected four ready diameter meshes {expected}, got {active_ids}")

    for run_id in active_ids:
        source = Path(mesh_sources[run_id]).resolve()
        for stem in ("mesh-data", "mesh-volume-data"):
            asset = source / f"{stem}.js"
            if not asset.is_file():
                raise FileNotFoundError(asset)
            shutil.copy2(asset, viewer / f"{stem}-{run_id}.js")

    html_path = viewer / "mesh.html"
    html = html_path.read_text(encoding="utf-8")
    if 'id="mesh-case-select"' not in html:
        marker = '<h1>메시 보기</h1><span id="count"></span>'
        if html.count(marker) != 1:
            raise ValueError("mesh page title marker missing")
        html = html.replace(
            marker,
            '<h1>메시 보기</h1><label class="mesh-case-control" for="mesh-case-select">'
            '결과 선택 <select id="mesh-case-select"></select></label><span id="count"></span>',
        )
    static_scripts = '  <script src="mesh-data.js"></script>\n  <script src="mesh-volume-data.js"></script>'
    if static_scripts in html:
        html = html.replace(static_scripts, '  <script src="four_case_data.js"></script>', 1)
    elif 'src="four_case_data.js"' not in html:
        raise ValueError("mesh page has no case-data script marker")
    html = re.sub(r'href="mesh\.css(?:\?[^\"]*)?"', 'href="mesh.css?v=diameter-mesh-2"', html)
    html = re.sub(r'src="mesh\.js(?:\?[^\"]*)?"', 'src="mesh.js?v=diameter-mesh-2"', html)
    html_path.write_text(html, encoding="utf-8")
    (viewer / "mesh.css").write_text(MESH_CSS, encoding="utf-8")
    (viewer / "mesh.js").write_text(MESH_JAVASCRIPT, encoding="utf-8")


def write_four_case_model(output: Path, combined: dict[str, Any]) -> str:
    """Write the selected result payload with a cache-safe model URL."""

    output = output.resolve()
    model_source = (
        "window.CFD_TWIN_MODEL = "
        + json.dumps(combined, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + ";\n"
    )
    model_asset = f"model.four-case-{hashlib.sha256(model_source.encode()).hexdigest()[:12]}.js"
    (output / model_asset).write_text(model_source, encoding="utf-8")
    (output / "model.js").write_text(model_source, encoding="utf-8")
    html_path = output / "results.html"
    html, replacements = re.subn(
        r'<script src="[^"?]*model[^"?]*\.js(?:\?[^"}]*)?"></script>',
        f'<script src="{model_asset}"></script>',
        html_path.read_text(encoding="utf-8"),
        count=1,
    )
    if replacements != 1:
        raise ValueError("failed to update viewer model reference")
    html_path.write_text(html, encoding="utf-8")
    (output / "viewer_manifest.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return model_asset


def refresh_four_case_labels(output: Path) -> str:
    """Relabel an assembled viewer without touching any CFD payload files."""
    model, _ = read_viewer_model(output)
    diameters = {"precision": 19, "single": 23, "exact_phi23": 23, "exact_phi25": 25}
    for run in model["mesh_runs"]:
        run_id = str(run.get("id", ""))
        if run_id in diameters:
            _diameter_label(run, diameters[run_id])
    asset = write_four_case_model(output, model)
    write_four_case_manifest(output, four_case_manifest(model, output))
    return asset


def refresh_diameter_comparison(
    viewer: Path, *, exact_phi21_viewer: Path | None = None
) -> dict[str, Any]:
    """Show one validated run per diameter; retain the older Ø23 payload archived.

    The Ø21 run is attached only after its independently gated viewer exists.
    Removing an option never deletes its CFD fields or packed frames.
    """
    viewer = viewer.resolve()
    model, _ = read_viewer_model(viewer)
    runs = model.get("mesh_runs", [])
    if not isinstance(runs, list):
        raise ValueError("mesh_runs is not a list")
    old_phi23 = next((run for run in runs if run.get("id") == "single"), None)
    if old_phi23 is not None:
        archived = model.setdefault("archived_mesh_runs", [])
        if not any(run.get("id") == "single" for run in archived):
            archived.append(copy.deepcopy(old_phi23))

    if exact_phi21_viewer is not None and not any(run.get("id") == "exact_phi21" for run in runs):
        source_model, _ = read_viewer_model(exact_phi21_viewer)
        source_id, source_run = _ready_run(source_model)
        new_id = "exact_phi21"
        new_run = copy.deepcopy(source_run)
        new_run.update({
            "id": new_id,
            "comparison_key": "c05_inlet_c19_outlet_phi21",
            "state_label": "계산·검증 완료",
            "state_label_en": "Calculation and Validation Complete",
            "note": "C05 상부 입수구·C19 하부 출수구의 정확 CAD로 새로 계산한 결과입니다.",
            "note_en": "Newly recomputed on the exact CAD with the inlet above C05 and outlet below C19.",
            "inlet_diameter_mm": 21,
            "outlet_diameter_mm": 21,
            "mesh_ready": True,
            "data_ready": True,
        })
        _diameter_label(new_run, 21)
        runs.append(new_run)
        for key in ("interactive_cfd_by_mesh", "convergence_by_mesh", "energy_balance_by_mesh", "mesh_wireframes"):
            source_bucket = source_model.get(key, {})
            if source_id not in source_bucket:
                raise ValueError(f"source viewer is missing {key}[{source_id!r}]")
            model.setdefault(key, {})[new_id] = _rewrite_data_path(
                copy.deepcopy(source_bucket[source_id]), source_id, new_id
            )
        for key, value in (
            ("geometry_by_mesh", _geometry(source_model)),
            ("spatial_transform_by_mesh", copy.deepcopy(source_model.get("spatial_transform"))),
            ("calculation_summary_by_mesh", source_model.get("calculation_summary", "")),
            ("calculation_note_by_mesh", source_model.get("calculation_note", "")),
            ("derived_by_mesh", copy.deepcopy(source_model.get("derived", {}))),
            ("inputs_by_mesh", copy.deepcopy(source_model.get("inputs", {}))),
        ):
            model.setdefault(key, {})[new_id] = value
        _copy_published_data(exact_phi21_viewer, source_id, viewer, new_id)

    active_ids = ("precision", "exact_phi21", "exact_phi23", "exact_phi25")
    model["mesh_runs"] = [
        next(run for run in runs if run.get("id") == run_id)
        for run_id in active_ids
        if any(run.get("id") == run_id for run in runs)
    ]
    model["four_case_comparison"] = {
        "status": "VALIDATED_DIAMETER_RESULTS_READY",
        "run_ids": [run["id"] for run in model["mesh_runs"]],
        "legacy_results_preserved": bool(model.get("archived_mesh_runs")),
        "archived_run_ids": [run["id"] for run in model.get("archived_mesh_runs", [])],
        "exact_geometry": "inlet above C05; outlet below C19",
    }
    write_four_case_model(viewer, model)
    manifest = four_case_manifest(model, viewer)
    write_four_case_manifest(viewer, manifest)
    return manifest


def assemble_four_case_viewer(
    legacy_viewer: Path,
    exact_phi23_viewer: Path,
    exact_phi25_viewer: Path,
    output: Path,
    *,
    cad_render_dirs: tuple[Path, Path] | None = None,
) -> dict[str, Any]:
    """Copy three published viewers into one portable four-result viewer."""

    from .viewer import _write_english_cad_drawings

    legacy_viewer = legacy_viewer.resolve()
    exact_phi23_viewer = exact_phi23_viewer.resolve()
    exact_phi25_viewer = exact_phi25_viewer.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to replace existing viewer: {output}")

    legacy, _ = read_viewer_model(legacy_viewer)
    phi23, _ = read_viewer_model(exact_phi23_viewer)
    phi25, _ = read_viewer_model(exact_phi25_viewer)
    combined, source_ids = combine_models(legacy, phi23, phi25)

    shutil.copytree(legacy_viewer, output)
    _copy_published_data(exact_phi23_viewer, source_ids["exact_phi23"], output, "exact_phi23")
    _copy_published_data(exact_phi25_viewer, source_ids["exact_phi25"], output, "exact_phi25")

    write_four_case_model(output, combined)
    refresh_four_case_app(output)
    (output / "viewer_manifest.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    source_cad = Path(__file__).with_name("assets") / "cad_reference"
    shutil.copytree(source_cad, output / "cad", dirs_exist_ok=True)
    _write_english_cad_drawings(output / "cad", source_cad)
    if cad_render_dirs:
        for diameter, render_dir in zip((23, 25), cad_render_dirs, strict=True):
            for view in ("top_XY", "side_XZ", "isometric"):
                source = render_dir / f"actual_CAD_{view}.png"
                if not source.is_file():
                    raise FileNotFoundError(source)
                shutil.copy2(source, output / "cad" / f"actual_phi{diameter}_{view}.png")

    manifest = four_case_manifest(combined, output)
    write_four_case_manifest(output, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble a four-result portable CFD viewer")
    parser.add_argument("--legacy-viewer", type=Path, required=True)
    parser.add_argument("--exact-phi23-viewer", type=Path, required=True)
    parser.add_argument("--exact-phi25-viewer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phi23-cad-renders", type=Path)
    parser.add_argument("--phi25-cad-renders", type=Path)
    args = parser.parse_args()
    renders = None
    if args.phi23_cad_renders or args.phi25_cad_renders:
        if not (args.phi23_cad_renders and args.phi25_cad_renders):
            parser.error("both CAD render directories are required")
        renders = (args.phi23_cad_renders, args.phi25_cad_renders)
    result = assemble_four_case_viewer(
        args.legacy_viewer,
        args.exact_phi23_viewer,
        args.exact_phi25_viewer,
        args.output,
        cad_render_dirs=renders,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
