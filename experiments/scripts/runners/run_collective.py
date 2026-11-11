#!/usr/bin/env python3
"""Fresh, paired C4Q4 AllReduce experiments with gateway probes off/on."""

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
import re
import shutil
import signal
import subprocess
import sys
from dci_common import record_json, relocated_path

ROOT = Path(__file__).resolve().parents[3]
from dci_common import measure
from dci_common import cnp_metrics as cnp_metrics  # DCQCN_CNP_V1
from dci_common import wan_metrics as wan_metrics  # DCI_WAN_PROFILE_V1
from dci_common import describe_topology as describe_topology, topology_preflight as topology_preflight
from dci_common import paced_workload, workload_info, validate_issued, fault_metrics

DELAY_PROFILES_US = {
    'equal': (245, 245),
    'asymmetric': (245, 745),
    'four-delay': (245, 745, 1245, 1745),
}


def fields(line):
    return dict(re.findall(r'([A-Za-z0-9_]+)=([^\s]+)', line))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def build_identity(simulator):
    result = subprocess.run(['ldd', str(simulator)], text=True, capture_output=True, check=True)
    if 'not found' in result.stdout:
        raise ValueError('Unresolved simulator shared library: ' + result.stdout)
    libraries = [Path(p) for p in re.findall(r'=>\s+(/\S+)', result.stdout)
                 if 'libns3' in Path(p).name]
    if not libraries:
        raise ValueError('Expected a dynamically linked ns-3 simulator')
    return {str(p.resolve()): sha(p) for p in [simulator, *libraries]}


def values(text, allowed=None):
    result = [int(s) for s in text.split(',')]
    if len(result) != len(set(result)) or not result or any(
            v < 0 or v > 4294967295 or (allowed is not None and v not in allowed) for v in result):
        raise argparse.ArgumentTypeError('Use distinct allowed comma-separated integers')
    return result


def config_text(template, monitor_us=10, buffer_mib=None, routing='ecmp'):
    text = template.read_text()
    # DCI_ROUTING_STUDY_V1: render the requested DATA policy explicitly.
    if routing not in ('ecmp','spray_rr','spray_random'):
        raise ValueError('Unsupported routing policy')
    overrides = [('DCI_ROUTING_MODE', routing), ('DCI_ROUTING_NODES', '6,7'),
                 ('QLEN_MON_INTERVAL', str(monitor_us)), ('BW_MON_INTERVAL', str(monitor_us))]
    if buffer_mib is not None:
        overrides.append(('BUFFER_SIZE', str(buffer_mib)))
    for key, value in overrides:
        pattern = r'(?m)^' + key + r'[^\S\n]+[^\n]*$'
        text, count = re.subn(pattern, key + ' ' + value, text)
        if count > 1:
            raise ValueError('Duplicate config key ' + key)
        if not count:
            text += '\n' + key + ' ' + value + '\n'
    for key, value in [('CC_MODE', '1'), ('ENABLE_QCN', '1'), ('RDMA_TRANSPORT_MODE', '0'), ('RC_ACK_RETRY_ENABLE', '0')]:
        found = re.findall(r'(?m)^' + key + r'\s+(\S+)', text)
        if found != [value]:
            raise ValueError('C4Q4 IRN experiment requires ' + key + '=' + value)
    return text


def queue_metrics(folder, ports):
    """Discover the actual monitor by its header, including renamed qlenconf."""
    required = ['time', 'sw_id', 'port_id', 'q_id', 'q_len', 'port_len']
    candidates = []
    for path in sorted((folder / 'outputs').iterdir()):
        if not path.is_file():
            continue
        try:
            with path.open() as stream:
                header = [s.strip() for s in next(csv.reader([stream.readline(4096)]), [])]
        except (UnicodeError, OSError):
            continue
        if header == required:
            candidates.append(path)
    result = dict(wan_mmu_sampled_peak_bytes='', wan_mmu_matched_rows=0,
                  wan_mmu_queue_status='MISSING', wan_mmu_queue_file='')
    if len(candidates) > 1:
        result['wan_mmu_queue_status'] = 'AMBIGUOUS'
        return result
    if not candidates:
        return result
    path = candidates[0]
    result.update(wan_mmu_queue_file=path.name, wan_mmu_queue_status='NO_WAN_ROWS')
    peak, count = 0, 0
    with path.open() as stream:
        reader = csv.reader(stream)
        next(reader)
        for cells in reader:
            if not cells:
                continue
            if len(cells) != 6:
                raise ValueError('Malformed queue row: ' + str(path))
            time, node, port, queue, qlen, total = map(int, cells)
            if (node, port) in ports:
                peak = max(peak, total)
                count += 1
    if count:
        result.update(wan_mmu_sampled_peak_bytes=peak, wan_mmu_matched_rows=count,
                      wan_mmu_queue_status='OK')
    return result


