#!/usr/bin/env python3
"""Fresh baseline then paired shrinker trial; accepted runs continue to S1/S2."""
import fcntl
import os
import time
from pathlib import Path
from recover_frame_fit_mesh import configure, execute, retryable
from retry_frame_fit_yplus import BATCH, read, write

PROFILES = ['production_frame_fit_8um_10layers_ab_A', 'production_frame_fit_8um_10layers_ab_B']

if __name__ == '__main__':
    with (BATCH/'retry.lock').open('a+') as retry_lock, (BATCH/'batch.lock').open('a+') as batch_lock:
        fcntl.flock(retry_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(batch_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        batch = read(BATCH/'batch.json')
        os.environ.update(OPENFOAM_MPI_RANKS=str(batch['solver_mpi_ranks']), OPENFOAM_MESH_MPI_RANKS='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
        batch['strategy'] = 'Fresh 8 µm / 10-layer baseline, then shrinker-only B; 600k–690k; snapshot and polygon-matched layer/y+ diagnostics; unchanged final gates'
        selected = None
        first = batch['jobs'][0]
        for profile in PROFILES:
            if profile != PROFILES[0] and not retryable(first):
                break
            configure(batch, first, 8, 10, profile=profile)
            print(f"Ø28 {profile}: 1500-iteration diagnostic, original full S0 gate, then S1/S2 only on pass", flush=True)
            state = execute(batch, first)
            if state.get('status') == 'complete':
                selected = profile
                break
        if selected:
            for entry in batch['jobs'][1:]:
                configure(batch, entry, 8, 10, profile=selected)
                state = execute(batch, entry)
                if state.get('status') != 'complete':
                    break
        states = [{'diameter_mm': e['diameter_mm'], 'id': e['id'], **read(Path(e['job_dir'])/'status.json')} for e in batch['jobs']]
        done = all(s.get('status') == 'complete' and not s.get('release_error') for s in states)
        final = {'status': 'complete' if done else 'needs_attention', 'runs': states, 'pid': os.getpid(), 'finished_at': time.time(), 'selected_profile': selected, 'strategy': batch['strategy']}
        write(BATCH/'status.json', final)
        write(BATCH/'retry_status.json', final)
