#!/usr/bin/env python3
"""Replay a recorded case with observation-only diagnostics for one existing QP."""

import sys
from pathlib import Path
_scripts_dir = Path(__file__).resolve().parents[1]
for _part in ('runners', 'tools'):
    sys.path.insert(0, str(_scripts_dir / _part))

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
from dci_common import record_json, relocated_path

ROOT = Path(__file__).resolve().parents[3]
from dci_common import measure
from dci_common import analyze_irn as analyze, fields
from run_collective import build_identity, pm_metrics


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def argument(command, flag):
    if command.count(flag) != 1:
        raise ValueError('Expected one recorded ' + flag)
    return relocated_path(Path(command[command.index(flag) + 1]).resolve())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--study', type=Path, required=True)
    p.add_argument('--routing', choices=('ecmp','spray_rr','spray_random'), default='spray_rr')
    p.add_argument('--ecmp-seed', type=int, default=0)
    p.add_argument('--rng-run', type=int, default=1)
    p.add_argument('--pm', type=int, choices=(0,1), default=0)
    p.add_argument('--src', type=int, default=0)
    p.add_argument('--dst', type=int, default=3)
    p.add_argument('--sport', type=int, default=10000)
    p.add_argument('--max-events', type=int, default=100000)
    p.add_argument('--wall-seconds', type=int, default=600)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    if not 0 <= a.src <= 65535 or not 0 <= a.dst <= 65535 or not 1 <= a.sport <= 65535 or not 1 <= a.max_events <= 1000000 or a.wall_seconds < 1:
        p.error('Invalid diagnostic filter or budget')
    study = relocated_path((ROOT / a.study).resolve())
    table = study / ('cases.csv' if (study / 'cases.csv').is_file() else 'summary.csv')
    with table.open() as f:
        rows = list(csv.DictReader(f))
    matches = [r for r in rows if r.get('routing') == a.routing and int(r.get('ecmp_seed',0)) == a.ecmp_seed and int(r['rng_run']) == a.rng_run and int(r.get('pm',0)) == a.pm]
    if len(matches) != 1:
        raise ValueError('Expected one matching recorded case')
    base = matches[0]
    if base['measurement'] != 'OK' or base['outcome'] != 'COMPLETE' or int(base['irn']) != 1:
        raise ValueError('Replay requires a valid completed IRN baseline')
    child = ROOT / 'experiments/runs/_batches' / base.get('source_group',study.name)
    state = record_json((child / 'study.json').read_text())
    entries = [e for e in state['cases'] if e['run'] == base['run']]
    if len(entries) != 1:
        raise ValueError('Missing/duplicate recorded run command')
    entry = entries[0]
    command = list(entry['command'])
    if len(command) < 3 or command[2] != base['run'] or Path(command[1]).name != 'run.py':
        raise ValueError('Unsupported recorded command layout')
    simulator = argument(command, '--simulator')
    builds = build_identity(simulator)
    if builds != state['build_sha256']:
        raise ValueError('Simulator/loaded libraries differ from baseline')
    for key, flag in (('workload','--workload'),('topology','--topology'),('configuration','--config-template')):
        expected = state['rendered_config_sha256'] if key == 'configuration' else state['input_sha256'][key]
        if sha(argument(command, flag)) != expected:
            raise ValueError('Recorded input changed since baseline: ' + key)
    base_log = ROOT / 'experiments/runs' / base['run'] / 'logs/simulator.log'
    qps = [fields(line) for line in base_log.read_text().splitlines() if line.startswith('FIG2_QP_CREATE ')]
    if not any(int(q['src']) == a.src and int(q['dst']) == a.dst and int(q['sport']) == a.sport for q in qps):
        raise ValueError('Selected QP was not present in the baseline')
    controls = dict(SIMAI_IRN_DIAG='1', SIMAI_IRN_DIAG_SRC=str(a.src), SIMAI_IRN_DIAG_DST=str(a.dst), SIMAI_IRN_DIAG_SPORT=str(a.sport), SIMAI_IRN_DIAG_MAX_EVENTS=str(a.max_events))
    if entry['environment'].get('SIMAI_IRN_DIAG','0') != '0':
        raise ValueError('Baseline already had diagnostics enabled')
    print('BASELINE run=%s global_ms=%s retx_MiB=%s' % (base['run'],base['global_exit_ms'],base['retrans_MiB']), flush=True)
    print('DIAG selected=%d->%d sport=%d observation_only=1' % (a.src,a.dst,a.sport), flush=True)
    if a.dry_run:
        print('DRY_RUN build_inputs_and_QP_verified=1 no_simulation_started=1')
        return 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    name = 'dci-irn-diag-%s-h%d-r%d-%s' % (a.routing,a.ecmp_seed,a.rng_run,stamp)
    command[2] = name
    record_dir = ROOT / 'experiments/runs/_batches' / name
    record_dir.mkdir(parents=True,exist_ok=False)
    env = {k:v for k,v in os.environ.items() if not k.startswith(('SIMAI_','TSRDMA_TEST_','AS_'))}
    env.update(entry['environment'])
    env.update(controls)
    record = dict(baseline_run=base['run'], baseline_row=base, command=command, environment={**entry['environment'],**controls}, build_sha256=builds, baseline_study=str(child), observation_equal=None)
    meta = record_dir / 'replay.json'
    meta.write_text(json.dumps(record,indent=2)+'\n')
    progress = record_dir / 'runner.log'
    print('RUN '+name+'\nprogress='+str(progress),flush=True)
    with progress.open('w') as output:
        proc = subprocess.Popen(command,cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            code = proc.wait(timeout=a.wall_seconds)
        except (subprocess.TimeoutExpired,KeyboardInterrupt) as exc:
            os.killpg(proc.pid,signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL)
                proc.wait()
            if isinstance(exc,KeyboardInterrupt):
                raise
            code = 124
    record['exit_code'] = code
    meta.write_text(json.dumps(record,indent=2)+'\n')
    folder = ROOT / 'experiments/runs' / name
    options = dict(state['options'])
    options.setdefault('routing',a.routing)
    options.setdefault('rng_run',a.rng_run)
    args = SimpleNamespace(**options)
    positional = [folder,a.routing,1,a.rng_run,code,args.budget_ms,1,(0,1,2,3),(6,7)]
    if len(inspect.signature(measure).parameters) >= 10:
        positional.append(a.ecmp_seed)
    result = measure(*positional)
    record['measurement'] = result
    try:
        if result['measurement'] != 'OK' or result['outcome'] != 'COMPLETE':
            raise ValueError('Diagnostic run did not complete validly: '+str(result.get('problem')))
        extras, signature = pm_metrics(folder,a.pm,a.ecmp_seed,args)
        result.update(extras)
        expected_signature = sorted(tuple(q[k] for k in ('src','dst','channel','index','sport')) for q in qps)
        if signature != expected_signature:
            raise ValueError('Diagnostic replay changed the QP allocation')
        if build_identity(simulator) != builds:
            raise ValueError('Build changed during replay')
        same = all(str(result[k]) == str(base[k]) for k in ('global_exit_ms','retrans_bytes','posted_bytes','acked_bytes','received_contiguous_bytes'))
        record['observation_equal'] = same
        if not same:
            raise ValueError('Diagnostics changed CCT/retransmission/payload totals; inspect before interpretation')
        record['diagnostics'] = analyze(folder/'logs/simulator.log')
        print('OBSERVATION_EQUAL_OK global_ms=%s retrans_MiB=%s' % (result['global_exit_ms'],result['retrans_MiB']))
        print('REPLAY_RECORD '+str(meta))
    finally:
        meta.write_text(json.dumps(record,indent=2)+'\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        print('ERROR: '+str(exc),file=sys.stderr)
        sys.exit(1)
