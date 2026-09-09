from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from .config import ResolvedConfig


FIGURE_CONTRACTS: tuple[dict[str, object], ...] = (
    {"id": 1, "title": "3D geometry", "required": ["cad_surface", "patch_labels"], "time_mode": "geometry"},
    {"id": 2, "title": "Mesh structure", "required": ["volume_mesh", "surface_mesh", "mesh_quality"], "time_mode": "mesh"},
    {"id": 3, "title": "Single mesh quality", "required": ["single_mesh_target", "mesh_quality", "mesh_topology"], "time_mode": "mesh"},
    {"id": 4, "title": "Cell surface temperature", "required": ["per_cell_time_series"], "time_mode": "style_adaptation"},
    {
        "id": 6,
        "title": "Fluid temperature and velocity",
        "required": ["fluid_temperature_field", "fluid_velocity_field"],
        "required_panels": ["fluid_temperature", "fluid_velocity"],
        "time_mode": "snapshot_and_extension_playback",
    },
    {"id": 7, "title": "C01-C23 temperature curves", "required": ["per_cell_time_series", "overall_tmax"], "time_mode": "style_adaptation_live"},
    {"id": 8, "title": "1800 s temperature", "required": ["fluid_temperature_field", "solid_temperature_field"], "time_mode": "thermal_transient"},
    {"id": 9, "title": "Temperature-coloured flow", "required": ["velocity_field", "temperature_field", "particle_seed"], "time_mode": "paper_snapshot_and_labelled_animation"},
    {"id": 10, "title": "Velocity-coloured streamlines", "required": ["velocity_field", "streamline_seed"], "time_mode": "paper_snapshot_and_labelled_animation"},
    {"id": 11, "title": "XY velocity section", "required": ["velocity_field", "z_plane"], "plane": "XY", "time_mode": "paper_snapshot_optional_playback"},
    {"id": 12, "title": "ZX velocity section", "required": ["velocity_field", "y_plane"], "plane": "ZX", "time_mode": "paper_snapshot_optional_playback"},
    {"id": 13, "title": "3D battery and vertical temperature", "required": ["solid_temperature_field", "fluid_temperature_field", "zx_plane"], "time_mode": "paper_snapshot_thermal_playback"},
    {
        "id": 14,
        "title": "Fluid and solid XY temperature",
        "required": ["fluid_temperature_field", "solid_temperature_field", "xy_plane", "cell_ids"],
        "required_regions": ["fluid", "solid"],
        "plane": "XY",
        "time_mode": "paper_snapshot_thermal_playback",
    },
)


CSV_SCHEMAS: dict[str, tuple[str, ...]] = {
    "telemetry.csv": (
        "time_s",
        "state",
        "temperature_status",
        "battery_tmin_K",
        "battery_tavg_K",
        "battery_tmax_K",
        "coolant_tin_K",
        "coolant_tout_K",
        "velocity_avg_m_s",
        "velocity_max_m_s",
        "pressure_drop_Pa",
        "mass_imbalance_fraction",
        "energy_imbalance_fraction",
    ),
    "per_cell_time_series.csv": (
        "time_s",
        "cell_id",
        "surface_tmin_K",
        "surface_tavg_K",
        "surface_tmax_K",
        "heat_flux_avg_W_m2",
        "value_status",
    ),
    "residuals.csv": ("iteration", "simulation_time_s", "region", "field", "initial_residual", "final_residual"),
    "energy_balance.csv": ("time_s", "generated_W", "removed_W", "stored_W", "imbalance_fraction"),
}


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _write_csv_header(path: Path, fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerow(fields)
    os.replace(temporary, path)


def figure_catalog() -> list[dict[str, object]]:
    return [
        {
            **contract,
            "status": "GEOMETRY_READY" if contract["id"] == 1 else "AWAITING_SOLVER_FIELDS",
            "synthetic_data": False,
        }
        for contract in FIGURE_CONTRACTS
    ]


def write_result_contract(config: ResolvedConfig, output_dir: str | Path) -> dict[str, object]:
    output = Path(output_dir).resolve()
    figures = figure_catalog()
    for filename, fields in CSV_SCHEMAS.items():
        _write_csv_header(output / filename, fields)
    for figure in figures:
        _atomic_write(
            output / "figures" / f'figure_{int(figure["id"]):02d}.json',
            json.dumps(figure, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    manifest = {
        "schema_version": 1,
        "case_name": config.case_name,
        "state": "Created",
        "data_status": "AWAITING_SOLVER_FIELDS",
        "synthetic_data": False,
        "figure_5_status": "EXCLUDED_BY_USER",
        "figure_ids": [item["id"] for item in figures],
        "cell_ids": [f"C{index:02d}" for index in range(1, config.cells.count + 1)],
        "temperature_storage_unit": "K",
        "resolved_input_sha256": config.resolved_input_sha256,
        "geometry_sha256": config.geometry_sha256,
        "csv_schemas": {name: list(fields) for name, fields in CSV_SCHEMAS.items()},
    }
    _atomic_write(output / "result_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    _atomic_write(output / "events.jsonl", "")
    return manifest


def update_figure_status(
    output_dir: str | Path,
    figure_id: int,
    status: str,
    *,
    artifacts: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    output = Path(output_dir).resolve()
    path = output / "figures" / f"figure_{figure_id:02d}.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    figure = json.loads(path.read_text(encoding="utf-8"))
    figure["status"] = status
    figure["artifacts"] = artifacts or []
    figure["synthetic_data"] = False
    if metadata:
        figure["metadata"] = metadata
    _atomic_write(path, json.dumps(figure, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return figure
