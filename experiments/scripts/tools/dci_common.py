"""Shared configuration and measurements for the experiment scripts."""


# dci_results
#!/usr/bin/env python3
"""Shared DCI result validation. Imported by the unified runner."""

import sys
from pathlib import Path
_scripts_dir = Path(__file__).resolve().parents[1]
for _part in ('runners', 'tools'):
    sys.path.insert(0, str(_scripts_dir / _part))

import re


def fields(line):
    return dict(re.findall(r'([A-Za-z0-9_]+)=([^\s]+)',line))


def measure(folder,mode,irn,rng_run,code=None,budget_ms=1000,seed=1,gpu_nodes=(0,1,2,3),dci_nodes=(6,7),ecmp_seed=0):
    row=dict(run=folder.name,routing=mode,irn=irn,rng_run=rng_run,ecmp_seed=ecmp_seed,measurement='INVALID',outcome='UNKNOWN')
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
            if any(int(v.get('ecmp_seed',0))!=ecmp_seed for v in selected):raise ValueError('ECMP seed mismatch')
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



# cnp_feedback
"""Measured independent-CNP counters; do not equate legacy ACK flags with CNPs."""
import re


def cnp_records(lines, marker):
    return [dict(re.findall(r'([A-Za-z0-9_]+)=([^\s]+)', line))
            for line in lines if line.startswith(marker + ' ')]


def cnp_metrics(lines, mode, interval_us, nodes=(0,1,2,3)):
    name = 'independent' if mode else 'legacy_ack'
    configs = cnp_records(lines, 'DCQCN_CNP_CONFIG')
    stats = cnp_records(lines, 'DCQCN_CNP_STATS')
    selected = []
    for node in nodes:
        c = [r for r in configs if int(r['node']) == node]
        s = [r for r in stats if int(r['node']) == node]
        if len(c) != 1 or len(s) != 1:
            raise ValueError('Missing/duplicate CNP markers for GPU node ' + str(node))
        if c[0]['mode'] != name or s[0]['mode'] != name or\
                int(c[0]['interval_ns']) != interval_us * 1000:
            raise ValueError('CNP mode/interval mismatch; rebuild active source/library')
        selected.append(s[0])
    keys = ('ce_rx_packets', 'cnp_tx_enqueued', 'cnp_tx_modeled_bytes', 'cnp_rx',
            'ack_feedback_rx', 'actual_rate_decreases', 'unknown_qp_cnp_rx',
            'invalid_cnp_rx', 'pending_rx_qps')
    out = {'cnp_mode': name, 'cnp_interval_us': interval_us}
    out.update(('feedback_' + key, sum(int(r[key]) for r in selected)) for key in keys)
    if mode and out['feedback_ack_feedback_rx']:
        raise ValueError('Independent CNP run also received ACK congestion flags')
    if out['feedback_unknown_qp_cnp_rx'] or out['feedback_invalid_cnp_rx']:
        raise ValueError('Unmatched/invalid CNP in persistent-QP collective experiment')
    if not mode and (out['feedback_cnp_tx_enqueued'] or out['feedback_cnp_rx']):
        raise ValueError('Legacy run unexpectedly used independent CNP')
    return out


# wan_profile
"""Validate the effective WAN profile, not just requested CLI options."""
import re


