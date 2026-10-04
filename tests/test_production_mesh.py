from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cfd_twin.config import load_config
from cfd_twin.openfoam_case import DEFAULT_SOLVER_MPI_RANKS
from cfd_twin.openfoam_zone_validation import (
    EXPECTED_BATTERY_ZONES,
    validate_cell_zones,
)
from cfd_twin.openfoam_mesh_integrals import validate_mesh_integrals
from cfd_twin.openfoam_mesh_target_validation import validate_mesh_target
from cfd_twin.openfoam_interface_validation import validate_interfaces
from cfd_twin.openfoam_layercheck import parse_layer_log
from cfd_twin.openfoam_production_gate import (
    REQUIRED_REPORTS,
    aggregate_production_gate,
)
from cfd_twin.openfoam_checkmesh import parse_checkmesh_log
from cfd_twin.openfoam_surfacecheck import parse_surfacecheck_log
from cfd_twin.openfoam_yplus_validation import validate_yplus
from cfd_twin.production_mesh import (
    DEFAULT_MESH_PROFILE,
    EXPECTED_BATTERY_FILES,
    MESH_PRESETS,
    SINGLE_MESH_TARGET_COUNTS,
    write_production_mesh_source_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "configs" / "baseline_23cell.yaml"


def _fake_cad(config, root: Path) -> Path:
    cad = root / "cad"
    surfaces = cad / "production_surfaces"
    surfaces.mkdir(parents=True)
    names = (
        "outerDomain.stl",
        "inlet.stl",
        "outlet.stl",
        "outerWall.stl",
        *EXPECTED_BATTERY_FILES,
    )
    hashes = {}
    for name in names:
        path = surfaces / name
        path.write_bytes(f"fixture:{name}\n".encode())
        hashes[f"production_surfaces/{name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {
        "cad_geometry_checks_passed": True,
        "geometry_sha256": config.geometry_sha256,
        "production_surfaces": {
            "length_unit": "m",
            "battery_files": [
                f"production_surfaces/{name}" for name in EXPECTED_BATTERY_FILES
            ],
        },
        "file_sha256": hashes,
    }
    (cad / "cad_result.json").write_text(json.dumps(result), encoding="utf-8")
    return cad


def _write_zone_fixture(mesh: Path, *, duplicate: bool = False) -> None:
    mesh.mkdir(parents=True, exist_ok=True)
    zones = [("coolant", [0])]
    for index, name in enumerate(EXPECTED_BATTERY_ZONES, start=1):
        zones.append((name, [0 if duplicate and index == 1 else index]))
    blocks = []
    for name, labels in zones:
        blocks.append(
            f'''{name}
{{
    type cellZone;
    cellLabels List<label>
    {len(labels)}
    (
        {' '.join(str(label) for label in labels)}
    );
}}'''
        )
    (mesh / "cellZones").write_text(
        '''FoamFile
{
    format ascii;
    class regIOobject;
    object cellZones;
}
24
(
'''
        + "\n".join(blocks)
        + "\n)\n",
        encoding="utf-8",
    )
    if not (mesh / "owner").is_file():
        (mesh / "owner").write_text(
            '''FoamFile
{
    format ascii;
    note "nPoints:100 nCells:24 nFaces:50 nInternalFaces:10";
    object owner;
}
''',
            encoding="utf-8",
        )


def _write_24_cube_polymesh(mesh: Path, *, interface_boundaries: bool = False) -> None:
    mesh.mkdir(parents=True, exist_ok=True)
    points = []
    faces = []
    owners = []
    local_faces = (
        (0, 3, 2, 1),
        (4, 5, 6, 7),
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    )
    for cell in range(24):
        base = len(points)
        x = cell * 2.0
        points.extend(
            (
                (x, 0, 0),
                (x + 1, 0, 0),
                (x + 1, 1, 0),
                (x, 1, 0),
                (x, 0, 1),
                (x + 1, 0, 1),
                (x + 1, 1, 1),
                (x, 1, 1),
            )
        )
        faces.extend(tuple(base + index for index in face) for face in local_faces)
        owners.extend([cell] * 6)

    def header(object_name: str) -> str:
        return f'''FoamFile
{{
    format ascii;
    object {object_name};
}}
'''

    (mesh / "points").write_text(
        header("points")
        + f"{len(points)}\n(\n"
        + "\n".join(f"({x} {y} {z})" for x, y, z in points)
        + "\n)\n",
        encoding="utf-8",
    )
    (mesh / "faces").write_text(
        header("faces")
        + f"{len(faces)}\n(\n"
        + "\n".join(f"4({' '.join(str(v) for v in face)})" for face in faces)
        + "\n)\n",
        encoding="utf-8",
    )
    (mesh / "owner").write_text(
        header("owner")
        + f"{len(owners)}\n(\n"
        + "\n".join(str(value) for value in owners)
        + "\n)\n",
        encoding="utf-8",
    )
    (mesh / "neighbour").write_text(
        header("neighbour") + "0\n(\n)\n", encoding="utf-8"
    )
    if interface_boundaries:
        patches = [
            f"battery_C{index:02d}_interface {{ type mappedWall; nFaces 1; startFace {index - 1}; }}"
            for index in range(1, 24)
        ]
        patches.append(
            f"outerWall {{ type wall; nFaces {len(faces)-23}; startFace 23; }}"
        )
        boundary = (
            header("boundary")
            + f"{len(patches)}\n(\n"
            + "\n".join(patches)
            + "\n)\n"
        )
    else:
        boundary = header("boundary") + f'''3
(
inlet {{ type patch; nFaces 1; startFace 0; }}
outlet {{ type patch; nFaces 1; startFace 1; }}
outerWall {{ type wall; nFaces {len(faces)-2}; startFace 2; }}
)
'''
    (mesh / "boundary").write_text(boundary, encoding="utf-8")
    _write_zone_fixture(mesh)


class ProductionMeshTests(unittest.TestCase):
    def test_single_profile_records_the_supplied_207390_cell_contract(self) -> None:
        preset = MESH_PRESETS[DEFAULT_MESH_PROFILE]
        self.assertEqual(preset.target_cells, 207_390)
        self.assertEqual(preset.target_fluid_cells, 136_550)
        self.assertEqual(preset.target_solid_cells, 70_840)
        self.assertEqual(preset.target_points, 221_878)
        self.assertEqual(SINGLE_MESH_TARGET_COUNTS["total_cells"], 207_390)

    def test_800k_profile_encodes_the_executed_yplus_calibration(self) -> None:
        preset = MESH_PRESETS["production_800k"]
        self.assertEqual(preset.target_cells, 800_000)
        self.assertEqual(preset.background_cell_m, 0.00290)
        self.assertEqual(preset.first_layer_thickness_m, 12e-6)
        self.assertEqual(preset.minimum_hexahedron_fraction, 0.90)
        self.assertEqual(preset.maximum_non_orthogonality, 70.0)
        self.assertEqual(preset.maximum_skewness, 5.0)
        self.assertEqual(preset.generation_max_non_orthogonality, 69.5)
        self.assertEqual(
            preset.calibration_status,
            "CALIBRATED_EXECUTED_849284_YPLUS_PASSED",
        )

    def test_frame_fit_800k_trial_keeps_the_passing_wall_treatment(self) -> None:
        preset = MESH_PRESETS["production_frame_fit_800k_6um_11layers_motion"]
        self.assertEqual(preset.target_cells, 800_000)
        self.assertEqual(preset.minimum_total_cells, 800_000)
        self.assertEqual(preset.background_cell_m, 0.00290)
        self.assertEqual(preset.first_layer_thickness_m, 6e-6)
        self.assertEqual(preset.prism_layer_count, 11)
        self.assertEqual(preset.mesh_shrinker, "displacementMotionSolver")
        self.assertTrue(preset.preserve_wall_diagnostics)
        self.assertEqual(preset.minimum_hexahedron_fraction, 0.88)

        port_retry = MESH_PRESETS[
            "production_frame_fit_800k_6um_11layers_motion_port_l1"
        ]
        self.assertEqual(port_retry.port_cap_refinement_level, 1)
        self.assertEqual(port_retry.target_cells, preset.target_cells)
        self.assertEqual(port_retry.background_cell_m, preset.background_cell_m)
        self.assertEqual(port_retry.first_layer_thickness_m, preset.first_layer_thickness_m)
        self.assertEqual(port_retry.prism_layer_count, preset.prism_layer_count)

        three_mm_retry = MESH_PRESETS[
            "production_frame_fit_800k_6um_11layers_motion_3mm_port_l1"
        ]
        self.assertEqual(three_mm_retry.background_cell_m, 0.003)
        self.assertEqual(three_mm_retry.port_cap_refinement_level, 1)
        self.assertEqual(three_mm_retry.target_cells, 800_000)

    def test_600k_profile_is_recalibrated_above_the_minimum(self) -> None:
        preset = MESH_PRESETS["production_600k"]
        self.assertEqual(preset.target_cells, 600_000)
        self.assertEqual(preset.background_cell_m, 0.00319)
        self.assertEqual(preset.first_layer_thickness_m, 10e-6)
        self.assertEqual(preset.minimum_total_cells, 600_000)
        self.assertEqual(
            preset.calibration_status,
            "CALIBRATED_EXECUTED_611313_YPLUS_PASSED",
        )

    def test_600k_phi23_profile_keeps_count_grid_and_uses_8um_layer(self) -> None:
        preset = MESH_PRESETS["production_600k_phi23"]
        self.assertEqual(preset.target_cells, 600_000)
        self.assertEqual(preset.background_cell_m, 0.00319)
        self.assertEqual(preset.first_layer_thickness_m, 8e-6)
        self.assertEqual(preset.minimum_total_cells, 600_000)
        self.assertEqual(
            preset.calibration_status,
            "CALIBRATED_EXECUTED_621273_YPLUS_PASSED",
        )

    def test_600k_phi25_profile_requires_fresh_execution(self) -> None:
        preset = MESH_PRESETS["production_600k_phi25"]
        self.assertEqual(preset.target_cells, 600_000)
        self.assertEqual(preset.background_cell_m, 0.00319)
        self.assertEqual(preset.first_layer_thickness_m, 8e-6)
        self.assertEqual(preset.minimum_total_cells, 600_000)
        self.assertEqual(
            preset.calibration_status,
            "INITIAL_C05_C19_PHI25_600K_FROM_PHI23_8UM",
        )

    def test_600k_phi23_adds_local_level2_port_cap_refinement(self) -> None:
        config = load_config(
            BASELINE,
            overlay_paths=(
                ROOT / "configs" / "recalculation_600k_phi23_vertical_flip_20260922.yaml",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "production"
            write_production_mesh_source_bundle(
                config,
                _fake_cad(config, root),
                output,
                mesh_level="production_600k",
            )
            snappy = (output / "system" / "snappyHexMeshDict").read_text()
            self.assertIn("inletCapRefinement", snappy)
            self.assertIn("outletCapRefinement", snappy)
            self.assertEqual(snappy.count("levels ((1e15 2));"), 2)

    def test_800k_bundle_encodes_numeric_gates_and_no_ai_decision(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "production"
            manifest = write_production_mesh_source_bundle(
                config,
                _fake_cad(config, root),
                output,
                mesh_level="production_800k",
            )
            allmesh = (output / "AllmeshProduction").read_text()
            self.assertIn(
                'cp "$case_dir/system/controlDict.mesh-only" "$case_dir/system/controlDict"',
                allmesh,
            )
            self.assertIn("--target-total-cells 800000", allmesh)
            self.assertIn("--minimum-hexahedron-fraction 0.9", allmesh)
            self.assertIn("--max-non-orthogonality 70", allmesh)
            self.assertIn("--max-skewness 5", allmesh)
            self.assertEqual(
                manifest["calibration_status"],
                "CALIBRATED_EXECUTED_849284_YPLUS_PASSED",
            )
            self.assertFalse(manifest["automatic_quality_limits"]["ai_decision_allowed"])
            self.assertTrue((output / "engineering_judgements.json").is_file())

    def test_700k_contract_and_adaptive_solver_are_bundled(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "production"
            manifest = write_production_mesh_source_bundle(
                config, _fake_cad(config, root), output, mesh_level="production_700k"
            )
            self.assertEqual(manifest["target_cell_count"], 700000)
            self.assertEqual(manifest["element_family"]["minimum_hexahedron_fraction"], 0.89)
            self.assertIn("--minimum-hexahedron-fraction 0.89", (output / "AllmeshProduction").read_text())
            self.assertIn("chtMultiRegionFoamAdaptive", (output / "Dockerfile.openfoam").read_text())
            self.assertIn("chtMultiRegionFoamAdaptive", (output / "AllrunFrozenFlowThermalActual1s").read_text())
            self.assertTrue((output / "tools/patch_adaptive_cht_solver.py").is_file())

    def test_source_bundle_has_exact_23_zone_and_two_region_contract(self) -> None:
        config = load_config(BASELINE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cad = _fake_cad(config, root)
            output = root / "production"
            (output / "runs" / "stale").mkdir(parents=True)
            (output / "runs" / "stale" / "solver.log").write_text(
                "runtime output", encoding="utf-8"
            )
            (output / "processor0" / "0").mkdir(parents=True)
            (output / "processor0" / "0" / "cellLevel").write_text(
                "runtime field", encoding="utf-8"
            )
            manifest = write_production_mesh_source_bundle(
                config, cad, output, mesh_level="single"
            )
            snappy = (output / "system" / "snappyHexMeshDict").read_text()
            block_mesh = (output / "system" / "blockMeshDict").read_text()
            allmesh = (output / "AllmeshProduction").read_text()
            fallback = (
                output / "system" / "topoSetDict.batteryZonesFallback"
            ).read_text()
            self.assertEqual(manifest["battery_surface_count"], 23)
            self.assertEqual(manifest["expected_regions"], ["coolant", "batterySolid"])
            self.assertFalse(manifest["production_mesh_ready"])
            self.assertFalse(
                any(name.startswith("runs/") for name in manifest["file_sha256"])
            )
            self.assertFalse(
                any(
                    name.startswith("processor")
                    for name in manifest["file_sha256"]
                )
            )
            self.assertFalse(
                any(
                    name.startswith("postProcessing/")
                    for name in manifest["file_sha256"]
                )
            )
            self.assertEqual(snappy.count("cellZone battery_C"), 23)
            self.assertEqual(snappy.count("faceZone battery_C"), 23)
            self.assertIn("locationsInMesh", snappy)
            self.assertIn(") none)", snappy)
            self.assertNotIn(") coolant)", snappy)
            self.assertNotIn(") battery_C", snappy)
            self.assertIn("allowFreeStandingZoneFaces false;", snappy)
            self.assertIn("outerDomain\n        {\n            level (1 2);", snappy)
            self.assertIn("battery_C01\n        {\n            level (0 1);", snappy)
            self.assertIn("battery_C11\n        {\n            level (0 1);", snappy)
            self.assertIn("(-0.005 -0.005 -0.037)", block_mesh)
            self.assertIn("addLayers       true", snappy)
            self.assertIn('"battery_C[0-9][0-9]_slave"', snappy)
            self.assertIn("nSurfaceLayers 10;", snappy)
            self.assertIn("multiRegionFeatureSnap false;", snappy)
            self.assertIn("minRefinementCells  10;", snappy)
            self.assertIn("nSmoothPatch       10;", snappy)
            self.assertIn("nSolveIter         100;", snappy)
            self.assertIn('#includeEtc "caseDicts/meshQualityDict"', snappy)
            self.assertIn("maxNonOrtho 75;", snappy)
            self.assertIn("minTetQuality -1e30;", snappy)
            self.assertIn("minFaceWeight 0.001;", snappy)
            self.assertIn("minVolRatio 0.001;", snappy)
            self.assertIn("nFaceSplitInterval 5;", snappy)
            self.assertIn("nSmoothInternal    10;", snappy)
            self.assertIn("nGrow                   0;", snappy)
            self.assertIn("featureAngle            60;", snappy)
            self.assertIn("maxFaceThicknessRatio   0.5;", snappy)
            self.assertIn("maxThicknessToMedialRatio 0.3;", snappy)
            self.assertIn("nBufferCellsNoExtrude   1;", snappy)
            self.assertIn("detectExtrusionIsland   true;", snappy)
            self.assertIn("additionalReporting     true;", snappy)
            self.assertIn("errorReduction 0.75;", snappy)
            self.assertIn('zone_groups=\'((batterySolid "battery_C.*")(coolant coolant))\'', allmesh)
            self.assertIn("-cellZonesOnly -addZones", allmesh)
            self.assertIn("--mode battery_region", allmesh)
            self.assertIn("parse_checkmesh.py", allmesh)
            self.assertIn("parse_surfacecheck.py", allmesh)
            self.assertIn("parse_layercheck.py", allmesh)
            self.assertIn("--expected-target-layers 10", allmesh)
            self.assertIn("decomposePar -case", allmesh)
            self.assertIn("snappyHexMesh -case \"$case_dir\" -parallel", allmesh)
            self.assertIn('if [ "$mesh_mpi_ranks" -eq 1 ]', allmesh)
            self.assertIn('snappyHexMesh -case "$case_dir" -overwrite', allmesh)
            self.assertIn("reconstructParMesh -case", allmesh)
            self.assertIn("topoSetDict.coolantZone", allmesh)
            self.assertIn("topoSetDict.batteryCore", allmesh)
            self.assertIn("refineMeshDict.batteryCore", allmesh)
            self.assertIn("refineMesh -case", allmesh)
            self.assertIn('rm -f "$case_dir/0/polyMesh/cellMap"', allmesh)
            self.assertIn('if [ -f "$case_dir/0/polyMesh/points" ]', allmesh)
            self.assertIn("--minimum-layer-coverage 0.75", allmesh)
            self.assertIn("--minimum-thickness-fraction 0.45", allmesh)
            self.assertIn('topoSet -case "$case_dir" -constant -dict system/topoSetDict.ports', allmesh)
            self.assertIn('rm -f "$case_dir/0/nSurfaceLayers"', allmesh)
            self.assertIn('"$case_dir/0/cellLevel"', allmesh)
            self.assertIn('"$case_dir/0/pointLevel"', allmesh)
            self.assertIn("--mesh-dir \"$case_dir/constant/polyMesh\"", allmesh)
            self.assertIn("validate_interfaces.py", allmesh)
            self.assertIn("--geometry-tolerance 0.06", allmesh)
            self.assertIn("interfaces_post_split.json", allmesh)
            self.assertIn("checkMesh_pre_split_diagnostic.log", allmesh)
            self.assertIn("checkMesh_all_regions_extended_audit.log", allmesh)
            self.assertIn('checkMesh -case "$case_dir" -constant -allRegions', allmesh)
            self.assertIn(
                'checkMesh -case "$case_dir" -constant -allRegions -meshQuality',
                allmesh,
            )
            mesh_quality = (
                output / "system" / "coolant" / "meshQualityDict"
            ).read_text()
            self.assertIn("maxBoundarySkewness 20;", mesh_quality)
            self.assertIn("maxInternalSkewness 4;", mesh_quality)
            self.assertNotIn("checkMesh_pre_split.json", allmesh)
            self.assertIn('WM_PROJECT_VERSION:-', allmesh)
            self.assertNotIn("foamVersion", allmesh)
            self.assertIn("aggregate_production_gate.py", allmesh)
            self.assertIn("production_mesh_gate.json", allmesh)
            self.assertTrue(
                (output / "tools" / "aggregate_production_gate.py").is_file()
            )
            self.assertTrue((output / "tools" / "validate_yplus.py").is_file())
            self.assertTrue(
                (output / "tools" / "validate_cold_flow.py").is_file()
            )
            self.assertTrue(
                (output / "tools" / "validate_steady_cht.py").is_file()
            )
            self.assertTrue(
                (output / "tools" / "validate_frozen_flow.py").is_file()
            )
            self.assertIn(
                "from validate_cell_zones import parse_cell_zones",
                (output / "tools" / "validate_mesh_integrals.py").read_text(),
            )
            self.assertEqual(fallback.count("source cylinderToCell;"), 23)
            self.assertTrue((output / "constant" / "triSurface" / "battery_C23.stl").is_file())
            self.assertIn(
                "cfd-twin-openfoam-v2512-python3:local",
                (output / "compose.yaml").read_text(),
            )
            self.assertIn(
                'OPENFOAM_MESH_MPI_RANKS: "${OPENFOAM_MESH_MPI_RANKS:-1}"',
                (output / "compose.yaml").read_text(),
            )
            self.assertTrue((output / "AllcontinueSteadyPilot").is_file())
            self.assertTrue((output / "run-steady-continue.sh").is_file())
            self.assertTrue((output / "AllcontinueColdFlowPilot").is_file())
            self.assertTrue((output / "run-cold-flow-continue.sh").is_file())
            continuation = (output / "AllcontinueSteadyPilot").read_text()
            self.assertIn("STEADY_ADDITIONAL_ITERATIONS:-2000", continuation)
            self.assertIn("startFrom -set latestTime", continuation)
            self.assertIn("processor directory count", continuation)
            self.assertIn(
                'decomposePar -case "$run_dir" -allRegions -latestTime -force',
                continuation,
            )
            self.assertIn("validate_steady_cht.py", continuation)
            self.assertIn("relaxationFactors.fields.p_rgh -set 0.3", continuation)
            self.assertIn("relaxationFactors.equations.U -set 0.15", continuation)
            self.assertIn("relaxationFactors.equations.h -set 0.5", continuation)
            cold_continuation = (output / "AllcontinueColdFlowPilot").read_text()
            self.assertIn("COLD_FLOW_ADDITIONAL_ITERATIONS:-500", cold_continuation)
            self.assertIn("relaxationFactors.equations.h -set 0.5", cold_continuation)
            self.assertIn("validate_cold_flow.py", cold_continuation)
            decompose = (output / "system" / "decomposeParDict").read_text()
            self.assertIn(f"numberOfSubdomains {DEFAULT_SOLVER_MPI_RANKS};", decompose)
            self.assertIn("method scotch;", decompose)
            self.assertEqual(
                (output / "system" / "coolant" / "decomposeParDict").read_text(),
                decompose,
            )
            self.assertEqual(
                (
                    output
                    / "system"
                    / "batterySolid"
                    / "decomposeParDict"
                ).read_text(),
                decompose,
            )
            self.assertEqual(manifest["parallel_execution"]["default_mpi_ranks"], DEFAULT_SOLVER_MPI_RANKS)
            self.assertEqual(manifest["parallel_execution"]["default_mesh_mpi_ranks"], 1)
            self.assertTrue(
                (output / "heat_source_reference" / "heat_source_manifest.json").is_file()
            )
            coolant_zone = (output / "system" / "topoSetDict.coolantZone").read_text()
            self.assertEqual(coolant_zone.count("source zoneToCell;"), 23)
            self.assertIn("source setToCellZone;", coolant_zone)
            port_faces = (output / "system" / "topoSetDict.ports").read_text()
            self.assertEqual(port_faces.count("source patchToFace;"), 2)
            self.assertEqual(port_faces.count("patch outerDomain;"), 2)
            self.assertEqual(port_faces.count("action subset;"), 2)
            dockerfile = (output / "Dockerfile.openfoam").read_text()
            self.assertIn("FROM opencfd/openfoam-default:2512", dockerfile)
            self.assertIn("apt-get install", dockerfile)
            self.assertIn("python3", dockerfile)
            self.assertIn(
                "/Applications/Docker.app/Contents/Resources/bin/docker",
                (output / "run-production-mesh.sh").read_text(),
            )
            self.assertTrue((output / "run-production-mesh.sh").is_file())
            self.assertTrue((output / "run-production-mesh.ps1").is_file())
            cold_solution = (
                output
                / "profiles"
                / "steady_cold_flow"
                / "system"
                / "coolant"
                / "fvSolution"
            ).read_text()
            self.assertIn("nNonOrthogonalCorrectors 2;", cold_solution)
            self.assertIn("p_rgh 0.3;", cold_solution)
            self.assertIn("U 0.15;", cold_solution)
            steady_solution = (
                output / "profiles" / "steady" / "system" / "coolant" / "fvSolution"
            ).read_text()
            self.assertIn("nNonOrthogonalCorrectors 2;", steady_solution)
            self.assertIn("p_rgh 0.3;", steady_solution)
            self.assertIn("h 0.5;", steady_solution)
            steady_solid_solution = (
                output / "profiles" / "steady" / "system" / "batterySolid" / "fvSolution"
            ).read_text()
            self.assertIn("h 0.5;", steady_solid_solution)
            steady_source = (
                output
                / "profiles"
                / "steady"
                / "constant"
                / "batterySolid"
                / "fvOptions"
            ).read_text()
            transient_source = (
                output
                / "profiles"
                / "frozen_flow"
                / "constant"
                / "batterySolid"
                / "fvOptions"
            ).read_text()
            self.assertIn("h            (16325 0);", steady_source)
            self.assertNotIn("explicit table", steady_source)
            self.assertIn("h            (16325 0);", transient_source)
            self.assertEqual(
                manifest["solver_source"]["battery_temperature_function_objects"],
                115,
            )
            self.assertEqual(
                manifest["solver_source"][
                    "battery_surface_temperature_function_objects"
                ],
                69,
            )
            self.assertEqual(
                manifest["solver_source"]["solver_profiles"][
                    "full_transient_actual_1s"
                ]["field_write_interval_s"],
                1.0,
            )
            self.assertEqual(
                manifest["solver_source"]["solver_profiles"]["frozen_flow"][
                    "field_write_interval_s"
                ],
                15.0,
            )
            self.assertTrue((output / "constant" / "regionProperties").is_file())
            self.assertTrue(
                (output / "constant" / "batterySolid" / "fvOptions").is_file()
            )
            self.assertTrue((output / "0" / "coolant" / "alphat").is_file())
            self.assertTrue((output / "0" / "batterySolid" / "T").is_file())
            solid_p = (output / "0" / "batterySolid" / "p").read_text()
            self.assertIn("type calculated;", solid_p)
            self.assertIn("internalField   uniform 101325;", solid_p)
            self.assertTrue((output / "system" / "coolant" / "fvSolution").is_file())
            self.assertTrue((output / "system" / "fvSchemes").is_file())
            control = (output / "system" / "controlDict").read_text()
            self.assertIn("purgeWrite 2;", control)
            self.assertIn("writeCompression on;", control)
            self.assertEqual(control.count("type volFieldValue;"), 46)
            self.assertEqual(control.count("type surfaceFieldValue;"), 77)
            self.assertIn("regionType cellZone;", control)
            self.assertIn("regionType patch;", control)
            self.assertIn('names (".*battery_C23_interface.*");', control)
            self.assertIn("operation areaAverage;", control)
            self.assertIn("type yPlus;", control)
            self.assertIn("coolant_inlet_mass_flow", control)
            self.assertIn("coolant_outlet_mass_flow", control)
            self.assertIn("coolant_inlet_normal_velocity_avg", control)
            self.assertIn("coolant_outlet_normal_velocity_avg", control)
            self.assertIn("operation areaNormalAverage;", control)
            self.assertIn("coolant_inlet_pressure_avg", control)
            self.assertIn("coolant_outlet_pressure_avg", control)
            self.assertIn("coolant_inlet_mass_weighted_temperature", control)
            self.assertIn("coolant_outlet_mass_weighted_temperature", control)
            self.assertIn("selectionMode   all;", (output / "constant" / "batterySolid" / "fvOptions").read_text())
            frozen_solution = (
                output
                / "profiles"
                / "frozen_flow"
                / "system"
                / "coolant"
                / "fvSolution"
            ).read_text()
            frozen_control = (
                output / "profiles" / "frozen_flow" / "system" / "controlDict"
            ).read_text()
            actual_control = (
                output
                / "profiles"
                / "frozen_flow_actual_1s"
                / "system"
                / "controlDict"
            ).read_text()
            full_actual_control = (
                output
                / "profiles"
                / "full_transient_actual_1s"
                / "system"
                / "controlDict"
            ).read_text()
            transient_root = (
                output / "profiles" / "frozen_flow" / "system" / "fvSolution"
            ).read_text()
            full_transient_root = (
                output
                / "profiles"
                / "full_transient_actual_1s"
                / "system"
                / "fvSolution"
            ).read_text()
            full_solution = (
                output
                / "profiles"
                / "full_transient_dt"
                / "system"
                / "coolant"
                / "fvSolution"
            ).read_text()
            transient_solid_solution = (
                output
                / "profiles"
                / "frozen_flow"
                / "system"
                / "batterySolid"
                / "fvSolution"
            ).read_text()
            full_transient_solid_solution = (
                output
                / "profiles"
                / "full_transient_actual_1s"
                / "system"
                / "batterySolid"
                / "fvSolution"
            ).read_text()
            half_control = (
                output
                / "profiles"
                / "full_transient_dt_half"
                / "system"
                / "controlDict"
            ).read_text()
            self.assertIn("application chtMultiRegionFoam;", frozen_control)
            self.assertIn("deltaT 1;", frozen_control)
            self.assertIn("writeInterval 15;", frozen_control)
            self.assertIn("writeFormat binary;", frozen_control)
            self.assertIn("writeCompression on;", frozen_control)
            self.assertIn("writeInterval 1800;", actual_control)
            self.assertEqual(actual_control.count("type writeObjects;"), 2)
            self.assertIn("dynamic_coolant_temperature", actual_control)
            self.assertIn("dynamic_batterySolid_temperature", actual_control)
            self.assertIn("fields (T);", actual_control)
            self.assertIn("actual_xy_temperature_1s", actual_control)
            self.assertIn("actual_xy15_temperature_1s", actual_control)
            self.assertIn("actual_xy85_temperature_1s", actual_control)
            self.assertIn("actual_yz15_temperature_1s", actual_control)
            self.assertIn("actual_yz85_temperature_1s", actual_control)
            self.assertIn("actual_battery_near_surface_temperature_1s", actual_control)
            self.assertEqual(actual_control.count("interpolationScheme cell;"), 16)
            self.assertIn("coolant_temperature_volume_integral", actual_control)
            self.assertIn("batterySolid_temperature_volume_integral", actual_control)
            self.assertEqual(actual_control.count("operation volIntegrate;"), 2)
            self.assertIn("deltaT 0.1;", full_actual_control)
            self.assertIn("adjustTimeStep yes;", full_actual_control)
            self.assertIn("maxDeltaT 0.1;", full_actual_control)
            self.assertIn("writeInterval 1;", full_actual_control)
            self.assertIn("actual_xy_temperature_1s", full_actual_control)
            self.assertIn("endTime 1;", half_control)
            self.assertEqual(
                manifest["solver_source"]["solver_profiles"]["full_transient_dt"][
                    "endTime_s"
                ],
                1.0,
            )
            self.assertIn("coolant_yPlus", frozen_control)
            self.assertIn("writeInterval 1800;", frozen_control)
            self.assertIn("frozenFlow yes;", frozen_solution)
            self.assertIn("momentumPredictor no;", frozen_solution)
            self.assertIn("h 0.5;", frozen_solution)
            self.assertIn("h 0.5;", transient_solid_solution)
            self.assertIn("h 0.3;", full_solution)
            self.assertIn("h 0.3;", full_transient_solid_solution)
            self.assertIn("nOuterCorrectors 3;", transient_root)
            self.assertIn("iterations 200;", transient_root)
            self.assertIn("iterations 20;", full_transient_root)
            self.assertIn('"h" 1e-05;', transient_root)
            self.assertIn('"h" 1e-06;', full_transient_root)
            self.assertNotIn("maxIter 1;", transient_root)
            self.assertIn("frozenFlow no;", full_solution)
            self.assertIn("momentumPredictor yes;", full_solution)
            self.assertIn("maxCo 5;", half_control)
            self.assertIn("deltaT 0.05;", half_control)
            self.assertTrue((output / "AllrunFrozenFlowThermalPilot").is_file())
            self.assertTrue((output / "AllrunFrozenFlowThermalActual1s").is_file())
            self.assertTrue((output / "AllrunFullTransientActual1s").is_file())
            self.assertTrue((output / "AllrunColdFlowPilot").is_file())
            self.assertTrue((output / "run-cold-flow-pilot.sh").is_file())
            self.assertTrue((output / "run-until-converged.sh").is_file())
            self.assertTrue((output / "tools/steady_convergence.py").is_file())
            self.assertTrue((output / "tools/audit_energy_coupling.py").is_file())
            self.assertIn(
                "--threshold 1e-5",
                (output / "AllrunFrozenFlowThermalActual1s").read_text(),
            )
            controller = (output / "run-until-converged.sh").read_text()
            self.assertIn("--maximum \"$maximum\"", controller)
            self.assertIn("--increment \"$increment\"", controller)
            self.assertIn("RUN_FROZEN_FLOW_AFTER_STEADY", controller)
            self.assertTrue((output / "run-cold-flow-pilot.ps1").is_file())
            self.assertTrue((output / "run-frozen-flow-pilot.sh").is_file())
            self.assertTrue((output / "run-frozen-flow-pilot.ps1").is_file())
            self.assertTrue((output / "run-full-transient-actual-1s.sh").is_file())
            self.assertTrue((output / "run-full-transient-actual-1s.ps1").is_file())
            frozen_script = (output / "AllrunFrozenFlowThermalPilot").read_text()
            frozen_actual_script = (
                output / "AllrunFrozenFlowThermalActual1s"
            ).read_text()
            full_actual_script = (output / "AllrunFullTransientActual1s").read_text()
            cold_script = (output / "AllrunColdFlowPilot").read_text()
            self.assertIn("validate_yplus.py", cold_script)
            self.assertIn("validate_cold_flow.py", cold_script)
            self.assertIn("potentialFoam", cold_script)
            self.assertNotIn("-writephi", cold_script)
            self.assertIn("decomposePar -case \"$run_dir\" -allRegions", frozen_script)
            self.assertNotIn("-copyZero", frozen_script)
            self.assertIn("chtMultiRegionFoamAdaptive -case \"$run_dir\" -parallel", frozen_script)
            self.assertIn("reconstructPar -case \"$run_dir\" -allRegions", frozen_script)
            self.assertIn("for field in U p p_rgh k omega nut alphat phi", frozen_script)
            self.assertIn('steady_dir="$case_dir/runs/steady_cold_flow"', frozen_script)
            self.assertIn("COLD_FLOW_VALIDATION_PASSED", frozen_script)
            self.assertIn("STEADY_CHT_VALIDATION_PASSED", frozen_script)
            self.assertIn("validate_frozen_flow.py", frozen_script)
            self.assertIn("--qvol-samples", frozen_script)
            self.assertIn("--field-interval 1", frozen_script)
            self.assertIn("build_energy_balance.py", frozen_script)
            self.assertLess(
                frozen_script.index("build_energy_balance.py"),
                frozen_script.index("validate_frozen_flow.py"),
            )
            self.assertIn("--energy-balance-report", frozen_script)
            self.assertIn("--initial-fluid-temperature", frozen_script)
            self.assertIn("--initial-solid-temperature", frozen_script)
            self.assertIn("TRANSIENT_DELTA_T", frozen_script)
            self.assertIn("ENERGY_COUPLING_ITERATIONS", frozen_script)
            self.assertIn("PIMPLE.energyCoupling.iterations", frozen_script)
            self.assertIn("runtime_configuration.txt", frozen_script)
            self.assertNotIn("TRANSIENT_DELTA_T", full_actual_script)
            self.assertTrue((output / "tools/build_energy_balance.py").is_file())
            self.assertIn("extract_transient_residuals.py", full_actual_script)
            self.assertIn("transient_residuals.csv", full_actual_script)
            self.assertTrue((output / "tools/extract_transient_residuals.py").is_file())
            self.assertNotIn(
                'reconstructPar -case "$run_dir" -allRegions -latestTime',
                frozen_script,
            )
            self.assertIn(
                'reconstructPar -case "$run_dir" -allRegions -latestTime',
                frozen_actual_script,
            )
            self.assertNotIn('cp "$source_field" "$run_dir/0/coolant/T"', frozen_script)
            steady_script = (output / "AllrunSteadyPilot").read_text()
            self.assertIn("COLD_FLOW_VALIDATION_PASSED", steady_script)
            self.assertIn("validate_steady_cht.py", steady_script)
            self.assertIn('elif [ -f "$source_field.gz" ]', steady_script)
            self.assertIn('cp "$source_field.gz" "$target_field.gz"', steady_script)
            self.assertIn('elif [ -f "$source_field.gz" ]', frozen_actual_script)
            self.assertIn(
                'cp "$source_field.gz" "$target_field.gz"', frozen_actual_script
            )
            cold_source = (
                output
                / "profiles"
                / "steady_cold_flow"
                / "constant"
                / "batterySolid"
                / "fvOptions"
            ).read_text()
            self.assertIn("heat source intentionally disabled", cold_source)
            self.assertNotIn("scalarSemiImplicitSource", cold_source)
            cold_control = (
                output
                / "profiles"
                / "steady_cold_flow"
                / "system"
                / "controlDict"
            ).read_text()
            self.assertIn("endTime 6000;", cold_control)
            steady_control = (
                output / "profiles" / "steady" / "system" / "controlDict"
            ).read_text()
            self.assertIn("endTime 6000;", steady_control)
            self.assertEqual(cold_control.count("type surfaceFieldValue;"), 8)
            self.assertIn("operation areaNormalAverage;", cold_control)
            self.assertNotIn("type volFieldValue;", cold_control)
            self.assertIn("type fieldMinMax;", cold_control)

    def test_transient_keeps_distinct_fluid_and_solid_initial_temperatures(self) -> None:
        config = load_config(
            BASELINE,
            overlay_paths=(ROOT / "configs" / "ppt_bernardi_udf.yaml",),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "production"
            write_production_mesh_source_bundle(
                config,
                _fake_cad(config, root),
                output,
                mesh_level="production_800k",
            )
            script = (output / "AllrunFrozenFlowThermalActual1s").read_text()
            self.assertIn("coolant T was not reset to 293 K", script)
            self.assertIn("batterySolid T was not reset to 293.15 K", script)
            self.assertIn("--initial-fluid-temperature 293", script)
            self.assertIn("--initial-solid-temperature 293.15", script)
            self.assertIn(
                'FROZEN_FLOW_STEADY_INITIAL_TEMPERATURE_K:-293', script
            )
            compose = (output / "compose.yaml").read_text(encoding="utf-8")
            self.assertIn(
                'FROZEN_FLOW_STEADY_INITIAL_TEMPERATURE_K:-293}', compose
            )
            self.assertNotIn(
                'FROZEN_FLOW_STEADY_INITIAL_TEMPERATURE_K:-293.15}', compose
            )
            self.assertIn("TRANSIENT_DELTA_T", script)
            self.assertIn("ENERGY_COUPLING_ITERATIONS", script)

    def test_time_dependent_udf_is_constant_in_steady_and_table_in_transient(self) -> None:
        config = load_config(
            BASELINE,
            overlay_paths=(ROOT / "configs" / "peneklioglu_udf_provisional.yaml",),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "production"
            write_production_mesh_source_bundle(
                config,
                _fake_cad(config, root),
                output,
                mesh_level="production_800k",
            )
            steady_source = (
                output
                / "profiles"
                / "steady"
                / "constant"
                / "batterySolid"
                / "fvOptions"
            ).read_text()
            transient_source = (
                output
                / "profiles"
                / "frozen_flow"
                / "constant"
                / "batterySolid"
                / "fvOptions"
            ).read_text()
            self.assertIn("h            (71877.2976185 0);", steady_source)
            self.assertNotIn("explicit table", steady_source)
            self.assertIn("explicit table", transient_source)
            self.assertIn("(1800 115445.866039)", transient_source)

    def test_aggregate_production_gate_requires_every_exact_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            for name, status in REQUIRED_REPORTS.items():
                (reports / name).write_text(
                    json.dumps({"status": status}), encoding="utf-8"
                )
            passed = aggregate_production_gate(reports)
            self.assertEqual(passed["status"], "PRODUCTION_MESH_GATE_PASSED")
            self.assertTrue(passed["runnable_solver_pilots"])
            self.assertFalse(passed["publication_results_authorized"])
            self.assertEqual(passed["required_report_count"], 31)

            (reports / "surfaceCheck_battery_C23.json").unlink()
            failed = aggregate_production_gate(reports)
            self.assertEqual(failed["status"], "PRODUCTION_MESH_GATE_FAILED")
            self.assertFalse(failed["runnable_solver_pilots"])
            self.assertEqual(
                failed["errors"]["surfaceCheck_battery_C23.json"],
                "missing report",
            )

    def test_mesh_target_gate_uses_actual_counts_and_hexa_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zones = {"coolant": 136_550}
            zones.update({f"battery_C{index:02d}": 3_080 for index in range(1, 23)})
            zones["battery_C23"] = 3_080
            # 23 * 3,080 = 70,840.
            zone_report = root / "zones.json"
            zone_report.write_text(
                json.dumps(
                    {
                        "status": "CELLZONE_VALIDATION_PASSED",
                        "mesh_cell_count": 207_390,
                        "zone_cell_counts": zones,
                    }
                ),
                encoding="utf-8",
            )
            checkmesh = root / "checkMesh.log"
            checkmesh.write_text(
                "points: 221878\n"
                "cells: 207390\n"
                "    hexahedra: 196000\n"
                "    prisms: 6000\n"
                "    wedges: 390\n"
                "    pyramids: 0\n"
                "    tet wedges: 0\n"
                "    tetrahedra: 0\n"
                "    polyhedra: 5000\n",
                encoding="utf-8",
            )
            report = validate_mesh_target(
                zone_report,
                checkmesh,
                target_total_cells=207_390,
                target_fluid_cells=136_550,
                target_solid_cells=70_840,
                target_points=221_878,
            )
            self.assertEqual(report["status"], "MESH_TARGET_VALIDATION_PASSED")
            self.assertAlmostEqual(report["actual"]["hexahedron_fraction"], 196_000 / 207_390)
            self.assertFalse(report["all_hexa_claimed"])

    def test_zone_validator_requires_full_exclusive_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mesh = Path(directory) / "polyMesh"
            _write_zone_fixture(mesh)
            report = validate_cell_zones(mesh)
            self.assertEqual(report["status"], "CELLZONE_VALIDATION_PASSED")
            self.assertEqual(report["covered_cell_count"], 24)
            self.assertEqual(report["duplicate_cell_label_count"], 0)

            _write_zone_fixture(mesh, duplicate=True)
            failed = validate_cell_zones(mesh)
            self.assertEqual(failed["status"], "CELLZONE_VALIDATION_FAILED")
            self.assertFalse(failed["checks"]["cell_labels_unique_across_zones"])
            self.assertFalse(failed["checks"]["all_mesh_cells_covered"])

    def test_mesh_target_can_require_an_asymmetric_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zone_report = root / "zones.json"
            zone_report.write_text(
                json.dumps(
                    {
                        "status": "CELLZONE_VALIDATION_PASSED",
                        "mesh_cell_count": 569_542,
                        "zone_cell_counts": {
                            "coolant": 364_484,
                            "battery_C01": 205_058,
                        },
                    }
                ),
                encoding="utf-8",
            )
            checkmesh = root / "checkMesh.log"
            checkmesh.write_text(
                "points: 645248\n"
                "cells: 569542\n"
                "    hexahedra: 502444\n"
                "    prisms: 0\n"
                "    wedges: 0\n"
                "    pyramids: 0\n"
                "    tet wedges: 0\n"
                "    tetrahedra: 0\n"
                "    polyhedra: 67098\n",
                encoding="utf-8",
            )
            report = validate_mesh_target(
                zone_report,
                checkmesh,
                target_total_cells=600_000,
                minimum_total_cells=600_000,
                minimum_hexahedron_fraction=0.88,
            )
            self.assertEqual(report["status"], "MESH_TARGET_VALIDATION_FAILED")
            self.assertFalse(
                report["checks"]["total_cell_count_at_least_configured_minimum_if_set"]
            )

    def test_battery_region_allows_only_empty_retained_coolant_zone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mesh = Path(directory) / "polyMesh"
            mesh.mkdir(parents=True)
            blocks = []
            for index, name in enumerate(EXPECTED_BATTERY_ZONES):
                blocks.append(
                    f'''{name}
{{
    type cellZone;
    cellLabels List<label>
    1
    (
        {index}
    );
}}'''
                )
            blocks.append(
                '''coolant
{
    type cellZone;
    cellLabels List<label>
    0
    (
    );
}'''
            )
            (mesh / "cellZones").write_text(
                '''FoamFile
{
    format ascii;
    class regIOobject;
    object cellZones;
}
24
(
'''
                + "\n".join(blocks)
                + "\n)\n",
                encoding="utf-8",
            )
            (mesh / "owner").write_text(
                '''FoamFile
{
    format ascii;
    note "nPoints:100 nCells:23 nFaces:50 nInternalFaces:10";
    object owner;
}
''',
                encoding="utf-8",
            )
            report = validate_cell_zones(mesh, mode="battery_region")
            self.assertEqual(report["status"], "CELLZONE_VALIDATION_PASSED")
            self.assertEqual(report["permitted_empty_foreign_zones"], ["coolant"])

            cell_zones = (mesh / "cellZones").read_text(encoding="utf-8")
            (mesh / "cellZones").write_text(
                cell_zones.replace(
                    "cellLabels List<label>\n    0\n    (\n    );\n}",
                    "cellLabels List<label>\n    1\n    (\n        0\n    );\n}",
                ),
                encoding="utf-8",
            )
            failed = validate_cell_zones(mesh, mode="battery_region")
            self.assertEqual(failed["status"], "CELLZONE_VALIDATION_FAILED")
            self.assertEqual(failed["nonempty_permitted_foreign_zones"], ["coolant"])

    def test_mesh_integrals_use_polymesh_geometry_not_config_arithmetic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mesh = Path(directory) / "polyMesh"
            _write_24_cube_polymesh(mesh)
            report = validate_mesh_integrals(
                mesh,
                expected_battery_volume_m3=23.0,
                expected_coolant_volume_m3=1.0,
                expected_inlet_area_m2=1.0,
                expected_outlet_area_m2=1.0,
            )
            self.assertEqual(report["status"], "MESH_INTEGRAL_VALIDATION_PASSED")
            self.assertAlmostEqual(report["actual"]["battery_total_volume_m3"], 23.0)
            self.assertAlmostEqual(report["actual"]["zone_volumes_m3"]["coolant"], 1.0)
            self.assertAlmostEqual(report["actual"]["patch_areas_m2"]["inlet"], 1.0)

    def test_post_split_interface_gate_requires_23_paired_nonempty_areas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coolant = root / "coolant" / "polyMesh"
            solid = root / "batterySolid" / "polyMesh"
            _write_24_cube_polymesh(coolant, interface_boundaries=True)
            _write_24_cube_polymesh(solid, interface_boundaries=True)
            report = validate_interfaces(
                coolant,
                solid,
                expected_each_area_m2=1.0,
            )
            self.assertEqual(report["status"], "INTERFACE_VALIDATION_PASSED")
            self.assertEqual(report["actual"]["coolant_total_area_m2"], 23.0)

            boundary = solid / "boundary"
            boundary.write_text(
                boundary.read_text().replace(
                    "battery_C23_interface", "missing_C23_interface"
                ),
                encoding="utf-8",
            )
            failed = validate_interfaces(
                coolant,
                solid,
                expected_each_area_m2=1.0,
            )
            self.assertEqual(failed["status"], "INTERFACE_VALIDATION_FAILED")
            self.assertEqual(failed["missing_solid_interfaces"], ["C23"])

    def test_yplus_validator_uses_area_weighted_battery_patch_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "polyMesh"
            _write_24_cube_polymesh(mesh, interface_boundaries=True)
            blocks = []
            for index in range(1, 24):
                value = 2.0 if index == 23 else 0.5
                blocks.append(
                    f"battery_C{index:02d}_interface "
                    f"{{ type calculated; value uniform {value}; }}"
                )
            blocks.append("outerWall { type calculated; value uniform 0; }")
            field = root / "yPlus"
            field.write_text(
                '''FoamFile
{
    format ascii;
    class volScalarField;
    object yPlus;
}
internalField uniform 0;
boundaryField
{
'''
                + "\n".join(blocks)
                + "\n}\n",
                encoding="utf-8",
            )
            report = validate_yplus(mesh, field)
            self.assertEqual(report["status"], "YPLUS_VALIDATION_PASSED")
            self.assertAlmostEqual(
                report["battery_surface"]["area_fraction_below_1"], 22 / 23
            )
            self.assertEqual(report["battery_surface"]["maximum"], 2.0)

    def test_yplus_validator_reads_openfoam_gzip_field_via_plain_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            import gzip

            root = Path(directory)
            mesh = root / "polyMesh"
            _write_24_cube_polymesh(mesh, interface_boundaries=True)
            blocks = [
                f"battery_C{index:02d}_interface "
                "{ type calculated; value uniform 0.5; }"
                for index in range(1, 24)
            ]
            payload = (
                "FoamFile\n{\n    format ascii;\n    class volScalarField;\n"
                "    object yPlus;\n}\ninternalField uniform 0;\nboundaryField\n{\n"
                + "\n".join(blocks)
                + "\n}\n"
            ).encode("utf-8")
            with gzip.open(root / "yPlus.gz", "wb") as stream:
                stream.write(payload)
            report = validate_yplus(mesh, root / "yPlus")
            self.assertEqual(report["status"], "YPLUS_VALIDATION_PASSED")

    def test_layer_parser_requires_coverage_for_every_battery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "snappyHexMesh.log"
            rows = [
                f"battery_C{index:02d} 100 5 4.5 0.00015 80"
                for index in range(1, 24)
            ]
            log.write_text(
                "trapFpe: Floating point exception trapping enabled (FOAM_SIGFPE).\n"
                + "\n".join(rows),
                encoding="utf-8",
            )
            report = parse_layer_log(log)
            self.assertEqual(report["status"], "LAYER_VALIDATION_PASSED")
            self.assertAlmostEqual(
                report["battery_summaries"]["C01"]["layer_coverage_fraction"],
                0.9,
            )

            rows[-1] = "battery_C23 100 5 2.0 0.00005 30"
            log.write_text("\n".join(rows), encoding="utf-8")
            failed = parse_layer_log(log)
            self.assertEqual(failed["status"], "LAYER_VALIDATION_FAILED")
            self.assertEqual(failed["low_coverage_batteries"], ["C23"])
            self.assertEqual(failed["low_thickness_batteries"], ["C23"])

    def test_checkmesh_parser_records_quality_metrics_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "checkMesh.log"
            log.write_text(
                '''trapFpe: Floating point exception trapping enabled (FOAM_SIGFPE).
Checking geometry...
Min volume = 1.2e-12. Max volume = 9e-10.
Mesh non-orthogonality Max: 64.5 average: 11.2
Max skewness = 3.1 OK.
Minimum determinant = 0.012
Mesh OK.
''',
                encoding="utf-8",
            )
            passed = parse_checkmesh_log(log)
            self.assertEqual(passed["status"], "CHECKMESH_LOG_PASSED")
            self.assertEqual(passed["metrics"]["max_non_orthogonality"], 64.5)
            self.assertEqual(passed["metrics"]["max_skewness"], 3.1)

            log.write_text(log.read_text() + "***Max skewness = 25 Failed\n")
            failed = parse_checkmesh_log(log)
            self.assertEqual(failed["status"], "CHECKMESH_LOG_FAILED")

            log.write_text(
                '''Min volume = 1.2e-12. Max volume = 9e-10.
Mesh non-orthogonality Max: 74.9 average: 11.2
***Max skewness = 4.7, 2 highly skew faces detected
Checking faces in error :
    non-orthogonality > 75 degrees : 0
    faces with face pyramid volume < 1e-20 : 0
    faces with concavity > 80 degrees : 0
    faces with skewness > 4 (internal) or 20 (boundary) : 0
    faces with interpolation weights < 0.001 : 0
    faces with volume ratio < 0.001 : 0
    faces with face twist < 0.02 : 0
    faces on cells with determinant < 0.0001 : 0
Failed 1 mesh checks.
Min volume = 3.0e-10. Max volume = 9e-10.
Mesh non-orthogonality Max: 52.9 average: 4.2
Max skewness = 3.4 OK.
Checking faces in error :
    non-orthogonality > 75 degrees : 0
    faces with face pyramid volume < 1e-20 : 0
    faces with concavity > 80 degrees : 0
    faces with skewness > 4 (internal) or 20 (boundary) : 0
    faces with interpolation weights < 0.001 : 0
    faces with volume ratio < 0.001 : 0
    faces with face twist < 0.02 : 0
    faces on cells with determinant < 0.0001 : 0
Mesh OK.
''',
                encoding="utf-8",
            )
            overridden = parse_checkmesh_log(log, expected_mesh_ok_count=2)
            self.assertEqual(overridden["status"], "CHECKMESH_LOG_PASSED")
            self.assertEqual(overridden["metrics"]["max_skewness"], 4.7)
            self.assertEqual(overridden["metrics"]["user_quality_pass_count"], 2)

    def test_surfacecheck_parser_requires_closed_connected_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "surfaceCheck.log"
            log.write_text(
                '''trapFpe: Floating point exception trapping enabled (FOAM_SIGFPE).
Surface is closed. All edges connected to two faces.
Number of unconnected parts : 1
''',
                encoding="utf-8",
            )
            self.assertEqual(
                parse_surfacecheck_log(log)["status"], "SURFACECHECK_LOG_PASSED"
            )
            log.write_text("Surface is not closed.\n", encoding="utf-8")
            self.assertEqual(
                parse_surfacecheck_log(log)["status"], "SURFACECHECK_LOG_FAILED"
            )


if __name__ == "__main__":
    unittest.main()
