"""Reference-guided all-hexahedral sweep mesh source generation."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import ResolvedConfig
from .geometry import cell_placements
from .reference_mesh import inspect_gambit_sweep_journal
from .sweep_mesh_template import render_gmsh_sweep_generator


SUPPORTED_SWEEP_TARGETS = (400_000, 600_000, 800_000, 1_000_000, 1_050_000)


@dataclass(frozen=True)
class SweepSection:
    name: str
    z_start_m: float
    z_end_m: float
    axial_intervals: int
    region_rule: str
    source_quad_target: int


def _polygon_area(points: tuple[tuple[float, float], ...]) -> float:
    return abs(
        sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
        )
    ) / 2.0


def build_sweep_mesh_plan(
    config: ResolvedConfig,
    target_cells: int,
    *,
    journal_path: str | Path | None = None,
) -> dict[str, object]:
    """Create the source-face and axial cell budget for a sweep mesh."""

    if target_cells not in SUPPORTED_SWEEP_TARGETS:
        raise ValueError(
            f"unsupported sweep target {target_cells}; expected {SUPPORTED_SWEEP_TARGETS}"
        )
    placements = cell_placements(config)
    if len(placements) != 23:
        raise ValueError("the reference-guided topology requires exactly 23 batteries")
    for label, centre, radius in (
        ("inlet", config.ports.inlet_center_xy_m, config.ports.inlet_diameter_m / 2.0),
        ("outlet", config.ports.outlet_center_xy_m, config.ports.outlet_diameter_m / 2.0),
    ):
        if not any(
            math.hypot(item.center_x_m - centre[0], item.center_y_m - centre[1]) < 1e-9
            and abs(config.cells.diameter_m / 2.0 - radius) < 1e-9
            for item in placements
        ):
            raise ValueError(
                f"{label} must coincide with one battery footprint for the current sweep backend"
            )

    tank_area = _polygon_area(config.enclosure.outline_polygon_m)
    height = config.enclosure.height_z_m
    port_length = config.ports.external_length_m
    inlet_area = math.pi * config.ports.inlet_diameter_m**2 / 4.0
    outlet_area = math.pi * config.ports.outlet_diameter_m**2 / 4.0
    total_volume = tank_area * height + (inlet_area + outlet_area) * port_length
    nominal_h = (total_volume / target_cells) ** (1.0 / 3.0)
    battery_base = (height - config.cells.length_m) / 2.0
    battery_top = battery_base + config.cells.length_m
    lengths = {
        "bottom_port": port_length,
        "lower_plenum": battery_base,
        "battery_body": config.cells.length_m,
        "upper_plenum": height - battery_top,
        "top_port": port_length,
    }
    raw_intervals = {
        name: max(1, int(round(length / nominal_h))) for name, length in lengths.items()
    }
    raw_tank_layers = sum(
        raw_intervals[name] for name in ("lower_plenum", "battery_body", "upper_plenum")
    )
    port_layer_equivalent = (
        raw_intervals["bottom_port"] * outlet_area / tank_area
        + raw_intervals["top_port"] * inlet_area / tank_area
    )
    source_quads = max(
        1, int(round(target_cells / (raw_tank_layers + port_layer_equivalent)))
    )
    # The first executable mapped-O-grid prototype produced 1,238,764 cells
    # for the 800k plan.  Its cross-section is intentionally retained because
    # it gives the requested 11 graded wall intervals and clean sweep blocks.
    # Calibrate only the vertical edge splits, matching the user's original
    # workflow of fixing the top-face mesh and then splitting sweep lines.
    axial_interval_scale = 0.64
    intervals = {
        name: max(1, int(round(value * axial_interval_scale)))
        for name, value in raw_intervals.items()
    }
    # Cover the two battery caps with the same 6 um x 11 graded wall stack as
    # the cylindrical side.  The 400k grid uses one remote bulk interval and
    # keeps the thin cap layers globally orthogonal (no lateral redistribution)
    # so that this aggressive axial coarsening does not create non-orthogonal
    # transition faces.  Higher-resolution studies retain production values.
    cap_layer_count = 11
    if target_cells == 400_000:
        cap_bulk_intervals = 1
    elif target_cells == 600_000:
        cap_bulk_intervals = 2
    else:
        cap_bulk_intervals = 4
    intervals["lower_plenum"] = cap_layer_count + cap_bulk_intervals
    intervals["upper_plenum"] = cap_layer_count + cap_bulk_intervals
    if target_cells == 400_000:
        intervals["battery_body"] = 2
    elif target_cells == 600_000:
        # Keep enough orthogonal body layers to retain the accepted
        # interpolation-weight/volume-ratio margin.  Combined with two remote
        # plenum bulk intervals on each side, this reaches the approved
        # 600k--690k band without touching the wall-normal layer contract.
        intervals["battery_body"] = 13
    elif target_cells == 1_050_000:
        # The original nominal 1M profile landed at 882,168 cells because its
        # proven source topology was paired with only 22 body sweep layers.
        # Keep that topology and every 6 um / 11-layer wall contract intact,
        # and add ten orthogonal body layers to obtain 1,049,808 cells.
        intervals["battery_body"] = 32
    else:
        intervals["battery_body"] = 22
    tank_layers = sum(
        intervals[name] for name in ("lower_plenum", "battery_body", "upper_plenum")
    )
    # Preserve the proven 36-interval Blossom topology at every study level.
    # Lower-count profiles change only orthogonal axial edge intervals, never
    # the 6 um x 11 wall-normal stack or the mapped source-face mechanism.
    circumferential_intervals = 36
    wall_ring_quads = (
        len(placements) * circumferential_intervals * 11
    )
    # Preserve the measured bulk/source spacing from v15-v17.  Reducing only
    # the repeated battery O-grid tangential intervals pays for the new cap
    # layers without starving the inter-battery coolant passages.
    bulk_source_quads = 745
    inlet_quads = max(4, int(round(source_quads * inlet_area / tank_area)))
    outlet_quads = max(4, int(round(source_quads * outlet_area / tank_area)))
    buffer_layer_count = 0
    quarter_intervals = circumferential_intervals // 4
    solid_face_quads_each = (
        quarter_intervals**2 + circumferential_intervals * 5
    )
    # v17 measured 1,653 non-O-grid coolant quads on the source section.  The
    # explicit central/transition/wall blocks have exact analytical counts.
    measured_bulk_coolant_quads = 1_653
    predicted_tank_source_quads = measured_bulk_coolant_quads + len(placements) * (
        circumferential_intervals * 11 + solid_face_quads_each
    )
    expected = (
        predicted_tank_source_quads * tank_layers
        + solid_face_quads_each
        * (intervals["bottom_port"] + intervals["top_port"])
    )
    sections = (
        SweepSection("bottom_port", -port_length, 0.0, intervals["bottom_port"], "coolant outlet column", outlet_quads),
        SweepSection("lower_plenum", 0.0, battery_base, intervals["lower_plenum"], "coolant with circular partitions", source_quads),
        SweepSection("battery_body", battery_base, battery_top, intervals["battery_body"], "23 solids plus surrounding coolant", source_quads),
        SweepSection("upper_plenum", battery_top, height, intervals["upper_plenum"], "coolant with circular partitions", source_quads),
        SweepSection("top_port", height, height + port_length, intervals["top_port"], "coolant inlet column", inlet_quads),
    )
    evidence = inspect_gambit_sweep_journal(journal_path) if journal_path else None
    return {
        "schema_version": 1,
        "status": "SWEEP_MESH_PLAN_READY",
        "backend": "gmsh_occ_quad_source_plus_axial_extrusion",
        "reference_method": "GAMBIT pave + edge intervals + Cooper sweep",
        "target_cells": target_cells,
        "target_tolerance_fraction": 0.12,
        "estimated_cells": expected,
        "estimated_target_error_fraction": abs(expected - target_cells) / target_cells,
        "axial_interval_scale": axial_interval_scale,
        "calibration_evidence": {
            "prototype_target_cells": 800000,
            "prototype_actual_cells": 1238764,
            "single_variable_changed": "vertical_edge_intervals",
        },
        "nominal_isotropic_scale_m": nominal_h,
        "source_quad_target": source_quads,
        "bulk_source_quad_target": bulk_source_quads,
        "source_surface_characteristic_length_m": math.sqrt(tank_area / bulk_source_quads),
        "source_mesh_algorithm": 5,
        "source_recombination_algorithm": 1,
        "cap_transition_depth_m": 0.00025,
        "cap_transition_width_m": 0.02,
        "cap_transition_easing": "linear",
        "battery_circumferential_intervals": circumferential_intervals,
        "first_layer_height_m": 6e-6,
        "wall_layer_count": 11,
        "wall_layer_growth": 1.2,
        "cap_layer_count": cap_layer_count,
        "cap_first_layer_height_m": 6e-6,
        "cap_layer_growth": 1.2,
        "cap_bulk_intervals": cap_bulk_intervals,
        "buffer_layer_count": buffer_layer_count,
        "buffer_layer_growth": 2.0,
        "z_partitions_m": [-port_length, 0.0, battery_base, battery_top, height, height + port_length],
        "sections": [asdict(section) for section in sections],
        "battery_centres_m": [
            {"id": item.cell_id, "x": item.center_x_m, "y": item.center_y_m}
            for item in placements
        ],
        "geometry": {
            "outline_polygon_m": [list(point) for point in config.enclosure.outline_polygon_m],
            "tank_height_m": height,
            "battery_radius_m": config.cells.diameter_m / 2.0,
            "battery_base_m": battery_base,
            "battery_top_m": battery_top,
            "inlet_center_xy_m": list(config.ports.inlet_center_xy_m),
            "outlet_center_xy_m": list(config.ports.outlet_center_xy_m),
            "inlet_radius_m": config.ports.inlet_diameter_m / 2.0,
            "outlet_radius_m": config.ports.outlet_diameter_m / 2.0,
        },
        "hard_gates": {
            "source_faces_all_quadrilateral": True,
            "volume_cells_all_hexahedral": True,
            "negative_or_zero_volume_cells": 0,
            "connected_coolant_regions": 1,
            "battery_regions": 23,
            "conformal_shared_interfaces": True,
            "openfoam_checkmesh_required": True,
            "openfoam_max_internal_skewness": 4.0,
            "openfoam_max_boundary_skewness": 5.0,
            "openfoam_max_non_orthogonality_deg": 65.0,
        },
        "reference_journal_evidence": evidence,
    }


def write_sweep_mesh_source_bundle(
    config: ResolvedConfig,
    output_directory: str | Path,
    *,
    target_cells: int,
    journal_path: str | Path | None = None,
) -> dict[str, object]:
    """Write an executable experimental sweep-mesh bundle."""

    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    plan = build_sweep_mesh_plan(config, target_cells, journal_path=journal_path)
    (output / "sweep_mesh_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "generate_sweep_mesh.py").write_text(
        render_gmsh_sweep_generator(plan), encoding="utf-8", newline="\n"
    )
    (output / "Dockerfile").write_text(
        "FROM opencfd/openfoam-default:2512\n"
        "USER root\n"
        "RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "--no-install-recommends gmsh python3-gmsh && rm -rf /var/lib/apt/lists/*\n"
        "WORKDIR /case\n",
        encoding="utf-8",
        newline="\n",
    )
    (output / "run_sweep_mesh.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "cd \"$(dirname \"$0\")\"\n"
        "python3 generate_sweep_mesh.py\n"
        "gmshToFoam sweep_mesh.msh > log.gmshToFoam 2>&1\n"
        "python3 set_openfoam_patch_types.py constant/polyMesh/boundary\n"
        "checkMesh -allGeometry -allTopology "
        "> log.checkMesh_pre_split_diagnostic 2>&1 || true\n"
        "zone_groups='((batterySolid \"battery_C.*\")(coolant coolant))'\n"
        "splitMeshRegions -cellZonesOnly -addZones \"$zone_groups\" "
        "-useFaceZones -detectOnly > log.splitMeshRegions_detectOnly 2>&1\n"
        "splitMeshRegions -cellZonesOnly -addZones \"$zone_groups\" "
        "-useFaceZones -overwrite > log.splitMeshRegions 2>&1\n"
        "mkdir -p system/coolant system/batterySolid\n"
        "cp system/meshQualityDict system/coolant/meshQualityDict\n"
        "cp system/meshQualityDict system/batterySolid/meshQualityDict\n"
        "checkMesh -constant -allRegions -meshQuality "
        "> log.checkMesh_all_regions 2>&1\n",
        encoding="utf-8",
        newline="\n",
    )
    (output / "set_openfoam_patch_types.py").write_text(
        "#!/usr/bin/env python3\n"
        "import re\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "path = Path(sys.argv[1])\n"
        "text = path.read_text(encoding='utf-8')\n"
        "pattern = re.compile(r'(?ms)(^\\s*outerWall\\s*\\{.*?\\btype\\s+)patch(\\s*;)')\n"
        "updated, count = pattern.subn(r'\\1wall\\2', text)\n"
        "if count != 1:\n"
        "    raise SystemExit(f'expected one outerWall patch block, changed {count}')\n"
        "path.write_text(updated, encoding='utf-8', newline='\\n')\n",
        encoding="utf-8",
        newline="\n",
    )
    system = output / "system"
    system.mkdir(exist_ok=True)
    constant = output / "constant"
    constant.mkdir(exist_ok=True)
    (constant / "regionProperties").write_text(
        "FoamFile\n{\n    format ascii;\n    class dictionary;\n"
        "    object regionProperties;\n}\n"
        "regions\n(\n    fluid (coolant)\n    solid (batterySolid)\n);\n",
        encoding="utf-8",
        newline="\n",
    )
    (system / "controlDict").write_text(
        "FoamFile\n{\n    format ascii;\n    class dictionary;\n    object controlDict;\n}\n"
        "application checkMesh;\nstartFrom startTime;\nstartTime 0;\nstopAt endTime;\n"
        "endTime 1;\ndeltaT 1;\nwriteControl timeStep;\nwriteInterval 1;\n",
        encoding="utf-8",
        newline="\n",
    )
    (system / "fvSchemes").write_text(
        "FoamFile\n{\n    format ascii;\n    class dictionary;\n    object fvSchemes;\n}\n"
        "ddtSchemes { default steadyState; }\ngradSchemes { default Gauss linear; }\n"
        "divSchemes { default none; }\nlaplacianSchemes { default Gauss linear corrected; }\n"
        "interpolationSchemes { default linear; }\nsnGradSchemes { default corrected; }\n",
        encoding="utf-8",
        newline="\n",
    )
    (system / "fvSolution").write_text(
        "FoamFile\n{\n    format ascii;\n    class dictionary;\n    object fvSolution;\n}\n"
        "solvers {}\n",
        encoding="utf-8",
        newline="\n",
    )
    (system / "meshQualityDict").write_text(
        "FoamFile\n{\n    format ascii;\n    class dictionary;\n"
        "    object meshQualityDict;\n}\n"
        "#includeEtc \"caseDicts/meshQualityDict\"\n\n"
        f"maxNonOrtho {plan['hard_gates']['openfoam_max_non_orthogonality_deg']};\n"
        f"maxBoundarySkewness {plan['hard_gates']['openfoam_max_boundary_skewness']};\n"
        f"maxInternalSkewness {plan['hard_gates']['openfoam_max_internal_skewness']};\n"
        "minVol 1e-15;\nminTetQuality -1e30;\nminDeterminant 0.0001;\n"
        "minFaceWeight 0.001;\nminVolRatio 0.001;\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "status": "SWEEP_MESH_SOURCE_CREATED",
        "output": str(output),
        "target_cells": target_cells,
        "files": [
            "sweep_mesh_plan.json",
            "generate_sweep_mesh.py",
            "Dockerfile",
            "run_sweep_mesh.sh",
            "set_openfoam_patch_types.py",
            "system/controlDict",
            "system/fvSchemes",
            "system/fvSolution",
            "system/meshQualityDict",
            "constant/regionProperties",
        ],
        "execution_policy": "experimental; promote only after all mesh, y+ and FOM gates pass",
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
