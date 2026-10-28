"""Reusable DCI experiment orchestration behind experiments/scripts/run.py."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys

from dci_results import measure

MODES=('ecmp','spray_rr','spray_random')
OUTPUT_KEYS={'FLOW_FILE','TRACE_FILE','TRACE_OUTPUT_FILE','FCT_OUTPUT_FILE','PFC_OUTPUT_FILE',
             'SEND_OUTPUT_FILE','QLEN_MON_FILE','BW_MON_FILE','RATE_MON_FILE','CNP_MON_FILE'}


def digest(data):return hashlib.sha256(data).hexdigest()
def fields(line):return dict(re.findall(r'([A-Za-z0-9_]+)=([^\s]+)',line))


def config_values(text):
    result={}
    for line in text.splitlines():
        parts=line.split('#',1)[0].split()
        if not parts:continue
        if len(parts)<2 or parts[0] in result:raise ValueError('missing/duplicate config value: '+parts[0])
        result[parts[0]]=' '.join(parts[1:])
    return result


def normalized_config(text):
    values=config_values(text)
    values.pop('DCI_ROUTING_MODE',None)
    values.setdefault('DCI_ROUTING_NODES','6,7')
    result={}
    for key,value in values.items():
        if key in OUTPUT_KEYS:continue
        tokens=[]
        for token in value.split():
            try:
                number=Decimal(token)
                tokens.append(format(number.normalize(),'f') if number.is_finite() else token)
            except InvalidOperation:tokens.append(token)
        result[key]=' '.join(tokens)
    return result


def configure(text,mode,nodes):
    for key,value in [('DCI_ROUTING_MODE',mode),('DCI_ROUTING_NODES',','.join(map(str,nodes)))]:
        pattern=r'(?m)^'+key+r'[^\S\n]+[^\n]*$'
        text,count=re.subn(pattern,key+' '+value,text)
        if count>1:raise ValueError('duplicate '+key)
        if not count:text+='\n'+key+' '+value+'\n'
    return text


def csv_values(raw,cast=int):
    values=[cast(v) for v in raw.split(',')]
    if not values or len(set(values))!=len(values):raise argparse.ArgumentTypeError('use distinct comma-separated values')
    return values


def load_options(root,args):
    source=(root/args.spec).resolve() if not Path(args.spec).is_absolute() else Path(args.spec)
    options=json.loads(source.read_text())
    for key in ['group','workload','topology','config_template','seed','channels','qps','budget_ms','wall_seconds']:
        if getattr(args,key,None) is not None:options[key]=getattr(args,key)
    for key in ['routing','irn','rng_runs','gpu_nodes','dci_nodes']:
        if getattr(args,key,None) is not None:options[key]=getattr(args,key)
    if not re.fullmatch('[A-Za-z0-9_-]+',options['group']):raise ValueError('invalid group name')
    if not options['routing'] or any(v not in MODES for v in options['routing']):raise ValueError('invalid routing mode')
    if not options['irn'] or any(v not in (0,1) for v in options['irn']):raise ValueError('IRN must be 0 and/or 1')
    if not options['rng_runs'] or any(v<1 for v in options['rng_runs']):raise ValueError('RngRun must be positive')
    if not 1<=options['seed']<2147483647 or options['channels']<1 or options['qps'] not in (1,2,4,8,16,32):raise ValueError('invalid seed/channels/QPs')
    if not 1<=options['budget_ms']<=10000 or options['wall_seconds']<1:raise ValueError('invalid time budget')
    if not options['gpu_nodes'] or not options['dci_nodes'] or any(v<0 for v in options['gpu_nodes']+options['dci_nodes']):raise ValueError('invalid node lists')
    for key in ('workload','topology','config_template'):
        options[key]=str((root/options[key]).resolve())
        if not Path(options[key]).is_file():raise ValueError('missing '+options[key])
    options['diagnostics']=bool(getattr(args,'diagnostics',False))
    options['diag_src']=getattr(args,'diag_src',0);options['diag_dst']=getattr(args,'diag_dst',3)
    options['diag_sport']=getattr(args,'diag_sport',0);options['max_events']=getattr(args,'max_events',200000)
    if options['diagnostics'] and 0 in options['irn']:raise ValueError('diagnostics require --irn 1')
    if any(not 0<=options[k]<=65535 for k in ('diag_src','diag_dst','diag_sport')) or options['max_events']<0:raise ValueError('invalid diagnostic filter')
    return options


def case_specs(options):
    template=Path(options['config_template']).read_text()
    baseline=config_values(template)
    if 1 in options['irn']:
        for key,value in [('RDMA_TRANSPORT_MODE','0'),('CC_MODE','1'),('RC_ACK_RETRY_ENABLE','0'),('L2_BACK_TO_ZERO','0')]:
            if key not in baseline or Decimal(baseline[key])!=Decimal(value):raise ValueError('IRN configuration requires '+key+'='+value)
    for mode in options['routing']:
        for rng in (options['rng_runs'] if mode=='spray_random' else options['rng_runs'][:1]):
            for irn in options['irn']:
                config=configure(template,mode,options['dci_nodes'])
                identity=dict(workload_sha256=digest(Path(options['workload']).read_bytes()),
                    topology_sha256=digest(Path(options['topology']).read_bytes()),configuration=normalized_config(config),
                    routing=mode,irn=irn,rng_run=rng,seed=options['seed'],channels=options['channels'],qps=options['qps'],
                    budget_ms=options['budget_ms'],gpu_nodes=options['gpu_nodes'],dci_nodes=options['dci_nodes'])
                if options['diagnostics']:
                    identity['diagnostics']={k:options[k] for k in ('diag_src','diag_dst','diag_sport','max_events')}
                key=digest(json.dumps(identity,sort_keys=True).encode())[:16]
                yield dict(identity=identity,key=key,mode=mode,irn=irn,rng=rng,config=config)


def read_snapshot(run_dir,manifest,key):
    value=manifest.get('inputs',{}).get(key,{}).get('snapshot')
    if not value:return None
    # Resolve inside this run, even after the project has moved to another machine.
    candidate=run_dir/'inputs'/Path(value).name
    return candidate if candidate.is_file() else None


def probe_historical(folder,spec):
    expected=spec['identity']
    try:
        manifest=json.loads((folder/'outputs/manifest.json').read_text())
        if manifest.get('threads')!=1 or manifest.get('environment',{}).get('AS_SEND_LAT')!='3' or manifest.get('environment',{}).get('AS_NVLS_ENABLE')!='0':return None
        workload=read_snapshot(folder,manifest,'workload');topology=read_snapshot(folder,manifest,'topology')
        config=read_snapshot(folder,manifest,'configuration')
        if any(p is None for p in (workload,topology,config)):return None
        if digest(workload.read_bytes())!=expected['workload_sha256'] or digest(topology.read_bytes())!=expected['topology_sha256']:return None
        if normalized_config(config.read_text())!=expected['configuration']:return None
        configs=[];qp=[];channels=set();diag=[];irn_config=[];bounds=[]
        with (folder/'logs/simulator.log').open(errors='replace') as source:
            for line in source:
                if line.startswith('DCI_ROUTING_CONFIG '):configs.append(fields(line))
                elif line.startswith('FIG2_QP_CONFIG '):qp.append(fields(line))
                elif line.startswith('FIG2_QP_CREATE '):channels.add(int(fields(line)['channel']))
                elif line.startswith('IRN_DIAG_CONFIG '):diag.append(fields(line))
                elif line.startswith('IRN_CONFIG '):irn_config.append(fields(line))
                elif line.startswith('maxRtt='):bounds.append(fields(line))
        if len(qp)!=1 or int(qp[0]['qps_per_channel'])!=expected['qps'] or len(channels)!=expected['channels']:return None
        if expected['irn']:
            # Manual BDP/RTO overrides were not recorded in older manifests.
            # Accept only the known derived-default initialization used by these baselines.
            if len(irn_config)!=1 or len(bounds)!=1:return None
            if int(irn_config[0]['bdp_bytes'])!=int(bounds[0]['maxBdp']) or int(irn_config[0]['rto_low_ns'])!=2*int(bounds[0]['maxRtt']) or int(irn_config[0]['rto_high_ns'])!=4*int(bounds[0]['maxRtt']):return None
        if 'diagnostics' in expected:
            d=expected['diagnostics']
            if len(diag)!=1 or int(diag[0]['src'])!=d['diag_src'] or int(diag[0]['dst'])!=d['diag_dst']:return None
            if d['diag_sport'] and int(diag[0]['sport'])!=d['diag_sport']:return None
            if int(diag[0]['max_events'])<d['max_events']:return None
        code=manifest.get('result',{}).get('return_code')
        row=measure(folder,spec['mode'],spec['irn'],spec['rng'],code,
                    expected['budget_ms'],expected['seed'],tuple(expected['gpu_nodes']),tuple(expected['dci_nodes']))
        if row['measurement']!='OK':return None
        row.update(source='historical',provenance='inputs_and_logged_settings; old binary/environment hashes unavailable')
        return row
    except (OSError,KeyError,ValueError,TypeError):return None


def find_result(root,spec,strict_build=None):
    runs=root/'experiments/runs'
    if not runs.is_dir():return None
    for folder in sorted(runs.iterdir(),key=lambda p:p.stat().st_mtime,reverse=True):
        if not folder.is_dir():continue
        metadata=folder/'inputs/dci-case.json'
        if metadata.is_file():
            try:
                record=json.loads(metadata.read_text())
                if record['identity']!=spec['identity']:continue
                if strict_build and record.get('simulator_sha256')!=strict_build:continue
                code=record.get('exit_code');e=spec['identity']
                row=measure(folder,spec['mode'],spec['irn'],spec['rng'],code,e['budget_ms'],e['seed'],tuple(e['gpu_nodes']),tuple(e['dci_nodes']))
                if row['measurement']=='OK':
                    row.update(source='cached',provenance='recorded_inputs_and_options');return row
            except (OSError,KeyError,ValueError,TypeError):continue
        elif not strict_build:
            row=probe_historical(folder,spec)
            if row:return row
    return None


def simulator_path(root,explicit=None):
    if explicit:return (root/explicit).resolve()
    names=['astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation/build-nccl/scratch/ns3.36.1-AstraSimNetwork-optimized',
           'bin/SimAI_simulator','ns-3-alibabacloud/simulation/build/scratch/ns3.36.1-AstraSimNetwork-default']
    return next((root/n for n in names if os.access(root/n,os.X_OK)),root/names[0])


def execute(root,options,spec,sim,study):
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    name=f"dci-{options['group']}-{spec['mode']}-irn{spec['irn']}-r{spec['rng']}-{spec['key'][:8]}-{stamp}"
    config=study/'prepared'/(spec['key']+'.conf');config.parent.mkdir(parents=True,exist_ok=True);config.write_text(spec['config'])
    env=os.environ.copy()
    for key in tuple(env):
        if key.startswith(('SIMAI_IRN_','SIMAI_NCCL_','SIMAI_FIG2_','SIMAI_DCI_')):env.pop(key)
    env.update(SIMAI_IRN_ENABLE=str(spec['irn']),SIMAI_NCCL_CHANNELS=str(options['channels']),
        SIMAI_FIG2_QPS_PER_CHANNEL=str(options['qps']),SIMAI_NCCL_NET_MONITOR='1',AS_PXN_ENABLE='0',AS_NVLS_ENABLE='0',
        SIMAI_DCI_EXPERIMENT_MS=str(options['budget_ms']),NS_GLOBAL_VALUE=f"RngSeed={options['seed']};RngRun={spec['rng']}")
    if options['diagnostics']:
        env.update(SIMAI_IRN_DIAG='1',SIMAI_IRN_DIAG_SRC=str(options['diag_src']),SIMAI_IRN_DIAG_DST=str(options['diag_dst']),
                   SIMAI_IRN_DIAG_SPORT=str(options['diag_sport']),SIMAI_IRN_DIAG_MAX_EVENTS=str(options['max_events']))
    cmd=[sys.executable,str(root/'experiments/scripts/run.py'),name,'--workload',options['workload'],
         '--topology',options['topology'],'--config-template',str(config),'--simulator',str(sim),'--nvls-enable','0']
    log=study/'prepared'/(name+'-runner.log');print('RUN '+name+'\nprogress='+str(log),flush=True)
    with log.open('w') as output:
        process=subprocess.Popen(cmd,cwd=root,env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        def stop():
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:return
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
        try:code=process.wait(timeout=options['wall_seconds'])
        except subprocess.TimeoutExpired:stop();code=124
        except KeyboardInterrupt:stop();raise
    folder=root/'experiments/runs'/name
    if (folder/'inputs').is_dir():
        record=dict(identity=spec['identity'],exit_code=code,simulator=str(sim),simulator_sha256=digest(sim.read_bytes()))
        (folder/'inputs/dci-case.json').write_text(json.dumps(record,indent=2)+'\n')
    e=spec['identity'];row=measure(folder,spec['mode'],spec['irn'],spec['rng'],code,e['budget_ms'],e['seed'],tuple(e['gpu_nodes']),tuple(e['dci_nodes']))
    row.update(source='new',provenance='recorded_inputs_and_options');return row


def report(rows,study):
    import csv
    study.mkdir(parents=True,exist_ok=True);output=study/'summary.csv'
    with output.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        writer.writeheader();writer.writerows(rows)
    print('\nrouting       irn run source      outcome       rank0_ms  global_ms  retx_MiB')
    for row in rows:
        value=lambda key:'NA' if key not in row else f'{row[key]:.6f}'
        print(f"{row['routing']:<13} {row['irn']:>3} {row['rng_run']:>3} {row.get('source',''):10} {row['outcome']:<12} "
              f"{value('rank0_ms'):>9} {value('global_exit_ms'):>10} {value('retrans_MiB'):>10}")
        if row['measurement']=='INVALID':print('  '+row.get('problem','invalid measurement'))
    for mode in dict.fromkeys(row['routing'] for row in rows):
        for irn in (0,1):
            group=[r for r in rows if r['routing']==mode and r['irn']==irn]
            if not group:continue
            good=[r for r in group if r['measurement']=='OK' and r['outcome']=='COMPLETE']
            print(f'{mode} irn={irn} complete={len(good)}/{len(group)}',end='')
            if len(good)==len(group):
                times=[r['global_exit_ms'] for r in good];sd=statistics.stdev(times) if len(times)>1 else 0
                print(f' global_ms_mean={statistics.mean(times):.6f} sd={sd:.6f}')
            else:print(' mean=NA (missing, invalid or censored cases)')
    if any(r.get('source')=='historical' for r in rows):
        print('REUSE: historical inputs/logged options matched; old builds and all old environment variables cannot be verified. Use --fresh after protocol changes.')
    print('SUMMARY '+str(output))


def resolve_log(root,value):
    path=Path(value)
    if not path.is_absolute():
        direct=root/path
        path=direct if direct.exists() else root/'experiments/runs'/path
    if path.is_dir():path=path/'logs/simulator.log'
    return path


def main(root,argv):
    parser=argparse.ArgumentParser(prog='run.py dci',description='One DCI entry: run missing cases, reuse results, summarize and diagnose.')
    sub=parser.add_subparsers(dest='action',required=True)
    for action in ('run','summary'):
        p=sub.add_parser(action)
        p.add_argument('--spec',default='experiments/configs/dci-routing/experiment.json')
        p.add_argument('--group');p.add_argument('--workload');p.add_argument('--topology');p.add_argument('--config-template')
        p.add_argument('--routing',type=lambda s:csv_values(s,str));p.add_argument('--irn',type=csv_values)
        p.add_argument('--rng-runs',type=csv_values);p.add_argument('--gpu-nodes',type=csv_values);p.add_argument('--dci-nodes',type=csv_values)
        for key in ('seed','channels','qps','budget-ms','wall-seconds'):p.add_argument('--'+key,type=int)
        p.add_argument('--simulator');p.add_argument('--fresh',action='store_true',help='rerun selected cases in new directories')
        p.add_argument('--strict-build',action='store_true',help='reuse only new records with the same executable SHA256')
        p.add_argument('--dry-run',action='store_true',help='print reuse/run plan without starting simulations')
        p.add_argument('--diagnostics',action='store_true');p.add_argument('--diag-src',type=int,default=0)
        p.add_argument('--diag-dst',type=int,default=3);p.add_argument('--diag-sport',type=int,default=0)
        p.add_argument('--max-events',type=int,default=200000)
    for action in ('diagnose','ack-lag'):
        p=sub.add_parser(action);p.add_argument('runs',nargs='+',help='run names, run directories or simulator.log paths')
    args=parser.parse_args(argv)
    try:
        if args.action in ('diagnose','ack-lag'):
            if args.action=='diagnose':from irn_diagnostics import analyze
            else:from ack_lag import analyze
            for value in args.runs:analyze(resolve_log(root,value))
            return 0
        spec_path=Path(args.spec) if Path(args.spec).is_absolute() else root/args.spec
        group=args.group or json.loads(spec_path.read_text())['group']
        if not re.fullmatch('[A-Za-z0-9_-]+',group):raise ValueError('invalid group name')
        study=root/'experiments/studies'/group
        saved=study/'study.json'
        overrides=('workload','topology','config_template','seed','channels','qps','budget_ms','wall_seconds','routing','irn','rng_runs','gpu_nodes','dci_nodes')
        if args.action=='summary' and saved.is_file() and not args.diagnostics and not any(getattr(args,k,None) is not None for k in overrides):
            state=json.loads(saved.read_text());rows=[]
            for entry in state['cases']:
                spec=entry['spec'];name=entry.get('run')
                if not name:
                    rows.append(dict(run='',routing=spec['mode'],irn=spec['irn'],rng_run=spec['rng'],measurement='INVALID',outcome='MISSING',source='missing'));continue
                e=spec['identity'];row=measure(root/'experiments/runs'/name,spec['mode'],spec['irn'],spec['rng'],entry.get('exit_code'),
                    e['budget_ms'],e['seed'],tuple(e['gpu_nodes']),tuple(e['dci_nodes']))
                row['source']=entry.get('source','cached');rows.append(row)
            report(rows,study);return int(any(r['measurement']!='OK' for r in rows))
        options=load_options(root,args)
        study=root/'experiments/studies'/options['group']
        sim=simulator_path(root,args.simulator)
        strict=digest(sim.read_bytes()) if args.strict_build else None
        rows=[];entries=[]
        # The run lock prevents two CLI invocations from writing the same study concurrently.
        import fcntl
        study.mkdir(parents=True,exist_ok=True)
        with (study/'runner.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            specs=list(case_specs(options))
            entries=[dict(spec=spec,run=None,source='missing') for spec in specs]
            if args.action=='run' and not args.dry_run:
                (study/'study.json').write_text(json.dumps(dict(options=options,cases=entries),indent=2)+'\n')
            for position,spec in enumerate(specs):
                row=None if args.fresh and args.action=='run' else find_result(root,spec,strict)
                if row:print('REUSE '+row['run'],flush=True)
                else:
                    print(f"MISSING {spec['mode']} irn={spec['irn']} seed={options['seed']} run={spec['rng']}",flush=True)
                    if args.action=='run' and not args.dry_run:
                        if not os.access(sim,os.X_OK):raise ValueError('simulator is not executable: '+str(sim))
                        row=execute(root,options,spec,sim,study)
                    else:row=dict(run='',routing=spec['mode'],irn=spec['irn'],rng_run=spec['rng'],measurement='INVALID',outcome='MISSING',source='missing')
                rows.append(row);entries[position]=dict(spec=spec,run=row.get('run'),source=row.get('source'),exit_code=row.get('runner_exit_code'))
                if args.action=='run' and not args.dry_run:
                    (study/'study.json').write_text(json.dumps(dict(options=options,cases=entries),indent=2)+'\n')
            if not args.dry_run:report(rows,study)
            else:print('DRY_RUN: no simulations started; existing result directories were not modified.')
        return int(not args.dry_run and any(r['measurement']!='OK' for r in rows))
    except (OSError,KeyError,ValueError,TypeError,InvalidOperation) as error:
        parser.exit(1,str(error)+'\n')