def repair_queue_stats(study):
    """Correct only derived queue columns; preserve raw runs and primary metrics."""
    path = study / 'summary.csv'
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError('Empty summary: ' + str(path))
    for row in rows:
        folder = ROOT / 'experiments/runs' / row['run']
        ports = set()
        for line in (folder / 'logs/simulator.log').read_text().splitlines():
            if line.startswith('DCI_ROUTING_CONFIG '):
                f = fields(line)
                if int(f['node']) in (6, 7) and int(f['candidate_count']) > 1:
                    ports.update((int(f['node']), int(p)) for p in f['ports'].split(','))
        if not ports:
            raise ValueError('No logged WAN candidate interfaces: ' + row['run'])
        row.update(queue_metrics(folder, ports))
    backup = path.with_name('summary.before-queue-fix.csv')
    if not backup.exists():
        shutil.copy2(path, backup)
    write_csv(path, rows)
    print('ecmp_seed\tpm\tqueue_file\tstatus\tmatched_rows\tpeak_bytes\tpeak_MiB')
    for row in rows:
        peak = row['wan_mmu_sampled_peak_bytes']
        print('\t'.join(map(str, [row['ecmp_seed'], row['pm'], row['wan_mmu_queue_file'],
            row['wan_mmu_queue_status'], row['wan_mmu_matched_rows'], peak if peak != '' else 'NA',
            f'{peak / 1048576:.3f}' if peak != '' else 'NA'])))
    print('SUMMARY ' + str(path))
    return int(any(row['wan_mmu_queue_status'] != 'OK' for row in rows))


