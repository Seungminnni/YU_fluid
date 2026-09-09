from __future__ import annotations

import json
import math
import os
from pathlib import Path

from .config import ResolvedConfig
from .geometry import cell_placements
from .heat_source import render_openfoam_fvoptions, source_from_config


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _header(object_name: str, class_name: str = "dictionary", location: str | None = None) -> str:
    location_line = "" if location is None else f'    location    "{location}";\n'
    return f'''FoamFile
{{
    format      ascii;
    class       {class_name};
{location_line}    object      {object_name};
}}

'''


def _region_properties() -> str:
    return _header("regionProperties") + '''regions
(
    fluid (coolant)
    solid (batterySolid)
);
'''


def _gravity() -> str:
    return _header("g", "uniformDimensionedVectorField", "constant") + '''dimensions [0 1 -2 0 0 0 0];
value      (0 0 0);
'''


def _href() -> str:
    return _header("hRef", "uniformDimensionedScalarField", "constant") + '''dimensions [0 1 0 0 0 0 0];
value      0;
'''


def _fluid_thermo(config: ResolvedConfig) -> str:
    fluid = config.fluid
    prandtl = fluid.dynamic_viscosity_pa_s * fluid.specific_heat_j_kgk / fluid.thermal_conductivity_w_mk
    return _header("thermophysicalProperties") + f'''thermoType
{{
    type            heRhoThermo;
    mixture         pureMixture;
    transport       const;
    thermo          hConst;
    equationOfState rhoConst;
    specie          specie;
    energy          sensibleEnthalpy;
}}

mixture
{{
    specie
    {{
        molWeight   18.01528;
    }}
    equationOfState
    {{
        rho         {fluid.density_kg_m3:.12g};
    }}
    thermodynamics
    {{
        Cp          {fluid.specific_heat_j_kgk:.12g};
        Hf          0;
    }}
    transport
    {{
        mu          {fluid.dynamic_viscosity_pa_s:.12g};
        Pr          {prandtl:.12g};
    }}
}}
'''


def _solid_thermo(config: ResolvedConfig) -> str:
    battery = config.battery
    return _header("thermophysicalProperties") + f'''thermoType
{{
    type            heSolidThermo;
    mixture         pureMixture;
    transport       constIso;
    thermo          hConst;
    equationOfState rhoConst;
    specie          specie;
    energy          sensibleEnthalpy;
}}

mixture
{{
    specie
    {{
        molWeight   1;
    }}
    equationOfState
    {{
        rho         {battery.density_kg_m3:.12g};
    }}
    thermodynamics
    {{
        Cp          {battery.specific_heat_j_kgk:.12g};
        Hf          0;
    }}
    transport
    {{
        kappa       {battery.thermal_conductivity_w_mk:.12g};
    }}
}}
'''


def _turbulence_properties(config: ResolvedConfig) -> str:
    return _header("turbulenceProperties") + f'''simulationType RAS;

RAS
{{
    RASModel        {config.solver.turbulence_model};
    turbulence      on;
    printCoeffs     on;
}}
'''


def _radiation_properties() -> str:
    return _header("radiationProperties") + '''radiationModel none;
solverFreq 1;
absorptionEmissionModel none;
scatterModel none;
sootModel none;
'''


def _empty_fvoptions() -> str:
    return _header("fvOptions") + '''// S0 steady cold-flow profile: battery heat source intentionally disabled.
'''


def _field(
    name: str,
    class_name: str,
    dimensions: str,
    internal: str,
    boundary: str,
    region: str,
) -> str:
    return _header(name, class_name, f"0/{region}") + f'''dimensions      {dimensions};
internalField   uniform {internal};
boundaryField
{{
{boundary}
}}
'''


def _fluid_fields(config: ResolvedConfig) -> dict[str, str]:
    velocity = config.boundary.inlet_velocity_m_s
    temperature = config.boundary.inlet_temperature_k
    pressure = config.boundary.outlet_absolute_pressure_pa
    k = config.derived.turbulence_k_m2_s2
    omega = config.derived.turbulence_omega_s_inv
    interface = '".*battery_C[0-9][0-9]_interface.*"'
    wall_names = ("outerWall", "background", "outerDomain")

    def walls(body: str) -> str:
        return "\n".join(f"    {name}\n    {{ {body} }}" for name in wall_names)

    u_boundary = f'''    inlet
    {{
        type fixedValue;
        value uniform (0 0 {-velocity:.12g});
    }}
    outlet
    {{
        type inletOutlet;
        inletValue uniform (0 0 0);
        value uniform (0 0 0);
    }}
{walls("type noSlip;")}
    {interface}
    {{
        type noSlip;
    }}'''
    p_boundary = f'''    inlet {{ type calculated; value uniform {pressure:.12g}; }}
    outlet {{ type calculated; value uniform {pressure:.12g}; }}
{walls(f"type calculated; value uniform {pressure:.12g};")}
    {interface} {{ type calculated; value uniform {pressure:.12g}; }}'''
    p_rgh_boundary = f'''    inlet {{ type fixedFluxPressure; value uniform {pressure:.12g}; }}
    outlet {{ type fixedValue; value uniform {pressure:.12g}; }}
{walls(f"type fixedFluxPressure; value uniform {pressure:.12g};")}
    {interface} {{ type fixedFluxPressure; value uniform {pressure:.12g}; }}'''
    temperature_boundary = f'''    inlet {{ type fixedValue; value uniform {temperature:.12g}; }}
    outlet
    {{
        type inletOutlet;
        inletValue uniform {temperature:.12g};
        value uniform {temperature:.12g};
    }}
{walls("type zeroGradient;")}
    {interface}
    {{
        type compressible::turbulentTemperatureRadCoupledMixed;
        Tnbr T;
        kappaMethod fluidThermo;
        QrNbr none;
        Qr none;
        value uniform {temperature:.12g};
    }}'''
    k_boundary = f'''    inlet {{ type fixedValue; value uniform {k:.12g}; }}
    outlet
    {{
        type inletOutlet;
        inletValue uniform {k:.12g};
        value uniform {k:.12g};
    }}
{walls(f"type kqRWallFunction; value uniform {k:.12g};")}
    {interface} {{ type kqRWallFunction; value uniform {k:.12g}; }}'''
    omega_boundary = f'''    inlet {{ type fixedValue; value uniform {omega:.12g}; }}
    outlet
    {{
        type inletOutlet;
        inletValue uniform {omega:.12g};
        value uniform {omega:.12g};
    }}
{walls(f"type omegaWallFunction; value uniform {omega:.12g};")}
    {interface} {{ type omegaWallFunction; value uniform {omega:.12g}; }}'''
    nut_boundary = f'''    inlet {{ type calculated; value uniform 0; }}
    outlet {{ type calculated; value uniform 0; }}
{walls("type nutkWallFunction; value uniform 0;")}
    {interface} {{ type nutkWallFunction; value uniform 0; }}'''
    alphat_boundary = f'''    inlet {{ type calculated; value uniform 0; }}
    outlet {{ type calculated; value uniform 0; }}
{walls("type compressible::alphatWallFunction; Prt 0.85; value uniform 0;")}
    {interface}
    {{
        type compressible::alphatWallFunction;
        Prt 0.85;
        value uniform 0;
    }}'''
    return {
        "U": _field("U", "volVectorField", "[0 1 -1 0 0 0 0]", "(0 0 0)", u_boundary, "coolant"),
        "p": _field("p", "volScalarField", "[1 -1 -2 0 0 0 0]", f"{pressure:.12g}", p_boundary, "coolant"),
        "p_rgh": _field("p_rgh", "volScalarField", "[1 -1 -2 0 0 0 0]", f"{pressure:.12g}", p_rgh_boundary, "coolant"),
        "T": _field("T", "volScalarField", "[0 0 0 1 0 0 0]", f"{config.boundary.initial_fluid_temperature_k:.12g}", temperature_boundary, "coolant"),
        "k": _field("k", "volScalarField", "[0 2 -2 0 0 0 0]", f"{k:.12g}", k_boundary, "coolant"),
        "omega": _field("omega", "volScalarField", "[0 0 -1 0 0 0 0]", f"{omega:.12g}", omega_boundary, "coolant"),
        "nut": _field("nut", "volScalarField", "[0 2 -1 0 0 0 0]", "0", nut_boundary, "coolant"),
        "alphat": _field("alphat", "volScalarField", "[1 -1 -1 0 0 0 0]", "0", alphat_boundary, "coolant"),
    }


