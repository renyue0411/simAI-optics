#!/usr/bin/env python3
"""Fixed GRC-reference C4Q4 baseline; vary DATA routing, ECMP hash and RNG only."""

import sys
from pathlib import Path
_scripts_dir = Path(__file__).resolve().parents[1]
for _part in ('runners', 'tools'):
    sys.path.insert(0, str(_scripts_dir / _part))

import argparse
import csv
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import subprocess
import sys
from dci_common import record_json, relocated_path

ROOT=Path(__file__).resolve().parents[3]
from run_collective import build_identity,values


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(path):
    records=[]
    for line in path.read_text().splitlines():
        if line.startswith('FIG2_QP_CREATE '):
            r=dict(re.findall(r'([A-Za-z0-9_]+)=([^\s]+)',line))
            records.append(tuple(r[k] for k in ('src','dst','channel','index','sport')))
    if len(records)!=64 or len(set(records))!=64:
        raise ValueError('Expected 64 persistent C4Q4 QPs')
    return hashlib.sha256(json.dumps(sorted(records)).encode()).hexdigest()


def aggregate(rows):
    groups={}
    for row in rows:groups.setdefault((row['routing'],str(row['ecmp_seed'])),[]).append(row)
    out=[]
    for (mode,seed),rs in groups.items():
        good=[r for r in rs if r.get('measurement')=='OK' and r.get('outcome')=='COMPLETE']
        item=dict(routing=mode,ecmp_seed=seed,complete=len(good),planned=len(rs),mean_ms='',sd_ms='')
        if len(good)==len(rs):
            times=[float(r['global_exit_ms']) for r in good]
            item.update(mean_ms=statistics.mean(times),sd_ms=statistics.stdev(times) if len(times)>1 else 0)
        out.append(item)
    return out


