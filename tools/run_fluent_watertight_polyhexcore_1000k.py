from __future__ import annotations

import json
import ast
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path


RUN_ID = "405282e95fe448c98e450806b2aafa02"
ROOT = Path(r"C:\Users\user\Desktop\액침냉각디지털트윈\YU_fluid")
RUN_DIR = ROOT / "build" / "imported_runs" / "runs" / RUN_ID
INPUT_STEP = RUN_DIR / "input" / "coolant.step"
FLUENT_RUN_DIR = Path(r"C:\Users\user\AppData\Local\YU_Fluent\runs") / RUN_ID
FLUENT_INPUT_STEP = FLUENT_RUN_DIR / "input" / "coolant.step"
STATUS_PATH = RUN_DIR / "status.json"
TRANSCRIPT_PATH = FLUENT_RUN_DIR / "logs" / "fluent_transcript.log"
MESH_PATH = RUN_DIR / "mesh" / "phi19_1000k_watertight_polyhexcore.msh.h5"
FLUENT_MESH_PATH = FLUENT_RUN_DIR / "mesh" / MESH_PATH.name
REPORT_PATH = RUN_DIR / "reports" / "mesh_quality_raw.txt"

TARGET_CELLS = 1_000_000
TARGET_TOLERANCE = 0.12
SURFACE_MAX_MM = 2.8
COOLANT_HEX_MAX_MM = 1.08
BATTERY_HEX_MAX_MM = 2.2
FIRST_LAYER_MM = 0.006
PRISM_LAYERS = 11
PRISM_GROWTH = 1.20


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(stage: str, state: str = "running", **extra) -> None:
    payload = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "backend": "ANSYS Fluent Meshing 2024 R1",
        "workflow": "Watertight Geometry / Poly-Hexcore",
        "state": state,
        "stage": stage,
        "updated_at_utc": now(),
        "target_cells": TARGET_CELLS,
        "target_tolerance_fraction": TARGET_TOLERANCE,
        "mesh_controls": {
            "surface_max_mm": SURFACE_MAX_MM,
            "sizing_method": "Region-based",
            "coolant_hex_max_cell_length_mm": COOLANT_HEX_MAX_MM,
            "battery_hex_max_cell_length_mm": BATTERY_HEX_MAX_MM,
            "first_layer_height_mm": FIRST_LAYER_MM,
            "prism_layers": PRISM_LAYERS,
            "prism_growth": PRISM_GROWTH,
        },
        "quality_gates": {
            "fluent_max_skewness": 0.85,
            "fluent_cells_above_0_90": 0,
            "fluent_min_orthogonal_quality": 0.15,
            "openfoam_internal_face_skewness": 4.0,
            "openfoam_boundary_face_skewness": 5.0,
            "openfoam_max_non_orthogonality_deg": 65.0,
        },
    }
    payload.update(extra)
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def set_args(task, values: dict) -> None:
    try:
        task.Arguments.set_state(values)
    except AttributeError:
        task.Arguments.setState(values)


def get_args(task):
    try:
        return task.Arguments.get_state()
    except Exception:
        try:
            return task.Arguments()
        except Exception:
            return None


def read_value(obj):
    for method in ("get_state", "getState"):
        try:
            return getattr(obj, method)()
        except Exception:
            pass
    return obj()


def execute(task, name: str) -> None:
    write_status(name)
    result = task.Execute()
    if result is False:
        raise RuntimeError(f"Workflow task returned False: {name}")


def capture(label: str, fn, *args) -> str:
    try:
        return f"[{label}]\n{fn(*args)}\n"
    except Exception as exc:
        return f"[{label}] ERROR: {type(exc).__name__}: {exc}\n"