def _solid_temperature(config: ResolvedConfig) -> str:
    temperature = config.boundary.initial_solid_temperature_k
    boundary = f'''    ".*battery_C[0-9][0-9]_interface.*"
    {{
        type compressible::turbulentTemperatureRadCoupledMixed;
        Tnbr T;
        kappaMethod solidThermo;
        QrNbr none;
        Qr none;
        value uniform {temperature:.12g};
    }}'''
    return _field(
        "T",
        "volScalarField",
        "[0 0 0 1 0 0 0]",
        f"{temperature:.12g}",
        boundary,
        "batterySolid",
    )


def _solid_pressure(config: ResolvedConfig) -> str:
    """Reference pressure required by heSolidThermo/rhoConst in v2512."""
    pressure = config.boundary.outlet_absolute_pressure_pa
    boundary = f'''    ".*battery_C[0-9][0-9]_interface.*"
    {{
        type calculated;
        value uniform {pressure:.12g};
    }}'''
    return _field(
        "p",
        "volScalarField",
        "[1 -1 -2 0 0 0 0]",
        f"{pressure:.12g}",
        boundary,
        "batterySolid",
    )


def _fluid_schemes(*, transient: bool = False) -> str:
    ddt_scheme = "Euler" if transient else "steadyState"
    content = _header("fvSchemes") + '''ddtSchemes
{
    default __DDT_SCHEME__;
}
gradSchemes
{
    default         Gauss linear;
    grad(U)         cellLimited Gauss linear 1;
}
divSchemes
{
    default                         none;
    div(phi,U)                      bounded Gauss upwind;
    div(phi,K)                      Gauss linear;
    div(phi,h)                      bounded Gauss upwind;
    div(phi,k)                      bounded Gauss upwind;
    div(phi,omega)                  bounded Gauss upwind;
    div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear;
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
wallDist
{
    method meshWave;
}
'''
    return content.replace("__DDT_SCHEME__", ddt_scheme)


def _solid_schemes(*, transient: bool = False) -> str:
    ddt_scheme = "Euler" if transient else "steadyState"
    content = _header("fvSchemes") + '''ddtSchemes
{
    default __DDT_SCHEME__;
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
    return content.replace("__DDT_SCHEME__", ddt_scheme)


def _fluid_solution(
    *, conservative: bool = False, enthalpy_relaxation: float | None = None
) -> str:
    non_orthogonal_correctors = 2 if conservative else 1
    pressure_relaxation = 0.3 if conservative else 0.7
    velocity_relaxation = 0.15 if conservative else 0.3
    if enthalpy_relaxation is None:
        enthalpy_relaxation = 0.2 if conservative else 0.5
    turbulence_relaxation = 0.15 if conservative else 0.3
    content = _header("fvSolution") + '''solvers
{
    Phi
    {
        solver GAMG;
        tolerance 1e-8;
        relTol 0;
        smoother GaussSeidel;
    }
    p_rgh
    {
        solver GAMG;
        tolerance 1e-8;
        relTol 0.05;
        smoother GaussSeidel;
    }
    p_rghFinal
    {
        solver GAMG;
        tolerance 1e-8;
        relTol 0;
        smoother GaussSeidel;
    }
    "(U|h|k|omega)"
    {
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance 1e-8;
        relTol 0.05;
    }
    "(U|h|k|omega)Final"
    {
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance 1e-8;
        relTol 0;
    }
}
SIMPLE
{
    nNonOrthogonalCorrectors __NON_ORTHOGONAL_CORRECTORS__;
    momentumPredictor true;
    frozenFlow no;
    pRefCell 0;
    pRefValue 101325;
    residualControl
    {
        p_rgh 1e-5;
        U 1e-6;
        h 1e-7;
        k 1e-6;
        omega 1e-6;
    }
}
relaxationFactors
{
    fields
    {
        p_rgh __PRESSURE_RELAXATION__;
    }
    equations
    {
        U __VELOCITY_RELAXATION__;
        h __ENTHALPY_RELAXATION__;
        k __TURBULENCE_RELAXATION__;
        omega __TURBULENCE_RELAXATION__;
    }
}
'''
    return (
        content.replace(
            "__NON_ORTHOGONAL_CORRECTORS__", str(non_orthogonal_correctors)
        )
        .replace("__PRESSURE_RELAXATION__", str(pressure_relaxation))
        .replace("__VELOCITY_RELAXATION__", str(velocity_relaxation))
        .replace("__ENTHALPY_RELAXATION__", str(enthalpy_relaxation))
        .replace("__TURBULENCE_RELAXATION__", str(turbulence_relaxation))
    )


def _transient_fluid_solution(*, frozen_flow: bool) -> str:
    frozen = "yes" if frozen_flow else "no"
    momentum_predictor = "no" if frozen_flow else "yes"
    return _header("fvSolution") + f'''solvers
{{
    rho
    {{
        solver PCG;
        preconditioner DIC;
        tolerance 1e-8;
        relTol 0.05;
    }}
    rhoFinal
    {{
        solver PCG;
        preconditioner DIC;
        tolerance 1e-8;
        relTol 0;
    }}
    p_rgh
    {{
        solver GAMG;
        tolerance 1e-8;
        relTol 0.05;
        smoother GaussSeidel;
    }}
    p_rghFinal
    {{
        solver GAMG;
        tolerance 1e-8;
        relTol 0;
        smoother GaussSeidel;
    }}
    "(U|h|k|omega)"
    {{
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance 1e-8;
        relTol 0.05;
    }}
    "(U|h|k|omega)Final"
    {{
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance 1e-8;
        relTol 0;
    }}
}}
PIMPLE
{{
    nCorrectors 2;
    nNonOrthogonalCorrectors 1;
    momentumPredictor {momentum_predictor};
    frozenFlow {frozen};
    pRefCell 0;
    pRefValue 101325;
}}
relaxationFactors
{{
    equations
    {{
        h 0.9;
    }}
}}
'''


def _solid_solution(*, conservative: bool = False) -> str:
    relaxation = 0.5 if conservative else 0.8
    content = _header("fvSolution") + '''solvers
{
    h
    {
        solver PCG;
        preconditioner DIC;
        tolerance 1e-9;
        relTol 0.01;
    }
    hFinal
    {
        solver PCG;
        preconditioner DIC;
        tolerance 1e-9;
        relTol 0;
    }
}
SIMPLE
{
    nNonOrthogonalCorrectors 1;
}
relaxationFactors
{
    equations
    {
        h __ENTHALPY_RELAXATION__;
    }
}
'''
    return content.replace("__ENTHALPY_RELAXATION__", str(relaxation))


def _transient_solid_solution() -> str:
    return _header("fvSolution") + '''solvers
{
    h
    {
        solver PCG;
        preconditioner DIC;
        tolerance 1e-9;
        relTol 0.01;
    }
    hFinal
    {
        solver PCG;
        preconditioner DIC;
        tolerance 1e-9;
        relTol 0;
    }
}
PIMPLE
{
    nNonOrthogonalCorrectors 1;
}
'''


def _root_solution(*, transient: bool) -> str:
    if transient:
        return _header("fvSolution") + '''solvers {}
