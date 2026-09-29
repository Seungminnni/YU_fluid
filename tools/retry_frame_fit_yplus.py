#!/usr/bin/env python3
"""Fresh mesh retries: remove failed output after freezing the replacement job."""
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from cfd_twin.workflow_run import run_imported_case

ROOT=Path(__file__).resolve().parents[1]
BATCH=ROOT/'build/frame_fit_water_20260929'
RUNS=ROOT/'build/imported_runs/runs'

def read(path):
    return json.loads(path.read_text()) if path.exists() else {}

def write(path,value):
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')
    os.replace(temp,path)

def yplus_only(job):
    report=read(job/'case/runs/steady_cold_flow/reports/solver/cold_flow_validation.json')
    checks=report.get('checks',{})
    return read(job/'status.json').get('status')=='failed' and report.get('status')=='COLD_FLOW_VALIDATION_FAILED' and {k for k,v in checks.items() if v is False}=={'battery_yplus_gate_passed'}

def delete_failed_job(previous, replacement):
    """Delete only a failed direct child; never follow a symlink or remove a published run."""
    for path in (previous,replacement):
        if path.is_symlink() or path.resolve().parent!=RUNS.resolve() or not re.fullmatch(r'[0-9a-f]{32}',path.name):
            raise RuntimeError('Unsafe job path for failed-output cleanup')
    if previous==replacement or read(previous/'status.json').get('status')!='failed':
        raise RuntimeError('Only failed jobs may be deleted')
    if read(replacement/'status.json').get('status')!='queued':
        raise RuntimeError('Replacement must already be queued')
    if read(replacement/'job.json').get('source',{}).get('retry_of')!=previous.name:
        raise RuntimeError('Replacement does not refer to this failed job')
    published=read(ROOT/'viewer/four_case_manifest.json').get('runs',[])
    if any(r.get('id')==f'run_{previous.name}' for r in published):
        raise RuntimeError('Published results must never be removed')
    shutil.rmtree(previous)

def prepare_retry(batch,entry,microns):
    previous=Path(entry['job_dir'])
    prior=read(previous/'job.json')
    # The later explicit request retains raw wall diagnostics for spatial A/B
    # analysis. This archive is never used to initialise a replacement run.
    archive=BATCH/'diagnostic_attempts'/previous.name
    archive.mkdir(parents=True,exist_ok=True)
    for name in ('job.json','status.json'):
        shutil.copy2(previous/name,archive/name)
    for relative in ('case/reports/production_mesh','case/diagnostics/wall','case/runs/steady_cold_flow/reports/solver'):
        source=previous/relative
        if source.is_dir():
            shutil.copytree(source,archive/relative,dirs_exist_ok=True)
    snappy=previous/'case/logs/production_mesh/snappyHexMesh.log'
    if snappy.is_file():shutil.copy2(snappy,archive/'snappyHexMesh.log')
    # The original STEP inputs live in shared staging, outside the failed job.
    for source in prior['files'].values():
        p=Path(source)
        if hashlib.sha256(p.read_bytes()).hexdigest()!=p.stem:
            raise RuntimeError('Frozen STEP input hash mismatch')
    new_id=uuid.uuid4().hex
    job_dir=RUNS/new_id
    job_dir.mkdir(exist_ok=False)
    job={**prior,'id':new_id,'status':'queued','mesh_profile':f'production_600k_frame_fit_{microns}um','source':{**prior.get('source',{}),'retry_of':previous.name,'first_layer_microns':microns,'reason':'y+ area criterion failed; regenerate mesh with unchanged physics and acceptance limits'}}
    write(job_dir/'job.json',job)
    write(job_dir/'status.json',{'status':'queued','stage':'cad','message':f'Ø{entry["diameter_mm"]} 재시도: 실패 생성물 삭제 후 새 메시, 첫 격자층 {microns} µm'})
    failure={'id':entry['id'],'status':read(previous/'status.json'),'mesh_profile':prior.get('mesh_profile'),'first_layer_microns':entry.get('first_layer_microns',10),'yplus':read(previous/'case/runs/steady_cold_flow/reports/solver/yplus.json').get('battery_surface',{}),'diagnostics_archive':str(archive),'output_deleted':False}
    entry.setdefault('prior_attempts',[]).append(failure)
    entry.update(id=new_id,job_dir=str(job_dir),first_layer_microns=microns)
    write(BATCH/'batch.json',batch)
    delete_failed_job(previous,job_dir)
    failure['output_deleted']=True
    write(BATCH/'batch.json',batch)
    print(f'Deleted failed {previous.name}; queued new Ø{entry["diameter_mm"]} job {new_id} ({microns} µm)',flush=True)
    return job_dir

def next_thickness(entry):
    current=entry.get('first_layer_microns',10)
    return 6 if current>6 else 4 if current>4 else None

if __name__=='__main__':
    with (BATCH/'retry.lock').open('a+') as single:
        fcntl.flock(single,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Existing batch only reads batch.json at startup and owns its current
        # job. Updating a terminal failed entry cannot touch its active job.
        batch=read(BATCH/'batch.json')
        for entry in batch['jobs']:
            thickness=next_thickness(entry)
            if thickness and yplus_only(Path(entry['job_dir'])):
                prepare_retry(batch,entry,thickness)
        write(BATCH/'retry_status.json',{'status':'waiting_for_current_batch','pid':os.getpid(),'plan':'Delete failed outputs and regenerate from frozen STEP inputs; 6 µm then 4 µm; unchanged gates'})
        with (BATCH/'batch.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            batch=read(BATCH/'batch.json')
            os.environ.update(OPENFOAM_MPI_RANKS=str(batch['solver_mpi_ranks']),OPENFOAM_MESH_MPI_RANKS='1',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
            for entry in batch['jobs']:
                while True:
                    job_dir=Path(entry['job_dir'])
                    state=read(job_dir/'status.json')
                    if state.get('status')=='failed' and yplus_only(job_dir):
                        microns=next_thickness(entry)
                        if microns is None: break
                        job_dir=prepare_retry(batch,entry,microns)
                        state=read(job_dir/'status.json')
                    if state.get('status')!='queued' or not read(job_dir/'job.json').get('source',{}).get('retry_of'):
                        break
                    active={'status':'running','current':entry,'pid':os.getpid(),'solver_mpi_ranks':batch['solver_mpi_ranks']}
                    write(BATCH/'retry_status.json',active)
                    write(BATCH/'status.json',active)
                    try:
                        run_imported_case(job_dir/'job.json')
                    except Exception as exc:
                        print(str(exc),flush=True)
            states=[{'diameter_mm':e['diameter_mm'],'id':e['id'],**read(Path(e['job_dir'])/'status.json')} for e in batch['jobs']]
            complete=all(s.get('status')=='complete' and not s.get('release_error') for s in states)
            final={'status':'complete' if complete else 'needs_attention','runs':states,'pid':os.getpid(),'finished_at':time.time()}
            write(BATCH/'retry_status.json',final)
            write(BATCH/'status.json',final)
