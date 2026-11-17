#!/bin/sh
set -eu

case_dir=${1:-/case}
log_dir="$case_dir/logs/production_mesh"
report_dir="$case_dir/reports/production_mesh"
mkdir -p "$log_dir" "$report_dir"

surfaceCheck -case "$case_dir" "$case_dir/constant/triSurface/outerDomain.stl" \
  >"$log_dir/surfaceCheck_outerDomain.log" 2>&1
python3 "$case_dir/tools/parse_surfacecheck.py" \
  --log "$log_dir/surfaceCheck_outerDomain.log" \
  --output "$report_dir/surfaceCheck_outerDomain.json"

for surface in "$case_dir"/constant/triSurface/battery_C??.stl; do
  name=$(basename "$surface" .stl)
  surfaceCheck -case "$case_dir" "$surface" \
    >"$log_dir/surfaceCheck_${name}.log" 2>&1
  python3 "$case_dir/tools/parse_surfacecheck.py" \
    --log "$log_dir/surfaceCheck_${name}.log" \
    --output "$report_dir/surfaceCheck_${name}.json"
done

python3 "$case_dir/tools/validate_cell_zones.py" \
  --mesh-dir "$case_dir/constant/polyMesh" --mode pre_split \
  --output "$report_dir/cellZones_pre_split.json"
python3 "$case_dir/tools/validate_cell_zones.py" \
  --mesh-dir "$case_dir/constant/batterySolid/polyMesh" --mode battery_region \
  --output "$report_dir/cellZones_batterySolid.json"
python3 "$case_dir/tools/validate_mesh_integrals.py" \
  --mesh-dir "$case_dir/constant/polyMesh" \
  --expected-battery-volume 0.000443438944646853 \
  --expected-coolant-volume 0.000656880852298322 \
  --expected-inlet-area 0.000855298599939821 \
  --expected-outlet-area 0.000283528736986479 \
  --output "$report_dir/mesh_integrals_pre_split.json"
python3 "$case_dir/tools/validate_interfaces.py" \
  --coolant-mesh-dir "$case_dir/constant/coolant/polyMesh" \
  --solid-mesh-dir "$case_dir/constant/batterySolid/polyMesh" \
  --expected-each-area 0.00462599518241 \
  --geometry-tolerance 0.06 \
  --output "$report_dir/interfaces_post_split.json"
python3 "$case_dir/tools/parse_checkmesh.py" \
  --log "$log_dir/checkMesh_all_regions.log" \
  --expected-mesh-ok-count 2 \
  --max-non-orthogonality 65 \
  --max-skewness 5 \
  --max-internal-skewness 4 \
  --max-boundary-skewness 5 \
  --mesh-dir "$case_dir/constant/coolant/polyMesh" \
  --mesh-dir "$case_dir/constant/batterySolid/polyMesh" \
  --output "$report_dir/checkMesh_all_regions.json"
python3 "$case_dir/tools/validate_mesh_target.py" \
  --zone-report "$report_dir/cellZones_pre_split.json" \
  --checkmesh-log "$log_dir/checkMesh_pre_split_diagnostic.log" \
  --target-total-cells 2017868 \
  --target-solid-cells 377568 \
  --target-fluid-cells 1640300 \
  --total-tolerance-fraction 0.001 \
  --component-tolerance-fraction 0.001 \
  --minimum-hexahedron-fraction 1.0 \
  --output "$report_dir/mesh_target_composition.json"
python3 "$case_dir/tools/aggregate_production_gate.py" \
  --report-dir "$report_dir" \
  --output "$report_dir/production_mesh_gate.json"