PIMPLE
{
    nOuterCorrectors 3;
    energyCoupling
    {
        maxIter 1;
    }
}
'''
    return _header("fvSolution") + '''solvers {}
SIMPLE {}
'''


def _functions(
    *, transient: bool = False, include_battery_temperatures: bool = True
) -> str:
    sample_control = "adjustableRunTime" if transient else "timeStep"
    sample_interval = "1" if transient else "10"
    yplus_control = "adjustableRunTime" if transient else "timeStep"
    # Frozen-flow velocity and turbulence are copied unchanged from the
    # validated cold-flow solution. A final-time y+ snapshot is sufficient and
    # avoids creating extra 10 s time directories between 15 s thermal frames.
    yplus_interval = "1800" if transient else "100"
    entries = []
    for patch in ("inlet", "outlet"):
        entries.append(
            f'''    coolant_{patch}_mass_flow
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        region coolant;
        regionType patch;
        name {patch};
        operation sum;
        fields (phi);
        empty-surface strict;
        writeControl {sample_control};
        writeInterval {sample_interval};
        writeFields false;
    }}'''
        )
        entries.append(
            f'''    coolant_{patch}_pressure_avg
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        region coolant;
        regionType patch;
        name {patch};
        operation areaAverage;
        fields (p);
        empty-surface strict;
        writeControl {sample_control};
        writeInterval {sample_interval};
        writeFields false;
    }}'''
        )
        entries.append(
            f'''    coolant_{patch}_mass_weighted_temperature
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        region coolant;
        regionType patch;
        name {patch};
        operation weightedAverage;
        weightField phi;
        fields (T);
        empty-surface strict;
        writeControl {sample_control};
        writeInterval {sample_interval};
        writeFields false;
    }}'''
        )
    if include_battery_temperatures:
        for index in range(1, 24):
            zone = f"battery_C{index:02d}"
            interface_pattern = f'".*{zone}_interface.*"'
            for operation, suffix in (("volAverage", "Tavg"), ("max", "Tmax")):
                entries.append(
                    f'''    {zone}_{suffix}
    {{
        type volFieldValue;
        libs (fieldFunctionObjects);
        region batterySolid;
        regionType cellZone;
        name {zone};
        operation {operation};
        fields (T);
        writeControl {sample_control};
        writeInterval {sample_interval};
        writeFields false;
    }}'''
                )
            for operation, suffix in (
                ("min", "Tmin"),
                ("areaAverage", "Tavg"),
                ("max", "Tmax"),
            ):
                entries.append(
                    f'''    {zone}_surface_{suffix}
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        region batterySolid;
        regionType patch;
        name {zone}_interfaces;
        names ({interface_pattern});
        operation {operation};
        fields (T);
        empty-surface strict;
        writeControl {sample_control};
        writeInterval {sample_interval};
        writeFields false;
    }}'''
                )
    entries.append(
        f'''    coolant_field_bounds
    {{
        type fieldMinMax;
        libs (fieldFunctionObjects);
        region coolant;
        mode magnitude;
        location false;
        fields (k omega U T p);
        writeControl {sample_control};
        writeInterval {sample_interval};
    }}'''
    )
    entries.append(
        f'''    coolant_yPlus
    {{
        type yPlus;
        libs (fieldFunctionObjects);
        region coolant;
        writeControl {yplus_control};
        writeInterval {yplus_interval};
    }}'''
    )
    return "\n".join(entries)


def _steady_control_dict(*, include_battery_temperatures: bool = True) -> str:
    return _header("controlDict") + f'''application chtMultiRegionSimpleFoam;
startFrom startTime;
startTime 0;
stopAt endTime;
endTime 2000;
deltaT 1;
writeControl timeStep;
writeInterval 100;
purgeWrite 0;
writeFormat ascii;
writePrecision 12;
writeCompression off;
timeFormat general;
timePrecision 8;
runTimeModifiable true;

functions
{{
{_functions(include_battery_temperatures=include_battery_temperatures)}
}}
'''


def _format_probe_locations(points: list[tuple[float, float, float]]) -> str:
    return "\n".join(
        f"            ({x:.12g} {y:.12g} {z:.12g})" for x, y, z in points
    )


def _probe_function(
    name: str,
    *,
    region: str,
    field: str,
    points: list[tuple[float, float, float]],
    interval_s: float,
) -> str:
    return f'''    {name}
    {{
        type probes;
        libs (sampling);
        region {region};
        fields ({field});
        fixedLocations true;
        interpolationScheme cell;
        writeControl adjustableRunTime;
        writeInterval {interval_s:.12g};
        probeLocations
        (
{_format_probe_locations(points)}
        );
    }}'''


def _actual_one_second_sampling(config: ResolvedConfig) -> str:
    """Compact direct-cell probes for the 1 s L3 visualisation run.

    The regular section locations are denser than the L3 background spacing,
    but ``interpolationScheme cell`` deliberately returns the value of the
    containing OpenFOAM cell.  Invalid locations stay invalid; the viewer must
    not fill or pad them.
    """

    def centred_axis(length: float, spacing: float) -> list[float]:
        count = max(1, math.ceil(length / spacing))
        return [length * (index + 0.5) / count for index in range(count)]

    x_values = centred_axis(config.enclosure.width_x_m, 0.001)
    y_values = centred_axis(config.enclosure.length_y_m, 0.001)
    z_values = centred_axis(config.enclosure.height_z_m, 0.0015)
    entries = []
    for label, fraction in (
        ("xy15", 0.15),
        ("xy33", 0.325),
        ("xy", 0.5),
        ("xy68", 0.675),
        ("xy85", 0.85),
    ):
        points = [
            (x, y, fraction * config.enclosure.height_z_m)
            for y in y_values
            for x in x_values
        ]
        function_name = (
            "actual_xy_temperature_1s"
            if label == "xy"
            else f"actual_{label}_temperature_1s"
        )
        entries.append(
            _probe_function(
                function_name,
                region="coolant",
                field="T",
                points=points,
                interval_s=1.0,
            )
        )
    for label, fraction in (
        ("xz15", 0.15),
        ("xz33", 0.325),
        ("xz50", 0.5),
        ("xz68", 0.675),
        ("xz85", 0.85),
    ):
        points = [
            (x, fraction * config.enclosure.length_y_m, z)
            for z in z_values
            for x in x_values
        ]
        entries.append(
            _probe_function(
                f"actual_{label}_temperature_1s",
                region="coolant",
                field="T",
                points=points,
                interval_s=1.0,
            )
        )

    radius = 0.5 * config.cells.diameter_m
    half_length = 0.5 * config.cells.length_m
    radial_probe = max(0.0, radius - 0.00035)
    cap_radius = 0.58 * radius
    surface_points: list[tuple[float, float, float]] = []
    for placement in cell_placements(config):
        for axial in range(16):
            z = (
                placement.center_z_m
                - half_length
                + config.cells.length_m * (axial + 0.5) / 16
            )
            for angular in range(32):
                angle = 2 * math.pi * (angular + 0.5) / 32
                surface_points.append(
                    (
                        placement.center_x_m + radial_probe * math.cos(angle),
                        placement.center_y_m + radial_probe * math.sin(angle),
                        z,
                    )
                )
        for z in (
            placement.center_z_m - half_length + 0.00035,
            placement.center_z_m + half_length - 0.00035,
        ):
            for angular in range(32):
                angle = 2 * math.pi * (angular + 0.5) / 32
                surface_points.append(
                    (
                        placement.center_x_m + cap_radius * math.cos(angle),
                        placement.center_y_m + cap_radius * math.sin(angle),
                        z,
                    )
                )
    entries.append(
        _probe_function(
            "actual_battery_near_surface_temperature_1s",
            region="batterySolid",
            field="T",
            points=surface_points,
            interval_s=1.0,
        )
    )
    return "\n".join(entries)


def _transient_control_dict(
    config: ResolvedConfig,
    *,
    delta_t: float,
    frozen_flow: bool,
    field_write_interval_s: float = 15.0,
    actual_one_second_sampling: bool = False,
) -> str:
    adjustable = "no" if frozen_flow else "yes"
    extra_functions = (
        "\n" + _actual_one_second_sampling(config)
        if actual_one_second_sampling
        else ""
    )
    return _header("controlDict") + f'''application chtMultiRegionFoam;
