#!/usr/bin/env python3
"""Shared DCI result validation. Imported by the unified runner."""
import re


def fields(line):
    return dict(re.findall(r'([A-Za-z0-9_]+)=([^\s]+)',line))


def measure(folder,mode,irn,rng_run,code=None,budget_ms=1000,seed=1,gpu_nodes=(0,1,2,3),dci_nodes=(6,7)):
    row=dict(run=folder.name,routing=mode,irn=irn,rng_run=rng_run,measurement='INVALID',outcome='UNKNOWN')
    if code is not None:row['runner_exit_code']=code
    if code==124:
        row.update(outcome='WALL_TIMEOUT',problem='wall-clock limit; not a simulation-budget result')
        return row
    try:
        end=[];transport=[];configs=[];rank=[];irn_stats=[];ports=[];budget=[]
        with (folder/'logs/simulator.log').open(errors='replace') as source:
            for line in source:
                if line.startswith('DCI_EXPERIMENT_END '):end.append(fields(line))
                elif line.startswith('DCI_TRANSPORT_STATS '):transport.append(fields(line))
                elif line.startswith('DCI_ROUTING_CONFIG '):configs.append(fields(line))
                elif line.startswith('IRN_STATS '):irn_stats.append(fields(line))
                elif line.startswith('DCI_ROUTING_PORT '):ports.append(fields(line))
                elif line.startswith('DCI_EXPERIMENT_CONFIG '):budget.append(fields(line))
                match=re.search(r'all passes finished at time:\s*(\d+)',line)
                if match:rank.append(int(match.group(1)))
        if len(end)!=1:raise ValueError('missing/duplicate experiment end')
        if len(budget)!=1 or int(budget[0]['budget_ms'])!=budget_ms:raise ValueError('simulation-budget mismatch')
        completed=end[0]['completed']=='1';observed=int(end[0]['time_ns'])/1e6
        if code is not None and code!=(0 if completed else 3):raise ValueError('exit code disagrees with experiment end')
        if not completed and (end[0]['reason']!='simulation_budget' or abs(observed-budget_ms)>1e-6):
            raise ValueError('unexpected incomplete-run termination')
        for node in map(str,dci_nodes):
            selected=[v for v in configs if v['node']==node and int(v['candidate_count'])>1]
            if not selected or any(v['mode']!=mode for v in selected):
                raise ValueError('WAN mode/candidates mismatch')
            if any(v['rng_seed']!=str(seed) or v['rng_run']!=str(rng_run) for v in selected):raise ValueError('RNG mismatch')
        gpu=[v for v in transport if int(v['node']) in gpu_nodes]
        if len(gpu)!=len(gpu_nodes) or {int(v['node']) for v in gpu}!=set(gpu_nodes):raise ValueError('expected four GPU transport records')
        if any(int(v['irn'])!=irn or int(v['completed'])!=int(completed) for v in gpu):raise ValueError('IRN/completion mismatch')
        for name in ('retrans_bytes','retrans_packets','timeouts','nack_count','outstanding_bytes','acked_bytes','posted_bytes','received_contiguous_bytes','qp_errors'):
            row[name]=sum(int(v[name]) for v in gpu)
        if completed and (row['outstanding_bytes'] or row['qp_errors']):raise ValueError('completed with outstanding DATA or QP errors')
        if completed and (row['acked_bytes']!=row['posted_bytes'] or row['acked_bytes']!=row['received_contiguous_bytes']):
            raise ValueError('completed send/receive byte accounting differs')
        if completed and len(rank)!=1:raise ValueError('missing/duplicate rank-0 completion')
        if len(rank)>1:raise ValueError('duplicate rank-0 completion')
        if rank:
            row['rank0_ms']=rank[0]/1e6
            if row['rank0_ms']>observed:raise ValueError('rank-0 time exceeds end time')
        row['observed_ms']=observed
        if completed:row['global_exit_ms']=observed
        row['retrans_MiB']=row['retrans_bytes']/1048576
        row['generated_retx_GBps']=row['retrans_bytes']/(observed/1000)/1e9
        if irn:
            rx=[v for v in irn_stats if int(v['node']) in gpu_nodes]
            if len(rx)!=len(gpu_nodes) or {int(v['node']) for v in rx}!=set(gpu_nodes):raise ValueError('missing/duplicate GPU IRN records')
            for name,key in [('ooo_packets','rx_ooo_packets'),('duplicate_packets','rx_duplicate_packets'),('window_drops','rx_window_drops')]:
                row[name]=sum(int(v[key]) for v in rx)
        # IRN=0 does not have the same OOO instrumentation; leave those fields empty.
        for node in map(str,dci_nodes):
            candidates={p for v in configs if v['node']==node and int(v['candidate_count'])>1
                        for p in v['ports'].split(',')}
            wan=[v for v in ports if v['node']==node and v['port'] in candidates]
            if len(wan)!=len(candidates) or {v['port'] for v in wan}!=candidates:
                raise ValueError('missing/duplicate candidate-port statistics')
            for port in wan:row['node'+node+'_port'+port['port']+'_packets']=int(port['selected_packets'])
        row.update(measurement='OK',outcome='COMPLETE' if completed else 'INCOMPLETE')
    except (OSError,KeyError,ValueError) as error:row['problem']=str(error)
    return row

