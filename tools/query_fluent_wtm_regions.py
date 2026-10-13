from __future__ import annotations

import json
import shutil
import ast
from pathlib import Path

from ansys.fluent.core import launch_fluent


ROOT = Path(r"C:\Users\user\Desktop\액침냉각디지털트윈\YU_fluid")
STEP = ROOT / "src" / "cfd_twin" / "assets" / "verified_cad_phi23" / "coolant.step"
STAGE = Path(r"C:\Users\user\AppData\Local\YU_Fluent\region_query")
STAGED_STEP = STAGE / "coolant.step"
OUT = ROOT / "build" / "experiments" / "phi19_mesh_independence_800_1000k_yplus_v12_20261008" / "diagnostic_attempts" / "wtm_region_query.json"


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
    try:
        return obj()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


STAGE.mkdir(parents=True, exist_ok=True)
shutil.copy2(STEP, STAGED_STEP)
session = launch_fluent(
    mode="meshing",
    precision="double",
    processor_count=2,
    product_version="24.1.0",
    cwd=str(STAGE),
    cleanup_on_exit=True,
)
try:
    workflow = session.workflow
    workflow.InitializeWorkflow(WorkflowType="Watertight Geometry")
    import_geometry = workflow.TaskObject["Import Geometry"]
    set_args(import_geometry, {"FileName": str(STAGED_STEP), "LengthUnit": "mm"})
    import_geometry.Execute()
    local = workflow.TaskObject["Add Local Sizing"]
    local.AddChildToTask()
    local.Execute()
    surface = workflow.TaskObject["Generate the Surface Mesh"]
    set_args(surface, {"CFDSurfaceMeshControls": {"MaxSize": 2.8}})
    surface.Execute()
    describe = workflow.TaskObject["Describe Geometry"]
    describe.UpdateChildTasks(SetupTypeChanged=False)
    set_args(describe, {"SetupType": "The geometry consists of only fluid regions with no voids"})
    describe.UpdateChildTasks(SetupTypeChanged=True)
    describe.Execute()
    workflow.TaskObject["Update Boundaries"].Execute()
    workflow.TaskObject["Update Regions"].Execute()

    before_update = read_value(session.meshing.UpdateRegions.create_instance())
    current_names = before_update["RegionCurrentList"]
    current_types = before_update["RegionCurrentTypeList"]
    if isinstance(current_names, str):
        current_names = ast.literal_eval(current_names)
    if isinstance(current_types, str):
        current_types = ast.literal_eval(current_types)
    update_result = None
    after_update = read_value(session.meshing.UpdateRegions.create_instance())

    volume_task = workflow.TaskObject["Generate the Volume Mesh"]
    volume_sizing_result = None
    volume_sizing_error = None
    direct_volume_state = None
    direct_volume_error = None
    try:
        set_args(volume_task, {"VolumeFill": "poly-hexcore"})
        volume_task.UpdateChildTasks(VolumeFillChanged=True)
        set_args(volume_task, {"SizingMethod": "Region-based"})
        volume_task.UpdateChildTasks(SizingMethodChanged=True)
        set_args(
            volume_task,
            {
                "RegionBasedPreferences": True,
                "RegionHexNameList": current_names,
                "RegionHexMaxCellLengthList": ["1.08"] + ["2.2"] * (len(current_names) - 1),
            },
        )
        volume_sizing_result = get_args(volume_task)
    except Exception as exc:
        volume_sizing_error = f"{type(exc).__name__}: {exc}"

    try:
        direct_volume = session.meshing.GenerateTheVolumeMeshWTM.create_instance()
        direct_volume.VolumeFill.set_state("poly-hexcore")
        direct_volume.SizingMethod.set_state("Region-based")
        direct_volume.RegionBasedPreferences.set_state(True)
        direct_volume.RegionHexNameList.set_state(current_names)
        direct_volume.RegionHexMaxCellLengthList.set_state(
            ["1.08"] + ["2.2"] * (len(current_names) - 1)
        )
        direct_volume_state = read_value(direct_volume)
    except Exception as exc:
        direct_volume_error = f"{type(exc).__name__}: {exc}"

    region_data = session.meshing.GlobalSettings.FTMRegionData
    keys = [name for name in dir(region_data) if name.startswith("AllRegion")]
    payload = {
        "update_result": update_result,
        "before_update": before_update,
        "after_update": after_update,
        "volume_sizing_result": volume_sizing_result,
        "volume_sizing_error": volume_sizing_error,
        "direct_volume_state": direct_volume_state,
        "direct_volume_error": direct_volume_error,
        "ftm_region_data": {name: read_value(getattr(region_data, name)) for name in keys},
    }
    for command_name in ("UpdateRegionSettings", "UpdateRegions", "GenerateTheVolumeMeshWTM"):
        command = getattr(session.meshing, command_name)
        instance = command.create_instance()
        payload[command_name] = {
            "state": read_value(instance),
            "static_info": command._get_static_info(),
        }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
finally:
    session.exit()