startFrom startTime;
startTime 0;
stopAt endTime;
endTime 1800;
deltaT {delta_t:.12g};
adjustTimeStep {adjustable};
maxCo 5;
maxDeltaT {delta_t:.12g};
writeControl adjustableRunTime;
writeInterval {field_write_interval_s:.12g};
purgeWrite 0;
writeFormat binary;
writePrecision 12;
writeCompression on;
timeFormat general;
timePrecision 8;
runTimeModifiable true;

functions
{{
{_functions(transient=True)}{extra_functions}
}}
'''


def _steady_pilot_script(
    config: ResolvedConfig,
    *,
    profile: str,
    run_name: str,
    copy_cold_flow: bool,
) -> str:
    flow_copy = ""
    flow_initialisation = ""
    required_initialisation_tool = ""
    post_validation = ""
    completion_message = (
        "Steady CHT pilot passed residual, temperature-stability, mass and energy gates."
    )
    if profile == "steady_cold_flow":
        required_initialisation_tool = " potentialFoam python3"
        flow_initialisation = '''
potentialFoam -case "$run_dir" -region coolant -initialiseUBCs \
    -noFunctionObjects >"$run_dir/logs/solver/potentialFoam.log" 2>&1
'''
        post_validation = '''
latest_time=$(foamListTimes -case "$run_dir" -latestTime)
python3 "$case_dir/tools/validate_yplus.py" \
    --mesh-dir "$run_dir/constant/coolant/polyMesh" \
    --field "$run_dir/$latest_time/coolant/yPlus" \
    --output "$run_dir/reports/solver/yplus.json" || :
python3 "$case_dir/tools/validate_cold_flow.py" \
    --log "$run_dir/logs/solver/steady_cold_flow.log" \
    --yplus-report "$run_dir/reports/solver/yplus.json" \
    --output "$run_dir/reports/solver/cold_flow_validation.json"
'''
        completion_message = (
            "Cold-flow pilot passed residual, mass-balance, pressure-stability, "
            "field-bounds and y+ gates."
        )
    elif profile == "steady":
        required_initialisation_tool = " python3"
        post_validation = f'''
python3 "$case_dir/tools/validate_steady_cht.py" \
    --log "$run_dir/logs/solver/steady_cht.log" \
    --cold-flow-report "$case_dir/runs/steady_cold_flow/reports/solver/cold_flow_validation.json" \
    --mesh-integral-report "$case_dir/reports/production_mesh/mesh_integrals_pre_split.json" \
    --qvol {config.battery.heat_source.qvol_w_m3:.12g} \
    --coolant-cp {config.fluid.specific_heat_j_kgk:.12g} \
    --initial-temperature {config.boundary.initial_solid_temperature_k:.12g} \
    --output "$run_dir/reports/solver/steady_cht_validation.json"
'''
    if copy_cold_flow:
        flow_copy = '''
cold_dir="$case_dir/runs/steady_cold_flow"
[ -d "$cold_dir" ] || { echo "steady cold-flow pilot is required first" >&2; exit 2; }
cold_report="$cold_dir/reports/solver/cold_flow_validation.json"
[ -f "$cold_report" ] || { echo "validated steady cold-flow report is required" >&2; exit 2; }
grep -q '"status": "COLD_FLOW_VALIDATION_PASSED"' "$cold_report" || {
    echo "steady cold-flow compound gate has not passed" >&2
    exit 2
}
cold_time=$(foamListTimes -case "$cold_dir" -latestTime)
case "$cold_time" in
    ""|0|0.0|0.00) echo "completed steady cold-flow fields are required" >&2; exit 2 ;;
esac
for field in U p p_rgh k omega nut alphat phi; do
    source_field="$cold_dir/$cold_time/coolant/$field"
    [ -f "$source_field" ] || { echo "missing cold-flow field: $source_field" >&2; exit 2; }
    cp "$source_field" "$run_dir/0/coolant/$field"
done
echo "$cold_time" >"$run_dir/reports/solver/source_cold_flow_time.txt"
'''
    return f'''#!/bin/sh
set -eu

case_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for report in \\
    "$case_dir/reports/production_mesh/checkMesh_all_regions.json" \\
    "$case_dir/reports/production_mesh/cellZones_pre_split.json" \\
    "$case_dir/reports/production_mesh/cellZones_batterySolid.json" \\
    "$case_dir/reports/production_mesh/mesh_integrals_pre_split.json" \\
    "$case_dir/reports/production_mesh/layer_coverage.json" \\
    "$case_dir/reports/production_mesh/interfaces_post_split.json" \\
    "$case_dir/reports/production_mesh/production_mesh_gate.json"
do
    [ -f "$report" ] || {{ echo "missing mesh gate report: $report" >&2; exit 2; }}
    grep -Eq '"status": "(CHECKMESH_LOG_PASSED|CELLZONE_VALIDATION_PASSED|MESH_INTEGRAL_VALIDATION_PASSED|LAYER_VALIDATION_PASSED|INTERFACE_VALIDATION_PASSED|PRODUCTION_MESH_GATE_PASSED)"' "$report" || {{
        echo "failed mesh gate report: $report" >&2
        exit 2
    }}
done

for tool in foamListTimes foamDictionary decomposePar mpirun reconstructPar chtMultiRegionSimpleFoam{required_initialisation_tool}; do
    command -v "$tool" >/dev/null 2>&1 || {{ echo "$tool is unavailable" >&2; exit 2; }}
done

run_dir="$case_dir/runs/{run_name}"
[ ! -e "$run_dir" ] || {{ echo "run directory already exists: $run_dir" >&2; exit 2; }}
mkdir -p "$run_dir/reports/solver"
cp -R "$case_dir/0" "$run_dir/0"
cp -R "$case_dir/constant" "$run_dir/constant"
cp -R "$case_dir/system" "$run_dir/system"

for relative in \\
    constant/batterySolid/fvOptions \\
    system/controlDict \\
    system/fvSolution \\
    system/coolant/fvSchemes \\
    system/coolant/fvSolution \\
    system/batterySolid/fvSchemes \\
    system/batterySolid/fvSolution
do
    source_file="$case_dir/profiles/{profile}/$relative"
    [ -f "$source_file" ] || {{ echo "missing profile file: $source_file" >&2; exit 2; }}
    cp "$source_file" "$run_dir/$relative"
done
{flow_copy}
mkdir -p "$run_dir/logs/solver"
{flow_initialisation}
mpi_ranks=${{OPENFOAM_MPI_RANKS:-10}}
case "$mpi_ranks" in
    ''|*[!0-9]*) echo "OPENFOAM_MPI_RANKS must be a positive integer" >&2; exit 2 ;;
esac
[ "$mpi_ranks" -ge 1 ] || {{ echo "OPENFOAM_MPI_RANKS must be at least 1" >&2; exit 2; }}
decompose_log="$run_dir/logs/solver/foamDictionary_decomposePar.log"
: >"$decompose_log"
for decompose_dict in \
    "$run_dir/system/decomposeParDict" \
    "$run_dir/system/coolant/decomposeParDict" \
    "$run_dir/system/batterySolid/decomposeParDict"
