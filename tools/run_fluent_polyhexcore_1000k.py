from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path


RUN_ID = "f6ccbe15acec41658026a60c26c47b39"
ROOT = Path(r"C:\Users\user\Desktop\액침냉각디지털트윈\YU_fluid")
RUN_DIR = ROOT / "build" / "imported_runs" / "runs" / RUN_ID
INPUT_STL = RUN_DIR / "input" / "phi19_closed_assembly.stl"
FLUENT_RUN_DIR = Path(r"C:\Users\user\AppData\Local\YU_Fluent\runs") / RUN_ID
FLUENT_INPUT_STL = FLUENT_RUN_DIR / "input" / "phi19_closed_assembly.stl"
STATUS_PATH = RUN_DIR / "status.json"
TRANSCRIPT_PATH = FLUENT_RUN_DIR / "logs" / "fluent_transcript.log"
MESH_PATH = RUN_DIR / "mesh" / "phi19_1000k_polyhexcore.msh.h5"
FLUENT_MESH_PATH = FLUENT_RUN_DIR / "mesh" / "phi19_1000k_polyhexcore.msh.h5"
REPORT_PATH = RUN_DIR / "reports" / "mesh_quality_raw.txt"

TARGET_CELLS = 1_000_000
TARGET_TOLERANCE = 0.12
SURFACE_MIN_M = 0.00040
SURFACE_MAX_M = 0.00180
HEX_MAX_M = 0.00180
FIRST_LAYER_M = 0.000006
PRISM_LAYERS = 11
PRISM_GROWTH = 1.20


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(stage: str, state: str = "running", **extra) -> None:
    payload = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "backend": "ANSYS Fluent Meshing 2024 R1",
        "workflow": "Fault-tolerant Meshing / Poly-Hexcore",
        "state": state,
        "stage": stage,
        "updated_at_utc": now(),
        "target_cells": TARGET_CELLS,
        "target_tolerance_fraction": TARGET_TOLERANCE,
        "mesh_controls": {
            "surface_min_mm": SURFACE_MIN_M * 1000.0,
            "surface_max_mm": SURFACE_MAX_M * 1000.0,
            "hex_max_cell_length_mm": HEX_MAX_M * 1000.0,
            "first_layer_height_mm": FIRST_LAYER_M * 1000.0,
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


def execute(task, name: str) -> None:
    write_status(name)
    result = task.Execute()
    if result is False:
        raise RuntimeError(f"Workflow task returned False: {name}")


def task_or_none(workflow, name: str):
    try:
        return workflow.TaskObject[name]
    except Exception:
        return None


def capture(label: str, fn, *args):
    try:
        value = fn(*args)
        return f"[{label}]\n{value}\n"
    except Exception as exc:
        return f"[{label}] ERROR: {type(exc).__name__}: {exc}\n"


def main() -> int:
    for directory in (RUN_DIR / "logs", RUN_DIR / "mesh", RUN_DIR / "reports"):
        directory.mkdir(parents=True, exist_ok=True)
    for directory in (
        FLUENT_RUN_DIR / "input",
        FLUENT_RUN_DIR / "logs",
        FLUENT_RUN_DIR / "mesh",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    if not INPUT_STL.is_file():
        raise FileNotFoundError(INPUT_STL)
    if not FLUENT_INPUT_STL.is_file():
        shutil.copy2(INPUT_STL, FLUENT_INPUT_STL)

    write_status("launch_fluent")
    from ansys.fluent.core import launch_fluent

    session = None
    try:
        session = launch_fluent(
            mode="meshing",
            precision="double",
            processor_count=6,
            product_version="24.1.0",
            cwd=str(FLUENT_RUN_DIR),
            cleanup_on_exit=True,
            start_transcript=False,
        )
        session.transcript.start(TRANSCRIPT_PATH)
        workflow = session.workflow
        workflow.InitializeWorkflow(WorkflowType="Fault-tolerant Meshing")

        session.PartManagement.InputFileChanged(
            FilePath=str(FLUENT_INPUT_STL),
            PartPerBody=True,
            PrefixParentName=False,
            RemoveEmptyParts=True,
            IgnoreSolidNames=False,
            FileLengthUnit="m",
        )
        session.PMFileManagement.FileManager.LoadFiles()
        import_task = workflow.TaskObject["Import CAD and Part Management"]
        set_args(
            import_task,
            {
                "Context": 0,
                "CreateObjectPer": "One per part",
                "FMDFileName": str(FLUENT_INPUT_STL),
                "FileLoaded": "yes",
                "ObjectSetting": "DefaultObjectSetting",
                "Options": {"Line": False, "Solid": False, "Surface": False},
            },
        )
        execute(import_task, "import_cad_and_part_management")

        describe = workflow.TaskObject["Describe Geometry and Flow"]
        set_args(
            describe,
            {
                "AddEnclosure": "No",
                "CloseCaps": "No",
                "FlowType": "Internal flow through the object",
            },
        )
        describe.UpdateChildTasks(SetupTypeChanged=False)
        execute(describe, "describe_geometry_and_flow")

        identify = workflow.TaskObject["Identify Regions"]
        fluid_region_args = {
            "MaterialPointsName": "fluid-region-1",
            "SelectionType": "zone",
            "X": 0.005,
            "Y": 0.005,
            "Z": 0.041,
            "ZoneSelectionList": ["outerdomain.1"],
        }
        set_args(identify, fluid_region_args)
        identify.AddChildToTask()
        identify.InsertCompoundChildTask()
        fluid_region = task_or_none(workflow, "fluid-region-1")
        if fluid_region is None:
            raise RuntimeError("Identify Regions did not create fluid-region-1")
        set_args(fluid_region, fluid_region_args)
        set_args(identify, {})
        execute(fluid_region, "identify_fluid_region")

        update_regions = task_or_none(workflow, "Update Region Settings")
        if update_regions is not None:
            object_regions = ["outerdomain"] + [f"battery_c{i:02d}" for i in range(1, 24)]
            all_regions = object_regions + ["fluid-region-1"]
            set_args(
                update_regions,
                {
                    "AllRegionFilterCategories": ["2"] * len(object_regions) + ["1"],
                    "AllRegionLeakageSizeList": ["none"] * len(all_regions),
                    "AllRegionLinkedConstructionSurfaceList": ["n/a"] * len(object_regions) + ["no"],
                    "AllRegionMeshMethodList": ["none"] * len(object_regions) + ["wrap"],
                    "AllRegionNameList": all_regions,
                    "AllRegionOversetComponenList": ["no"] * len(all_regions),
                    "AllRegionSourceList": ["object"] * len(object_regions) + ["mpt"],
                    "AllRegionTypeList": ["void"] * len(object_regions) + ["fluid"],
                    "AllRegionVolumeFillList": ["none"] * len(object_regions) + ["hexcore"],
                    "FilterCategory": "Identified Regions",
                    "OldRegionLeakageSizeList": [""],
                    "OldRegionMeshMethodList": ["wrap"],
                    "OldRegionNameList": ["fluid-region-1"],
                    "OldRegionOversetComponenList": ["no"],
                    "OldRegionTypeList": ["fluid"],
                    "OldRegionVolumeFillList": ["hexcore"],
                    "RegionLeakageSizeList": [""],
                    "RegionMeshMethodList": ["wrap"],
                    "RegionNameList": ["fluid-region-1"],
                    "RegionOversetComponenList": ["no"],
                    "RegionTypeList": ["fluid"],
                    "RegionVolumeFillList": ["hexcore"],
                },
            )
            execute(update_regions, "update_region_settings")

        choose = workflow.TaskObject["Choose Mesh Control Options"]
        set_args(
            choose,
            {
                "ReadOrCreate": "Create new",
                "CreationMethod": "Default",
                "ViewOption": "Separate tasks",
                "GlobalMin": SURFACE_MIN_M,
                "GlobalMax": SURFACE_MAX_M,
                "GlobalGrowthRate": 1.20,
            },
        )
        execute(choose, "choose_mesh_control_options")

        surface = workflow.TaskObject["Generate the Surface Mesh"]
        set_args(
            surface,
            {
                "SurfaceQuality": 0.70,
                "GlobalMin": SURFACE_MIN_M,
                "SeparateSurface": "No",
                "AdvancedOptions": False,
                "SaveSurfaceMesh": True,
            },
        )
        execute(surface, "generate_surface_mesh")

        for optional_name in ("Update Boundaries",):
            optional_task = task_or_none(workflow, optional_name)
            if optional_task is not None:
                execute(optional_task, optional_name.lower().replace(" ", "_"))

        battery_labels = [f"battery_c{i:02d}" for i in range(1, 24)]
        add_layers = task_or_none(workflow, "Add Boundary Layers")
        if add_layers is not None:
            layer_args = {
                "AddChild": "yes",
                "BLControlName": "battery-prisms",
                "OffsetMethodType": "uniform",
                "NumberOfLayers": PRISM_LAYERS,
                "FirstHeight": FIRST_LAYER_M,
                "Rate": PRISM_GROWTH,
                "BlLabelList": battery_labels,
            }
            set_args(add_layers, layer_args)
            try:
                add_layers.AddChildAndUpdate(DeferUpdate=False)
            except Exception:
                add_layers.AddChildToTask()
                add_layers.InsertCompoundChildTask()
            battery_prisms = task_or_none(workflow, "battery-prisms")
            if battery_prisms is not None:
                execute(battery_prisms, "add_battery_prism_layers")
            set_args(add_layers, {})

        volume = workflow.TaskObject["Generate the Volume Mesh"]
        (RUN_DIR / "reports" / "workflow_volume_arguments.json").write_text(
            json.dumps(get_args(volume), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        execute(volume, "generate_volume_mesh")

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
        error_path = RUN_DIR / "logs" / "failure.txt"
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        write_status(
            "failed",
            state="failed",
            error_type=type(exc).__name__,
            error=str(exc),
            failure_log=str(error_path),
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
