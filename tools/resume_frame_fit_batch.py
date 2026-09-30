#!/usr/bin/env python3
"""Retry one failed frame-fit diameter without touching completed siblings."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

from cfd_twin.file_lock import lock_file
from cfd_twin.production_mesh import MESH_PRESETS
from cfd_twin.steady_numerics import PROFILES, validate_profile
from cfd_twin.outlet_boundary import PROFILES as OUTLET_PROFILES, validate_profile as validate_outlet_profile
from recover_frame_fit_mesh import configure, execute, retryable
from retry_frame_fit_yplus import BATCH, read, write


def pressure_retryable(entry: dict, numerical_profile: str) -> bool:
    """An explicit new trial only after a clean, pressure-only failure at the cap."""
    job = Path(entry['job_dir'])
    prior = read(job / 'job.json').get('steady_numerics_profile')
    return (prior != numerical_profile and (
        pressure_failure_at_cap(entry)
        or (numerical_profile == 'pressure_relaxation_0p5'
            and prior == 'pressure_relaxation_0p15'
            and under_relaxed_pressure_diverged(entry))
    ))


def under_relaxed_pressure_diverged(entry: dict) -> bool:
    """Recognise the one unsafe 0.15 pressure-relaxation trial; never reuse it."""
    job = Path(entry['job_dir'])
    state = read(job / 'status.json')
    gate = read(job / 'case/reports/production_mesh/production_mesh_gate.json')
    log = job / 'case/runs/steady_cold_flow/logs/solver/steady_cold_flow.log'
    try:
        tail = log.read_text(encoding='utf-8', errors='replace')[-65536:]
    except OSError:
        return False
    return (
        state.get('status') == 'failed'
        and state.get('stage') == 'steady'
        and gate.get('status') == 'PRODUCTION_MESH_GATE_PASSED'
        and 'Negative initial temperature' in tail
        and 'FOAM parallel run aborting' in tail
    )


def pressure_failure_at_cap(entry: dict) -> bool:
    """Require all mesh, y+, safety and monitor gates; only pressure may fail."""
    job = Path(entry['job_dir'])
    state = read(job / 'status.json')
    if state.get('status') != 'failed' or state.get('stage') != 'steady':
        return False
    if read(job / 'case/reports/production_mesh/production_mesh_gate.json').get('status') != 'PRODUCTION_MESH_GATE_PASSED':
        return False
    report = read(job / 'case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json')
    checks = report.get('checks', {})
    required = {
        'battery_yplus_gate_passed', 'final_fields_finite', 'final_iteration_present',
        'final_turbulence_fields_nonnegative', 'last_200_flow_residuals_below_1e-4',
        'mass_imbalance_below_0p1_percent', 'minimum_5000_outer_iterations_completed',
        'monitor_window_has_at_least_21_samples', 'pilot_derived_residual_floors_reached',
        'pressure_drop_span_below_0p5_percent', 'solver_clean_end',
    }
    audit = report.get('acceptance_residual_audit', {})
    fields = {'coolant.Ux', 'coolant.Uy', 'coolant.Uz', 'coolant.p_rgh', 'coolant.k', 'coolant.omega'}
    return (
        report.get('status') == 'COLD_FLOW_VALIDATION_FAILED'
        and report.get('final_iteration') == 9500
        and required <= checks.keys()
        and {key for key, value in checks.items() if value is not True} == {'last_200_flow_residuals_below_1e-4'}
        and checks['last_200_flow_residuals_below_1e-4'] is False
        and audit.get('target') == 1e-4
        and audit.get('window_outer_iterations') == 200
        and fields == set(audit.get('checks', {}))
        and {key for key, value in audit['checks'].items() if value is not True} == {'coolant.p_rgh'}
        and audit['checks']['coolant.p_rgh'] is False
    )


def resume(batch: dict, diameter: int, profiles: list[str], *, steady_numerics_profile: str | None = None, outlet_velocity_profile: str | None = None) -> dict:
    entries = [entry for entry in batch['jobs'] if entry['diameter_mm'] == diameter]
    if len(entries) != 1:
        raise ValueError('Expected exactly one matching diameter')
    entry = entries[0]
    state = read(Path(entry['job_dir']) / 'status.json')
    if state.get('status') == 'complete':
        return state
    validate_profile(steady_numerics_profile)
    validate_outlet_profile(outlet_velocity_profile)
    if steady_numerics_profile and outlet_velocity_profile:
        raise ValueError('Change only one trial setting at a time')
    if outlet_velocity_profile:
        prior = read(Path(entry['job_dir']) / 'job.json')
        if prior.get('outlet_velocity_profile') == outlet_velocity_profile or not pressure_failure_at_cap(entry):
            raise ValueError('Outlet trial requires a new boundary profile after a pressure-only failure at the cap')
        if profiles != [prior.get('mesh_profile')]:
            raise ValueError('An outlet trial must use exactly the previous mesh profile')
    elif steady_numerics_profile:
        if not pressure_retryable(entry, steady_numerics_profile):
            raise ValueError('Pressure trial requires a clean pressure-only failure at 9500; never repeat the same numerical profile')
        if profiles != [read(Path(entry['job_dir']) / 'job.json').get('mesh_profile')]:
            raise ValueError('A pressure trial must use exactly the previous mesh profile')
    elif not retryable(entry):
        raise ValueError('Only a failed mesh/y+ job may enter fresh mesh trials')
    os.environ.update(OPENFOAM_MPI_RANKS=str(batch['solver_mpi_ranks']),
                      OPENFOAM_MESH_MPI_RANKS='1', OMP_NUM_THREADS='1',
                      OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    for profile in profiles:
        preset = MESH_PRESETS[profile]
        if (preset.target_cells != 600000 or preset.minimum_total_cells != 600000
                or preset.total_tolerance_fraction > 0.15
                or preset.minimum_hexahedron_fraction < 0.88
                or not preset.preserve_wall_diagnostics):
            raise ValueError('Retry profile must preserve the approved mesh gates and diagnostics')
    for profile in profiles:
        if not steady_numerics_profile and not outlet_velocity_profile and not retryable(entry):
            break
        preset = MESH_PRESETS[profile]
        options = {'steady_numerics_profile': steady_numerics_profile} if steady_numerics_profile else {}
        if outlet_velocity_profile:
            options['outlet_velocity_profile'] = outlet_velocity_profile
        configure(batch, entry, round(preset.first_layer_thickness_m * 1e6),
                  preset.prism_layer_count, profile=profile, **options)
        print(f'Ø{diameter}: fresh retry with {profile}', flush=True)
        state = execute(batch, entry)
        if state.get('status') == 'complete':
            break
    states = [{'diameter_mm': item['diameter_mm'], 'id': item['id'],
               **read(Path(item['job_dir']) / 'status.json')} for item in batch['jobs']]
    done = all(item.get('status') == 'complete' and not item.get('release_error') for item in states)
    final = {'status': 'complete' if done else 'needs_attention', 'runs': states,
             'pid': os.getpid(), 'finished_at': time.time(),
             'strategy': 'Fresh per-diameter retries; preserve completed results and unchanged physics/gates'}
    write(BATCH / 'status.json', final)
    write(BATCH / 'retry_status.json', final)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diameter', type=int, required=True)
    parser.add_argument('--profiles', nargs='+', required=True, choices=sorted(MESH_PRESETS))
    trial = parser.add_mutually_exclusive_group()
    trial.add_argument('--steady-numerics-profile', choices=PROFILES)
    trial.add_argument('--outlet-velocity-profile', choices=OUTLET_PROFILES)
    args = parser.parse_args()
    with (BATCH / 'retry.lock').open('a+b') as retry_lock, (BATCH / 'batch.lock').open('a+b') as batch_lock:
        lock_file(retry_lock)
        lock_file(batch_lock)
        state = resume(read(BATCH / 'batch.json'), args.diameter, args.profiles,
                       steady_numerics_profile=args.steady_numerics_profile,
                       outlet_velocity_profile=args.outlet_velocity_profile)
    return 0 if state.get('status') == 'complete' and not state.get('release_error') else 1


if __name__ == '__main__':
    raise SystemExit(main())