do
    [ -f "$decompose_dict" ] || {{ echo "missing decomposition dictionary: $decompose_dict" >&2; exit 2; }}
    foamDictionary "$decompose_dict" \
        -entry numberOfSubdomains -set "$mpi_ranks" \
        >>"$decompose_log" 2>&1
done
decomposePar -case "$run_dir" -allRegions -force \
    >"$run_dir/logs/solver/decomposePar.log" 2>&1
mpirun --allow-run-as-root -np "$mpi_ranks" \
    chtMultiRegionSimpleFoam -case "$run_dir" -parallel \
    >"$run_dir/logs/solver/{run_name}.log" 2>&1
reconstructPar -case "$run_dir" -allRegions -latestTime \
    >"$run_dir/logs/solver/reconstructPar.log" 2>&1
: >"$run_dir/{run_name}.foam"
{post_validation}

echo "{completion_message} Run: $run_dir"
'''


def _steady_continue_script(config: ResolvedConfig) -> str:
    return f'''#!/bin/sh
set -eu

case_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
run_dir="$case_dir/runs/steady_cht"
[ -d "$run_dir" ] || {{ echo "steady CHT run is unavailable: $run_dir" >&2; exit 2; }}
[ -d "$run_dir/processor0" ] || {{ echo "parallel steady CHT fields are unavailable" >&2; exit 2; }}

for report in \
    "$case_dir/reports/production_mesh/production_mesh_gate.json" \
    "$case_dir/runs/steady_cold_flow/reports/solver/cold_flow_validation.json"
do
    [ -f "$report" ] || {{ echo "required validation report is unavailable: $report" >&2; exit 2; }}
done
grep -q '"status": "PRODUCTION_MESH_GATE_PASSED"' \
    "$case_dir/reports/production_mesh/production_mesh_gate.json" || {{
    echo "production mesh gate has not passed" >&2
    exit 2
}}
grep -q '"status": "COLD_FLOW_VALIDATION_PASSED"' \
    "$case_dir/runs/steady_cold_flow/reports/solver/cold_flow_validation.json" || {{
    echo "cold-flow compound gate has not passed" >&2
    exit 2
}}

for tool in foamListTimes foamDictionary mpirun reconstructPar chtMultiRegionSimpleFoam python3; do
    command -v "$tool" >/dev/null 2>&1 || {{ echo "$tool is unavailable" >&2; exit 2; }}
done

mpi_ranks=${{OPENFOAM_MPI_RANKS:-10}}
additional_iterations=${{STEADY_ADDITIONAL_ITERATIONS:-2000}}
case "$mpi_ranks" in
    ''|*[!0-9]*) echo "OPENFOAM_MPI_RANKS must be a positive integer" >&2; exit 2 ;;
esac
case "$additional_iterations" in
    ''|*[!0-9]*) echo "STEADY_ADDITIONAL_ITERATIONS must be a positive integer" >&2; exit 2 ;;
esac
[ "$mpi_ranks" -ge 1 ] || {{ echo "OPENFOAM_MPI_RANKS must be at least 1" >&2; exit 2; }}
[ "$additional_iterations" -ge 1 ] || {{ echo "STEADY_ADDITIONAL_ITERATIONS must be at least 1" >&2; exit 2; }}

processor_count=$(find "$run_dir" -maxdepth 1 -type d -name 'processor[0-9]*' | wc -l | tr -d ' ')
[ "$processor_count" -eq "$mpi_ranks" ] || {{
    echo "processor directory count $processor_count does not match MPI rank count $mpi_ranks" >&2
    exit 2
}}

latest_time=$(foamListTimes -case "$run_dir" -latestTime)
case "$latest_time" in
    ''|*[!0-9]*) echo "steady continuation requires an integer latest time, got: $latest_time" >&2; exit 2 ;;
esac
target_time=$((latest_time + additional_iterations))
foamDictionary "$run_dir/system/controlDict" -entry startFrom -set latestTime >/dev/null
foamDictionary "$run_dir/system/controlDict" -entry endTime -set "$target_time" >/dev/null

# A continuation is used once conservative pressure/velocity coupling is
# established. Keep those stable flow relaxations and increase only thermal
# relaxation to reach the energy and millikelvin temperature gates sooner.
relaxation_log="$run_dir/logs/solver/steady_cht_continue_relaxation_${{target_time}}.log"
: >"$relaxation_log"
foamDictionary "$run_dir/system/coolant/fvSolution" \
    -entry relaxationFactors.fields.p_rgh -set 0.3 >>"$relaxation_log" 2>&1
foamDictionary "$run_dir/system/coolant/fvSolution" \
    -entry relaxationFactors.equations.U -set 0.15 >>"$relaxation_log" 2>&1
foamDictionary "$run_dir/system/coolant/fvSolution" \
    -entry relaxationFactors.equations.h -set 0.5 >>"$relaxation_log" 2>&1
foamDictionary "$run_dir/system/coolant/fvSolution" \
    -entry relaxationFactors.equations.k -set 0.15 >>"$relaxation_log" 2>&1
foamDictionary "$run_dir/system/coolant/fvSolution" \
    -entry relaxationFactors.equations.omega -set 0.15 >>"$relaxation_log" 2>&1
foamDictionary "$run_dir/system/batterySolid/fvSolution" \
    -entry relaxationFactors.equations.h -set 0.5 >>"$relaxation_log" 2>&1

log="$run_dir/logs/solver/steady_cht_continue_${{latest_time}}_to_${{target_time}}.log"
[ ! -e "$log" ] || {{ echo "continuation log already exists: $log" >&2; exit 2; }}
mpirun --allow-run-as-root -np "$mpi_ranks" \
    chtMultiRegionSimpleFoam -case "$run_dir" -parallel >"$log" 2>&1
reconstructPar -case "$run_dir" -allRegions -latestTime \
    >"$run_dir/logs/solver/reconstructPar_continue_${{target_time}}.log" 2>&1
: >"$run_dir/steady_cht.foam"

python3 "$case_dir/tools/validate_steady_cht.py" \
    --log "$log" \
    --cold-flow-report "$case_dir/runs/steady_cold_flow/reports/solver/cold_flow_validation.json" \
    --mesh-integral-report "$case_dir/reports/production_mesh/mesh_integrals_pre_split.json" \
    --qvol {config.battery.heat_source.qvol_w_m3:.12g} \
    --coolant-cp {config.fluid.specific_heat_j_kgk:.12g} \
    --initial-temperature {config.boundary.initial_solid_temperature_k:.12g} \
    --output "$run_dir/reports/solver/steady_cht_validation.json"

echo "Steady CHT continuation passed all gates at iteration $target_time. Run: $run_dir"
'''


def _cold_flow_continue_script() -> str:
    return '''#!/bin/sh
set -eu

case_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
run_dir="$case_dir/runs/steady_cold_flow"
[ -d "$run_dir" ] || { echo "cold-flow run is unavailable: $run_dir" >&2; exit 2; }
[ -d "$run_dir/processor0" ] || { echo "parallel cold-flow fields are unavailable" >&2; exit 2; }
mesh_report="$case_dir/reports/production_mesh/production_mesh_gate.json"
[ -f "$mesh_report" ] || { echo "production mesh report is unavailable" >&2; exit 2; }
grep -q '"status": "PRODUCTION_MESH_GATE_PASSED"' "$mesh_report" || {
    echo "production mesh gate has not passed" >&2
    exit 2
}

for tool in foamListTimes foamDictionary mpirun reconstructPar chtMultiRegionSimpleFoam python3; do
    command -v "$tool" >/dev/null 2>&1 || { echo "$tool is unavailable" >&2; exit 2; }