def pm_metrics(folder, enabled, hash_seed, args):
    lines = (folder / 'logs/simulator.log').read_text(errors='replace').splitlines()
    select = lambda prefix: [fields(s) for s in lines if s.startswith(prefix + ' ')]
    recovery = select('IRN_TIMEOUT_RECOVERY_CONFIG')
    gpu_recovery = [v for v in recovery if int(v['node']) in (0,1,2,3)]
    if len(gpu_recovery) != 4 or {int(v['node']) for v in gpu_recovery} != {0,1,2,3} or any(
            int(v['replay']) != args.irn_timeout_replay for v in recovery):
        raise ValueError('IRN timeout policy missing or mismatched; rebuild the active simulator/library')
    fifo = select('COLLECTIVE_FIFO_CONFIG')
    if len(fifo) != 1 or int(fifo[0]['enabled']) != args.collective_fifo:
        raise ValueError('Collective FIFO setting missing or mismatched; rebuild the active applications library')
    configs = select('DCI_PM_COEXIST_CONFIG')
    end = select('DCI_PM_COEXIST_END')
    if len(configs) != 1 or len(end) != 1 or int(end[0]['enabled']) != enabled:
        raise ValueError('Missing/duplicate PM markers or wrong enable setting')
    business_end = select('DCI_EXPERIMENT_END')
    if len(business_end) != 1 or end[0]['time_ns'] != business_end[0]['time_ns']:
        raise ValueError('PM summary time differs from collective completion time')
    c = configs[0]
    if int(c.get('probe_enable', '1')) != args.probe_enable:
        raise ValueError('PM probe-enable mismatch')
    if any(int(c[k]) != v for k, v in dict(enabled=enabled, interval_us=args.interval_us,
            timeout_us=args.timeout_us, service_us=args.service_us, observe_us=args.observe_us).items()):
        raise ValueError('PM logged settings differ from requested settings')
    wan = [v for v in select('DCI_ROUTING_CONFIG')
           if int(v['node']) in (6, 7) and int(v['candidate_count']) > 1]
    delay_profile = getattr(args, 'delay_profile', 'none')
    topology = args.topology_info
    paths_per_peer = topology['paths_per_peer']
    if {int(v['node']) for v in wan} != {6, 7} or any(
            v['mode'] != args.routing or int(v['candidate_count']) != paths_per_peer or
            {int(p) for p in v['ports'].split(',')} !=
                {p['port'] for p in topology['wan_by_node'][v['node']]} or
            int(v.get('ecmp_seed', '0')) != hash_seed for v in wan):
        raise ValueError('WAN ECMP candidate count/hash setting mismatch')
    if delay_profile != 'none':
        declared = select('DCI_PM_DELAY_CONFIG')
        bound_paths = select('DCI_PM_DELAY_PATH')
        if len(declared) != 1 or declared[0]['profile'] != delay_profile or len(bound_paths) != 8 * paths_per_peer:
            raise ValueError('Missing or wrong delay-profile initialization')
        path_keys = ('src', 'dst', 'wan_port', 'rate_bps', 'delay_ns', 'rtt_bound_ns', 'bdp_bytes')
        actual = sorted(tuple(int(p[k]) for k in path_keys) for p in bound_paths)
        expected = sorted(tuple(p[k] for k in path_keys) for p in topology['expected_paths'])
        if actual != expected:
            raise ValueError('Logged candidate attributes/transport bounds differ from input topology')
        links = select('DCI_PM_TOPO_LINK')
        link_keys = ('node', 'port', 'rate_bps', 'delay_ns', 'control_serial_ns')
        expected_links = sorted((int(node), p['port'], p['rate_bps'], p['delay_ns'], p['control_serial_ns'])
            for node, paths in topology['wan_by_node'].items() for p in paths)
        if sorted(tuple(int(p[k]) for k in link_keys) for p in links) != expected_links:
            raise ValueError('Runtime candidate links differ from input topology')
        irn_config = select('IRN_CONFIG')
        bound = declared[0]
        if int(bound.get('path_count', 0)) != paths_per_peer or \
                int(bound['max_rtt_ns']) < max(p['rtt_bound_ns'] for p in topology['expected_paths']) or \
                int(bound['max_bdp_bytes']) < max(p['bdp_bytes'] for p in topology['expected_paths']):
            raise ValueError('Global initialization bounds do not cover WAN candidates')
        if len(irn_config) != 1 or int(irn_config[0]['bdp_bytes']) != int(bound['max_bdp_bytes']) or \
                int(irn_config[0]['rto_low_ns']) != 2 * int(bound['max_rtt_ns']) or \
                int(irn_config[0]['rto_high_ns']) != 4 * int(bound['max_rtt_ns']):
            raise ValueError('IRN initialization does not use the profile RTT/BDP bounds')
        mmu = select('DCI_PM_DELAY_MMU')
        if len(mmu) != 2 or {int(v['node']) for v in mmu} != {6, 7} or any(
                int(v['buffer_bytes']) <= int(v['reserved_bytes']) for v in mmu):
            raise ValueError('Missing or invalid gateway MMU capacity accounting')
        requested_buffer = args.dci_buffer_mib if args.wan_profile == 'grc-ref' else getattr(args, 'buffer_mib', None)
        if requested_buffer is not None and any(int(v['buffer_bytes']) != requested_buffer * 1048576 for v in mmu):
            raise ValueError('Gateway buffer differs from requested experiment capacity')
        if any(int(v['buffer_bytes']) != topology['mmu_by_node'][v['node']]['buffer_bytes'] or
               int(v['reserved_bytes']) != topology['mmu_by_node'][v['node']]['reserved_bytes'] for v in mmu):
            raise ValueError('Runtime MMU accounting differs from topology/config preflight')
    qp = select('FIG2_QP_CONFIG')
    created = select('FIG2_QP_CREATE')
    signature = sorted(tuple(v[k] for k in ('src', 'dst', 'channel', 'index', 'sport')) for v in created)
    if len(qp) != 1 or int(qp[0]['qps_per_channel']) != 4 or len(signature) != 64 or \
            len(set(signature)) != 64 or {int(v['channel']) for v in created} != {0, 1, 2, 3}:
        raise ValueError('Expected audited C4Q4: four channels and 64 persistent QPs')
    completed = business_end[0]['completed'] == '1'
    volume = validate_issued(lines, args.workload_info, completed)
    if completed and int(business_end[0]['time_ns']) < args.workload_info['min_compute_ns']:
        raise ValueError('Completed before the modeled WG compute spacing elapsed')
    stats = select('DCI_PM_STATS')
    resources = select('DCI_PM_RESOURCE')
    if enabled:
        if len(stats) != 2 * paths_per_peer or len(resources) != 2 or int(end[0]['apps']) != 2 or \
                int(end[0]['candidates']) != 2 * paths_per_peer:
            raise ValueError('Wrong gateway app or directed PM candidate count')
        for node in (6, 7):
            local = [v for v in stats if int(v['node']) == node]
            expected = {v for w in wan if int(w['node']) == node for v in w['ports'].split(',')}
            if len(local) != paths_per_peer or {v['interface'] for v in local} != expected:
                raise ValueError('PM interfaces differ from business WAN candidates')
        if sum(int(v['tx_bytes']) for v in stats) != int(end[0]['tx_bytes']):
            raise ValueError('PM TX accounting mismatch')
        if any(int(v['peak_outstanding']) > int(v['paths']) for v in resources):
            raise ValueError('More than one outstanding probe per candidate')
        if args.probe_enable == 0 and (int(end[0]['tx_packets']) or int(end[0]['rx_packets']) or
                any(int(v['samples']) or int(v['timeouts']) for v in stats)):
            raise ValueError('Passive two-gateway control emitted or received probe traffic')
    elif stats or resources or int(end[0]['tx_bytes']) or int(end[0]['apps']):
        raise ValueError('Disabled PM still emitted service activity')
    result = dict(pm=enabled, pm_probe_enable=args.probe_enable, ecmp_seed=hash_seed,
                  delay_profile=delay_profile, paths_per_peer=paths_per_peer, total_qps=len(signature),
                  pm_candidates=int(end[0]['candidates']), pm_usable=int(end[0]['usable']),
                  pm_tx_packets=int(end[0]['tx_packets']), pm_tx_bytes=int(end[0]['tx_bytes']),
                  pm_rx_packets=int(end[0]['rx_packets']), pm_timeouts=int(end[0]['timeouts']))
    result['pm_latest_rtt_max_us'] = max([int(v['control_rtt_ns']) / 1000
        for v in stats if v['has_rtt'] == '1'], default=0)
    observed = [v for v in select('DCI_PM_OBSERVE') if v['has_rtt'] == '1']
    result['pm_snapshot_rtt_max_us'] = max([int(v['control_rtt_ns']) / 1000 for v in observed], default=0)
    result['pm_peak_outstanding_sum'] = sum(int(v['peak_outstanding']) for v in resources)
    result['pm_raw_snapshot_bytes'] = sum(int(v['snapshot_bytes']) for v in resources)
    result['pm_enqueue_drops'] = sum(int(v['enqueue_drops']) for v in stats)
    result['pm_stale_replies'] = sum(int(v['stale_replies']) for v in stats)
    for path in stats:
        prefix = 'node' + path['node'] + '_port' + path['interface']
        result[prefix + '_pm_state'] = path['state']
        result[prefix + '_pm_samples'] = int(path['samples'])
        result[prefix + '_pm_timeouts'] = int(path['timeouts'])
        result[prefix + '_pm_rtt_us'] = int(path['control_rtt_ns']) / 1000 if path['has_rtt'] == '1' else ''
        if delay_profile != 'none':
            port = int(path['interface'])
            link = next(p for p in topology['wan_by_node'][path['node']] if p['port'] == port)
            result[prefix + '_pm_expected_idle_rtt_us'] = (
                2 * link['delay_ns'] + 2 * link['control_serial_ns']) / 1000 + 2 * args.service_us
            result[prefix + '_rate_bps'] = link['rate_bps']
            result[prefix + '_propagation_ns'] = link['delay_ns']
    if delay_profile != 'none':
        for record in select('DCI_PM_DELAY_MMU'):
            prefix = 'node' + record['node']
            result[prefix + '_buffer_bytes'] = int(record['buffer_bytes'])
            result[prefix + '_mmu_reserved_bytes'] = int(record['reserved_bytes'])
            result[prefix + '_mmu_shared_capacity_bytes'] = int(record['buffer_bytes']) - int(record['reserved_bytes'])
    # Existing monitor is MMU accounting, not a full q0/control-buffer measurement.
    ports = {(int(v['node']), int(p)) for v in wan for p in v['ports'].split(',')}
    result.update(queue_metrics(folder, ports))
    result.update(fault_metrics(lines, args, enabled))
    result.update(volume)
    result['irn_timeout_replay'] = args.irn_timeout_replay
    result['collective_fifo'] = args.collective_fifo
    result.update(cnp_metrics(lines, args.cnp_mode, args.cnp_interval_us))
    result.update(wan_metrics(lines, args, topology))
    gpu_irn = [v for v in select('IRN_STATS') if int(v['node']) in (0,1,2,3)]
    result['irn_timeout_sweeps'] = sum(int(v.get('timeout_sweeps',0)) for v in gpu_irn)
    result['irn_timeout_sweep_packets'] = sum(int(v.get('timeout_sweep_retrans_packets',0)) for v in gpu_irn)
    if args.irn_timeout_replay == 0 and (result['irn_timeout_sweeps'] or result['irn_timeout_sweep_packets']):
        raise ValueError('Legacy recovery unexpectedly performed timeout replay')
    result['workload_buckets'] = args.workload_info['count']
    result['workload_gradient_bytes'] = args.workload_info['bytes']
    return result, signature


