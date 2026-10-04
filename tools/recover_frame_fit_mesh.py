#!/usr/bin/env python3
"""Calibrate one port variant before applying its meshing profile to the others."""
from cfd_twin.file_lock import lock_file
import os
import time
from pathlib import Path
from retry_frame_fit_yplus import BATCH, read, write, prepare_retry
from cfd_twin.workflow_run import run_imported_case
from cfd_twin.production_mesh import MESH_PRESETS
from cfd_twin.steady_numerics import validate_profile
from cfd_twin.outlet_boundary import validate_profile as validate_outlet_profile

CANDIDATES=[(6,12),(8,10),(6,14)]

def configure(batch,entry,um,layers, *, profile=None, steady_numerics_profile=None, outlet_velocity_profile=None):
    old=Path(entry['job_dir'])
    profile=profile or f'production_600k_frame_fit_{um}um_{layers}layers'
    preset=MESH_PRESETS[profile]
    validate_profile(steady_numerics_profile)
    validate_outlet_profile(outlet_velocity_profile)
    prior_target=read(old/'job.json').get('inputs',{}).get('mesh_target_cells')
    reason=('Fresh pressure numerical trial; unchanged mesh profile, physics and acceptance limits'
            if steady_numerics_profile else
            'Fresh higher-resolution mesh trial; unchanged operating conditions, wall treatment and validation thresholds'
            if isinstance(prior_target,(int,float)) and preset.target_cells>prior_target else
            'Fresh boundary-layer envelope trial; all physical conditions and validation thresholds unchanged')
    overrides={'mesh_profile':profile}
    if steady_numerics_profile:
        overrides['steady_numerics_profile']=steady_numerics_profile
    if outlet_velocity_profile:
        overrides['outlet_velocity_profile']=outlet_velocity_profile
        reason='Fresh pressure-outlet backflow trial; unchanged mesh profile, numerical settings, operating inputs and acceptance limits; outlet velocity boundary changes'
    state=read(old/'status.json')
    layer=read(old/'case/reports/production_mesh/layer_coverage.json')
    summary={k:layer[k] for k in ['status','checks','low_coverage_batteries','low_thickness_batteries'] if k in layer}
    if state.get('status')=='failed':
        new=prepare_retry(batch,entry,um,replacement_overrides=overrides,reason=reason)
        entry['prior_attempts'][-1]['mesh_failure']=summary
    elif state.get('status')=='queued' and not (old/'case').exists():
        new=old
    else:
        raise RuntimeError('Only failed or untouched queued jobs may be reconfigured')
    job=read(new/'job.json')
    job.update(overrides)
    job['inputs']['mesh_target_cells']=preset.target_cells
    if preset.preserve_wall_diagnostics:
        job['wall_diagnostic_iterations']=1500
    else:
        job.pop('wall_diagnostic_iterations',None)
    job['source'].update(first_layer_microns=um,requested_prism_layers=layers,reason=reason)
    entry.update(first_layer_microns=um,requested_prism_layers=layers,mesh_profile=profile)
    entry.update(refined_battery_cells=list(preset.refined_battery_cells),mesh_target_cells=preset.target_cells)
    job['source'].update(refined_battery_cells=list(preset.refined_battery_cells))
    write(new/'job.json',job)
    write(new/'status.json',{'status':'queued','stage':'cad','message':f'Ø{entry["diameter_mm"]}: 새 메시, 첫 층 {um} µm · 요청 {layers}층'})
    write(BATCH/'batch.json',batch)
    return new

def execute(batch,entry):
    active={'status':'running','current':entry,'pid':os.getpid(),'solver_mpi_ranks':batch['solver_mpi_ranks'],'strategy':'fresh mesh for selected failed diameter; preserve completed runs'}
    write(BATCH/'status.json',active)
    write(BATCH/'retry_status.json',active)
    try:
        run_imported_case(Path(entry['job_dir'])/'job.json')
    except Exception as exc:
        print(f'Ø{entry["diameter_mm"]}: {exc}',flush=True)
    return read(Path(entry['job_dir'])/'status.json')

def retryable(entry):
    job=Path(entry['job_dir'])
    state=read(job/'status.json')
    if state.get('status')!='failed': return False
    if state.get('stage')=='mesh': return True
    checks=read(job/'case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json').get('checks',{})
    return checks.get('battery_yplus_gate_passed') is False

if __name__=='__main__':
    with (BATCH/'retry.lock').open('a+b') as rlock, (BATCH/'batch.lock').open('a+b') as block:
        lock_file(rlock)
        lock_file(block)
        batch=read(BATCH/'batch.json')
        os.environ.update(OPENFOAM_MPI_RANKS=str(batch['solver_mpi_ranks']),OPENFOAM_MESH_MPI_RANKS='1',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
        # Delete the failed outputs now, freezing minimal diagnoses and new inputs.
        for entry in batch['jobs']:
            configure(batch,entry,*CANDIDATES[0])
        first=batch['jobs'][0]
        chosen=None
        for index,settings in enumerate(CANDIDATES):
            if index:
                if not retryable(first): break
                configure(batch,first,*settings)
            state=execute(batch,first)
            if state.get('status')=='complete':
                chosen=settings
                break
        if chosen:
            for entry in batch['jobs'][1:]:
                configure(batch,entry,*chosen)
                execute(batch,entry)
        states=[{'diameter_mm':e['diameter_mm'],'id':e['id'],**read(Path(e['job_dir'])/'status.json')} for e in batch['jobs']]
        complete=all(s.get('status')=='complete' and not s.get('release_error') for s in states)
        final={'status':'complete' if complete else 'needs_attention','runs':states,'pid':os.getpid(),'finished_at':time.time(),'selected_profile':chosen}
        write(BATCH/'status.json',final)
        write(BATCH/'retry_status.json',final)