done

mpi_ranks=${OPENFOAM_MPI_RANKS:-10}
additional_iterations=${COLD_FLOW_ADDITIONAL_ITERATIONS:-500}
case "$mpi_ranks" in
    ''|*[!0-9]*) echo "OPENFOAM_MPI_RANKS must be a positive integer" >&2; exit 2 ;;
esac
case "$additional_iterations" in
    ''|*[!0-9]*) echo "COLD_FLOW_ADDITIONAL_ITERATIONS must be a positive integer" >&2; exit 2 ;;
esac
[ "$mpi_ranks" -ge 1 ] || { echo "OPENFOAM_MPI_RANKS must be at least 1" >&2; exit 2; }
[ "$additional_iterations" -ge 1 ] || { echo "COLD_FLOW_ADDITIONAL_ITERATIONS must be at least 1" >&2; exit 2; }
processor_count=$(find "$run_dir" -maxdepth 1 -type d -name 'processor[0-9]*' | wc -l | tr -d ' ')
[ "$processor_count" -eq "$mpi_ranks" ] || {
    echo "processor directory count $processor_count does not match MPI rank count $mpi_ranks" >&2
    exit 2
}

latest_time=$(foamListTimes -case "$run_dir" -latestTime)
case "$latest_time" in
    ''|*[!0-9]*) echo "cold-flow continuation requires an integer latest time, got: $latest_time" >&2; exit 2 ;;
esac
target_time=$((latest_time + additional_iterations))
foamDictionary "$run_dir/system/controlDict" -entry startFrom -set latestTime >/dev/null
foamDictionary "$run_dir/system/controlDict" -entry endTime -set "$target_time" >/dev/null
# The only missed gate after the conservative L2 pilot was the zero-source
# solid enthalpy residual. Dampen that equation and continue the same fields.
foamDictionary "$run_dir/system/batterySolid/fvSolution" \
    -entry relaxationFactors.equations.h -set 0.5 \
    >"$run_dir/logs/solver/cold_flow_continue_relaxation_${target_time}.log" 2>&1

log="$run_dir/logs/solver/steady_cold_flow_continue_${latest_time}_to_${target_time}.log"
[ ! -e "$log" ] || { echo "continuation log already exists: $log" >&2; exit 2; }
mpirun --allow-run-as-root -np "$mpi_ranks" \
    chtMultiRegionSimpleFoam -case "$run_dir" -parallel >"$log" 2>&1
reconstructPar -case "$run_dir" -allRegions -latestTime \
    >"$run_dir/logs/solver/reconstructPar_continue_${target_time}.log" 2>&1
: >"$run_dir/steady_cold_flow.foam"
python3 "$case_dir/tools/validate_yplus.py" \
    --mesh-dir "$run_dir/constant/coolant/polyMesh" \
    --field "$run_dir/$target_time/coolant/yPlus" \
    --output "$run_dir/reports/solver/yplus.json" || :
python3 "$case_dir/tools/validate_cold_flow.py" \
    --log "$log" \
    --yplus-report "$run_dir/reports/solver/yplus.json" \
    --output "$run_dir/reports/solver/cold_flow_validation.json"

echo "Cold-flow continuation passed all gates at iteration $target_time. Run: $run_dir"
'''


def _activate_profile_script() -> str:
    return '''#!/bin/sh
set -eu

case_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
profile=${1:-}
case "$profile" in
    steady_cold_flow|steady|frozen_flow|frozen_flow_actual_1s|full_transient_actual_1s|full_transient_dt|full_transient_dt_half) ;;
    *)
        echo "usage: $0 {steady_cold_flow|steady|frozen_flow|frozen_flow_actual_1s|full_transient_actual_1s|full_transient_dt|full_transient_dt_half}" >&2
        exit 2
        ;;
esac

profile_dir="$case_dir/profiles/$profile"
for relative in \
    constant/batterySolid/fvOptions \
    system/controlDict \
    system/fvSolution \
    system/coolant/fvSchemes \
    system/coolant/fvSolution \
    system/batterySolid/fvSchemes \
    system/batterySolid/fvSolution
do
    source_file="$profile_dir/$relative"
    [ -f "$source_file" ] || { echo "missing profile file: $source_file" >&2; exit 2; }
    cp "$source_file" "$case_dir/$relative"
done

echo "Activated OpenFOAM solver profile: $profile"
'''


def _transient_pilot_script(
    config: ResolvedConfig,
    *,
    profile: str,
    run_name: str,
    frozen_flow: bool,
    field_interval_s: float = 15.0,
    result_run: bool = False,
) -> str:
    mode = (
        "frozen-flow"
        if frozen_flow
        else "full-transient result" if result_run else "full-transient validation"
    )
    initial_temperature = config.boundary.initial_solid_temperature_k
    reconstruct_scope = "" if frozen_flow or result_run else "-latestTime"
    required_python = " python3"
    post_validation = f'''
python3 "$case_dir/tools/extract_transient_residuals.py" \
    --log "$run_dir/logs/solver/{run_name}.log" \
    --csv "$run_dir/reports/solver/transient_residuals.csv" \
    --summary "$run_dir/reports/solver/transient_residuals_summary.json"
'''
    if frozen_flow:
        post_validation += f'''
python3 "$case_dir/tools/validate_frozen_flow.py" \
    --log "$run_dir/logs/solver/{run_name}.log" \
    --run-dir "$run_dir" \
    --steady-report "$case_dir/runs/steady_cht/reports/solver/steady_cht_validation.json" \
    --mesh-integral-report "$case_dir/reports/production_mesh/mesh_integrals_pre_split.json" \
    --qvol {config.battery.heat_source.qvol_w_m3:.12g} \
    --coolant-cp {config.fluid.specific_heat_j_kgk:.12g} \
    --initial-temperature {initial_temperature:.12g} \
    --steady-initial-temperature "${{FROZEN_FLOW_STEADY_INITIAL_TEMPERATURE_K:-{initial_temperature:.12g}}}" \
    --field-interval {field_interval_s:.12g} \
    --output "$run_dir/reports/solver/frozen_flow_validation.json"
'''
    return f'''#!/bin/sh
set -eu

case_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for report in \\
    "$case_dir/reports/production_mesh/checkMesh_all_regions.json" \\
    "$case_dir/reports/production_mesh/cellZones_pre_split.json" \\
    "$case_dir/reports/production_mesh/cellZones_batterySolid.json" \\
    "$case_dir/reports/production_mesh/mesh_integrals_pre_split.json" \\
    "$case_dir/reports/production_mesh/layer_coverage.json" \\
    "$case_dir/reports/production_mesh/interfaces_post_split.json" \\
    "$case_dir/reports/production_mesh/production_mesh_gate.json"
do
    [ -f "$report" ] || {{ echo "missing mesh gate report: $report" >&2; exit 2; }}
    grep -Eq '"status": "(CHECKMESH_LOG_PASSED|CELLZONE_VALIDATION_PASSED|MESH_INTEGRAL_VALIDATION_PASSED|LAYER_VALIDATION_PASSED|INTERFACE_VALIDATION_PASSED|PRODUCTION_MESH_GATE_PASSED)"' "$report" || {{
        echo "failed mesh gate report: $report" >&2
        exit 2
    }}
done

for tool in foamListTimes foamDictionary decomposePar mpirun reconstructPar chtMultiRegionFoam{required_python}; do
    command -v "$tool" >/dev/null 2>&1 || {{ echo "$tool is unavailable" >&2; exit 2; }}
done

