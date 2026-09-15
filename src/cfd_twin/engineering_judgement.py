"""사람의 공학적 판단과 프로그램 자동 판정을 분리해 기록한다."""

from __future__ import annotations

import json
import os
from pathlib import Path


def framework_judgement_manifest() -> dict[str, object]:
    """현재 프레임워크 결정과 미해결 항목을 기계 판독 가능한 형태로 반환한다."""

    return {
        "schema_version": 1,
        "runtime_policy": {
            "ai_decision_allowed": False,
            "automatic_pass_basis": "numeric gates encoded in source only",
            "visual_mesh_acceptance": "MANUAL_REVIEW_REQUIRED",
            "silent_fallback_allowed": False,
        },
        "records": [
            {
                "id": "EJ-001",
                "subject": "production mesh cell-count target",
                "decision": "target approximately 800000 cells with a 10 percent count gate",
                "origin": "professor feedback supplied by user",
                "implementation": "production_800k preset and automatic count report",
                "status": "CALIBRATED_EXECUTED_849284_ALL_MESH_AND_COLD_FLOW_GATES_PASSED",
            },
            {
                "id": "EJ-002",
                "subject": "mesh family",
                "decision": "retain hexa-dominant body-fitted hybrid mesh rather than claim a structured O-grid",
                "reason": "hybrid mesh was permitted and the existing variable-ready CAD pipeline uses snappyHexMesh",
                "implementation": "numeric topology, layer, interface and checkMesh gates",
                "status": "CALIBRATED_EXECUTED_ALL_MESH_AND_COLD_FLOW_GATES_PASSED",
            },
            {
                "id": "EJ-003",
                "subject": "visual mesh quality",
                "decision": "the program never infers PASS from an image",
                "reason": "the deployable application has no AI engineering reviewer",
                "implementation": "visual acceptance remains MANUAL_REVIEW_REQUIRED; automatic status uses stored thresholds only",
                "status": "POLICY_ACTIVE",
            },
            {
                "id": "EJ-004",
                "subject": "steady residual target",
                "decision": "attempt every reported equation below 1e-6 and record whether this strict target was met",
                "reason": "professor requested additional iterations where feasible",
                "implementation": "SIMPLE residualControl plus a non-silent strict-target audit in validation JSON",
                "status": "EXECUTED_5000_STRICT_TARGET_NOT_MET_COMPOUND_GATE_PASSED",
            },
            {
                "id": "EJ-005",
                "subject": "paper fourth-order UDF equivalence",
                "decision": "do not implement the printed rounded fourth-order table as the final UDF",
                "reason": "the PDF contains no original UDF and the rounded raw-Kelvin polynomial coefficients fail physical-domain checks through cancellation",
                "required_input": "original Fluent UDF or full-precision fourth-order coefficients with a reference test vector",
                "fallback": "explicitly opt-in provisional first-order equation (4.1)",
                "status": "BLOCKED_EXTERNAL_INPUT_REQUIRED",
            },
            {
                "id": "EJ-006",
                "subject": "provisional UDF temperature coupling",
                "decision": "use a fixed reference temperature and SOC time law for the provisional implementation",
                "reason": "equation (4.1) is reproducible, while the current OpenFOAM source interface is a deterministic time table",
                "limitation": "not local cell-temperature feedback and not the thesis final fourth-order numerical model",
                "status": "ENCODED_OPT_IN_ONLY",
            },
            {
                "id": "EJ-007",
                "subject": "800k mesh calibration attempt 1",
                "observation": "3.25 mm produced 644736 cells, max non-orthogonality 74.9982736285 and max skewness 6.57546537018",
                "decision": "keep the 70/5 quality gates, reduce the background size to 3.00 mm, and enforce those limits during snappyHexMesh",
                "reason": "the first run failed both the 800000 plus-or-minus 10 percent count contract and the stricter quality contract",
                "evidence": "build/framework_udf_800k/calibration_history/attempt_001_3p25mm",
                "status": "RECORDED_NEXT_ATTEMPT_REQUIRED",
            },
            {
                "id": "EJ-008",
                "subject": "800k mesh calibration attempt 2",
                "observation": "3.00 mm produced 734204 cells, hexahedron fraction 0.8937434283, max non-orthogonality 70.0954025633 and max skewness 3.21333705822",
                "decision": "retain the 90 percent hexa and 70/5 acceptance gates, use 2.90 mm background cells, and set the snappy generation non-orthogonality limit to 69.5",
                "reason": "count and skewness passed, but hexa fraction and non-orthogonality did not",
                "evidence": "build/framework_udf_800k/calibration_history/attempt_002_3p00mm_strict",
                "status": "RECORDED_NEXT_ATTEMPT_REQUIRED",
            },
            {
                "id": "EJ-009",
                "subject": "800k mesh calibration attempt 3",
                "observation": "2.90 mm produced 836318 cells, hexahedron fraction 0.9005318551, max non-orthogonality 69.4998211023 and max skewness 3.42533600015",
                "decision": "accept the 2.90 mm production_800k preset as the deterministic framework mesh profile",
                "reason": "cell count, hexa fraction, final two-region quality, prism-layer, volume, zone and interface gates all passed without relaxing a threshold",
                "evidence": "build/framework_udf_800k/calibration_history/attempt_003_2p90mm_generation69p5",
                "limitation": "y+ and solver convergence remain result-stage gates; visual mesh acceptance remains manual",
                "status": "CALIBRATED_MESH_GATE_PASSED",
            },
            {
                "id": "EJ-010",
                "subject": "production_800k first-layer y+ recalibration",
                "observation": "the 14 micrometre mesh completed 5000 cold-flow iterations but only 0.9436840874 of battery area had y+ below 1, below the fixed 0.95 gate",
                "decision": "do not relax the y+ gate; reduce the absolute first prism-layer height from 14 to 12 micrometres and regenerate the mesh",
                "reason": "area-weighted scaling of the computed field predicts 0.951390 at 13 micrometres and 0.957412 at 12 micrometres, so 12 micrometres provides a deterministic margin",
                "evidence": "build/framework_udf_800k/calibration_history/attempt_003_2p90mm_generation69p5/solver_validation_14um",
                "limitation": "the scaling estimate was only the selection rule; final acceptance comes from the independently computed fourth-attempt field",
                "status": "EXECUTED_RECALIBRATION_CONFIRMED",
            },
            {
                "id": "EJ-011",
                "subject": "production_800k final mesh and cold-flow calibration",
                "observation": "the 2.90 millimetre background and 12 micrometre first-layer mesh produced 849284 cells, 0.9022270524 hexahedron fraction, max non-orthogonality 69.4602318009 and max skewness 3.42533600015; after 5000 cold-flow iterations, 0.9621816798 of battery area had y+ below 1",
                "decision": "adopt this deterministic preset as the current framework mesh and permit the gated steady CHT run",
                "reason": "all unchanged count, topology, mesh-quality, region, interface, layer, mass-balance, pressure-stability, field-bound and y+ gates passed",
                "evidence": "build/framework_udf_800k/calibration_history/attempt_004_2p90mm_12um",
                "limitation": "the compound cold-flow gate passed, but the separate strict audit did not place every reported initial residual below 1e-6; visual mesh acceptance remains manual",
                "status": "CALIBRATED_EXECUTED_COLD_FLOW_PASSED",
            },
            {
                "id": "EJ-012",
                "subject": "steady CHT source must not consume the transient UDF table",
                "observation": "the first 5000-iteration steady CHT run advanced the provisional source table using the SIMPLE iteration counter, so qvol rose from 71877.2976185 to 115445.866039 W/m3; the measured coolant heat removal of 51.0148 W matched the unintended final source power of about 51.06 W rather than the validated t=0 source power of 31.7910 W",
                "decision": "render a constant t=0 fvOptions source for the steady profile and retain the time table only in transient profiles",
                "reason": "chtMultiRegionSimpleFoam iteration Time is not physical discharge time",
                "evidence": "build/framework_udf_800k/calibration_history/attempt_004_2p90mm_12um/steady_validation_unintended_dynamic_source",
                "limitation": "the failed run is diagnostic evidence only and cannot authorize a transient run; the corrected continuation must pass the same unchanged thermal gates",
                "status": "BUG_CONFIRMED_FIX_ENCODED_REVALIDATION_PASSED_AT_9500",
            },
            {
                "id": "EJ-013",
                "subject": "transient UDF coupling and solver-path selection",
                "decision": "separate physical output time, solver time steps and nonlinear outer corrections; validate frozen-flow against a short full-transient pilot before allowing the 1800-second production run",
                "reason": "a one-second UI frame is not one equation solve, and temperature-dependent heat generation must be updated inside the thermal coupling loop",
                "implementation": "evaluate the heat source at each physical time step; when the final UDF depends on local battery temperature, re-evaluate it after the solid-energy update in every PIMPLE outer correction with a configured relaxation rule; select frozen-flow or full-transient only by numeric comparison gates",
                "required_input": "project acceptance tolerances for Tmax, solid Tavg, outlet T and energy balance between the short full-transient and frozen-flow pilots",
                "limitation": "the provisional equation (4.1) uses fixed 293.15 K, so its source changes with SOC/time but has no local temperature feedback",
                "status": "ALGORITHM_RECORDED_ACCEPTANCE_TOLERANCES_REQUIRED",
            },
            {
                "id": "EJ-014",
                "subject": "corrected steady CHT continuation at 7000 iterations",
                "observation": "with the corrected constant 31.7910376921 W source, the 6800-to-7000 window passed mean and maximum energy-imbalance gates at 1.9814 and 2.3327 percent, but battery Tavg and Tmax spans were 0.0020528 K and 0.0101157 K, above the unchanged 0.001 K limits",
                "decision": "do not relax the thermal stability gates and continue the same field for 2500 additional iterations",
                "reason": "the source-power bug is resolved and all other compound checks pass, while the monotonic temperature and energy trends show the inherited overheated field is still relaxing toward the corrected steady state",
                "evidence": "build/framework_udf_800k/calibration_history/attempt_004_2p90mm_12um/steady_validation_correct_source_7000_not_stable",
                "result": "at 9500 iterations the battery Tavg and Tmax spans were 0.0000525415 K and 0.0005035500 K; mean and maximum energy imbalance were 0.0444641 and 0.0516086 percent, so every compound steady CHT gate passed",
                "final_evidence": "build/framework_udf_800k/calibration_history/attempt_004_2p90mm_12um/steady_validation_correct_source_9500_passed",
                "status": "EXECUTED_STEADY_CHT_GATES_PASSED_AT_9500",
            },
            {
                "id": "EJ-015",
                "subject": "production_800k full-transient startup stability",
                "observation": "the optional one-second full-transient pilot became unstable at physical time 0.0036911727 s; local coolant Courant number rose to 3976.9043 and the solver aborted on negative temperature",
                "decision": "reject this run, preserve its log, and do not authorize the current full-transient profile on the 12 micrometre near-wall mesh until a deterministic low-Courant startup preflight and time-step study are implemented",
                "reason": "a clean steady initial field and maxCo 5 were insufficient for the smallest local cells; silently substituting this failed path would invalidate the result",
                "current_production_path": "use gated frozen-flow energy coupling only while density, viscosity, heat capacity, conductivity, inlet conditions and gravity remain independent of temperature as currently configured",
                "evidence": "build/framework_udf_800k/calibration_history/attempt_004_2p90mm_12um/full_transient_dt_unstable_startup",
                "status": "FULL_TRANSIENT_PROFILE_BLOCKED_FROZEN_FLOW_PHYSICALLY_AUTHORIZED",
            },
            {
                "id": "EJ-016",
                "subject": "production_800k frozen-flow energy coupling and provisional UDF run",
                "observation": "the first one-second frozen-flow attempt used energyCoupling.maxIter, which is not the loopControl iteration key, and became thermally unstable; after changing the key to iterations 20, an unrelaxed 60-second attempt still oscillated at 11 seconds",
                "decision": "use PIMPLE nOuterCorrectors 3, energyCoupling.iterations 20 and fluid/solid enthalpy relaxation 0.3; require a clean 60-second preflight before the 1800-second run",
                "reason": "OpenFOAM executes the additional fluid-solid energy sub-loop only through loopControl and the strongly coupled CHT fixed point required explicit damping on this mesh",
                "preflight_result": "the corrected 60-second run was monotonic and finite, ending at battery Tavg 293.3564899876 K and Tmax 293.41477136 K",
                "production_result": "the 1800-second provisional first-order source run completed cleanly with 1800 one-second telemetry records and 121 reconstructed temperature fields; final battery Tavg was 293.7369838544 K, Tmax was 294.180071144 K, and the final source power was 51.0612390872 W",
                "validation_result": "the final 60-second mean and maximum abs(Qsource-Qcoolant)/Qsource gaps were 1.6141743226 and 1.6244350996 percent, and maximum mass imbalance was 1.9994694697e-9; all frozen-flow pilot gates passed",
                "evidence": "build/framework_udf_800k/runs/frozen_flow/reports/solver/frozen_flow_validation.json",
                "failed_evidence": [
                    "build/framework_udf_800k/calibration_history/attempt_004_2p90mm_12um/frozen_flow_dt1_unstable_missing_energy_loop",
                    "build/framework_udf_800k/calibration_history/attempt_004_2p90mm_12um/frozen_flow_energy_loop_20_unrelaxed_unstable",
                ],
                "limitation": "this result uses the opt-in first-order equation with fixed 293.15 K reference temperature; it is not local-temperature feedback and not the unavailable final fourth-order UDF. In a transient run Qsource-Qcoolant contains fluid and solid energy storage, so the reported gap is not a pure numerical conservation residual; future production runs must integrate both regions' internal-energy rates for the complete balance",
                "status": "EXECUTED_PROVISIONAL_UDF_FROZEN_FLOW_GATES_PASSED",
            },
            {
                "id": "EJ-017",
                "subject": "PPT-selected Bernardi component-fit heat source",
                "decision": "implement the published 2C and 293.15 K Ri(SOC) and DeltaS(SOC) fits with q=I^2*Ri-I*T*DeltaS/F as a separate opt-in model",
                "origin": "UDF 설명_20260910 (2).pptx supplied by the user",
                "reason": "the component equations and coefficients are directly reproducible from Peneklioglu thesis Figures 4.2 and 4.6 and avoid evaluating the rounded raw-Kelvin fourth-order regression",
                "implementation": "restrict inputs to a 2 Ah cell, 2C, and 293.15 K; emit 1801 one-second OpenFOAM source samples and a direct Fluent DEFINE_SOURCE expression; record irreversible and reversible terms separately",
                "limitation": "this is a published Bernardi component-fit path, not the unavailable fourth-order expanded-correlation UDF used in the thesis final CFD; temperature is a fixed fit condition, not local cell-temperature feedback",
                "status": "EXECUTED_VALIDATED_BY_EJ_022_RERUN",
            },
            {
                "id": "EJ-018",
                "subject": "reuse of the validated 849284-cell mesh and cold flow",
                "decision": "reuse the exact mesh and only U, p, p_rgh, k, omega, nut, alphat and phi; reset both temperature fields and recompute steady and transient thermal stages",
                "reason": "the coolant temperature changed from 293.15 K to 293 K, but the heat source is disabled in cold flow, gravity is zero and all momentum-coupled properties are constant, so temperature cannot feed back into the reused momentum/turbulence solution",
                "evidence": "build/ppt_bernardi_800k/mesh_cold_flow_reuse_manifest.json",
                "status": "FLOW_ONLY_REUSE_VERIFIED_THERMAL_RECALCULATION_PASSED",
            },
            {
                "id": "EJ-019",
                "subject": "Mac mini solver MPI default",
                "decision": "use four MPI ranks by default on this Apple M4 host",
                "observation": "the same 849284-cell 60-iteration case took 39 s at four ranks and 61 s at ten ranks",
                "limitation": "hardware-specific measurement; other hosts must benchmark and override OPENFOAM_MPI_RANKS",
                "status": "MEASURED_DEFAULT_ENCODED",
            },
            {
                "id": "EJ-020",
                "subject": "one-second thermal storage and energy-balance evidence",
                "decision": "retain all native parallel T states through validation, reconstruct only the final spatial field, then remove reconstructible processor time fields after the direct probes and browser package pass exhaustive checks",
                "reason": "validation needs the native fields during execution, but the delivered browser uses direct probes and the final reconstructed field; retaining both copies afterward adds storage without display or numerical information",
                "implementation": "compute d(rho*Cp*integral(T dV))/dt separately from coolant heat removal and numerical imbalance",
                "status": "EXECUTED_AND_PACKAGED",
            },
            {
                "id": "EJ-021",
                "subject": "PPT Bernardi transient energy-balance disposition",
                "decision": "supersede the original 20-coupling-iteration thermal result and exclude it from the deliverable browser package",
                "observation": "the final-60-second complete energy-balance mean/max are 3.8996%/4.0013%, above the 2% target; all 1801 direct UI frames and the 1800-second field are present",
                "diagnostic": "a single 1800-to-1801-second continuation with 100 coupling iterations and h relaxation 0.5 reduced the complete imbalance to 1.0654%",
                "limitation": "the one-step diagnostic was not substituted into the old result; it was used only to choose the settings for a full rerun",
                "superseded_by": "EJ-022 full 0-1800 s Fluent-aligned thermal rerun",
                "status": "SUPERSEDED_NOT_PACKAGED",
            },
            {
                "id": "EJ-022",
                "subject": "Fluent-aligned thermal initial conditions and transient coupling budget",
                "decision": "start coolant at 293 K, start every battery at 293.15 K, expose only the dynamic Peneklioglu source, increase frozen-flow energyCoupling iterations from 20 to 100, and use 0.5 enthalpy relaxation on both regions",
                "reason": "the supplied Fluent setup uses distinct fluid and solid initial temperatures and the completed 20-iteration run exceeded the complete 2 percent energy-balance gate; the measured 1.0654 percent diagnostic used the combined 100-iteration and 0.5-relaxation setting",
                "implementation": "recompute steady CHT and the full 0-1800 s thermal history; keep deltaT and coupling count as recorded runtime inputs instead of silently changing them",
                "cost_note": "deltaT 0.01 s with 100 coupling iterations is an exact but roughly 180000-step long run; deltaT 1 s with 100 iterations is the practical production default and 0.01 s must be used as an explicit sensitivity or long-run selection",
                "steady_result": "5000 iterations; compound gate passed; final battery Tavg/Tmax 293.4066614222/293.719256416 K; strict 1e-6 audit remained false only for k and p_rgh",
                "transient_result": "1800 one-second steps completed; Tavg/Tmax/outlet 293.6436767072/294.134288096/293.116331217 K; final-60-second complete energy-balance mean/max 0.216397%/0.223259%; every frozen-flow gate passed",
                "status": "FLUENT_ALIGNED_THERMAL_RECALCULATION_PASSED",
            },
        ],
    }


def write_engineering_judgements(path: str | Path) -> dict[str, object]:
    destination = Path(path).resolve()
    manifest = framework_judgement_manifest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, destination)
    return manifest