def write_csv(path, rows):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repair-queue-stats', type=Path,
                        help='Recompute queue columns of an existing study without simulations')
    parser.add_argument('--simulator', default='bin/SimAI_simulator')
    parser.add_argument('--workload', default='experiments/workloads/nccl-channel-buffer/micro-dp-allreduce4-256mib.txt')
    parser.add_argument('--topology', default='experiments/topologies/dci-routing/4gpu-2site-2x400g-50km.txt')
    parser.add_argument('--config-template', default='experiments/configs/dci-routing/monitor-10us.conf')
    parser.add_argument('--group')
    parser.add_argument('--delay-profile', choices=('topology', 'none', *DELAY_PROFILES_US), default='topology',
                        help='Default topology derives WAN attributes at runtime; old profile names are compatibility checks')
    parser.add_argument('--buffer-mib', type=int,
                        help='Explicit simulated buffer capacity; four-delay defaults to 1024 MiB')
    # DCI_ROUTING_STUDY_V1: retain historical ECMP/RNG defaults.
    parser.add_argument('--routing', choices=('ecmp','spray_rr','spray_random'), default='ecmp')
    parser.add_argument('--rng-run', type=int, default=1)
    parser.add_argument('--hash-seeds', type=values, default=[0, 15])
    parser.add_argument('--pm', type=lambda s: values(s, (0, 1)), default=[0, 1])
    parser.add_argument('--probe-enable', type=int, choices=(0, 1), default=1,
                        help='Diagnostic: zero installs a passive PM service without originating probes')
    parser.add_argument('--interval-us', type=int, default=1000)
    parser.add_argument('--timeout-us', type=int, default=10000)
    parser.add_argument('--service-us', type=int, default=5)
    parser.add_argument('--observe-us', type=int, default=1000)
    parser.add_argument('--monitor-us', type=int, default=10,
                        help='Effective queue/bandwidth monitor interval; overrides template values')
    parser.add_argument('--wall-seconds', type=int, default=600)
    parser.add_argument('--budget-ms', type=int, default=1000)
    parser.add_argument('--irn-timeout-replay', type=int, choices=(0,1), default=0,
                        help='Opt-in bounded replay of unsacked sent data after IRN timeout; default keeps v11')
    parser.add_argument('--collective-fifo', type=int, choices=(0,1), default=0,
                        help='Honor explicit FIFO application collective priorities; default keeps old scheduling')
    # DCQCN_CNP_V1: isolated from IRN and path-maintenance options.
    parser.add_argument('--cnp-mode', type=int, choices=(0,1), default=0,
                        help='0: legacy ACK feedback; 1: independent CE-only CNP')
    parser.add_argument('--cnp-interval-us', type=int, default=50)
    parser.add_argument('--cnp-trace', type=int, choices=(0,1), default=0)
    # DCI_WAN_PROFILE_V1: gateway and WAN only; local fabric template unchanged.
    parser.add_argument('--wan-profile', choices=('legacy','grc-ref'), default='legacy')
    parser.add_argument('--dci-buffer-mib', type=int, default=160)
    parser.add_argument('--wan-ecn-kmin-kb', type=int, default=1000)
    parser.add_argument('--wan-ecn-kmax-kb', type=int, default=20000)
    parser.add_argument('--wan-ecn-pmax', type=float, default=0.15)
    parser.add_argument('--paced-buckets', type=int, default=0,
                        help='Generate a gradient-bucket maintenance workload; zero uses --workload')
    parser.add_argument('--bucket-mib', type=int, default=64)
    parser.add_argument('--bucket-gap-us', type=int, default=20000)
    parser.add_argument('--fault', action='store_true')
    parser.add_argument('--fault-path-index', type=int, default=0,
                        help='Zero-based physical WAN link index in topology order')
    parser.add_argument('--fault-start-us', type=int, default=50000)
    parser.add_argument('--fault-end-us', type=int, default=100000)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.rng_run <= 2147483647:
        parser.error('rng-run must be 1..2147483647')
    if not 1 <= args.cnp_interval_us <= 1000000:
        parser.error('cnp-interval-us must be 1..1000000')
    if not 1 <= args.dci_buffer_mib <= 4095 or not (
            0 <= args.wan_ecn_kmin_kb < args.wan_ecn_kmax_kb <= 4294967) or not (
            args.wan_ecn_kmax_kb * 1000 < args.dci_buffer_mib * 1048576) or not (
            0 <= args.wan_ecn_pmax <= 1):
        parser.error('Invalid DCI buffer/WAN ECN thresholds')
    if args.wan_profile == 'grc-ref' and (args.delay_profile == 'none' or args.cnp_mode != 1):
        parser.error('grc-ref requires topology initialization and --cnp-mode 1')
    if args.repair_queue_stats is not None:
        return repair_queue_stats(relocated_path((ROOT / args.repair_queue_stats).resolve()))
    if not 1 <= args.budget_ms <= 10000:
        parser.error('budget-ms must be 1..10000')
    if args.buffer_mib is None and args.delay_profile == 'four-delay':
        args.buffer_mib = 1024
    if args.buffer_mib is not None and not 1 <= args.buffer_mib <= 4095:
        parser.error('buffer-mib must fit the existing 32-bit buffer byte counter (1..4095 MiB)')
    if not 1 <= args.interval_us <= 10000000 or not 1 <= args.timeout_us <= 10000000 or \
            not 0 <= args.service_us <= 1000000 or not 0 <= args.observe_us <= 10000000 or \
            not 1 <= args.monitor_us <= 10000000 or args.wall_seconds < 1:
        parser.error('Invalid PM timers or wall-clock limit')
    paths = {k: (ROOT / getattr(args, k)).resolve() for k in ('simulator', 'workload', 'topology', 'config_template')}
    for path in paths.values():
        if not path.is_file():
            raise ValueError('Missing input: ' + str(path))
    group = args.group or 'allreduce-c4q4-pm-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    if not re.fullmatch('[A-Za-z0-9_-]+', group):
        parser.error('Invalid group name')
    prepared_config = config_text(paths['config_template'], args.monitor_us, args.buffer_mib, args.routing)
    args.topology_info = topology_preflight(describe_topology(paths['topology']), prepared_config,
                                            args.delay_profile, DELAY_PROFILES_US,
                                            wan_profile=args.wan_profile, dci_buffer_mib=args.dci_buffer_mib)
    generated = paced_workload(args.paced_buckets, args.bucket_mib, args.bucket_gap_us) if args.paced_buckets else None
    args.workload_info = workload_info(generated if generated is not None else paths['workload'].read_text())
    if args.fault:
        if not 0 <= args.fault_path_index < args.topology_info['paths_per_peer'] or \
                not 0 < args.fault_start_us < args.fault_end_us < args.budget_ms * 1000:
            parser.error('Invalid fault candidate or time window')
        if args.workload_info['min_compute_ns'] <= (args.fault_end_us + args.timeout_us + args.interval_us) * 1000:
            parser.error('Use paced gradient production beyond recovery, e.g. --paced-buckets 12 --bucket-gap-us 20000')
        print(f'FAULT path_index={args.fault_path_index} blackhole_us={args.fault_start_us} restore_us={args.fault_end_us}')
    print(f"WORKLOAD buckets={args.workload_info['count']} gradient_mib={args.workload_info['bytes']/1048576:g} "
          f"minimum_compute_ms={args.workload_info['min_compute_ns']/1e6:g}")
    print(f"TOPOLOGY paths={args.topology_info['paths_per_peer']} buffer_mib={args.topology_info['buffer_mib']}", flush=True)
    print(f"WAN_PROFILE profile={args.wan_profile} gateway_buffer_mib={args.topology_info['mmu_by_node']['6']['buffer_bytes']/1048576:g} cnp_mode={args.cnp_mode}", flush=True)
    for node, links in args.topology_info['wan_by_node'].items():
        for link in links:
            expected_us = (2 * link['delay_ns'] + 2 * link['control_serial_ns']) / 1000 + 2 * args.service_us
            print(f"CANDIDATE node={node} port={link['port']} rate_gbps={link['rate_bps']/1e9:g} "
                  f"oneway_us={link['delay_ns']/1000:g} expected_idle_rtt_us={expected_us:.3f}", flush=True)
    for seed in args.hash_seeds:
        for pm in args.pm:
            print(f'PLAN C4Q4 routing={args.routing} rng_run={args.rng_run} hash={seed} IRN=1 timeout_replay={args.irn_timeout_replay} collective_fifo={args.collective_fifo} PM={pm} probe_enable={args.probe_enable} monitor_us={args.monitor_us} '
                  f'delay_profile={args.delay_profile} paths={args.topology_info["paths_per_peer"]} '
                  f'buffer_mib={args.buffer_mib if args.buffer_mib is not None else "template"}', flush=True)
    if args.dry_run:
        return 0
    study = ROOT / 'experiments/runs/_batches' / group
    study.mkdir(parents=True, exist_ok=False)
    prepared = study / 'prepared'
    prepared.mkdir()
    if generated is not None:
        paths['workload'] = prepared / 'paced-workload.txt'
        paths['workload'].write_text(generated)
    args.effective_workload = str(paths['workload'])
    config = prepared / 'coexist.conf'
    config.write_text(prepared_config)
    builds = build_identity(paths['simulator'])
    state = dict(version=7, options=vars(args), build_sha256=builds,
                 input_sha256={k: sha(p) for k, p in paths.items() if k != 'simulator'},
                 rendered_config_sha256=sha(config), cases=[])
    rows, signatures, failed = [], {}, False
    for seed in args.hash_seeds:
        for pm in args.pm:
            name = f'dci-{group}-hash{seed}-pm{pm}'
            folder = ROOT / 'experiments/runs' / name
            env = {k: v for k, v in os.environ.items() if not k.startswith(
                ('SIMAI_', 'TSRDMA_TEST_', 'AS_'))}
            settings = dict(SIMAI_IRN_ENABLE='1', SIMAI_IRN_TIMEOUT_REPLAY=str(args.irn_timeout_replay),
                SIMAI_COLLECTIVE_FIFO=str(args.collective_fifo), SIMAI_NCCL_CHANNELS='4',
                SIMAI_CNP_MODE=str(args.cnp_mode), SIMAI_CNP_INTERVAL_US=str(args.cnp_interval_us),
                SIMAI_CNP_TRACE=str(args.cnp_trace),
                SIMAI_WAN_PROFILE=str(int(args.wan_profile == 'grc-ref')),
                SIMAI_DCI_BUFFER_MIB=str(args.dci_buffer_mib),
                SIMAI_WAN_ECN_KMIN_KB=str(args.wan_ecn_kmin_kb),
                SIMAI_WAN_ECN_KMAX_KB=str(args.wan_ecn_kmax_kb),
                SIMAI_WAN_ECN_PMAX=str(args.wan_ecn_pmax),
                SIMAI_FIG2_QPS_PER_CHANNEL='4', SIMAI_NCCL_NET_MONITOR='1',
                AS_PXN_ENABLE='0', AS_NVLS_ENABLE='0', AS_SEND_LAT='3',
                SIMAI_DCI_EXPERIMENT_MS=str(args.budget_ms), SIMAI_DCI_ECMP_SEED=str(seed),
                NS_GLOBAL_VALUE=f'RngSeed=1;RngRun={args.rng_run}', SIMAI_PM_ENABLE=str(pm),
                SIMAI_PM_GATEWAYS='6@10.255.0.6,7@10.255.0.7',
                SIMAI_PM_PROBE_ENABLE=str(args.probe_enable),
                SIMAI_PM_INTERVAL_US=str(args.interval_us), SIMAI_PM_TIMEOUT_US=str(args.timeout_us),
                SIMAI_PM_SERVICE_US=str(args.service_us), SIMAI_PM_OBSERVE_US=str(args.observe_us))
            if args.fault:
                settings.update(SIMAI_PM_FAULT_ENABLE='1', SIMAI_PM_FAULT_PATH_INDEX=str(args.fault_path_index),
                    SIMAI_PM_FAULT_START_US=str(args.fault_start_us), SIMAI_PM_FAULT_END_US=str(args.fault_end_us))
            if args.delay_profile != 'none':
                settings['SIMAI_PM_DELAY_PROFILE'] = args.delay_profile
            env.update(settings)
            command = [sys.executable, str(ROOT / 'experiments/scripts/run.py'), name,
                '--workload', str(paths['workload']), '--topology', str(paths['topology']),
                '--config-template', str(config), '--simulator', str(paths['simulator']),
                '--threads', '1', '--send-lat', '3', '--nvls-enable', '0']
            entry = dict(run=name, pm=pm, ecmp_seed=seed, environment=settings, command=command)
            state['cases'].append(entry)
            (study / 'study.json').write_text(json.dumps(state, indent=2) + '\n')
            progress = prepared / (name + '-runner.log')
            print('RUN ' + name + '\nprogress=' + str(progress), flush=True)
            with progress.open('w') as output:
                proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output,
                                        stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = proc.wait(timeout=args.wall_seconds)
                except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    code = 124
            entry['exit_code'] = code
            if (folder / 'inputs').is_dir():
                (folder / 'inputs/pm-case.json').write_text(json.dumps(dict(entry, build_sha256=builds), indent=2) + '\n')
            measure_args = [folder, args.routing, 1, args.rng_run, code, args.budget_ms, 1, (0, 1, 2, 3), (6, 7)]
            # The migrated VM adds the ECMP seed as the tenth positional option.
            if len(inspect.signature(measure).parameters) >= 10:
                measure_args.append(seed)
            row = measure(*measure_args)
            row.update(pm=pm, ecmp_seed=seed)
            try:
                if row['measurement'] != 'OK':
                    raise ValueError(row.get('problem', row['outcome']))
                if row['outcome'] != 'COMPLETE':
                    # Still report maintenance evidence; never fabricate a completion time.
                    failed = True
                    row['problem'] = 'Application censored at simulation budget; FCT unavailable'
                metrics, signature = pm_metrics(folder, pm, seed, args)
                if seed in signatures and signatures[seed] != signature:
                    raise ValueError('Business QP allocation differs between PM off/on')
                signatures[seed] = signature
                row.update(metrics)
                row['pm_mean_aggregate_tx_mbps'] = row['pm_tx_bytes'] * 8 / (row['observed_ms'] * 1000)
                if args.fault:
                    print(f"FAULT_CHECK hash={seed} pm={pm} maintenance={row['fault_maintenance']} "
                          f"data_drops={row['fault_data_drops']} probe_drops={row['fault_probe_drops']}", flush=True)
                    for node in (6,7):
                        prefix = f'node{node}'
                        if prefix + '_fault_detect_ms' in row:
                            print(f"FAULT_PATH node={node} port={row[prefix + '_fault_port']} "
                                  f"detect_ms={row[prefix + '_fault_detect_ms']} "
                                  f"recover_ms={row[prefix + '_fault_recover_ms']}", flush=True)
                for node in (6, 7):
                    for port in sorted(p['port'] for p in args.topology_info['wan_by_node'][str(node)]):
                        prefix = f'node{node}_port{port}'
                        if prefix + '_pm_state' in row:
                            print(f"PM_PATH hash={seed} node={node} port={port} "
                                  f"state={row[prefix + '_pm_state']} samples={row[prefix + '_pm_samples']} "
                                  f"rtt_us={row[prefix + '_pm_rtt_us']} "
                                  f"timeouts={row[prefix + '_pm_timeouts']}", flush=True)
            except (OSError, KeyError, ValueError) as exc:
                row.update(measurement='INVALID', problem=str(exc))
                failed = True
            rows.append(row)
            write_csv(study / 'summary.csv', rows)
            (study / 'study.json').write_text(json.dumps(state, indent=2) + '\n')
            print(f"RESULT hash={seed} pm={pm} measurement={row['measurement']} "
                  f"timeout_replay={args.irn_timeout_replay} "
                  f"collective_fifo={args.collective_fifo} "
                  f"application={row['outcome']} "
                  f"global_ms={row.get('global_exit_ms', 'NA')} pm_tx_bytes={row.get('pm_tx_bytes', 'NA')} "
                  f"problem={row.get('problem', '-')}", flush=True)
    if build_identity(paths['simulator']) != builds:
        state['build_consistent'] = False
        (study / 'study.json').write_text(json.dumps(state, indent=2) + '\n')
        for row in rows:
            row.update(measurement='INVALID', problem='Build changed during batch')
        write_csv(study / 'summary.csv', rows)
        raise ValueError('Simulator or shared libraries changed during the batch; invalidate this batch')
    state['build_consistent'] = True
    (study / 'study.json').write_text(json.dumps(state, indent=2) + '\n')
    paired = []
    for seed in args.hash_seeds:
        pair = {r['pm']: r for r in rows if r['ecmp_seed'] == seed and r['measurement'] == 'OK'
                and r['outcome'] == 'COMPLETE'}
        if set(pair) != {0, 1}:
            continue
        off, on = (pair[i]['global_exit_ms'] for i in (0, 1))
        item = dict(ecmp_seed=seed, off_ms=off, on_ms=on, delta_us=(on-off)*1000,
                    delta_percent=(on/off-1)*100)
        if args.probe_enable == 0:
            item['passive_equal'] = int(on == off)
            if on != off:
                failed = True
            print(('PASSIVE_CONTROL_OK' if on == off else 'PASSIVE_CONTROL_MISMATCH') +
                  f' hash={seed} delta_ns={(on-off)*1000000:.0f}')
        paired.append(item)
        print(f'PAIR hash={seed} off_ms={off:.6f} on_ms={on:.6f} '
              f"delta_us={item['delta_us']:.3f} delta_percent={item['delta_percent']:.4f}")
    write_csv(study / 'paired.csv', paired)
    print('SUMMARY ' + str(study / 'summary.csv'))
    print('PAIRED ' + str(study / 'paired.csv'))
    return int(failed)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