steady_dir="$case_dir/runs/steady_cold_flow"
[ -d "$steady_dir" ] || {{ echo "steady cold-flow pilot is required first" >&2; exit 2; }}
cold_report="$steady_dir/reports/solver/cold_flow_validation.json"
[ -f "$cold_report" ] || {{ echo "validated steady cold-flow report is required" >&2; exit 2; }}
grep -q '"status": "COLD_FLOW_VALIDATION_PASSED"' "$cold_report" || {{
    echo "steady cold-flow compound gate has not passed" >&2
    exit 2
}}
steady_cht_report="$case_dir/runs/steady_cht/reports/solver/steady_cht_validation.json"
[ -f "$steady_cht_report" ] || {{ echo "validated steady CHT report is required" >&2; exit 2; }}
grep -q '"status": "STEADY_CHT_VALIDATION_PASSED"' "$steady_cht_report" || {{
    echo "steady CHT compound gate has not passed" >&2
    exit 2
}}
latest_time=$(foamListTimes -case "$steady_dir" -latestTime)
case "$latest_time" in
    ""|0|0.0|0.00) echo "a completed steady pilot time is required" >&2; exit 2 ;;
esac

run_dir="$case_dir/runs/{run_name}"
[ ! -e "$run_dir" ] || {{ echo "run directory already exists: $run_dir" >&2; exit 2; }}
mkdir -p "$run_dir"
cp -R "$case_dir/0" "$run_dir/0"
cp -R "$case_dir/constant" "$run_dir/constant"
cp -R "$case_dir/system" "$run_dir/system"

for relative in \\
    0/coolant/T \\
    0/batterySolid/T \\
    constant/batterySolid/fvOptions \\
    system/controlDict \\
    system/fvSolution \\
    system/coolant/fvSchemes \\
    system/coolant/fvSolution \\
    system/batterySolid/fvSchemes \\
    system/batterySolid/fvSolution
do
    source_file="$case_dir/profiles/{profile}/$relative"
    [ -f "$source_file" ] || {{ echo "missing profile file: $source_file" >&2; exit 2; }}
    cp "$source_file" "$run_dir/$relative"
done

# Preserve the converged steady flow and turbulence state. Do not copy steady T.
for field in U p p_rgh k omega nut alphat phi; do
    source_field="$steady_dir/$latest_time/coolant/$field"
    [ -f "$source_field" ] || {{ echo "missing steady field: $source_field" >&2; exit 2; }}
    cp "$source_field" "$run_dir/0/coolant/$field"
done

grep -Eq 'internalField[[:space:]]+uniform[[:space:]]+{initial_temperature:.12g};' \
    "$run_dir/0/coolant/T" || {{ echo "coolant T was not reset to {initial_temperature:.12g} K" >&2; exit 2; }}
grep -Eq 'internalField[[:space:]]+uniform[[:space:]]+{initial_temperature:.12g};' \
    "$run_dir/0/batterySolid/T" || {{ echo "batterySolid T was not reset to {initial_temperature:.12g} K" >&2; exit 2; }}

mkdir -p "$run_dir/logs/solver" "$run_dir/reports/solver"
echo "$latest_time" >"$run_dir/reports/solver/source_cold_flow_time.txt"
mpi_ranks=${{OPENFOAM_MPI_RANKS:-10}}
case "$mpi_ranks" in
    ''|*[!0-9]*) echo "OPENFOAM_MPI_RANKS must be a positive integer" >&2; exit 2 ;;
esac
[ "$mpi_ranks" -ge 1 ] || {{ echo "OPENFOAM_MPI_RANKS must be at least 1" >&2; exit 2; }}
decompose_log="$run_dir/logs/solver/foamDictionary_decomposePar.log"
: >"$decompose_log"
for decompose_dict in \
    "$run_dir/system/decomposeParDict" \
    "$run_dir/system/coolant/decomposeParDict" \
    "$run_dir/system/batterySolid/decomposeParDict"
do
    [ -f "$decompose_dict" ] || {{ echo "missing decomposition dictionary: $decompose_dict" >&2; exit 2; }}
    foamDictionary "$decompose_dict" \
        -entry numberOfSubdomains -set "$mpi_ranks" \
        >>"$decompose_log" 2>&1
done
decomposePar -case "$run_dir" -allRegions -force \
    >"$run_dir/logs/solver/decomposePar.log" 2>&1
mpirun --allow-run-as-root -np "$mpi_ranks" \
    chtMultiRegionFoam -case "$run_dir" -parallel \
    >"$run_dir/logs/solver/{run_name}.log" 2>&1
reconstructPar -case "$run_dir" -allRegions {reconstruct_scope} \
    >"$run_dir/logs/solver/reconstructPar.log" 2>&1
{post_validation}