def write_csv(path,rows):
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--simulator',default='bin/SimAI_simulator')
    p.add_argument('--workload',default='experiments/workloads/nccl-channel-buffer/micro-dp-allreduce4-256mib.txt')
    p.add_argument('--topology',default='experiments/topologies/dci-routing/4gpu-2site-2x400g-50km.txt')
    p.add_argument('--config-template',default='experiments/configs/dci-routing/spray_rr.conf')
    p.add_argument('--ecmp-seeds',type=values,default=[0,15])
    p.add_argument('--rng-runs',type=values,default=[1,2,3])
    p.add_argument('--cnp-interval-us',type=int,default=50)
    p.add_argument('--budget-ms',type=int,default=1000)
    p.add_argument('--wall-seconds',type=int,default=600)
    p.add_argument('--group',default='allreduce-routing-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    p.add_argument('--dry-run',action='store_true')
    a=p.parse_args()
    if not re.fullmatch('[A-Za-z0-9_-]+',a.group):p.error('invalid group')
    if any(not 1<=n<=2147483647 for n in a.rng_runs):p.error('RNG runs must be positive integers')
    if not 1<=a.cnp_interval_us<=1000000 or not 1<=a.budget_ms<=10000 or a.wall_seconds<1:
        p.error('Invalid CNP interval or runtime budget')
    plans=[(mode,rng,a.ecmp_seeds if mode=='ecmp' else [0])
           for rng in a.rng_runs for mode in ('ecmp','spray_rr','spray_random')]
    for mode,rng,seeds in plans:
        print('PLAN routing=%s rng_run=%d ecmp_seeds=%s wan_profile=grc-ref cnp_us=%d PM=0'%(
            mode,rng,','.join(map(str,seeds)),a.cnp_interval_us),flush=True)
    count=sum(len(seeds) for mode,rng,seeds in plans)
    if a.dry_run:
        print('DRY_RUN cases=%d; no simulations started or result directories created'%count);return 0
    simulator=(ROOT/a.simulator).resolve()
    builds=build_identity(simulator)
    folder=ROOT/'experiments/runs/_batches'/a.group;folder.mkdir(parents=True,exist_ok=False)
    state=dict(options=vars(a),build_sha256=builds,input_sha256={
        k:digest(ROOT/getattr(a,k)) for k in ('workload','topology','config_template')},cases=[])
    rows=[];qp_signature=None;failed=False
    for mode,rng,seeds in plans:
        group='%s-%s-r%d'%(a.group,mode,rng)
        command=[sys.executable,str(ROOT/'experiments/scripts/runners/run_collective.py'),
            '--simulator',str(simulator),'--workload',a.workload,'--topology',a.topology,
            '--config-template',a.config_template,'--group',group,'--pm','0',
            '--wan-profile','grc-ref','--cnp-mode','1','--cnp-interval-us',str(a.cnp_interval_us),
            '--irn-timeout-replay','1','--collective-fifo','1','--routing',mode,'--rng-run',str(rng),
            '--hash-seeds',','.join(map(str,seeds)),'--budget-ms',str(a.budget_ms),
            '--wall-seconds',str(a.wall_seconds)]
        progress=folder/(mode+'-r%d-runner.log'%rng)
        print('RUN routing=%s rng_run=%d progress=%s'%(mode,rng,progress),flush=True)
        with progress.open('w') as output:
            rc=subprocess.run(command,cwd=str(ROOT),stdout=output,stderr=subprocess.STDOUT).returncode
        path=ROOT/'experiments/runs/_batches'/group/'summary.csv'
        found=list(csv.DictReader(path.open())) if path.is_file() else []
        by_seed={int(r['ecmp_seed']):r for r in found}
        for seed in seeds:
            row=by_seed.get(seed,dict(routing=mode,rng_run=rng,ecmp_seed=seed,measurement='INVALID',
                                     outcome='MISSING',problem='Missing child summary; see runner log'))
            row.update(routing=mode,rng_run=rng,ecmp_seed=seed,source_group=group)
            if row.get('measurement')=='OK' and row.get('outcome')=='COMPLETE':
                try:
                    signature=fingerprint(ROOT/'experiments/runs'/row['run']/'logs/simulator.log')
                    if qp_signature is not None and signature!=qp_signature:
                        raise ValueError('QP allocation differs across routing/RNG cases')
                    qp_signature=signature
                    row['qp_signature_sha256']=signature
                except (OSError,ValueError,KeyError) as exc:
                    row.update(measurement='INVALID',problem=str(exc))
            if row.get('measurement')!='OK' or row.get('outcome')!='COMPLETE':failed=True
            rows.append(row)
            print('RESULT routing=%s hash=%d rng_run=%d outcome=%s measurement=%s global_ms=%s retx_MiB=%s'%(
                mode,seed,rng,row.get('outcome'),row.get('measurement'),row.get('global_exit_ms','NA'),
                row.get('retrans_MiB','NA')),flush=True)
        if rc:failed=True
        state['cases'].append(dict(command=command,exit_code=rc,group=group,progress=str(progress)))
        write_csv(folder/'cases.csv',rows)
        (folder/'study.json').write_text(json.dumps(state,indent=2)+'\n')
    state['build_consistent']=build_identity(simulator)==builds
    if not state['build_consistent']:
        failed=True
        for row in rows:row.update(measurement='INVALID',problem='Loaded build changed during entire study')
        write_csv(folder/'cases.csv',rows)
    state['qp_signature_sha256']=qp_signature
    (folder/'study.json').write_text(json.dumps(state,indent=2)+'\n')
    summary=aggregate(rows);write_csv(folder/'aggregate.csv',summary)
    print('routing\thash\tcomplete/planned\tmean_ms\tsd_ms')
    for r in summary:
        mean='NA' if r['mean_ms']=='' else '%.6f'%r['mean_ms']
        sd='NA' if r['sd_ms']=='' else '%.6f'%r['sd_ms']
        print('%s\t%s\t%d/%d\t%s\t%s'%(r['routing'],r['ecmp_seed'],r['complete'],r['planned'],mean,sd))
    print(('BASELINE_STUDY_FAILED' if failed else 'BASELINE_STUDY_OK')+' cases=%d results=%s'%(len(rows),folder))
    return int(failed)


if __name__=='__main__':sys.exit(main())
