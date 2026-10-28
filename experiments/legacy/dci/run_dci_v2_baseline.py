#!/usr/bin/env python3
"""Run ECMP+IRN and five packet-random runs on the symmetric two-path topology."""
import argparse
import csv
import os
from pathlib import Path
import re
import shlex
import signal
import statistics
import subprocess
import sys


def fields(line):
    return dict(re.findall(r'([A-Za-z0-9_]+)=([^\s]+)', line))


def measure(run_dir, mode, rng_run):
    row = dict(run=run_dir.name, routing=mode, rng_run=rng_run, measurement='INVALID', outcome='UNKNOWN')
    try:
        log = (run_dir / 'logs/simulator.log').read_text(errors='replace')
        endings = [fields(s) for s in log.splitlines() if s.startswith('DCI_EXPERIMENT_END ')]
        if len(endings) != 1:
            raise ValueError('missing or duplicate DCI_EXPERIMENT_END')
        end = endings[0]
        if end['completed'] != '1':
            row.update(measurement='OK', outcome='INCOMPLETE')
            return row
        configs = [fields(s) for s in log.splitlines() if s.startswith('DCI_ROUTING_CONFIG ')]
        for node in ('6', '7'):
            multi = [s for s in configs if s['node'] == node and s['candidate_count'] == '2']
            if len(multi) != 1 or multi[0]['mode'] != mode or multi[0]['ports'] != '3,4':
                raise ValueError('unexpected WAN routing configuration at node ' + node)
            if multi[0]['rng_seed'] != '1' or multi[0]['rng_run'] != str(rng_run):
                raise ValueError('RNG setting mismatch')
        times = re.findall(r'all passes finished at time:\s*(\d+)', log)
        if len(times) != 1:
            raise ValueError('missing or duplicate rank-0 completion')
        stats = [fields(s) for s in log.splitlines() if s.startswith('IRN_STATS ')]
        stats = [s for s in stats if s['node'] in ('0', '1', '2', '3')]
        if len(stats) != 4 or {s['node'] for s in stats} != {'0','1','2','3'}:
            raise ValueError('expected one IRN record per GPU')
        row.update(rank0_ms=int(times[0])/1e6, global_exit_ms=int(end['time_ns'])/1e6)
        mapping = dict(retrans_bytes='live_qp_retrans_bytes', ooo_packets='rx_ooo_packets',
                       duplicate_packets='rx_duplicate_packets', timeouts='live_qp_timeouts',
                       window_drops='rx_window_drops', outstanding_bytes='outstanding_bytes')
        for name, key in mapping.items():
            row[name] = sum(int(s[key]) for s in stats)
        if row['outstanding_bytes'] != 0 or row['global_exit_ms'] < row['rank0_ms']:
            raise ValueError('inconsistent completion or outstanding DATA')
        ports = [fields(s) for s in log.splitlines() if s.startswith('DCI_ROUTING_PORT ')]
        for node in ('6', '7'):
            wan = [s for s in ports if s['node'] == node and s['port'] in ('3','4')]
            if len(wan) != 2 or {s['port'] for s in wan} != {'3','4'}:
                raise ValueError('expected two WAN port records at node ' + node)
            for port in wan:
                row['node'+node+'_port'+port['port']+'_packets'] = int(port['selected_packets'])
        row.update(measurement='OK', outcome='COMPLETE')
    except (OSError, KeyError, ValueError) as exc:
        row['problem'] = str(exc)
    return row


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--suffix', default='002')
    p.add_argument('--root', type=Path, default=Path.cwd())
    p.add_argument('--simulator', type=Path)
    p.add_argument('--wall-seconds', type=int, default=600)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--summarize', action='store_true')
    a = p.parse_args()
    if not re.fullmatch('[A-Za-z0-9_-]+', a.suffix) or a.wall_seconds < 1:
        p.error('invalid suffix or wall-seconds')
    root = a.root.resolve()
    runner = root / 'experiments/scripts/run.py'
    if not runner.is_file():
        p.error('run from the project root, or specify --root')
    sim = a.simulator or root / ('astra-sim-alibabacloud/extern/network_backend/ns3-interface/'
                                'simulation/build-nccl/scratch/ns3.36.1-AstraSimNetwork-optimized')
    sim = sim.resolve()
    runs = root / 'experiments/runs'
    prepared = root / 'experiments/prepared' / ('dci-v2-baseline-' + a.suffix)
    cases = [('ecmp', 1)] + [('spray_random', i) for i in range(1, 6)]
    cases = [(mode, rng, f'dci-v2-{mode}-2path-run{rng}-{a.suffix}') for mode, rng in cases]
    if not a.summarize:
        template = (root / 'experiments/configs/dci-routing/spray_random.conf').read_text()
        required = dict(RDMA_TRANSPORT_MODE='0', CC_MODE='1', RC_ACK_RETRY_ENABLE='0',
                        QP_WIN_BYTES='0', L2_BACK_TO_ZERO='0', OCS_SCHEDULE_ENABLE='0',
                        ERROR_RATE_PER_LINK='0')
        config = dict(line.split()[:2] for line in template.splitlines()
                      if line.strip() and not line.lstrip().startswith('#') and len(line.split()) >= 2)
        for key, value in required.items():
            if key not in config or float(config[key]) != float(value):
                p.error('baseline config mismatch: ' + key)
        if any((runs/name).exists() for _, _, name in cases):
            p.error('a target run already exists; use --summarize or a new --suffix')
        if not a.dry_run and not os.access(sim, os.X_OK):
            p.error('simulator is not executable: ' + str(sim))
        prepared.mkdir(parents=True, exist_ok=True)
        for mode in ('ecmp','spray_random'):
            text, count = re.subn(r'(?m)^DCI_ROUTING_MODE\s+\S+\s*$', 'DCI_ROUTING_MODE '+mode, template)
            if count != 1:
                p.error('expected one DCI_ROUTING_MODE in template')
            target = prepared / (mode+'.conf')
            if target.exists() and target.read_text() != text:
                p.error('prepared config changed; use a new suffix')
            target.write_text(text)
        for mode, rng, name in cases:
            env = os.environ.copy()
            for key in tuple(env):
                if key.startswith(('SIMAI_IRN_', 'SIMAI_NCCL_', 'SIMAI_FIG2_', 'SIMAI_DCI_')):
                    env.pop(key)
            env.update(SIMAI_IRN_ENABLE='1', SIMAI_NCCL_CHANNELS='4', SIMAI_FIG2_QPS_PER_CHANNEL='4',
                       SIMAI_NCCL_NET_MONITOR='1', AS_PXN_ENABLE='0', AS_NVLS_ENABLE='0',
                       SIMAI_DCI_EXPERIMENT_MS='1000', NS_GLOBAL_VALUE=f'RngSeed=1;RngRun={rng}')
            cmd = [sys.executable, str(runner), name, '--workload',
                   'nccl-channel-buffer/micro-allreduce4-256mib.txt', '--topology',
                   'dci-routing/4gpu-2site-2x400g-50km.txt', '--config-template',
                   str(prepared/(mode+'.conf')), '--simulator', str(sim), '--nvls-enable','0']
            print(f'CASE mode={mode} seed=1 run={rng}: ' + shlex.join(cmd), flush=True)
            if a.dry_run:
                continue
            print('progress_log='+str(prepared/(name+'-runner.log')), flush=True)
            with (prepared/(name+'-runner.log')).open('w') as output:
                process = subprocess.Popen(cmd, cwd=root, env=env, stdout=output,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = process.wait(timeout=a.wall_seconds)
                except subprocess.TimeoutExpired:
                    stop(process)
                    p.exit(1, f'Wall-clock timeout: {name}; log={prepared/(name+"-runner.log")}\n')
                except KeyboardInterrupt:
                    stop(process)
                    raise
            (prepared/(name+'-exit.txt')).write_text(str(code)+'\n')
            print('runner_exit_code='+str(code), flush=True)
            if code != 0:
                p.exit(1, 'Stopped after unsuccessful case; inspect '+str(prepared/(name+'-runner.log'))+'\n')
        if a.dry_run:
            return 0
    rows = []
    for mode, rng, name in cases:
        row = measure(runs/name, mode, rng)
        execution = prepared/(name+'-exit.txt')
        if execution.exists():
            row['runner_exit_code'] = int(execution.read_text())
            if row['runner_exit_code'] not in (0,3):
                row.update(measurement='INVALID', problem='runner failed')
        rows.append(row)
    runs.mkdir(parents=True, exist_ok=True)
    csv_path = runs / ('dci-v2-baseline-'+a.suffix+'-summary.csv')
    with csv_path.open('w', newline='') as out:
        writer = csv.DictWriter(out, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        writer.writeheader(); writer.writerows(rows)
    for row in rows:
        print(' '.join(f'{k}={row[k]}' for k in ('run','measurement','outcome','rank0_ms','global_exit_ms',
                                               'retrans_bytes','ooo_packets','duplicate_packets','problem') if k in row))
    complete = [row for row in rows if row['measurement']=='OK' and row['outcome']=='COMPLETE']
    random = [row for row in complete if row['routing']=='spray_random']
    baseline = [row for row in complete if row['routing']=='ecmp']
    if len(random)==5 and len(baseline)==1:
        for key in ('rank0_ms','global_exit_ms','retrans_bytes','ooo_packets'):
            values = [row[key] for row in random]
            print(f'RANDOM_5 {key} mean={statistics.mean(values):.6f} sd={statistics.stdev(values):.6f}'
                  f' min={min(values)} max={max(values)} ecmp={baseline[0][key]}')
    print('summary='+str(csv_path))
    return int(len(complete)!=len(cases))


if __name__ == '__main__':
    raise SystemExit(main())
