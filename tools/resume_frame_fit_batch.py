#!/usr/bin/env python3
"""Retry one failed frame-fit diameter without touching completed siblings."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

from cfd_twin.file_lock import lock_file
from cfd_twin.production_mesh import MESH_PRESETS
from recover_frame_fit_mesh import configure, execute, retryable
from retry_frame_fit_yplus import BATCH, read, write


def resume(batch: dict, diameter: int, profiles: list[str]) -> dict:
    entries = [entry for entry in batch['jobs'] if entry['diameter_mm'] == diameter]
    if len(entries) != 1:
        raise ValueError('Expected exactly one matching diameter')
    entry = entries[0]
    state = read(Path(entry['job_dir']) / 'status.json')
    if state.get('status') == 'complete':
        return state
    if not retryable(entry):
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
        if not retryable(entry):
            break
        preset = MESH_PRESETS[profile]
        configure(batch, entry, round(preset.first_layer_thickness_m * 1e6),
                  preset.prism_layer_count, profile=profile)
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
    args = parser.parse_args()
    with (BATCH / 'retry.lock').open('a+b') as retry_lock, (BATCH / 'batch.lock').open('a+b') as batch_lock:
        lock_file(retry_lock)
        lock_file(batch_lock)
        state = resume(read(BATCH / 'batch.json'), args.diameter, args.profiles)
    return 0 if state.get('status') == 'complete' and not state.get('release_error') else 1


if __name__ == '__main__':
    raise SystemExit(main())