def wan_metrics(lines, args, topology):
    enabled = args.wan_profile == 'grc-ref'
    def select(marker):
        return [dict(re.findall(r'([A-Za-z0-9_]+)=([^\s]+)', line))
                for line in lines if line.startswith(marker + ' ')]
    configs, ports, pools, pfcs = [select(s) for s in (
        'DCI_WAN_PROFILE_CONFIG','DCI_WAN_PROFILE_PORT','DCI_WAN_POOL_STATS','DCI_WAN_PFC_STATS')]
    result = dict(wan_profile=args.wan_profile)
    if not configs and not enabled:
        return result
    wan_keys = {(int(node),p['port']) for node, ps in topology['wan_by_node'].items() for p in ps}
    peak, drops, wan_tx, wan_rx, ignored, suppressed, lan_tx = 0, 0, 0, 0, 0, 0, 0
    for node in (6,7):
        cs=[r for r in configs if int(r['node'])==node]
        ss=[r for r in pools if int(r['node'])==node]
        if len(cs)!=1 or len(ss)!=1 or int(cs[0]['enabled'])!=int(enabled) or int(ss[0]['enabled'])!=int(enabled):
            raise ValueError('Missing/duplicate/mismatched WAN profile markers')
        s=ss[0]
        if enabled:
            expected=topology['mmu_by_node'][str(node)]
            if int(cs[0]['ecn_enabled'])!=1:
                raise ValueError('Gateway ECN marking is disabled')
            if int(cs[0]['buffer_bytes'])!=expected['buffer_bytes'] or int(s['capacity_bytes'])!=expected['buffer_bytes'] or\
                    int(s['shared_capacity_bytes'])!=expected['shared_capacity_bytes'] or not int(s['tracked']):
                raise ValueError('WAN MMU capacity/reservation differs from requested model')
            if int(s['data_peak_bytes'])>int(s['capacity_bytes']) or int(s['data_used_bytes'])!=int(s['egress_data_bytes']) or\
                    int(s['shared_used_bytes'])>int(s['shared_capacity_bytes']):
                raise ValueError('WAN finite DATA pool accounting failure')
        peak=max(peak,int(s['data_peak_bytes']))
        drops+=int(s['admission_drops'])
        ps=[r for r in ports if int(r['node'])==node]
        fs=[r for r in pfcs if int(r['node'])==node]
        expected_ports=set(range(1,topology['port_counts'][node]+1))
        if {int(r['port']) for r in ps}!=expected_ports or len(ps)!=len(expected_ports) or\
                {int(r['port']) for r in fs}!=expected_ports or len(fs)!=len(expected_ports):
            raise ValueError('Missing/duplicate gateway port observations')
        for p,f in zip(sorted(ps,key=lambda r:int(r['port'])),sorted(fs,key=lambda r:int(r['port']))):
            is_wan=(node,int(p['port'])) in wan_keys
            if int(p['wan'])!=int(is_wan) or int(f['wan'])!=int(is_wan):
                raise ValueError('WAN interface set differs from topology')
            if enabled and is_wan:
                if int(p['pfc']) or int(f['enabled']) or int(p['headroom_bytes']) or int(f['paused_queues']):
                    raise ValueError('PFC remains enabled/paused on a WAN port')
                if int(p['kmin_bytes'])!=args.wan_ecn_kmin_kb*1000 or int(p['kmax_bytes'])!=args.wan_ecn_kmax_kb*1000 or\
                        abs(float(p['pmax'])-args.wan_ecn_pmax)>1e-6:
                    raise ValueError('WAN ECN settings differ from requested values')
                if int(f['tx_packets']) or int(f['rx_packets']):
                    raise ValueError('Unexpected WAN PFC packet during collective acceptance')
            if enabled and not is_wan and not int(p['pfc']):
                raise ValueError('LAN PFC was unexpectedly disabled')
            if is_wan:
                wan_tx+=int(f['tx_packets']); wan_rx+=int(f['rx_packets'])
                ignored+=int(f['rx_ignored']); suppressed+=int(f['tx_suppressed'])
            else:
                lan_tx+=int(f['tx_packets'])
    closed=''
    end=select('DCI_EXPERIMENT_END')
    if enabled and len(end)==1 and end[0]['completed']=='1':
        transport=[r for r in select('DCI_TRANSPORT_STATS') if int(r['node']) in (0,1,2,3)]
        expected=6*args.workload_info['bytes']
        if len(transport)!=4 or {int(r['node']) for r in transport}!={0,1,2,3} or any(
                sum(int(r[k]) for r in transport)!=expected
                for k in ('posted_bytes','acked_bytes','received_contiguous_bytes')) or any(
                int(r['outstanding_bytes']) or int(r['qp_errors']) for r in transport):
            raise ValueError('Completed AllReduce payload volume does not close')
        closed=1
    result.update(wan_volume_closed=closed,wan_data_peak_bytes=peak if enabled else '',wan_admission_drops=drops,
        wan_pfc_tx=wan_tx,wan_pfc_rx=wan_rx,wan_pfc_ignored=ignored,
        wan_pfc_tx_suppressed=suppressed,gateway_lan_pfc_tx=lan_tx)
    return result


# pm_topology
"""Runtime description/preflight for the existing eight-node two-DC PM benchmark.

No topology value here is a measurement supplied to the path manager. The
description is used only to initialize/check the simulator and interpret logs.
"""
from decimal import Decimal
from pathlib import Path
import re

UINT32 = (1 << 32) - 1
UINT64 = (1 << 64) - 1
RATE_UNITS = {'bps': 1, 'b/s': 1, 'Kbps': 1000, 'kbps': 1000, 'kb/s': 1000,
              'Mbps': 1000000, 'Mb/s': 1000000, 'Gbps': 1000000000,
              'Gb/s': 1000000000, 'Tbps': 1000000000000, 'Tb/s': 1000000000000}
TIME_UNITS = {'s': 1000000000, 'ms': 1000000, 'us': 1000, 'ns': 1, 'ps': Decimal('.001')}


def quantity(token, units, label):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([A-Za-z/]+)', token)
    if not match or match[2] not in units:
        raise ValueError('Unsupported ' + label + ': ' + token)
    value = Decimal(match[1]) * units[match[2]]
    if value != value.to_integral_value() or value < 0:
        raise ValueError(label + ' must be an integral bps/ns value: ' + token)
    return int(value)