def main() -> int:
    for directory in (RUN_DIR / "logs", RUN_DIR / "mesh", RUN_DIR / "reports"):
        directory.mkdir(parents=True, exist_ok=True)
    for directory in (FLUENT_RUN_DIR / "input", FLUENT_RUN_DIR / "logs", FLUENT_RUN_DIR / "mesh"):
        directory.mkdir(parents=True, exist_ok=True)
    if not INPUT_STEP.is_file():
        raise FileNotFoundError(INPUT_STEP)
    if not FLUENT_INPUT_STEP.is_file():
        shutil.copy2(INPUT_STEP, FLUENT_INPUT_STEP)

    write_status("launch_fluent")
    from ansys.fluent.core import launch_fluent

    session = None
    try:
        session = launch_fluent(
            mode="meshing",
            precision="double",
            processor_count=2,
            product_version="24.1.0",
            cwd=str(FLUENT_RUN_DIR),
            cleanup_on_exit=True,
            start_transcript=False,
        )
        session.transcript.start(TRANSCRIPT_PATH)
        workflow = session.workflow
        workflow.InitializeWorkflow(WorkflowType="Watertight Geometry")

        import_geometry = workflow.TaskObject["Import Geometry"]
        set_args(import_geometry, {"FileName": str(FLUENT_INPUT_STEP), "LengthUnit": "mm"})
        execute(import_geometry, "import_geometry")

        add_sizing = workflow.TaskObject["Add Local Sizing"]
        add_sizing.AddChildToTask()
        execute(add_sizing, "add_local_sizing")

        surface = workflow.TaskObject["Generate the Surface Mesh"]
        set_args(surface, {"CFDSurfaceMeshControls": {"MaxSize": SURFACE_MAX_MM}})
        execute(surface, "generate_surface_mesh")

        describe = workflow.TaskObject["Describe Geometry"]
        describe.UpdateChildTasks(SetupTypeChanged=False)
        set_args(describe, {"SetupType": "The geometry consists of only fluid regions with no voids"})
        describe.UpdateChildTasks(SetupTypeChanged=True)
        execute(describe, "describe_geometry")

        update_boundaries = workflow.TaskObject["Update Boundaries"]
        (RUN_DIR / "reports" / "update_boundaries_arguments.json").write_text(
            json.dumps(get_args(update_boundaries), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        execute(update_boundaries, "update_boundaries")

        update_regions = workflow.TaskObject["Update Regions"]
        (RUN_DIR / "reports" / "update_regions_arguments.json").write_text(
            json.dumps(get_args(update_regions), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        execute(update_regions, "update_regions")

        region_state = read_value(session.meshing.UpdateRegions.create_instance())
        region_names = region_state["RegionCurrentList"]
        if isinstance(region_names, str):
            region_names = ast.literal_eval(region_names)
        else:
            # PyFluent 0.37 (required by Fluent 2024 R1) returns a protobuf
            # RepeatedScalarContainer here.  Materialize it before validating
            # or writing the immutable sizing diagnostic.
            region_names = list(region_names)
        if len(region_names) != 24:
            raise RuntimeError(f"Expected 24 mesh regions, found {len(region_names)}: {region_names}")
        region_sizes = [str(COOLANT_HEX_MAX_MM)] + [str(BATTERY_HEX_MAX_MM)] * 23
        (RUN_DIR / "reports" / "region_sizing.json").write_text(
            json.dumps(
                {
                    "ordered_regions": region_names,
                    "hex_max_cell_length_mm": region_sizes,
                    "allocation": "main coolant refined; 23 battery interiors coarsened",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        add_layers = workflow.TaskObject["Add Boundary Layers"]
        add_layers.AddChildToTask()
        add_layers.InsertCompoundChildTask()
        layer_child = workflow.TaskObject["smooth-transition_1"]
        battery_zones = [
            f"dead{i}-freeparts-open-cascade-step-translator-7.9-2:{i + 1}"
            for i in range(23)
        ]
        set_args(
            layer_child,
            {
                "BLControlName": "battery-prisms",
                "OffsetMethodType": "uniform",
                "NumberOfLayers": PRISM_LAYERS,
                "FirstHeight": FIRST_LAYER_MM,
                "Rate": PRISM_GROWTH,
                "ZoneSelectionList": battery_zones,
            },
        )
        set_args(add_layers, {})
        execute(layer_child, "add_boundary_layers")

        volume = workflow.TaskObject["Generate the Volume Mesh"]
        set_args(
            volume,
            {
                "VolumeFill": "poly-hexcore",
                "SizingMethod": "Region-based",
                "RegionBasedPreferences": True,
                "RegionHexMaxCellLengthList": region_sizes,
                "VolumeFillControls": {
                    "HexMaxCellLength": COOLANT_HEX_MAX_MM,
                    "PeelLayers": 3,
                    "BufferLayers": 3,
                    "GrowthRate": 1.2,
                },
                "VolumeMeshPreferences": {
                    "UseSizeField": "Yes",
                    "Avoid1_8Transition": "Yes",
                    "QualityMethod": "Orthogonal",
                    "QualityWarningLimit": 0.15,
                },
                "MeshDeadRegions": False,
            },
        )
        (RUN_DIR / "reports" / "workflow_volume_arguments.json").write_text(
            json.dumps(get_args(volume), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        execute(volume, "generate_volume_mesh")

        write_status("improve_volume_mesh")
        improve_result = session.meshing.ImproveVolumeMesh(
            QualityMethod="Orthogonal",
            CellQualityLimit=0.15,
            VMImprovePreferences={"VIQualityIterations": 10},
        )
        if improve_result is False:
            raise RuntimeError("Fluent ImproveVolumeMesh returned False")

        write_status("mesh_quality_checks")
        quality_text = ""
        quality_text += capture("mesh-size", session.tui.report.mesh_size)
        quality_text += capture("mesh-check", session.tui.mesh.check_mesh)
        quality_text += capture("quality-method-skewness", session.tui.report.quality_method, "skewness")
        quality_text += capture("cell-quality-skewness", session.tui.report.cell_quality_limits)
        quality_text += capture(
            "quality-method-orthogonal", session.tui.report.quality_method, "orthogonal-quality"
        )
        quality_text += capture("cell-quality-orthogonal", session.tui.report.cell_quality_limits)
        REPORT_PATH.write_text(quality_text, encoding="utf-8")

        write_status("write_mesh")
        session.tui.file.write_mesh(str(FLUENT_MESH_PATH))
        shutil.copy2(FLUENT_MESH_PATH, MESH_PATH)
        shutil.copy2(TRANSCRIPT_PATH, RUN_DIR / "logs" / TRANSCRIPT_PATH.name)
        write_status(
            "mesh_written_pending_gate_parse",
            state="mesh_complete",
            mesh_path=str(MESH_PATH),
            quality_report=str(REPORT_PATH),
        )
        return 0
    except BaseException as exc:
        failure = RUN_DIR / "logs" / "failure.txt"
        failure.write_text(traceback.format_exc(), encoding="utf-8")
        write_status(
            "failed",
            state="failed",
            error_type=type(exc).__name__,
            error=str(exc),
            failure_log=str(failure),
        )
        return 1
    finally:
        if session is not None:
            try:
                session.transcript.stop()
            except Exception:
                pass
            try:
                session.exit()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