echo "{mode} pilot finished in $run_dir. Publication gates remain mandatory."
'''


def _solver_profile_files(
    config: ResolvedConfig,
    *,
    delta_t: float | None = None,
    frozen_flow: bool = False,
    include_battery_temperatures: bool = True,
    conservative: bool = False,
    enthalpy_relaxation: float | None = None,
    field_write_interval_s: float = 15.0,
    actual_one_second_sampling: bool = False,
) -> dict[str, str]:
    transient = delta_t is not None
    if not transient:
        effective_conservative = conservative or not include_battery_temperatures
        return {
            "system/controlDict": _steady_control_dict(
                include_battery_temperatures=include_battery_temperatures
            ),
            "system/fvSolution": _root_solution(transient=False),
            "system/coolant/fvSchemes": _fluid_schemes(),
            "system/coolant/fvSolution": _fluid_solution(
                conservative=effective_conservative,
                enthalpy_relaxation=enthalpy_relaxation,
            ),
            "system/batterySolid/fvSchemes": _solid_schemes(),
            "system/batterySolid/fvSolution": _solid_solution(
                conservative=effective_conservative
            ),
        }
    return {
        "system/controlDict": _transient_control_dict(
            config,
            delta_t=delta_t,
            frozen_flow=frozen_flow,
            field_write_interval_s=field_write_interval_s,
            actual_one_second_sampling=actual_one_second_sampling,
        ),
        "system/fvSolution": _root_solution(transient=True),
        "system/coolant/fvSchemes": _fluid_schemes(transient=True),
        "system/coolant/fvSolution": _transient_fluid_solution(
            frozen_flow=frozen_flow
        ),
        "system/batterySolid/fvSchemes": _solid_schemes(transient=True),
        "system/batterySolid/fvSolution": _transient_solid_solution(),
        "0/coolant/T": _fluid_fields(config)["T"],
        "0/batterySolid/T": _solid_temperature(config),
    }


def write_openfoam_solver_bundle(
    config: ResolvedConfig, output_dir: str | Path
) -> dict[str, object]:
    output = Path(output_dir).resolve()
    # The million-cell L2 pilot showed that the faster L0/L1 relaxation can
    # destabilise pressure coupling even when initialized from converged cold
    # flow. Use one conservative production profile for all newly built levels.
    steady_profile = _solver_profile_files(
        config,
        conservative=True, enthalpy_relaxation=0.5
    )
    steady_cold_flow_profile = _solver_profile_files(
        config,
        include_battery_temperatures=False
    )
    heat_source_dict = render_openfoam_fvoptions(
        source_from_config(config), config.solver.energy_field
    )
    profiles = {
        "steady_cold_flow": {
            **steady_cold_flow_profile,
            "constant/batterySolid/fvOptions": _empty_fvoptions(),
        },
        "steady": {
            **steady_profile,
            "constant/batterySolid/fvOptions": heat_source_dict,
        },
        "frozen_flow": {
            **_solver_profile_files(config, delta_t=1.0, frozen_flow=True),
            "constant/batterySolid/fvOptions": heat_source_dict,
        },
        "frozen_flow_actual_1s": {
            **_solver_profile_files(
                config,
                delta_t=1.0,
                frozen_flow=True,
                field_write_interval_s=1800.0,
                actual_one_second_sampling=True,
            ),
            "constant/batterySolid/fvOptions": heat_source_dict,
        },
        "full_transient_actual_1s": {
            **_solver_profile_files(
                config,
                delta_t=0.1,
                frozen_flow=False,
                field_write_interval_s=1.0,
                actual_one_second_sampling=True,
            ),
            "constant/batterySolid/fvOptions": heat_source_dict,
        },
        "full_transient_dt": {
            **_solver_profile_files(config, delta_t=0.1, frozen_flow=False),
            "constant/batterySolid/fvOptions": heat_source_dict,
        },
        "full_transient_dt_half": {
            **_solver_profile_files(config, delta_t=0.05, frozen_flow=False),
            "constant/batterySolid/fvOptions": heat_source_dict,
        },
    }
    files: dict[str, str] = {
        "constant/regionProperties": _region_properties(),
        "constant/g": _gravity(),
        "constant/hRef": _href(),
        "constant/coolant/thermophysicalProperties": _fluid_thermo(config),
        "constant/coolant/turbulenceProperties": _turbulence_properties(config),
        "constant/coolant/radiationProperties": _radiation_properties(),
        "constant/batterySolid/thermophysicalProperties": _solid_thermo(config),
        "constant/batterySolid/radiationProperties": _radiation_properties(),
        "constant/batterySolid/fvOptions": heat_source_dict,
        "0/batterySolid/T": _solid_temperature(config),
        "0/batterySolid/p": _solid_pressure(config),
        "AllrunColdFlowPilot": _steady_pilot_script(
            config,
            profile="steady_cold_flow",
            run_name="steady_cold_flow",
            copy_cold_flow=False,
        ),
        "AllrunSteadyPilot": _steady_pilot_script(
            config,
            profile="steady",
            run_name="steady_cht",
            copy_cold_flow=True,
        ),
        "AllcontinueSteadyPilot": _steady_continue_script(config),
        "AllcontinueColdFlowPilot": _cold_flow_continue_script(),
        "activate-solver-profile.sh": _activate_profile_script(),
        "AllrunFrozenFlowThermalPilot": _transient_pilot_script(
            config,
            profile="frozen_flow",
            run_name="frozen_flow",
            frozen_flow=True,
        ),
        "AllrunFrozenFlowThermalActual1s": _transient_pilot_script(
            config,
            profile="frozen_flow_actual_1s",
            run_name="frozen_flow_actual_1s",
            frozen_flow=True,
            field_interval_s=1800.0,
        ),
        "AllrunFullTransientActual1s": _transient_pilot_script(
            config,
            profile="full_transient_actual_1s",
            run_name="full_transient_actual_1s",
            frozen_flow=False,
            field_interval_s=1.0,
            result_run=True,
        ),
        "AllrunFullTransientDtPilot": _transient_pilot_script(
            config,
            profile="full_transient_dt",
            run_name="full_transient_dt",
            frozen_flow=False,
        ),
        "AllrunFullTransientDtHalfPilot": _transient_pilot_script(
            config,
            profile="full_transient_dt_half",
            run_name="full_transient_dt_half",
            frozen_flow=False,
        ),
    }
    files.update(steady_profile)
    for profile_name, profile_files in profiles.items():
        files.update(
            {
                f"profiles/{profile_name}/{relative}": content
                for relative, content in profile_files.items()
            }
        )
    for name, content in _fluid_fields(config).items():
        files[f"0/coolant/{name}"] = content
    for relative, content in files.items():
        _atomic_write(output / relative, content)
    for executable in (
        "AllrunSteadyPilot",
        "AllcontinueSteadyPilot",
        "AllcontinueColdFlowPilot",
        "AllrunColdFlowPilot",
        "activate-solver-profile.sh",
        "AllrunFrozenFlowThermalPilot",
        "AllrunFrozenFlowThermalActual1s",
        "AllrunFullTransientActual1s",
        "AllrunFullTransientDtPilot",
        "AllrunFullTransientDtHalfPilot",
    ):
        (output / executable).chmod(0o755)

    manifest = {
        "schema_version": 1,
        "status": "OPENFOAM_SOLVER_SOURCE_CREATED_NOT_EXECUTED",
        "distribution": config.solver.distribution,
        "version": config.solver.version,
        "steady_solver": "chtMultiRegionSimpleFoam",
        "transient_solver": "chtMultiRegionFoam",
        "parallel_execution": {
            "enabled": True,
            "default_mpi_ranks": 10,
            "override_environment_variable": "OPENFOAM_MPI_RANKS",
            "decomposition_method": "scotch",
        },
        "solver_profiles": {
            "steady_cold_flow": {
                "transient": False,
                "heat_source_enabled": False,
            },
            "steady": {"transient": False},
            "frozen_flow": {
                "transient": True,
                "frozenFlow": True,
                "deltaT_s": 1.0,
                "endTime_s": 1800,
                "telemetry_interval_s": 1.0,
                "field_write_interval_s": 15.0,
            },
            "frozen_flow_actual_1s": {
                "transient": True,
                "frozenFlow": True,
                "deltaT_s": 1.0,
                "endTime_s": 1800,
                "telemetry_interval_s": 1.0,
                "direct_cell_probe_interval_s": 1.0,
                "field_write_interval_s": 1800.0,
                "section_probe_spacing_mm": {"xy": 1.0, "xz": 1.5},
                "cell_surface_probe_lattice": "32-angle by 16-height plus caps",
                "interpolation_scheme": "cell",
                "invalid_point_policy": "preserve_invalid_no_padding",
            },
            "full_transient_actual_1s": {
                "transient": True,
                "frozenFlow": False,
                "initial_deltaT_s": 0.1,
                "max_deltaT_s": 0.1,
                "adjust_time_step": True,
                "maxCo": 5,
                "nOuterCorrectors": 3,
                "endTime_s": 1800,
                "telemetry_interval_s": 1.0,
                "direct_cell_probe_interval_s": 1.0,
                "field_write_interval_s": 1.0,
                "nominal_global_iterations_at_max_deltaT": 54000,
                "iteration_axis": "cumulative_pimple_outer_iteration",
                "residual_definition": "OpenFOAM raw initial linear-solver residual",
                "fluent_scaled_residual": False,
                "intended_use": "canonical new single-mesh result run",
            },
            "full_transient_dt": {
                "transient": True,
                "frozenFlow": False,
                "deltaT_max_s": 0.1,
                "maxCo": 5,
                "nOuterCorrectors": 3,
            },
            "full_transient_dt_half": {
                "transient": True,
                "frozenFlow": False,
                "deltaT_max_s": 0.05,
                "maxCo": 5,
                "nOuterCorrectors": 3,
            },
        },
        "regions": ["coolant", "batterySolid"],
        "fluid_fields": sorted(_fluid_fields(config)),
        "solid_fields": ["T", "p"],
        "battery_temperature_function_objects": 115,
        "battery_volume_temperature_function_objects": 46,
        "battery_surface_temperature_function_objects": 69,
        "flow_and_field_diagnostic_function_objects": 7,
        "heat_source_field": config.solver.energy_field,
        "heat_source_mode": config.battery.heat_source.mode,
        "qvol_W_m3": config.battery.heat_source.qvol_w_m3,
        "initial_temperature_K": config.boundary.initial_solid_temperature_k,
        "solver_pilot_ready": False,
        "publication_results_authorized": False,
        "pending_gates": [
            "OpenFOAM v2512 dictionary parse",
            "steady pilot convergence",
            "full-transient 1 s output execution and residual history",
            "full-transient dt/dt-half equivalence",
            "mass and energy balance",
            "prism layer coverage",
            "battery-wall y+ distribution",
            "single-mesh target count and topology",
        ],
        "files": sorted(files),
    }
    _atomic_write(
        output / "openfoam_solver_manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return manifest
