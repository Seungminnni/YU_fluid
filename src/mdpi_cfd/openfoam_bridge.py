from __future__ import annotations

import json
import os
from pathlib import Path

from .config import ResolvedConfig


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _allmesh() -> str:
    return '''#!/bin/sh
set -eu

case_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mesh_file="$case_dir/mesh/mesh.msh"

if [ ! -f "$mesh_file" ]; then
    echo "missing Gmsh mesh: $mesh_file" >&2
    exit 2
fi

command -v gmshToFoam >/dev/null 2>&1 || { echo "gmshToFoam is unavailable" >&2; exit 2; }
command -v transformPoints >/dev/null 2>&1 || { echo "transformPoints is unavailable" >&2; exit 2; }
command -v splitMeshRegions >/dev/null 2>&1 || { echo "splitMeshRegions is unavailable" >&2; exit 2; }
command -v checkMesh >/dev/null 2>&1 || { echo "checkMesh is unavailable" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is unavailable" >&2; exit 2; }

log_dir="$case_dir/logs/smoke_mesh"
report_dir="$case_dir/reports/smoke_mesh"
mkdir -p "$log_dir" "$report_dir"

gmshToFoam -case "$case_dir" "$mesh_file" >"$log_dir/gmshToFoam.log" 2>&1

# CadQuery/Gmsh coordinates are millimetres. OpenFOAM uses metres.
transformPoints -case "$case_dir" -scale '(0.001 0.001 0.001)' \
    >"$log_dir/transformPoints.log" 2>&1

check_status=0
checkMesh -case "$case_dir" -constant -allGeometry -allTopology \
    >"$log_dir/checkMesh_pre_split.log" 2>&1 || check_status=$?
python3 "$case_dir/openfoam/parse_checkmesh.py" \
    --log "$log_dir/checkMesh_pre_split.log" --expected-mesh-ok-count 1 \
    --output "$report_dir/checkMesh_pre_split.json" || check_status=$?
if [ "$check_status" -ne 0 ]; then
    echo "smoke pre-split checkMesh gate failed" >&2
    exit "$check_status"
fi

splitMeshRegions -case "$case_dir" -cellZonesOnly -overwrite \
    >"$log_dir/splitMeshRegions.log" 2>&1

check_status=0
checkMesh -case "$case_dir" -constant -allRegions -allGeometry -allTopology \
    >"$log_dir/checkMesh_all_regions.log" 2>&1 || check_status=$?
python3 "$case_dir/openfoam/parse_checkmesh.py" \
    --log "$log_dir/checkMesh_all_regions.log" --expected-mesh-ok-count 2 \
    --output "$report_dir/checkMesh_all_regions.json" || check_status=$?
if [ "$check_status" -ne 0 ]; then
    echo "smoke all-region checkMesh gate failed" >&2
    exit "$check_status"
fi

mkdir -p "$case_dir/constant/batterySolid"
cp "$case_dir/heat_source/OpenFOAM/batterySolid/fvOptions" \
   "$case_dir/constant/batterySolid/fvOptions"

echo "OpenFOAM mesh bridge completed; inspect both checkMesh reports before solving."
'''


def _readme() -> str:
    return '''# OpenFOAM v2512 mesh bridge

Run `./openfoam/Allmesh` from an OpenCFD OpenFOAM v2512 environment.

The source mesh is Gmsh 2.2 ASCII in **millimetres**. `Allmesh` converts it and
then applies a mandatory 0.001 coordinate scale before splitting the
`coolant` and `batterySolid` cell zones. Removing this scale step is an error.

Expected physical names:

- volume zones: `coolant`, `batterySolid`;
- surfaces: `inlet`, `outlet`, `walls`, `batteryInterfaces`.

This bridge has not run in the local macOS environment. Do not mark the case
runnable until the pre/post-split `checkMesh` logs are parsed and the inlet,
outlet and interface areas are rechecked in SI units.
'''


def write_openfoam_bridge(config: ResolvedConfig, output_dir: str | Path) -> dict[str, object]:
    output = Path(output_dir).resolve()
    manifest = {
        "schema_version": 1,
        "status": "OPENFOAM_BRIDGE_CREATED_NOT_EXECUTED",
        "distribution": config.solver.distribution,
        "version": config.solver.version,
        "source_mesh_format": "Gmsh 2.2 ASCII",
        "source_length_unit": "mm",
        "target_length_unit": "m",
        "mandatory_coordinate_scale": 0.001,
        "expected_volume_zones": ["coolant", "batterySolid"],
        "expected_surface_groups": ["inlet", "outlet", "walls", "batteryInterfaces"],
        "geometry_sha256": config.geometry_sha256,
        "openfoam_checkmesh_passed": False,
    }
    _atomic_write(output / "Allmesh", _allmesh())
    (output / "Allmesh").chmod(0o755)
    parser_source = Path(__file__).with_name("openfoam_checkmesh.py")
    _atomic_write(
        output / "parse_checkmesh.py", parser_source.read_text(encoding="utf-8")
    )
    _atomic_write(output / "README.md", _readme())
    _atomic_write(output / "bridge_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest
