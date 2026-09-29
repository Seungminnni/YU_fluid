#!/usr/bin/env python3
"""Try bounded local refinement at the best 8 µm / 10-layer wall setting."""
from cfd_twin.file_lock import lock_file
import os
import time
from pathlib import Path
from recover_frame_fit_mesh import configure, execute, retryable
from retry_frame_fit_yplus import BATCH, read, write

PROFILES=[
 'production_frame_fit_8um_10layers_local2',
 'production_frame_fit_8um_10layers_local4',
 'production_frame_fit_8um_10layers_local5_700k',
]

if __name__=='__main__':
 with (BATCH/'retry.lock').open('a+b') as rlock,(BATCH/'batch.lock').open('a+b') as lock:
  lock_file(rlock)
  lock_file(lock)
  batch=read(BATCH/'batch.json')
  os.environ.update(OPENFOAM_MPI_RANKS=str(batch['solver_mpi_ranks']),OPENFOAM_MESH_MPI_RANKS='1',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
  # Preserve only compact diagnostics; the failed output itself is deleted.
  first=Path(batch['jobs'][0]['job_dir'])
  last_yplus=read(first/'case/runs/steady_cold_flow/reports/solver/yplus.json')
  if last_yplus:
   write(BATCH/'local_refinement_basis.json',{'source_job':first.name,'source_profile':batch['jobs'][0].get('mesh_profile'),'note':'Latest available per-patch diagnostics; not the deleted 8 µm run','battery_surface':last_yplus.get('battery_surface'),'per_battery_patch':last_yplus.get('per_battery_patch')})
  for entry in batch['jobs']:
   configure(batch,entry,8,10,profile=PROFILES[0])
  successful_index=0
  for entry in batch['jobs']:
   succeeded=False
   for i in range(successful_index,len(PROFILES)):
    if i>successful_index and not retryable(entry):break
    state=read(Path(entry['job_dir'])/'status.json')
    if state.get('status')=='queued':
     configure(batch,entry,8,10,profile=PROFILES[i])
    elif state.get('status')=='failed':
     configure(batch,entry,8,10,profile=PROFILES[i])
    else:break
    print(f"Ø{entry['diameter_mm']} starting {PROFILES[i]}",flush=True)
    state=execute(batch,entry)
    if state.get('status')=='complete':
     successful_index=i
     succeeded=True
     break
   if not succeeded:
    break
  states=[{'diameter_mm':e['diameter_mm'],'id':e['id'],**read(Path(e['job_dir'])/'status.json')} for e in batch['jobs']]
  complete=all(s.get('status')=='complete' and not s.get('release_error') for s in states)
  final={'status':'complete' if complete else 'needs_attention','runs':states,'pid':os.getpid(),'finished_at':time.time(),'strategy':'8 µm / 10 layers with local battery rim refinement; bounded 2-cell, 4-cell, then 5-cell 700k-target trials'}
  write(BATCH/'status.json',final)
  write(BATCH/'retry_status.json',final)
