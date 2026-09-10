"""CAD 표면으로부터 계산 가능한 OpenFOAM 다중영역 메시를 만든다.

핵심 순서는 blockMesh 배경격자 → snappyHexMesh 곡면 적합/경계층 →
배터리 코어 refineMesh → zone 검증 → coolant/batterySolid 영역 분리다.
생성 목표 셀 수는 참고값일 뿐이며, 최종 보고서에는 polyMesh에서 다시 센
실제 셀 수와 요소 종류만 기록한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import ResolvedConfig
from .errors import CfdTwinError
from .geometry import cell_placements
from .openfoam_case import write_openfoam_solver_bundle


# 배터리 STL 이름과 C01~C23 ID가 CAD, zone, 계면, UI에서 동일해야 한다.
EXPECTED_BATTERY_FILES = tuple(
    f"battery_C{index:02d}.stl" for index in range(1, 24)
)
DEFAULT_MESH_PROFILE = "single"
SUPPORTED_LEVELS = (DEFAULT_MESH_PROFILE,)
DEFAULT_MPI_RANKS = 10
DEFAULT_MESH_MPI_RANKS = 1
# 10은 요청 layer 수다. snappyHexMesh가 품질을 위해 국부적으로 층을
# 종료할 수 있으므로 실제 coverage와 thickness는 별도 gate로 재검증한다.
PRISM_LAYER_COUNT = 10
MINIMUM_LAYER_COVERAGE = 0.75
MINIMUM_WANTED_THICKNESS_FRACTION = 0.45
BATTERY_CORE_REFINEMENT_RADIUS_M = 0.004
BATTERY_CORE_END_CLEARANCE_M = 0.006
MAXIMUM_INTERFACE_AREA_RELATIVE_ERROR = 0.06

# Reference mesh supplied for the new single-run workflow.  These are targets,
# not labels to be applied to an unexecuted mesh.  The generated validation
# report compares the actual polyMesh counts against this contract.
SINGLE_MESH_TARGET_COUNTS = {
    "total_cells": 207_390,
    "solid_cells": 70_840,
    "fluid_cells": 136_550,
    "points": 221_878,
}


@dataclass(frozen=True)
class MeshPreset:
    target_cells: int
    background_cell_m: float
    surface_min_level: int
    surface_max_level: int
    battery_inside_level: int
    first_layer_thickness_m: float
    target_solid_cells: int | None = None
    target_fluid_cells: int | None = None
    target_points: int | None = None


MESH_PRESETS = {
    # One default calculation replaces the former multi-level workflow.  A
    # coarser background controls coolant count while level-1 battery interiors
    # retain the solid resolution in the supplied 207,390-cell reference.  The
    # mesh must still pass the generated count/composition gate after execution.
    "single": MeshPreset(
        SINGLE_MESH_TARGET_COUNTS["total_cells"],
        0.0050,
        0,
        1,
        1,
        14e-6,
        SINGLE_MESH_TARGET_COUNTS["solid_cells"],
        SINGLE_MESH_TARGET_COUNTS["fluid_cells"],
        SINGLE_MESH_TARGET_COUNTS["points"],
    ),
}


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _foam_header(object_name: str, class_name: str = "dictionary") -> str:
    return f'''FoamFile
{{
    format      ascii;
    class       {class_name};
    object      {object_name};
}}

'''


def _decompose_par_dict() -> str:
    return _foam_header("decomposeParDict") + f'''numberOfSubdomains {DEFAULT_MPI_RANKS};

method scotch;

distributed no;

roots ();
'''


def _mesh_quality_dict() -> str:
    return _foam_header("meshQualityDict") + '''#includeEtc "caseDicts/meshQualityDict"

// These are the same acceptance limits used during snappy layer insertion.
// In particular, a split CHT interface is a boundary face and is therefore
// governed by maxBoundarySkewness, not the generic checkMesh warning at 4.
maxNonOrtho 75;
maxBoundarySkewness 20;
maxInternalSkewness 4;
minVol 1e-20;
minTetQuality -1e30;
minDeterminant 0.0001;
minFaceWeight 0.001;
minVolRatio 0.001;
'''


def _fmt(value: float) -> str:
    return f"{value:.12g}"


def _bounds(config: ResolvedConfig) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """형상 바깥 5 mm 여유를 둔 blockMesh 배경 박스 범위를 반환한다."""

    margin = 0.005
    return (
        (-margin, -margin, -config.ports.external_length_m - margin),
        (
            config.enclosure.width_x_m + margin,
            config.enclosure.length_y_m + margin,
            config.enclosure.height_z_m + config.ports.external_length_m + margin,
        ),
    )


def _block_mesh_dict(config: ResolvedConfig, preset: MeshPreset) -> tuple[str, tuple[int, int, int]]:
    """명목 5 mm 육면체 배경격자의 blockMeshDict를 생성한다."""

    lower, upper = _bounds(config)
    counts = tuple(
        max(1, math.ceil((hi - lo) / preset.background_cell_m))
        for lo, hi in zip(lower, upper, strict=True)
    )
    lx, ly, lz = lower
    ux, uy, uz = upper
    nx, ny, nz = counts
    return (
        _foam_header("blockMeshDict")
        + f'''scale 1;

vertices
(
    ({_fmt(lx)} {_fmt(ly)} {_fmt(lz)})
    ({_fmt(ux)} {_fmt(ly)} {_fmt(lz)})
    ({_fmt(ux)} {_fmt(uy)} {_fmt(lz)})
    ({_fmt(lx)} {_fmt(uy)} {_fmt(lz)})
    ({_fmt(lx)} {_fmt(ly)} {_fmt(uz)})
    ({_fmt(ux)} {_fmt(ly)} {_fmt(uz)})
    ({_fmt(ux)} {_fmt(uy)} {_fmt(uz)})
    ({_fmt(lx)} {_fmt(uy)} {_fmt(uz)})
);

blocks
(
    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)
);

edges ();

boundary
(
    background
    {{
        type patch;
        faces
        (
            (0 4 7 3)
            (1 2 6 5)
            (0 1 5 4)
            (3 7 6 2)
            (0 3 2 1)
            (4 5 6 7)
        );
    }}
);

mergePatchPairs ();
''',
        counts,
    )


def _surface_feature_extract_dict() -> str:
    names = ("outerDomain.stl", *EXPECTED_BATTERY_FILES)
    entries = "\n".join(
        f'''{name}
{{
    extractionMethod extractFromSurface;
    extractFromSurfaceCoeffs
    {{
        includedAngle 150;
    }}
    writeObj no;
}}'''
        for name in names
    )
    return _foam_header("surfaceFeatureExtractDict") + entries + "\n"


def _snappy_dict(config: ResolvedConfig, preset: MeshPreset) -> str:
    """탱크/배터리 STL snap, 국부 refinement와 coolant측 prism층을 정의한다."""

    # Keep enclosure and port surfaces one level finer than the battery skin so
    # the 19 mm circular caps remain resolved on the standard mesh.
    outer_surface_min_level = max(1, preset.surface_min_level)
    outer_surface_max_level = max(2, preset.surface_max_level)
    geometry_entries = [
        '''    outerDomain.stl
    {
        type triSurfaceMesh;
        name outerDomain;
    }'''
    ]
    geometry_entries.extend(
        f'''    {name}
    {{
        type triSurfaceMesh;
        name {name.removesuffix(".stl")};
    }}'''
        for name in EXPECTED_BATTERY_FILES
    )

    port_refinement_regions: list[str] = []
    if preset.background_cell_m <= 0.0027:
        port_half_width = config.ports.inlet_diameter_m / 2 + 0.001
        refinement_depth = 0.004
        for name, center, end_z, inward_sign in (
            (
                "inletCapRefinement",
                config.ports.inlet_center_xy_m,
                config.enclosure.height_z_m + config.ports.external_length_m,
                -1.0,
            ),
            (
                "outletCapRefinement",
                config.ports.outlet_center_xy_m,
                -config.ports.external_length_m,
                1.0,
            ),
        ):
            x, y = center
            inner_z = end_z + inward_sign * refinement_depth
            geometry_entries.append(
                f'''    {name}
    {{
        type searchableBox;
        min ({_fmt(x-port_half_width)} {_fmt(y-port_half_width)} {_fmt(min(end_z, inner_z)-1e-6)});
        max ({_fmt(x+port_half_width)} {_fmt(y+port_half_width)} {_fmt(max(end_z, inner_z)+1e-6)});
    }}'''
            )
            port_refinement_regions.append(
                f'''        {name}
        {{
            mode inside;
            levels ((1e15 3));
        }}'''
            )

    feature_entries = [
        f'''        {{ file "outerDomain.eMesh"; level {outer_surface_max_level}; }}'''
    ]
    feature_entries.extend(
        f'''        {{ file "{name.removesuffix(".stl")}.eMesh"; level {preset.surface_max_level}; }}'''
        for name in EXPECTED_BATTERY_FILES
    )

    surface_entries = [
        f'''        outerDomain
        {{
            level ({outer_surface_min_level} {outer_surface_max_level});
            patchInfo {{ type wall; }}
        }}'''
    ]
    surface_entries.extend(
        f'''        {name.removesuffix(".stl")}
        {{
            level ({preset.surface_min_level} {preset.surface_max_level});
            faceZone {name.removesuffix(".stl")}_interface;
            cellZone {name.removesuffix(".stl")};
            cellZoneInside inside;
            faceType internal;
        }}'''
        for name in EXPECTED_BATTERY_FILES
    )

    region_entries = "\n".join(
        f'''        {name.removesuffix(".stl")}
        {{
            mode inside;
            levels ((1e15 {preset.battery_inside_level}));
        }}'''
        for name in EXPECTED_BATTERY_FILES
    )
    region_entries += "\n" + "\n".join(port_refinement_regions)
    inlet_x, inlet_y = config.ports.inlet_center_xy_m
    coolant_z = config.enclosure.height_z_m + config.ports.external_length_m * 0.5
    return _foam_header("snappyHexMeshDict") + f'''castellatedMesh true;
snap            true;
addLayers       true;

geometry
{{
{chr(10).join(geometry_entries)}
}}

castellatedMeshControls
{{
    maxLocalCells       2000000;
    maxGlobalCells      8000000;
    // Stop chasing single-cell refinement islands.  The previous value of
    // zero spent dozens of parallel iterations refining 1--5 cells without
    // changing the resolved battery surfaces.
    minRefinementCells  10;
    nCellsBetweenLevels 3;
    resolveFeatureAngle 30;
    // Keep only faces that actually bound their associated battery cellZone.
    // Free-standing intersections created multiply-connected faceZones in the
    // first production run and are not physical CHT interfaces.
    allowFreeStandingZoneFaces false;

    features
    (
{chr(10).join(feature_entries)}
    );

    refinementSurfaces
    {{
{chr(10).join(surface_entries)}
    }}

    refinementRegions
    {{
{region_entries}
    }}

    locationsInMesh
    (
        // Keep the connected enclosure/ports mesh unzoned.  The closed battery
        // surfaces then assign their full interiors via cellZoneInside inside.
        // A subsequent topoSet action names every remaining cell as coolant.
        (({_fmt(inlet_x)} {_fmt(inlet_y)} {_fmt(coolant_z)}) none)
    );
}}

snapControls
{{
    nSmoothPatch       10;
    tolerance          2.0;
    nSolveIter         100;
    nRelaxIter         10;
    nFeatureSnapIter   10;
    // Split badly aligned feature faces halfway through feature snapping and
    // smooth refinement transitions before layer extrusion.
    nFaceSplitInterval 5;
    nSmoothInternal    10;
    implicitFeatureSnap false;
    explicitFeatureSnap true;
    // The 23 touching/tangent cylinders are separate cellZone surfaces.  Multi-
    // region feature snapping repeatedly drove points through neighbouring
    // battery faces in the first v2512 production run.  Keep explicit circular
    // edge snapping, but do not couple the independent battery feature meshes.
    multiRegionFeatureSnap false;
}}

addLayersControls
{{
    relativeSizes false;
    layers
    {{
        // Match only the coolant-side slave patch.  The surface-normal/master
        // side is the battery solid for these exported STLs; the first
        // production diagnostic proved that selecting the master builds the
        // prisms inside the solid and leaves coolant-side y+ undefined.
        "battery_C[0-9][0-9]_slave"
        {{
            nSurfaceLayers {PRISM_LAYER_COUNT};
        }}
    }}
    expansionRatio          1.2;
    firstLayerThickness     {preset.first_layer_thickness_m:.12g};
    minThickness            3e-6;
    nGrow                   0;
    featureAngle            60;
    slipFeatureAngle        30;
    nRelaxIter              5;
    nSmoothSurfaceNormals   5;
    nSmoothNormals          10;
    nSmoothThickness        15;
    maxFaceThicknessRatio   0.5;
    maxThicknessToMedialRatio 0.3;
    minMedialAxisAngle      90;
    nBufferCellsNoExtrude   1;
    detectExtrusionIsland   true;
    additionalReporting     true;
    nLayerIter              50;
    nRelaxedIter            20;
}}

meshQualityControls
{{
    #includeEtc "caseDicts/meshQualityDict"
    // During layer insertion the still-internal CHT interface joins a thin
    // wall cell to a much larger solid cell.  The interface is split into two
    // boundary patches before solving, so its internal-face interpolation
    // weight is not a solver stencil.  Using the default 0.05 here caused all
    // otherwise valid layers to be removed.  Keep positive-volume and
    // determinant guards active, use interface-compatible insertion limits,
    // then apply the full checkMesh gate again after region splitting.
    maxNonOrtho 75;
    maxBoundarySkewness 20;
    maxInternalSkewness 4;
    minVol 1e-20;
    minTetQuality -1e30;
    minDeterminant 0.0001;
    minFaceWeight 0.001;
    minVolRatio 0.001;
    nSmoothScale 4;
    errorReduction 0.75;
    relaxed
    {{
        maxNonOrtho 75;
    }}
}}

writeFlags
(
    scalarLevels
    layerSets
    layerFields
);

mergeTolerance 1e-6;
'''


def _topo_set_ports_dict(config: ResolvedConfig) -> str:
    radius = config.ports.inlet_diameter_m / 2
    epsilon = 1e-5
    entries: list[str] = []
    for name, center, z in (
        (
            "inlet",
            config.ports.inlet_center_xy_m,
            config.enclosure.height_z_m + config.ports.external_length_m,
        ),
        ("outlet", config.ports.outlet_center_xy_m, -config.ports.external_length_m),
    ):
        x, y = center
        entries.append(
            f'''    {{
        name {name}Faces;
        type faceSet;
        action new;
        source patchToFace;
        patch outerDomain;
    }}
    {{
        name {name}Faces;
        type faceSet;
        action subset;
        source boxToFace;
        min ({_fmt(x-radius-epsilon)} {_fmt(y-radius-epsilon)} {_fmt(z-epsilon)});
        max ({_fmt(x+radius+epsilon)} {_fmt(y+radius+epsilon)} {_fmt(z+epsilon)});
    }}'''
        )
    return _foam_header("topoSetDict.ports") + "actions\n(\n" + "\n".join(entries) + "\n);\n"


def _topo_set_coolant_dict(config: ResolvedConfig) -> str:
    """전체 셀에서 23개 battery zone을 빼 나머지를 coolant zone으로 만든다."""

    lower, upper = _bounds(config)
    subtract_batteries = "\n".join(
        f'''    {{
        name coolantSet;
        type cellSet;
        action subtract;
        source zoneToCell;
        zone battery_C{index:02d};
    }}'''
        for index in range(1, 24)
    )
    return _foam_header("topoSetDict.coolantZone") + f'''actions
(
    {{
        name coolantSet;
        type cellSet;
        action new;
        source boxToCell;
        min ({_fmt(lower[0])} {_fmt(lower[1])} {_fmt(lower[2])});
        max ({_fmt(upper[0])} {_fmt(upper[1])} {_fmt(upper[2])});
    }}
{subtract_batteries}
    {{
        name coolant;
        type cellZoneSet;
        action new;
        source setToCellZone;
        set coolantSet;
    }}
);
'''


def _topo_set_battery_core_dict(config: ResolvedConfig) -> str:
    """CHT 표면은 유지하면서 각 배터리 내부 코어만 추가 세분할 셀을 고른다."""

    placements = cell_placements(config)
    z1 = (config.enclosure.height_z_m - config.cells.length_m) / 2
    z2 = z1 + config.cells.length_m
    entries = []
    for index, cell in enumerate(placements):
        entries.append(
            f'''    {{
        name batteryCore;
        type cellSet;
        action {"new" if index == 0 else "add"};
        source cylinderToCell;
        p1 ({_fmt(cell.center_x_m)} {_fmt(cell.center_y_m)} {_fmt(z1 + BATTERY_CORE_END_CLEARANCE_M)});
        p2 ({_fmt(cell.center_x_m)} {_fmt(cell.center_y_m)} {_fmt(z2 - BATTERY_CORE_END_CLEARANCE_M)});
        radius {_fmt(BATTERY_CORE_REFINEMENT_RADIUS_M)};
    }}'''
        )
    return _foam_header("topoSetDict.batteryCore") + "actions\n(\n" + "\n".join(entries) + "\n);\n"


def _refine_mesh_battery_core_dict() -> str:
    """선택한 배터리 코어를 hexa 위상으로 한 단계 세분하는 설정을 만든다."""

    return _foam_header("refineMeshDict.batteryCore") + '''set batteryCore;

coordinateSystem global;

globalCoeffs
{
    tan1 (1 0 0);
    tan2 (0 1 0);
}

directions (tan1 tan2 normal);

useHexTopology true;
geometricCut false;
writeMesh false;
'''


def _create_patch_ports_dict() -> str:
    entries = "\n".join(
        f'''    {{
        name {name};
        patchInfo {{ type patch; }}
        constructFrom set;
        set {name}Faces;
    }}'''
        for name in ("inlet", "outlet")
    )
    return _foam_header("createPatchDict.ports") + f'''pointSync false;

patches
(
{entries}
);
'''


def _create_patch_wall_dict() -> str:
    return _foam_header("createPatchDict.wall") + '''pointSync false;

patches
(
    {
        name outerWall;
        patchInfo { type wall; }
        constructFrom patches;
        patches (outerDomain);
    }
);
'''


def _topo_set_fallback_dict(config: ResolvedConfig) -> str:
    placements = cell_placements(config)
    z1 = (config.enclosure.height_z_m - config.cells.length_m) / 2
    z2 = z1 + config.cells.length_m
    radius = config.cells.diameter_m / 2
    lower, upper = _bounds(config)
    actions: list[str] = []
    for cell in placements:
        zone = f"battery_{cell.cell_id}"
        actions.extend(
            (
                f'''    {{
        name {zone}_set;
        type cellSet;
        action new;
        source cylinderToCell;
        p1 ({_fmt(cell.center_x_m)} {_fmt(cell.center_y_m)} {_fmt(z1)});
        p2 ({_fmt(cell.center_x_m)} {_fmt(cell.center_y_m)} {_fmt(z2)});
        radius {_fmt(radius)};
    }}''',
                f'''    {{
        name {zone};
        type cellZoneSet;
        action new;
        source setToCellZone;
        set {zone}_set;
    }}''',
            )
        )
    actions.append(
        f'''    {{
        name coolant_set;
        type cellSet;
        action new;
        source boxToCell;
        min ({' '.join(_fmt(value) for value in lower)});
        max ({' '.join(_fmt(value) for value in upper)});
    }}'''
    )
    for cell in placements:
        actions.append(
            f'''    {{
        name coolant_set;
        type cellSet;
        action subtract;
        source cellToCell;
        set battery_{cell.cell_id}_set;
    }}'''
        )
    actions.append(
        '''    {
        name coolant;
        type cellZoneSet;
        action new;
        source setToCellZone;
        set coolant_set;
    }'''
    )
    return _foam_header("topoSetDict.batteryZonesFallback") + "actions\n(\n" + "\n".join(actions) + "\n);\n"


def _control_dict() -> str:
    return _foam_header("controlDict") + '''application     chtMultiRegionSimpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         1;
deltaT          1;
writeControl    timeStep;
writeInterval   1;
purgeWrite      0;
writeFormat     ascii;
writePrecision  12;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;
'''


def _mesh_fv_schemes() -> str:
    return _foam_header("fvSchemes") + '''ddtSchemes
{
    default steadyState;
}
gradSchemes
{
    default Gauss linear;
}
divSchemes
{
    default none;
}
laplacianSchemes
{
    default Gauss linear corrected;
}
interpolationSchemes
{
    default linear;
}
snGradSchemes
{
    default corrected;
}
'''


def _allmesh_production(config: ResolvedConfig, preset: MeshPreset) -> str:
    battery_volume = config.derived.cell_volume_m3 * config.cells.count
    battery_interface_area_each = (
        math.pi * config.cells.diameter_m * config.cells.length_m
        + math.pi * config.cells.diameter_m**2 / 2
    )
    coolant_volume = (
        # The exact PPT polygon volume plus both external ports minus batteries.
        abs(sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(
                config.enclosure.outline_polygon_m,
                config.enclosure.outline_polygon_m[1:] + config.enclosure.outline_polygon_m[:1],
            )
        ))
        * 0.5
        * config.enclosure.height_z_m
        + math.pi
        * (config.ports.inlet_diameter_m / 2) ** 2
        * config.ports.external_length_m
        + math.pi
        * (config.ports.outlet_diameter_m / 2) ** 2
        * config.ports.external_length_m
        - battery_volume
    )
    target_options = []
    if preset.target_solid_cells is not None:
        target_options.append(f"--target-solid-cells {preset.target_solid_cells}")
    if preset.target_fluid_cells is not None:
        target_options.append(f"--target-fluid-cells {preset.target_fluid_cells}")
    if preset.target_points is not None:
        target_options.append(f"--target-points {preset.target_points}")
    component_targets = "".join(
        f" \\{chr(10)}    {option}" for option in target_options
    )
    return f'''#!/bin/sh
set -eu

case_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
log_dir="$case_dir/logs/production_mesh"
report_dir="$case_dir/reports/production_mesh"
rm -rf "$log_dir" "$report_dir"
mkdir -p "$log_dir" "$report_dir"

for tool in surfaceCheck blockMesh surfaceFeatureExtract foamDictionary decomposePar mpirun snappyHexMesh reconstructParMesh reconstructPar topoSet refineMesh createPatch checkMesh splitMeshRegions python3; do
    command -v "$tool" >/dev/null 2>&1 || {{ echo "$tool is unavailable" >&2; exit 2; }}
done

foam_version=${{WM_PROJECT_VERSION:-}}
case "$foam_version" in
    v2512|2512) ;;
    *) echo "OpenCFD OpenFOAM v2512 required; WM_PROJECT_VERSION=$foam_version" >&2; exit 2 ;;
esac

surfaceCheck -case "$case_dir" "$case_dir/constant/triSurface/outerDomain.stl" \
    >"$log_dir/surfaceCheck_outerDomain.log" 2>&1
python3 "$case_dir/tools/parse_surfacecheck.py" \
    --log "$log_dir/surfaceCheck_outerDomain.log" \
    --output "$report_dir/surfaceCheck_outerDomain.json"
for surface in "$case_dir"/constant/triSurface/battery_C??.stl; do
    name=$(basename "$surface" .stl)
    surfaceCheck -case "$case_dir" "$surface" >"$log_dir/surfaceCheck_${{name}}.log" 2>&1
    python3 "$case_dir/tools/parse_surfacecheck.py" \
        --log "$log_dir/surfaceCheck_${{name}}.log" \
        --output "$report_dir/surfaceCheck_${{name}}.json"
done

# createPatch writes the repatched pre-split mesh under 0/polyMesh when initial
# fields are present.  Remove only that generated mesh before blockMesh so a
# retry cannot accidentally feed stale faceZones back into snappyHexMesh.
rm -rf "$case_dir/0/polyMesh"
# Remove only generated split-region meshes and split diagnostics from a prior
# interrupted mesh attempt.  Initial-condition dictionaries and run results are
# retained.
rm -rf "$case_dir/constant/coolant/polyMesh" \
    "$case_dir/constant/batterySolid/polyMesh"
rm -f "$case_dir/constant/cellToRegion" \
    "$case_dir/0/cellToRegion" \
    "$case_dir/0/coolant/cellToRegion" \
    "$case_dir/0/batterySolid/cellToRegion"
# Parallel retries must also discard processor meshes containing the same stale
# faceZones.  These directories are generated solely by decomposePar below.
find "$case_dir" -maxdepth 1 -type d -name 'processor[0-9]*' -exec rm -rf {{}} +

blockMesh -case "$case_dir" >"$log_dir/blockMesh.log" 2>&1
surfaceFeatureExtract -case "$case_dir" >"$log_dir/surfaceFeatureExtract.log" 2>&1
mesh_mpi_ranks=${{OPENFOAM_MESH_MPI_RANKS:-{DEFAULT_MESH_MPI_RANKS}}}
case "$mesh_mpi_ranks" in
    ''|*[!0-9]*) echo "OPENFOAM_MESH_MPI_RANKS must be a positive integer" >&2; exit 2 ;;
esac
[ "$mesh_mpi_ranks" -ge 1 ] || {{ echo "OPENFOAM_MESH_MPI_RANKS must be at least 1" >&2; exit 2; }}
if [ "$mesh_mpi_ranks" -eq 1 ]; then
    # snappyHexMesh layer termination can vary with processor partitioning on
    # this coarse geometry.  Serial meshing is the reproducible default; the
    # CFD solvers still use every configured OPENFOAM_MPI_RANKS rank.
    snappyHexMesh -case "$case_dir" -overwrite \
        >"$log_dir/snappyHexMesh.log" 2>&1
else
    foamDictionary "$case_dir/system/decomposeParDict" \
        -entry numberOfSubdomains -set "$mesh_mpi_ranks" \
        >"$log_dir/foamDictionary_decomposePar.log" 2>&1
    decomposePar -case "$case_dir" -force -no-fields \
        >"$log_dir/decomposePar_snappy.log" 2>&1
    mpirun --allow-run-as-root -np "$mesh_mpi_ranks" \
        snappyHexMesh -case "$case_dir" -parallel -overwrite \
        >"$log_dir/snappyHexMesh.log" 2>&1
    reconstructParMesh -case "$case_dir" -constant \
        >"$log_dir/reconstructParMesh.log" 2>&1
    reconstructPar -case "$case_dir" -withZero \
        -fields '(nSurfaceLayers thickness thicknessFraction)' \
        >"$log_dir/reconstructPar_layerFields.log" 2>&1
fi
topoSet -case "$case_dir" -constant -dict system/topoSetDict.coolantZone \
    >"$log_dir/topoSet_coolantZone.log" 2>&1
python3 "$case_dir/tools/parse_layercheck.py" \
    --log "$log_dir/snappyHexMesh.log" \
    --mesh-dir "$case_dir/constant/polyMesh" \
    --field-dir "$case_dir/0" \
    --expected-target-layers {PRISM_LAYER_COUNT} \
    --minimum-layer-coverage {MINIMUM_LAYER_COVERAGE} \
    --minimum-thickness-fraction {MINIMUM_WANTED_THICKNESS_FRACTION} \
    --output "$report_dir/layer_coverage.json"

# These are snappyHexMesh diagnostics, not solver fields.  Keeping them after
# inlet/outlet repatching makes splitMeshRegions try to remap stale boundary
# dictionaries and fail.  Their validated statistics are preserved in JSON.
rm -f "$case_dir/0/nSurfaceLayers" \
    "$case_dir/0/thickness" \
    "$case_dir/0/thicknessFraction" \
    "$case_dir/0/cellLevel" \
    "$case_dir/0/pointLevel"

# Increase resolution only inside each battery core. The 4 mm radius and 6 mm
# axial end clearance keep every selected cell away from the CHT interface, so
# coolant-side faces and paired mapped-wall topology remain unchanged.
topoSet -case "$case_dir" -constant -dict system/topoSetDict.batteryCore \
    >"$log_dir/topoSet_batteryCore.log" 2>&1
refineMesh -case "$case_dir" -overwrite -dict system/refineMeshDict.batteryCore \
    >"$log_dir/refineMesh_batteryCore.log" 2>&1
# refineMesh writes only its diagnostic cellMap under 0/polyMesh while the
# actual overwritten mesh remains in constant/polyMesh.  Remove that map-only
# directory so later mesh-instance detection cannot mistake it for a mesh.
rm -f "$case_dir/0/polyMesh/cellMap"
rmdir "$case_dir/0/polyMesh" 2>/dev/null || true
# Recreate the coolant complement after refinement and explicitly verify that
# every new child cell belongs to exactly one material zone below.
topoSet -case "$case_dir" -constant -dict system/topoSetDict.coolantZone \
    >"$log_dir/topoSet_coolantZone_after_core_refine.log" 2>&1

# Split the closed outerDomain patch into exact circular port caps and the wall.
# createPatch writes the repatched mesh to the selected time (normally
# 0/polyMesh because this case deliberately contains initial fields at time 0).
topoSet -case "$case_dir" -constant -dict system/topoSetDict.ports \
    >"$log_dir/topoSet_ports.log" 2>&1
createPatch -case "$case_dir" -dict system/createPatchDict.ports -overwrite \
    >"$log_dir/createPatch_ports.log" 2>&1
createPatch -case "$case_dir" -dict system/createPatchDict.wall -overwrite \
    >"$log_dir/createPatch_wall.log" 2>&1

# The unsplit CHT interface is still an internal face joining a thin coolant
# layer to a much larger solid cell. Its interpolation/non-orthogonality
# stencil disappears at splitMeshRegions, so this expanded pre-split check is
# diagnostic only. The authoritative quality gate is the standard checkMesh
# on the final two regions below.
checkMesh -case "$case_dir" -allGeometry -allTopology \
    >"$log_dir/checkMesh_pre_split_diagnostic.log" 2>&1 || true

pre_split_mesh_dir="$case_dir/constant/polyMesh"
if [ -f "$case_dir/0/polyMesh/points" ]; then
    pre_split_mesh_dir="$case_dir/0/polyMesh"
fi
python3 "$case_dir/tools/validate_cell_zones.py" \
    --mesh-dir "$pre_split_mesh_dir" --mode pre_split \
    --output "$report_dir/cellZones_pre_split.json"
python3 "$case_dir/tools/validate_mesh_integrals.py" \
    --mesh-dir "$pre_split_mesh_dir" \
    --expected-battery-volume {_fmt(battery_volume)} \
    --expected-coolant-volume {_fmt(coolant_volume)} \
    --expected-inlet-area {_fmt(config.derived.inlet_area_m2)} \
    --expected-outlet-area {_fmt(math.pi * config.ports.outlet_diameter_m**2 / 4)} \
    --output "$report_dir/mesh_integrals_pre_split.json"

zone_groups='((batterySolid "battery_C.*")(coolant coolant))'
splitMeshRegions -case "$case_dir" -cellZonesOnly -addZones "$zone_groups" \
    -useFaceZones -detectOnly >"$log_dir/splitMeshRegions_detectOnly.log" 2>&1
splitMeshRegions -case "$case_dir" -cellZonesOnly -addZones "$zone_groups" \
    -useFaceZones -overwrite >"$log_dir/splitMeshRegions.log" 2>&1

python3 "$case_dir/tools/validate_cell_zones.py" \
    --mesh-dir "$case_dir/constant/batterySolid/polyMesh" --mode battery_region \
    --output "$report_dir/cellZones_batterySolid.json"
python3 "$case_dir/tools/validate_interfaces.py" \
    --coolant-mesh-dir "$case_dir/constant/coolant/polyMesh" \
    --solid-mesh-dir "$case_dir/constant/batterySolid/polyMesh" \
    --expected-each-area {_fmt(battery_interface_area_each)} \
    --geometry-tolerance {MAXIMUM_INTERFACE_AREA_RELATIVE_ERROR} \
    --output "$report_dir/interfaces_post_split.json"
check_status=0
checkMesh -case "$case_dir" -constant -allRegions -meshQuality \
    >"$log_dir/checkMesh_all_regions.log" 2>&1 || check_status=$?
python3 "$case_dir/tools/parse_checkmesh.py" \
    --log "$log_dir/checkMesh_all_regions.log" \
    --expected-mesh-ok-count 2 \
    --output "$report_dir/checkMesh_all_regions.json" || check_status=$?
if [ "$check_status" -ne 0 ]; then
    echo "all-region checkMesh gate failed" >&2
    exit "$check_status"
fi
python3 "$case_dir/tools/validate_mesh_target.py" \
    --zone-report "$report_dir/cellZones_pre_split.json" \
    --checkmesh-log "$log_dir/checkMesh_pre_split_diagnostic.log" \
    --target-total-cells {preset.target_cells}{component_targets} \
    --output "$report_dir/mesh_target_composition.json"
# Record OpenFOAM's optional exhaustive diagnostics as an engineering audit.
# Thin wall-resolved prisms and the intentionally disconnected 23 bodies make
# several generic allGeometry thresholds inapplicable; this audit never
# replaces or weakens the standard final-region gate above.
checkMesh -case "$case_dir" -constant -allRegions -allGeometry -allTopology \
    >"$log_dir/checkMesh_all_regions_extended_audit.log" 2>&1 || true
python3 "$case_dir/tools/aggregate_production_gate.py" \
    --report-dir "$report_dir" \
    --output "$report_dir/production_mesh_gate.json"

echo "Production mesh gate passed. Solver pilots are now authorized; y+ remains a solver-result gate."
'''


def _fallback_script() -> str:
    return '''#!/bin/sh
set -eu

case_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
report_dir="$case_dir/reports/production_mesh"
mkdir -p "$report_dir"

echo "Explicit fallback selected: replacing battery/coolant cellZones from cell centres." >&2
topoSet -case "$case_dir" -constant -dict system/topoSetDict.batteryZonesFallback
python3 "$case_dir/tools/validate_cell_zones.py" \
    --mesh-dir "$case_dir/constant/polyMesh" --mode pre_split \
    --output "$report_dir/cellZones_fallback_pre_split.json"

echo "Fallback cellZones pass name/non-empty/exclusive/coverage checks."
echo "Zone-volume and interface checks are still mandatory before region splitting."
'''


def _compose_yaml() -> str:
    return f'''services:
  production-mesh:
    build:
      context: .
      dockerfile: Dockerfile.openfoam
    image: cfd-twin-openfoam-v2512-python3:local
    working_dir: /case
    environment:
      OPENFOAM_MESH_MPI_RANKS: "${{OPENFOAM_MESH_MPI_RANKS:-{DEFAULT_MESH_MPI_RANKS}}}"
    volumes:
      - .:/case
    command: ["/bin/sh", "/case/AllmeshProduction"]
  steady-pilot:
    build:
      context: .
      dockerfile: Dockerfile.openfoam
    image: cfd-twin-openfoam-v2512-python3:local
    working_dir: /case
    environment:
      OPENFOAM_MPI_RANKS: "${{OPENFOAM_MPI_RANKS:-{DEFAULT_MPI_RANKS}}}"
    volumes:
      - .:/case
    command: ["/bin/sh", "/case/AllrunSteadyPilot"]
  steady-continue:
    build:
      context: .
      dockerfile: Dockerfile.openfoam
    image: cfd-twin-openfoam-v2512-python3:local
    working_dir: /case
    environment:
      OPENFOAM_MPI_RANKS: "${{OPENFOAM_MPI_RANKS:-{DEFAULT_MPI_RANKS}}}"
      STEADY_ADDITIONAL_ITERATIONS: "${{STEADY_ADDITIONAL_ITERATIONS:-2000}}"
    volumes:
      - .:/case
    command: ["/bin/sh", "/case/AllcontinueSteadyPilot"]
  cold-flow-pilot:
    build:
      context: .
      dockerfile: Dockerfile.openfoam
    image: cfd-twin-openfoam-v2512-python3:local
    working_dir: /case
    environment:
      OPENFOAM_MPI_RANKS: "${{OPENFOAM_MPI_RANKS:-{DEFAULT_MPI_RANKS}}}"
    volumes:
      - .:/case
    command: ["/bin/sh", "/case/AllrunColdFlowPilot"]
  cold-flow-continue:
    build:
      context: .
      dockerfile: Dockerfile.openfoam
    image: cfd-twin-openfoam-v2512-python3:local
    working_dir: /case
    environment:
      OPENFOAM_MPI_RANKS: "${{OPENFOAM_MPI_RANKS:-{DEFAULT_MPI_RANKS}}}"
      COLD_FLOW_ADDITIONAL_ITERATIONS: "${{COLD_FLOW_ADDITIONAL_ITERATIONS:-500}}"
    volumes:
      - .:/case
    command: ["/bin/sh", "/case/AllcontinueColdFlowPilot"]
  frozen-flow-pilot:
    build:
      context: .
      dockerfile: Dockerfile.openfoam
    image: cfd-twin-openfoam-v2512-python3:local
    working_dir: /case
    environment:
      OPENFOAM_MPI_RANKS: "${{OPENFOAM_MPI_RANKS:-{DEFAULT_MPI_RANKS}}}"
    volumes:
      - .:/case
    command: ["/bin/sh", "/case/AllrunFrozenFlowThermalPilot"]
  frozen-flow-actual-1s:
    build:
      context: .
      dockerfile: Dockerfile.openfoam
    image: cfd-twin-openfoam-v2512-python3:local
    working_dir: /case
    environment:
      OPENFOAM_MPI_RANKS: "${{OPENFOAM_MPI_RANKS:-{DEFAULT_MPI_RANKS}}}"
      FROZEN_FLOW_STEADY_INITIAL_TEMPERATURE_K: "${{FROZEN_FLOW_STEADY_INITIAL_TEMPERATURE_K:-293.15}}"
    volumes:
      - .:/case
    command: ["/bin/sh", "/case/AllrunFrozenFlowThermalActual1s"]
  full-transient-actual-1s:
    build:
      context: .
      dockerfile: Dockerfile.openfoam
    image: cfd-twin-openfoam-v2512-python3:local
    working_dir: /case
    environment:
      OPENFOAM_MPI_RANKS: "${{OPENFOAM_MPI_RANKS:-{DEFAULT_MPI_RANKS}}}"
    volumes:
      - .:/case
    command: ["/bin/sh", "/case/AllrunFullTransientActual1s"]
  full-transient-dt-pilot:
    build:
      context: .
      dockerfile: Dockerfile.openfoam
    image: cfd-twin-openfoam-v2512-python3:local
    working_dir: /case
    environment:
      OPENFOAM_MPI_RANKS: "${{OPENFOAM_MPI_RANKS:-{DEFAULT_MPI_RANKS}}}"
    volumes:
      - .:/case
    command: ["/bin/sh", "/case/AllrunFullTransientDtPilot"]
  full-transient-dt-half-pilot:
    build:
      context: .
      dockerfile: Dockerfile.openfoam
    image: cfd-twin-openfoam-v2512-python3:local
    working_dir: /case
    environment:
      OPENFOAM_MPI_RANKS: "${{OPENFOAM_MPI_RANKS:-{DEFAULT_MPI_RANKS}}}"
    volumes:
      - .:/case
    command: ["/bin/sh", "/case/AllrunFullTransientDtHalfPilot"]
'''


def _dockerfile() -> str:
    return '''FROM opencfd/openfoam-default:2512

RUN apt-get update \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 \\
 && rm -rf /var/lib/apt/lists/*
'''


def _docker_shell_script() -> str:
    return '''#!/bin/sh
set -eu

case_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v docker >/dev/null 2>&1; then
    docker_cmd=$(command -v docker)
elif [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
    docker_cmd=/Applications/Docker.app/Contents/Resources/bin/docker
else
    echo "Docker Desktop CLI is unavailable" >&2
    exit 2
fi
"$docker_cmd" compose -f "$case_dir/compose.yaml" run --rm --build production-mesh
'''


def _docker_powershell_script() -> str:
    return '''$ErrorActionPreference = "Stop"
$CaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($DockerCommand) {
    $DockerExe = $DockerCommand.Source
} else {
    $DockerExe = Join-Path $Env:ProgramFiles "Docker/Docker/resources/bin/docker.exe"
}
if (-not (Test-Path $DockerExe)) {
    throw "Docker Desktop is unavailable"
}
& $DockerExe compose -f (Join-Path $CaseDir "compose.yaml") run --rm --build production-mesh
if ($LASTEXITCODE -ne 0) {
    throw "OpenFOAM production mesh failed with exit code $LASTEXITCODE"
}
'''


def _docker_solver_shell_script(service: str) -> str:
    return f'''#!/bin/sh
set -eu

case_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v docker >/dev/null 2>&1; then
    docker_cmd=$(command -v docker)
elif [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
    docker_cmd=/Applications/Docker.app/Contents/Resources/bin/docker
else
    echo "Docker Desktop CLI is unavailable" >&2
    exit 2
fi
"$docker_cmd" compose -f "$case_dir/compose.yaml" run --rm --build {service}
'''


def _docker_solver_powershell_script(service: str) -> str:
    return f'''$ErrorActionPreference = "Stop"
$CaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($DockerCommand) {{
    $DockerExe = $DockerCommand.Source
}} else {{
    $DockerExe = Join-Path $Env:ProgramFiles "Docker/Docker/resources/bin/docker.exe"
}}
if (-not (Test-Path $DockerExe)) {{
    throw "Docker Desktop is unavailable"
}}
& $DockerExe compose -f (Join-Path $CaseDir "compose.yaml") run --rm --build {service}
if ($LASTEXITCODE -ne 0) {{
    throw "OpenFOAM solver pilot failed with exit code $LASTEXITCODE"
}}
'''


def _readme(level: str) -> str:
    profile_note = (
        "This is the default single-run profile targeting 207,390 total cells "
        "(70,840 solid + 136,550 fluid) and 221,878 points."
        if level == DEFAULT_MESH_PROFILE
        else "This is a retained legacy validation profile."
    )
    return f'''# Production mesh source ({level})

This is an OpenCFD OpenFOAM v2512 `blockMesh + snappyHexMesh` input bundle.
It has been generated, not executed on this macOS host.
{profile_note}

The background is hexahedral and the finished body-fitted mesh is
**hexa-dominant**. Curved cylinders, snapped boundaries and wall layers create
some cut polyhedra/prisms, so this workflow does not claim a mathematically
all-hexa mesh. `mesh_target_composition.json` records the actual mix and counts.

Primary path:

1. `outerDomain.stl` trims the background block.
2. `battery_C01.stl` through `battery_C23.stl` create 23 individual cellZones
   and matching `*_interface` faceZones.
3. `locationsInMesh` creates the required `coolant` cellZone.
4. port faceSets split the outer boundary into `inlet`, `outlet`, `outerWall`.
5. the validator requires exact zone names, non-empty zones, no overlapping cell
   labels and complete cell coverage.
6. `splitMeshRegions -addZones` maps the 23 disconnected battery zones into one
   `batterySolid` region and coolant into one `coolant` region.

Run `./run-production-mesh.sh` on macOS/Linux or
`./run-production-mesh.ps1` in PowerShell on Windows. `Dockerfile.openfoam`
uses the official multi-architecture `opencfd/openfoam-default:2512` image and
adds only Python 3 for the validation scripts. Both host launchers execute the
same `AllmeshProduction` script inside Linux. Direct execution is also
supported inside an already initialized v2512 environment with Python 3.
The snappyHexMesh stage runs on one rank by default because layer termination
on this coarse geometry otherwise changes with processor partitioning. Override
it with `OPENFOAM_MESH_MPI_RANKS` when explicitly testing parallel meshing.
Solver pilots use {DEFAULT_MPI_RANKS} MPI ranks by default and reconstruct their
latest result; override that count with `OPENFOAM_MPI_RANKS`.

After the production mesh gates pass, run the source-free cold-flow pilot and
then the steady CHT pilot. The frozen-flow pilot clones the mesh and cold-flow
fields into `runs/frozen_flow`, resets both region temperatures to
the configured initial temperature, activates `PIMPLE.frozenFlow yes`, and runs
to 1800 s with a 1 s physical time step. Compact telemetry is sampled every
1 s, compressed binary 3D fields are written every 15 s and all 121 field
frames are reconstructed. The frozen-flow gate checks the time/frame contract,
mass and final energy balances, monotonic heating and agreement with the
validated steady CHT result. The canonical new result uses the one-second
frozen-flow profile. In this constant-property, zero-gravity model temperature
cannot feed back into momentum, so it advances the coupled fluid/solid energy
equations while retaining the separately converged flow field. Both regional
temperature fields remain in the native parallel decomposition at every
physical second and the final complete state is reconstructed. The fully
repeated momentum/energy launcher uses an adaptive step no larger than 0.1 s,
`maxCo 5` and three PIMPLE outer correctors and remains available for time-step
sensitivity work. Raw OpenFOAM residuals are never relabelled as Fluent scaled
residuals.

`AllmeshProductionFallbackZones` is an explicit, non-automatic recovery path for
cellZone creation. It does not prove zone volumes, interface integrity, prism-layer
coverage, y+ or mesh independence.

The ten-layer setting starts at
{MESH_PRESETS[level].first_layer_thickness_m * 1e6:.0f} micrometres. Every mesh
remains subject to the actual wall-y+ and count/composition gates; no production
result is authorized by this source bundle alone.
'''


def _verify_cad(cad_dir: Path, config: ResolvedConfig) -> dict[str, object]:
    result_path = cad_dir / "cad_result.json"
    if not result_path.is_file():
        raise CfdTwinError(
            f"production mesh requires executed CAD result: {result_path}"
        )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not result.get("cad_geometry_checks_passed"):
        raise CfdTwinError("production mesh requires passed CAD geometry checks")
    if result.get("geometry_sha256") != config.geometry_sha256:
        raise CfdTwinError("CAD geometry hash does not match the resolved configuration")
    surfaces = result.get("production_surfaces", {})
    if surfaces.get("length_unit") != "m":
        raise CfdTwinError("production STL manifest must declare length_unit=m")
    battery_files = tuple(Path(item).name for item in surfaces.get("battery_files", []))
    if battery_files != EXPECTED_BATTERY_FILES:
        raise CfdTwinError("CAD result must contain battery_C01.stl through battery_C23.stl")
    required = (
        "outerDomain.stl",
        "inlet.stl",
        "outlet.stl",
        "outerWall.stl",
        *EXPECTED_BATTERY_FILES,
    )
    source_dir = cad_dir / "production_surfaces"
    missing = [name for name in required if not (source_dir / name).is_file()]
    if missing:
        raise CfdTwinError(f"production CAD surfaces missing: {', '.join(missing)}")
    expected_hashes = result.get("file_sha256", {})
    for name in required:
        relative = f"production_surfaces/{name}"
        actual_hash = hashlib.sha256((source_dir / name).read_bytes()).hexdigest()
        if expected_hashes.get(relative) != actual_hash:
            raise CfdTwinError(f"CAD surface hash mismatch: {relative}")
    return result


def write_production_mesh_source_bundle(
    config: ResolvedConfig,
    cad_dir: str | Path,
    output_dir: str | Path,
    *,
    mesh_level: str = DEFAULT_MESH_PROFILE,
) -> dict[str, object]:
    """메시 dictionary, 실행기, 검증기를 한 OpenFOAM case 묶음으로 기록한다.

    이 함수는 목표 숫자를 결과로 가장하지 않는다. 실제 실행 뒤 생성되는
    mesh_target_composition/checkMesh/zone/interface 보고서가 모두 통과해야
    솔버 단계가 실행될 수 있다.
    """
    if mesh_level not in SUPPORTED_LEVELS:
        raise CfdTwinError(
            f"production mesh level must be one of {', '.join(SUPPORTED_LEVELS)}"
        )
    cad = Path(cad_dir).resolve()
    output = Path(output_dir).resolve()
    cad_result = _verify_cad(cad, config)
    preset = MESH_PRESETS[mesh_level]

    tri_surface = output / "constant" / "triSurface"
    tri_surface.mkdir(parents=True, exist_ok=True)
    copied_files = (
        "outerDomain.stl",
        "inlet.stl",
        "outlet.stl",
        "outerWall.stl",
        *EXPECTED_BATTERY_FILES,
    )
    for name in copied_files:
        shutil.copy2(cad / "production_surfaces" / name, tri_surface / name)

    block_dict, block_counts = _block_mesh_dict(config, preset)
    _atomic_write(output / "system" / "blockMeshDict", block_dict)
    _atomic_write(
        output / "system" / "surfaceFeatureExtractDict",
        _surface_feature_extract_dict(),
    )
    _atomic_write(
        output / "system" / "snappyHexMeshDict", _snappy_dict(config, preset)
    )
    _atomic_write(
        output / "system" / "topoSetDict.ports", _topo_set_ports_dict(config)
    )
    _atomic_write(
        output / "system" / "topoSetDict.coolantZone",
        _topo_set_coolant_dict(config),
    )
    _atomic_write(
        output / "system" / "topoSetDict.batteryCore",
        _topo_set_battery_core_dict(config),
    )
    _atomic_write(
        output / "system" / "refineMeshDict.batteryCore",
        _refine_mesh_battery_core_dict(),
    )
    _atomic_write(
        output / "system" / "createPatchDict.ports", _create_patch_ports_dict()
    )
    _atomic_write(
        output / "system" / "createPatchDict.wall", _create_patch_wall_dict()
    )
    _atomic_write(
        output / "system" / "topoSetDict.batteryZonesFallback",
        _topo_set_fallback_dict(config),
    )
    _atomic_write(output / "system" / "controlDict.mesh-only", _control_dict())
    _atomic_write(output / "system" / "fvSchemes", _mesh_fv_schemes())
    _atomic_write(output / "system" / "decomposeParDict", _decompose_par_dict())
    _atomic_write(output / "system" / "meshQualityDict", _mesh_quality_dict())

    solver_source = write_openfoam_solver_bundle(config, output)
    for region in ("coolant", "batterySolid"):
        _atomic_write(
            output / "system" / region / "decomposeParDict",
            _decompose_par_dict(),
        )
        _atomic_write(
            output / "system" / region / "meshQualityDict",
            _mesh_quality_dict(),
        )

    validator_destination = output / "tools" / "validate_cell_zones.py"
    validator_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        Path(__file__).with_name("openfoam_zone_validation.py"), validator_destination
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_mesh_integrals.py"),
        output / "tools" / "validate_mesh_integrals.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_mesh_integrals.py"),
        output / "tools" / "openfoam_mesh_integrals.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_interface_validation.py"),
        output / "tools" / "validate_interfaces.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_checkmesh.py"),
        output / "tools" / "parse_checkmesh.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_surfacecheck.py"),
        output / "tools" / "parse_surfacecheck.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_layercheck.py"),
        output / "tools" / "parse_layercheck.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_yplus_validation.py"),
        output / "tools" / "validate_yplus.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_cold_flow_validation.py"),
        output / "tools" / "validate_cold_flow.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_steady_cht_validation.py"),
        output / "tools" / "validate_steady_cht.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_frozen_flow_validation.py"),
        output / "tools" / "validate_frozen_flow.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_transient_residuals.py"),
        output / "tools" / "extract_transient_residuals.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_production_gate.py"),
        output / "tools" / "aggregate_production_gate.py",
    )
    shutil.copy2(
        Path(__file__).with_name("openfoam_mesh_target_validation.py"),
        output / "tools" / "validate_mesh_target.py",
    )

    _atomic_write(output / "AllmeshProduction", _allmesh_production(config, preset))
    (output / "AllmeshProduction").chmod(0o755)
    _atomic_write(output / "AllmeshProductionFallbackZones", _fallback_script())
    (output / "AllmeshProductionFallbackZones").chmod(0o755)
    _atomic_write(output / "compose.yaml", _compose_yaml())
    _atomic_write(output / "Dockerfile.openfoam", _dockerfile())
    _atomic_write(output / "run-production-mesh.sh", _docker_shell_script())
    (output / "run-production-mesh.sh").chmod(0o755)
    _atomic_write(
        output / "run-production-mesh.ps1", _docker_powershell_script()
    )
    solver_launchers = {
        "run-cold-flow-pilot.sh": _docker_solver_shell_script(
            "cold-flow-pilot"
        ),
        "run-cold-flow-continue.sh": _docker_solver_shell_script(
            "cold-flow-continue"
        ),
        "run-steady-pilot.sh": _docker_solver_shell_script("steady-pilot"),
        "run-steady-continue.sh": _docker_solver_shell_script(
            "steady-continue"
        ),
        "run-frozen-flow-pilot.sh": _docker_solver_shell_script(
            "frozen-flow-pilot"
        ),
        "run-frozen-flow-actual-1s.sh": _docker_solver_shell_script(
            "frozen-flow-actual-1s"
        ),
        "run-full-transient-actual-1s.sh": _docker_solver_shell_script(
            "full-transient-actual-1s"
        ),
        "run-full-transient-dt-pilot.sh": _docker_solver_shell_script(
            "full-transient-dt-pilot"
        ),
        "run-full-transient-dt-half-pilot.sh": _docker_solver_shell_script(
            "full-transient-dt-half-pilot"
        ),
        "run-cold-flow-pilot.ps1": _docker_solver_powershell_script(
            "cold-flow-pilot"
        ),
        "run-cold-flow-continue.ps1": _docker_solver_powershell_script(
            "cold-flow-continue"
        ),
        "run-steady-pilot.ps1": _docker_solver_powershell_script(
            "steady-pilot"
        ),
        "run-steady-continue.ps1": _docker_solver_powershell_script(
            "steady-continue"
        ),
        "run-frozen-flow-pilot.ps1": _docker_solver_powershell_script(
            "frozen-flow-pilot"
        ),
        "run-frozen-flow-actual-1s.ps1": _docker_solver_powershell_script(
            "frozen-flow-actual-1s"
        ),
        "run-full-transient-actual-1s.ps1": _docker_solver_powershell_script(
            "full-transient-actual-1s"
        ),
        "run-full-transient-dt-pilot.ps1": _docker_solver_powershell_script(
            "full-transient-dt-pilot"
        ),
        "run-full-transient-dt-half-pilot.ps1": _docker_solver_powershell_script(
            "full-transient-dt-half-pilot"
        ),
    }
    for name, content in solver_launchers.items():
        _atomic_write(output / name, content)
        if name.endswith(".sh"):
            (output / name).chmod(0o755)
    _atomic_write(output / "README.md", _readme(mesh_level))

    lower, upper = _bounds(config)
    generated_hashes = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "production_mesh_manifest.json":
            continue
        relative = path.relative_to(output)
        # This manifest fingerprints reproducible source inputs only.  A bundle
        # refresh must not absorb large solver/mesh outputs from an earlier run.
        if relative.parts[0] in {"runs", "logs", "reports", "postProcessing"}:
            continue
        if relative.parts[0].startswith("processor"):
            continue
        if "polyMesh" in relative.parts or "__pycache__" in relative.parts:
            continue
        if "extendedFeatureEdgeMesh" in relative.parts or path.suffix == ".eMesh":
            continue
        if path.name == "cellToRegion":
            continue
        generated_hashes[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest: dict[str, object] = {
        "schema_version": 1,
        "status": "PRODUCTION_MESH_SOURCE_CREATED_NOT_EXECUTED",
        "backend": "blockMesh+snappyHexMesh",
        "distribution": config.solver.distribution,
        "openfoam_version": config.solver.version,
        "container_image": "cfd-twin-openfoam-v2512-python3:local",
        "base_container_image": "opencfd/openfoam-default:2512",
        "container_execution_tested": False,
        "parallel_execution": {
            "enabled": True,
            "default_mpi_ranks": DEFAULT_MPI_RANKS,
            "override_environment_variable": "OPENFOAM_MPI_RANKS",
            "default_mesh_mpi_ranks": DEFAULT_MESH_MPI_RANKS,
            "mesh_override_environment_variable": "OPENFOAM_MESH_MPI_RANKS",
            "decomposition_method": "scotch",
            "parallel_mesh_stage": "optional snappyHexMesh override",
        },
        "host_launchers": [
            "run-production-mesh.sh",
            "run-production-mesh.ps1",
            *sorted(solver_launchers),
        ],
        "mesh_level": mesh_level,
        "mesh_profile": mesh_level,
        "default_single_run_profile": mesh_level == DEFAULT_MESH_PROFILE,
        "target_cell_count": preset.target_cells,
        "target_is_calibration_goal_not_exact_gate": True,
        "target_composition": {
            "total_cells": preset.target_cells,
            "solid_cells": preset.target_solid_cells,
            "fluid_cells": preset.target_fluid_cells,
            "points": preset.target_points,
        },
        "element_family": {
            "requested": "hexahedral-based",
            "implemented": "hexa-dominant body-fitted",
            "all_hexa_claimed": False,
            "minimum_hexahedron_fraction": 0.80,
            "reason": "curved snapped boundaries and prism wall layers create non-hexa cells",
        },
        "background_bounds_m": {"min": list(lower), "max": list(upper)},
        "background_divisions": list(block_counts),
        "background_nominal_cell_m": preset.background_cell_m,
        "geometry_sha256": config.geometry_sha256,
        "cad_geometry_checks_passed": cad_result["cad_geometry_checks_passed"],
        "surface_length_unit": "m",
        "copied_surface_count": len(copied_files),
        "battery_surface_count": len(EXPECTED_BATTERY_FILES),
        "expected_pre_split_cell_zones": [
            "coolant",
            *(name.removesuffix(".stl") for name in EXPECTED_BATTERY_FILES),
        ],
        "expected_regions": ["coolant", "batterySolid"],
        "solver_source": solver_source,
        "region_grouping": {
            "batterySolid": "battery_C.*",
            "coolant": "coolant",
            "method": "splitMeshRegions -cellZonesOnly -addZones",
        },
        "prism_layers": {
            "enabled_in_source": True,
            "surface_layers": PRISM_LAYER_COUNT,
            "first_layer_thickness_m": preset.first_layer_thickness_m,
            "expansion_ratio": 1.2,
            "minimum_average_layer_coverage_fraction": MINIMUM_LAYER_COVERAGE,
            "minimum_average_wanted_thickness_fraction": MINIMUM_WANTED_THICKNESS_FRACTION,
            "validated": False,
            "required_gate": "coverage plus actual y+ distribution",
        },
        "direct_zone_path": (
            "snappyHexMesh closed-surface battery cellZone/faceZone plus "
            "topoSet complement coolant cellZone"
        ),
        "fallback_zone_path": "explicit topoSet cylinderToCell; never automatic",
        "validation": {
            "closed_outer_and_23_battery_surfaces": "generated_not_run",
            "cellzone_names_nonempty_exclusive_coverage": "generated_not_run",
            "mesh_integrated_zone_volumes": "generated_not_run",
            "mesh_integrated_port_patch_areas": "generated_not_run",
            "paired_23_interface_areas": "generated_not_run",
            "checkMesh_pre_split_extended_diagnostic": "not_run_nonblocking",
            "checkMesh_all_regions": "not_run",
            "layer_coverage": "not_run",
            "aggregate_production_mesh_gate": "generated_not_run",
            "y_plus": "not_run",
        },
        "production_mesh_ready": False,
        "runnable_production": False,
        "blocker": "PRODUCTION_CHECKMESH",
        "file_sha256": generated_hashes,
    }
    _atomic_write(
        output / "production_mesh_manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return manifest