def describe_topology(path):
    lines = [line.split('#', 1)[0].split() for line in Path(path).read_text().splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 3 or len(lines[0]) != 6 or [int(v) for v in lines[0][:4]] != [8, 2, 2, 2] or\
            lines[1] != ['4', '5', '6', '7']:
        raise ValueError('Use the existing eight-node/four-GPU layout with NVSwitches 4,5 and gateways 6,7')
    if len(lines) - 2 != int(lines[0][4]):
        raise ValueError('Topology link count does not match its header')
    port_counts = [0] * 8  # InternetStack installs device 0 (loopback) first.
    wan = {'6': [], '7': []}
    links = []
    required = {(0, 4), (1, 4), (2, 5), (3, 5), (0, 6), (1, 6), (2, 7), (3, 7)}
    local = {}
    for line in lines[2:]:
        if len(line) != 5:
            raise ValueError('This benchmark accepts legacy five-column links: src dst rate delay error')
        a, b = int(line[0]), int(line[1])
        if not 0 <= a < 8 or not 0 <= b < 8 or a == b:
            raise ValueError('Invalid topology endpoint')
        pair = tuple(sorted((a, b)))
        if pair not in required and pair != (6, 7):
            raise ValueError('Unexpected link outside the two-DC direct-WAN layout: ' + str(pair))
        rate = quantity(line[2], RATE_UNITS, 'rate')
        delay = quantity(line[3], TIME_UNITS, 'delay')
        error = Decimal(line[4])
        if not 0 < rate <= UINT64 or delay > (1 << 63) - 1 or not error.is_finite() or\
                not 0 <= error <= 1 or rate * delay > UINT64:
            raise ValueError('Invalid rate/error or overflowing rate-delay product')
        record = dict(src=a, dst=b, rate_bps=rate, delay_ns=delay, error=str(error))
        for node in (a, b):
            port_counts[node] += 1
            if port_counts[node] > 1024:
                raise ValueError('Topology exceeds the current SwitchNode/MMU port limit')
            record['port' + str(node)] = port_counts[node]
        links.append(record)
        if pair == (6, 7):
            for node, peer in ((6, 7), (7, 6)):
                wan[str(node)].append(dict(port=record['port' + str(node)],
                    peer_port=record['port' + str(peer)], rate_bps=rate, delay_ns=delay,
                    control_serial_ns=656000000000 // rate, error=str(error)))
        else:
            if pair in local:
                raise ValueError('Duplicate GPU access/NVSwitch link: ' + str(pair))
            local[pair] = record
    if set(local) != required or len(wan['6']) < 2:
        raise ValueError('Keep all eight local links and at least two direct WAN links')
    for gpu in range(4):
        nv = 4 if gpu < 2 else 5
        if local[(gpu, nv)]['port' + str(gpu)] != 1:
            raise ValueError('Keep each GPU NVSwitch link as its first device; WAN links may be reordered freely')
    return dict(schema=1, node_count=8, paths_per_peer=len(wan['6']),
                wan_by_node=wan, links=links, port_counts=port_counts)


def topology_preflight(info, config, profile, legacy_profiles, wan_profile='legacy', dci_buffer_mib=160):
    values = {}
    for line in config.splitlines():
        cells = line.split('#', 1)[0].split()
        if not cells:
            continue
        if cells[0] in values:
            raise ValueError('Duplicate config key: ' + cells[0])
        values[cells[0]] = cells[1:]
    for key in ('PACKET_PAYLOAD_SIZE', 'BUFFER_SIZE', 'KMIN_MAP', 'KMAX_MAP', 'PMAX_MAP'):
        if key not in values or not values[key]:
            raise ValueError('Missing config value: ' + key)
    payload = int(values['PACKET_PAYLOAD_SIZE'][0])
    if not 1 <= payload <= 65500:
        raise ValueError('Payload size must be in 1..65500 for this experiment')
    buffer_mib = int(values['BUFFER_SIZE'][0])
    if not 1 <= buffer_mib <= 4095:
        raise ValueError('BUFFER_SIZE must fit the current 32-bit byte counter (1..4095 MiB)')
    maps = {}
    for name in ('KMIN_MAP', 'KMAX_MAP', 'PMAX_MAP'):
        cells = values[name]
        if len(cells) != 1 + 2 * int(cells[0]):
            raise ValueError('Malformed ' + name)
        maps[name] = {int(cells[i]): Decimal(cells[i + 1]) for i in range(1, len(cells), 2)}
        if len(maps[name]) != int(cells[0]) or any(not v.is_finite() for v in maps[name].values()):
            raise ValueError('Duplicate rate in ' + name)
    wan = info['wan_by_node']
    if profile in legacy_profiles:
        expected = legacy_profiles[profile]
        if len(wan['6']) != len(expected) or any(
                p['rate_bps'] != 400000000000 or p['delay_ns'] != us * 1000
                for p, us in zip(wan['6'], expected)):
            raise ValueError('Legacy profile mismatch; use --delay-profile topology for arbitrary WAN parameters')
    if profile == 'none' and len({(p['rate_bps'], p['delay_ns']) for p in wan['6']}) > 1:
        raise ValueError('Unequal WAN links require --delay-profile topology')
    mmu = {}
    eps_rates = {link['rate_bps'] for link in info['links'] if link['src'] in (6, 7) or link['dst'] in (6, 7)}
    for rate in eps_rates:
        if any(rate not in maps[name] for name in maps):
            raise ValueError('Add rate ' + str(rate) + ' to KMIN_MAP/KMAX_MAP/PMAX_MAP in the config; no rebuild is needed')
        if not 0 <= maps['KMIN_MAP'][rate] < maps['KMAX_MAP'][rate] or not 0 <= maps['PMAX_MAP'][rate] <= 1:
            raise ValueError('Invalid ECN thresholds for rate ' + str(rate))
    for node in (6, 7):
        incident = [p for p in info['links'] if node in (p['src'], p['dst'])]
        # DCI_WAN_PROFILE_V1: WAN ingress has no PFC headroom/port reservation.
        retained = [p for p in incident if {p['src'],p['dst']} != {6,7}] if wan_profile == 'grc-ref' else incident
        gateway_buffer_mib = dci_buffer_mib if wan_profile == 'grc-ref' else buffer_mib
        headroom = sum((p['rate_bps'] * p['delay_ns'] // 8 // 1000000000) * 3 for p in retained)
        reserved = headroom + len(retained) * 4096  # current SwitchMmu::reserve
        if headroom > UINT32 or any((p['rate_bps'] * p['delay_ns'] // 8 // 1000000000) * 3 > UINT32 for p in retained):
            raise ValueError('Headroom exceeds the current 32-bit MMU model')
        if reserved >= gateway_buffer_mib * 1048576:
            minimum = reserved // 1048576 + 1
            raise ValueError(f'Gateway {node} needs buffer above {reserved} B; choose --buffer-mib >= {minimum} (minimum leaves little shared space)')
        mmu[str(node)] = dict(buffer_bytes=gateway_buffer_mib * 1048576, reserved_bytes=reserved,
                              shared_capacity_bytes=gateway_buffer_mib * 1048576 - reserved)
    expected_paths = []
    access = {}
    for link in info['links']:
        pair = tuple(sorted((link['src'], link['dst'])))
        if pair in ((0, 6), (1, 6), (2, 7), (3, 7)):
            access[pair[0]] = link
    for src in range(4):
        for dst in range(4):
            if (src < 2) == (dst < 2):
                continue
            a, b = access[src], access[dst]
            gateway = 6 if src < 2 else 7
            for p in wan[str(gateway)]:
                rtt = 2 * (a['delay_ns'] + p['delay_ns'] + b['delay_ns']) + sum(
                    payload * 8000000000 // link['rate_bps'] for link in (a, p, b))
                bdp = rtt * min(a['rate_bps'], p['rate_bps'], b['rate_bps']) // 8000000000
                if rtt > ((1 << 63) - 1) // 4:
                    raise ValueError('Candidate RTT would overflow the existing recovery-time representation')
                if bdp > UINT32:
                    raise ValueError('Candidate BDP exceeds the current 32-bit QP window model')
                expected_paths.append(dict(src=src, dst=dst, wan_port=p['port'], rate_bps=p['rate_bps'],
                    delay_ns=p['delay_ns'], rtt_bound_ns=rtt, bdp_bytes=bdp))
    info.update(mmu_by_node=mmu, expected_paths=expected_paths, payload_bytes=payload, buffer_mib=buffer_mib)
    return info


# pm_fault
"""Paced gradient-bucket inputs and evidence-based blackhole acceptance."""
from collections import Counter
import re





def paced_workload(buckets, mib, gap_us):
    if not 1 <= buckets <= 128 or not 1 <= mib <= 256 or not 1 <= gap_us <= 1000000:
        raise ValueError('Paced input limits: 1..128 buckets, 1..256 MiB, 1..1000000 us spacing')
    header = ('HYBRID_TRANSFORMER_FWD_IN_BCKWD model_parallel_NPU_group: 1 ep: 1 '
              'pp: 1 vpp: 1 ga: 1 all_gpus: 4 checkpoints: 0 checkpoint_initiates: 0')
    rows = [f'grad_bucket_{i} -1 0 NONE 0 0 NONE 0 {gap_us * 1000} ALLREDUCE {mib * 1048576} 0'
            for i in range(buckets)]
    return header + '\n' + str(buckets) + '\n' + '\n'.join(rows) + '\n'


def workload_info(text):
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if len(lines) < 3 or lines[0][0] != 'HYBRID_TRANSFORMER_FWD_IN_BCKWD':
        raise ValueError('Use the verified hybrid DP gradient-bucket workload syntax')
    header = dict(zip(lines[0][1::2], lines[0][2::2]))
    if header.get('model_parallel_NPU_group:') != '1' or header.get('all_gpus:') != '4':
        raise ValueError('This workflow evaluates four-GPU DP AllReduce')
    if int(lines[1][0]) != len(lines) - 2:
        raise ValueError('Workload row count mismatch')
    buckets = []
    for row in lines[2:]:
        if len(row) != 12 or row[3] != 'NONE' or row[6] != 'NONE' or row[9] != 'ALLREDUCE' or\
                any(int(row[i]) != 0 for i in (2,4,5,7,11)) or int(row[8]) < 0 or int(row[10]) <= 0:
            raise ValueError('Expected gradient-only AllReduce rows with nonnegative WG compute spacing')
        buckets.append(dict(name=row[0], bytes=int(row[10]), compute_ns=int(row[8])))
    if len({b['name'] for b in buckets}) != len(buckets):
        raise ValueError('Use unique bucket names')
    total = sum(b['bytes'] for b in buckets)
    if total > 32 * 1024**3:
        raise ValueError('Workload exceeds the audited persistent-QP sequence budget')
    return dict(buckets=buckets, count=len(buckets), bytes=total,
                min_compute_ns=sum(b['compute_ns'] for b in buckets))


def validate_issued(lines, info, completed=True):
    issued = []
    for line in lines:
        match = re.search(r'all-reduce weight grad collective issued for layer:\s*(\S+)\s+with size:\s*(\d+)', line)
        if match:
            issued.append((match[1], int(match[2])))
    actual = Counter(issued)
    expected = Counter((b['name'], b['bytes']) for b in info['buckets'])
    if (completed and actual != expected) or actual - expected:
        raise ValueError('Issued gradient buckets differ from workload input')
    transport = [fields(s) for s in lines if s.startswith('DCI_TRANSPORT_STATS ')]
    total_posted = sum(int(v['posted_bytes']) for v in transport if int(v['node']) in (0,1,2,3))
    if total_posted > 6 * info['bytes'] or (completed and total_posted != 6 * info['bytes']):
        raise ValueError('Completed AllReduce byte volume differs from workload input')
    return dict(workload_issued_buckets=len(issued), workload_posted_bytes=total_posted,
                workload_volume_status='COMPLETE' if completed else 'CENSORED')


def fault_metrics(lines, args, pm):
    select = lambda prefix: [fields(s) for s in lines if s.startswith(prefix + ' ')]
    configs, events, end = select('DCI_PM_FAULT_CONFIG'), select('DCI_PM_FAULT_EVENT'), select('DCI_PM_FAULT_END')
    if not args.fault:
        if configs or events or end:
            raise ValueError('Unexpected fault injection in a no-fault run')
        return dict(fault_enabled=0)
    if len(configs) != 1 or len(events) != 2 or len(end) != 1 or int(end[0]['events']) != 2:
        raise ValueError('Fault start/restore did not both occur before experiment end')
    config = configs[0]
    if any(int(config[k]) != v for k, v in dict(path_index=args.fault_path_index,
            start_us=args.fault_start_us, end_us=args.fault_end_us).items()):
        raise ValueError('Fault fixture differs from requested parameters')
    start, restore = args.fault_start_us * 1000, args.fault_end_us * 1000
    if [(v['action'], int(v['time_ns']), v['link_stays_up']) for v in events] != [
            ('blackhole', start, '1'), ('restore', restore, '1')]:
        raise ValueError('Fault event timing/link state mismatch')
    targets = {(node, args.topology_info['wan_by_node'][str(node)][args.fault_path_index]['port']) for node in (6,7)}
    declared = select('DCI_PM_FAULT_TARGET')
    if {(int(v['node']),int(v['port'])) for v in declared} != targets or len(declared) != 2:
        raise ValueError('Wrong physical fault endpoints')
    drops, traffic = select('DCI_PM_FAULT_DROPS'), select('DCI_PM_FAULT_TRAFFIC')
    if len(drops) != 2 or {(int(v['node']),int(v['port'])) for v in drops} != targets or\
            any(v['link_up'] != '1' for v in drops):
        raise ValueError('Missing drop evidence or interface did not stay UP')
    if len(traffic) != 2 * args.topology_info['paths_per_peer']:
        raise ValueError('Missing actual WAN traffic trace records')
    result = dict(fault_enabled=1, fault_path_index=args.fault_path_index,
        fault_data_drops=sum(int(v['data_packets']) for v in drops),
        fault_probe_drops=sum(int(v['probe_packets']) for v in drops),
        fault_maintenance='NOT_RUNNING' if not pm or not args.probe_enable else 'PASS')
    if not result['fault_data_drops']:
        raise ValueError('Selected link dropped no business DATA; this is not a loaded fault test')
    for window in ('before','during','after'):
        value = sum(int(v['data_' + window]) for v in traffic)
        result['fault_data_tx_' + window] = value
        if not value:
            raise ValueError('No actual business DATA transmission ' + window + ' fault')
    others = [v for v in traffic if (int(v['node']),int(v['port'])) not in targets]
    if not sum(int(v['data_during']) for v in others):
        raise ValueError('No business DATA on unaffected paths during fault')
    if not pm or not args.probe_enable:
        return result
    mapping = {(int(v['node']),int(v['path'])): int(v['interface']) for v in select('DCI_PM_CANDIDATE')}
    states = {}
    for v in select('DCI_PM_STATE'):
        key = (int(v['node']), mapping[(int(v['node']),int(v['path']))])
        states.setdefault(key, []).append((int(v['time_ns']), v['to']))
    for key, transitions in states.items():
        if not any(t < start and state == 'UP' for t,state in transitions):
            raise ValueError('Candidate was not verified UP before fault: ' + str(key))
        if key not in targets:
            if any(state == 'DOWN' for t,state in transitions if t >= start):
                raise ValueError('Unaffected candidate went DOWN: ' + str(key))
            continue
        down = [t for t,state in transitions if start <= t < restore and state == 'DOWN']
        up = [t for t,state in transitions if t >= restore and state == 'UP']
        if not down or not up:
            raise ValueError('Target candidate did not complete DOWN/UP transitions: ' + str(key))
        if any(state == 'UP' and down[0] < t < restore for t,state in transitions):
            raise ValueError('Target candidate incorrectly resurrected inside the blackhole')
        if down[0] - start > (args.timeout_us + args.interval_us) * 1000:
            raise ValueError('Detection exceeds timeout plus one tick')
        prefix = 'node' + str(key[0])
        result[prefix + '_fault_port'] = key[1]
        result[prefix + '_fault_down_ns'] = down[0]
        result[prefix + '_fault_up_ns'] = up[0]
        result[prefix + '_fault_detect_ms'] = (down[0] - start) / 1e6
        result[prefix + '_fault_recover_ms'] = (up[0] - restore) / 1e6
    final = select('DCI_PM_STATS')
    if len(states) != 2 * args.topology_info['paths_per_peer'] or any(v['state'] != 'UP' for v in final):
        raise ValueError('Not all candidates recovered by collective completion')
    return result


# irn_diagnostics
#!/usr/bin/env python3
"""Summarize one-QP diagnostic evidence, keeping unseen originals unresolved."""
import argparse
from pathlib import Path
import re





def analyze_irn(log_path):
    summaries=[]; counters=[]; first_retx=None; originals={}; arrivals={}; truncated=False
    done=None
    with Path(log_path).open(errors='replace') as source:
        for line in source:
            if line.startswith('IRN_DIAG_SUMMARY '): summaries.append(fields(line))
            elif line.startswith('IRN_DIAG_COUNTER '): counters.append(fields(line))
            elif line.startswith('IRN_DIAG_GEN '):
                f=fields(line)
                if f['retx']=='0': originals[f['seq']]=f
                elif first_retx is None: first_retx=f
            elif line.startswith('IRN_DIAG_RX '):
                f=fields(line); arrivals.setdefault(f['uid'],f)
            elif line.startswith('DCI_EXPERIMENT_END '): done=fields(line)
    if len(summaries)!=1 or summaries[0]['selected']!='1':
        raise ValueError('expected one diagnostic summary with a selected QP')
    s=summaries[0]; count=int(s['retx_generated'])
    categories=['retx_original_accepted_before_gen','retx_original_accepted_after_gen',
                'retx_original_observed_drop','retx_original_rx_window_drop','retx_original_unresolved']
    if sum(int(s[k]) for k in categories)!=count:
        raise ValueError('original-fate categories do not sum to generated retransmissions')
    if not counters:
        raise ValueError('missing selected-QP transport counter')
    c=counters[-1]
    if int(c['retrans_packets'])!=count or c['retrans_bytes']!=s['retx_bytes']:
        raise ValueError('diagnostic and transport counters disagree')
    if done and done['completed']!='1':
        print('WORKLOAD_INCOMPLETE: original fates are censored at the simulation deadline')
    print('SELECTED_QP src='+s['src']+' dst='+s['dst']+' sport='+s['sport'])
    for key in ['original_generated','original_source_tx','retx_generated','retx_bytes','retx_source_tx',
                'retx_data_already_at_rx',*categories,'retx_known_sack','retx_below_una',
                'recovery_gap','recovery_timeout','suppressed']:
        print(key+'='+s[key])
    if count:
        print('data_already_at_receiver_pct={:.3f}'.format(100*int(s['retx_data_already_at_rx'])/count))
    if int(s['retx_known_sack']) or int(s['retx_below_una']):
        print('ANOMALY: retransmission contradicted sender acknowledgement state; investigate implementation')
    if first_retx:
        original=originals.get(first_retx['seq']); rx=arrivals.get(original['uid']) if original else None
        print('FIRST_RETX seq='+first_retx['seq']+' generated_ns='+first_retx['time_ns']+
              ' original_generated_ns='+(original['time_ns'] if original else 'unlogged')+
              ' original_received_ns='+(rx['time_ns'] if rx else 'unlogged')+
              ' sender_known_sack='+first_retx['known_sack']+
              ' receiver_already_has_data='+first_retx['rx_has_data'])
    if int(s['suppressed']):
        print('TRACE_TRUNCATED: aggregate counters are complete; event timeline is a bounded prefix')
    print('NOTE: receiver state is an experimental oracle, not information available to the sender.')
    print('NOTE: an original accepted later was not lost, but its retransmission may still have accelerated delivery.')
    print('NOTE: unresolved does not mean dropped; observed drop is not a full-network loss counter.')
    return s





# ack_lag
#!/usr/bin/env python3
"""Read existing IRN diagnostic logs; measure observed confirmation lag, not a performance counterfactual."""
import argparse
from bisect import bisect_right
from collections import Counter
from pathlib import Path
import re
import statistics





def describe_ack_lag(label, values):
    print(label+'_count='+str(len(values)))
    if not values:return
    ordered=sorted(values)
    def percentile(fraction):
        position=(len(ordered)-1)*fraction;lo=int(position);hi=min(lo+1,len(ordered)-1)
        return ordered[lo]+(ordered[hi]-ordered[lo])*(position-lo)
    print(label+'_us mean={:.6f} median={:.6f} p90={:.6f} p99={:.6f} max={:.6f}'.format(
        statistics.mean(values)/1000,percentile(.5)/1000,percentile(.9)/1000,
        percentile(.99)/1000,max(values)/1000))
    for us in [0,0.1,1,2,5,10,20,50,100,500]:
        print('  observed_confirmation_within_{}_us={}/{}'.format(us,sum(v<=us*1000 for v in values),len(values)))


def analyze_ack_lag(path):
    acks=[]; ack_indices=[]; generated=[]; summaries=[]; transport=[]; counters=[]
    rank0=[];ending=[]
    with path.open(errors='replace') as source:
        for index,line in enumerate(source):
            if line.startswith('IRN_DIAG_ACK '):
                f=fields(line);acks.append((index,int(f['time_ns']),int(f['cum']),int(f['sack_seq']),int(f['sack_bytes'])))
                ack_indices.append(index)
            elif line.startswith('IRN_DIAG_GEN '):
                f=fields(line)
                if f['retx']=='1':generated.append((index,{k:int(v) for k,v in f.items()}))
            elif line.startswith('IRN_DIAG_SUMMARY '):summaries.append(fields(line))
            elif line.startswith('IRN_DIAG_COUNTER '):counters.append(fields(line))
            elif line.startswith('IRN_STATS '):transport.append(fields(line))
            elif line.startswith('DCI_EXPERIMENT_END '):ending.append(fields(line))
            match=re.search(r'all passes finished at time:\s*(\d+)',line)
            if match:rank0.append(int(match.group(1)))
    if len(summaries)!=1 or summaries[0]['selected']!='1':raise ValueError('expected one selected-QP summary')
    summary=summaries[0]
    if int(summary['suppressed']):raise ValueError('event trace is truncated; rerun with a larger --max-events')
    if len(generated)!=int(summary['retx_generated']):raise ValueError('GEN event count disagrees with summary')
    if not counters or int(counters[-1]['retrans_packets'])!=len(generated):raise ValueError('transport counter mismatch')
    print('\nRUN='+path.parent.parent.name)
    print('SELECTED_QP src={} dst={} sport={}'.format(summary['src'],summary['dst'],summary['sport']))
    if len(rank0)==1:print('rank0_ms={:.6f}'.format(rank0[0]/1e6))
    if len(ending)==1:
        print('completed='+ending[0]['completed']+' global_exit_ms={:.6f}'.format(int(ending[0]['time_ns'])/1e6))
    gpu=[r for r in transport if r.get('node') in ('0','1','2','3')]
    if len(gpu)==4 and len({r['node'] for r in gpu})==4:
        print('ALL_GPUS_LIVE_QPS retrans_bytes={} ooo_packets={} duplicate_packets={}'.format(
            sum(int(r['live_qp_retrans_bytes']) for r in gpu),sum(int(r['rx_ooo_packets']) for r in gpu),
            sum(int(r['rx_duplicate_packets']) for r in gpu)))
    copies=Counter(f['seq'] for _,f in generated)
    print('retx_attempts={} unique_retransmitted_sequences={} repeated_attempts={}'.format(
        len(generated),len(copies),len(generated)-len(copies)))
    delays=[]; pending=0; sack_evidence=0; cum_evidence=0; earlier_cum=0
    for index,g in generated:
        if not g['rx_has_data']:continue
        end=g['seq']+g['bytes'];start=bisect_right(ack_indices,index)
        if any(a[2]>=end for a in acks[:start]):earlier_cum+=1
        for pos in range(start,len(acks)):
            _,time,cum,sack,size=acks[pos]
            cum_covers=cum>=end;sack_covers=size>0 and sack<=g['seq'] and sack+size>=end
            if cum_covers or sack_covers:
                lag=time-g['time_ns']
                if lag<0:raise ValueError('nonmonotonic simulation event timestamps')
                delays.append(lag);cum_evidence+=cum_covers;sack_evidence+=(sack_covers and not cum_covers)
                break
        else:pending+=1
    describe_ack_lag('receiver_already_had_data_to_sender_confirmation',delays)
    print('first_covering_feedback cumulative={} selective_only={} not_observed={} earlier_cumulative_anomalies={}'.format(
        cum_evidence,sack_evidence,pending,earlier_cum))
    print('NOTE: observations use actual ACKs in this run, including possible feedback from retransmissions.')
    print('NOTE: these delays do not predict performance after introducing a wait or NACK threshold.')
    return dict(delays=delays,pending=pending,earlier_cum=earlier_cum)





# Historical manifests are kept byte-for-byte. Resolve their old paths on read.
import json

_OLD_TOOL_PATHS = (('experiments/studies', 'experiments/runs/_batches'), ('experiments/workflows/', 'experiments/scripts/'), ('experiments/lib/', 'experiments/scripts/'), ('experiments/patches/dci-routing-study-v1/run_study.py', 'experiments/scripts/run_routing.py'), ('experiments/patches/dcqcn-cnp-v1/run_checks.py', 'experiments/scripts/check_cnp.py'), ('experiments/patches/dcqcn-cnp-v1/run_collective.py', 'experiments/scripts/calibrate_cnp.py'), ('experiments/patches/dci-wan-profile-v1/run_acceptance.py', 'experiments/scripts/check_wan.py'), ('experiments/patches/dci-pm-fault-v1/run_acceptance.sh', 'experiments/scripts/check_fault.sh'))

def relocated_path(value):
    value = str(value)
    for old, new in _OLD_TOOL_PATHS:
        value = value.replace(old, new)
    return Path(value)

def record_json(text):
    def relocate(value):
        if isinstance(value, str):
            for old, new in _OLD_TOOL_PATHS:
                value = value.replace(old, new)
            return value
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if isinstance(value, dict):
            return {key: relocate(item) for key, item in value.items()}
        return value
    return relocate(json.loads(text))


#!/usr/bin/env python3
"""Inspect actual QP creation and four WAN interface rates for Figure 2."""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path


QP_LINE = re.compile(r"^\[RNIC QP TERMINAL\]\s+(.*)$")
CREATE_LINE = re.compile(r"^FIG2_QP_CREATE\s+(.*)$")
CONFIG_LINE = re.compile(r"^FIG2_QP_CONFIG\s+(.*)$")
COMPLETION = re.compile(r"all passes finished at time:\s*(\d+)")


def wan_fields(line):
    return dict(re.findall(r"([a-z_]+)=([^\s]+)", line))


def analyze_wan(run_dir, node, ports):
    log_file = run_dir / "logs" / "simulator.log"
    bw_file = run_dir / "outputs" / "bwconf"
    qp_counts = defaultdict(int)
    qp_ports = defaultdict(set)
    created = defaultdict(set)
    qp_config = None
    finished_at = None
    with log_file.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            match = CREATE_LINE.match(line)
            if match:
                item = wan_fields(match.group(1))
                created[int(item["src"]), int(item["dst"])].add(
                    (int(item["channel"]), int(item["index"]), int(item["sport"])))
            match = CONFIG_LINE.match(line)
            if match:
                qp_config = wan_fields(match.group(1))
            match = QP_LINE.match(line)
            if match:
                item = wan_fields(match.group(1))
                if "src" in item and "dst" in item:
                    key = (int(item["src"]), int(item["dst"]))
                    qp_counts[key] += 1
                    if "sport" in item:
                        qp_ports[key].add(item["sport"])
            match = COMPLETION.search(line)
            if match:
                finished_at = int(match.group(1))

    rates = defaultdict(lambda: {port: 0.0 for port in ports})
    with bw_file.open("r", encoding="utf-8", errors="replace", newline="") as source:
        for row in csv.DictReader(source, skipinitialspace=True):
            if int(row["node_id"]) == node and int(row["port_id"]) in ports:
                rates[int(row["time"])][int(row["port_id"])] = float(row["bandwidth"])

    active = [sample for sample in rates.values() if sum(sample.values()) > 0]
    if not active:
        raise ValueError("no active samples for the selected router and ports")

    print("run=" + run_dir.name)
    print("completion_tick=" + (str(finished_at) if finished_at is not None else "missing"))
    if qp_config:
        print("qps_per_channel=" + qp_config["qps_per_channel"])
    print("persistent_qps_created=" + str(sum(map(len, created.values()))))
    for (src, dst), qps in sorted(created.items()):
        channels = {ch for ch, _, _ in qps}
        print("qp_pair src={} dst={} created={} channels={} distinct_ports={}".format(
            src, dst, len(qps), len(channels), len({sport for _, _, sport in qps})))
    print("terminal_qps=" + str(sum(qp_counts.values())))
    for (src, dst), count in sorted(qp_counts.items()):
        print("qp_pair src={} dst={} terminal={} unique_sports={}".format(
            src, dst, count, len(qp_ports[src, dst])))

    print("router={} active_samples={}".format(node, len(active)))
    for port in ports:
        values = [sample[port] for sample in active]
        print("port={} peak_gbps={:.3f} mean_active_gbps={:.3f} nonzero_samples={}".format(
            port, max(values), sum(values) / len(values), sum(rate > 0 for rate in values)))
    totals = [sum(sample.values()) for sample in active]
    fairness = [total * total / (len(ports) * sum(rate * rate for rate in sample.values()))
                for sample, total in zip(active, totals)]
    print("aggregate_peak_gbps={:.3f} aggregate_mean_active_gbps={:.3f}".format(
        max(totals), sum(totals) / len(totals)))
    print("jain_fairness_mean={:.4f} jain_fairness_min={:.4f}".format(
        sum(fairness) / len(fairness), min(fairness)))


def wan_main():
    parser = argparse.ArgumentParser(description='Analyze WAN bandwidth and QP placement')
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--node", type=int, default=6)
    parser.add_argument("--ports", default="3,4,5,6")
    args = parser.parse_args()
    ports = tuple(int(value) for value in args.ports.split(","))
    if not ports or len(set(ports)) != len(ports):
        parser.error("--ports must contain distinct port IDs")
    analyze_wan(args.run_dir, args.node, ports)




# CURRENT_DCI_SCRIPTS_V1
_OLD_TOOL_PATHS += (
    ('experiments/scripts/run_pm_coexist.py', 'experiments/scripts/run_collective.py'),
)


# GROUPED_DCI_SCRIPTS_V1
_OLD_TOOL_PATHS += (('experiments/scripts/run_collective.py', 'experiments/scripts/runners/run_collective.py'), ('experiments/scripts/run_routing.py', 'experiments/scripts/runners/run_routing.py'), ('experiments/scripts/dci.py', 'experiments/scripts/runners/dci.py'), ('experiments/scripts/dci_common.py', 'experiments/scripts/tools/dci_common.py'), ('experiments/scripts/replay_irn_diag.py', 'experiments/scripts/tools/replay_irn_diag.py'), ('experiments/scripts/check_tools.py', 'experiments/scripts/tools/check_tools.py'))
