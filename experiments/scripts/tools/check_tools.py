"""Workflow tests use synthetic logs/processes; they never start NS-3."""

import sys
from pathlib import Path
_scripts_dir = Path(__file__).resolve().parents[1]
for _part in ('runners', 'tools'):
    sys.path.insert(0, str(_scripts_dir / _part))

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from dci_common import record_json, relocated_path

ROOT=Path(__file__).resolve().parents[3]
import dci

FAKE_RUNNER=r'''
import argparse,json,os,re,sys,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('name')
for key in ['workload','topology','config-template','simulator','nvls-enable']:p.add_argument('--'+key)
a=p.parse_args();root=Path.cwd();folder=root/'experiments/runs'/a.name
for child in ['inputs','outputs','logs']:(folder/child).mkdir(parents=True,exist_ok=True)
if (root/'timeout.flag').exists():time.sleep(20)
inputs={}
for key,value in [('workload',a.workload),('topology',a.topology)]:
 src=Path(value);dst=folder/'inputs'/src.name;dst.write_bytes(src.read_bytes());inputs[key]={'snapshot':str(dst)}
text=Path(a.config_template).read_text();config=folder/'inputs/SimAI.conf'
config.write_text(text.replace('__OUTPUT_DIR__',str(folder/'outputs')));inputs['configuration']={'snapshot':str(config)}
mode=re.search(r'^DCI_ROUTING_MODE\s+(\S+)',text,re.M)[1];irn=int(os.environ['SIMAI_IRN_ENABLE'])
rng=int(re.search('RngRun=(\d+)',os.environ['NS_GLOBAL_VALUE'])[1]);seed=int(re.search('RngSeed=(\d+)',os.environ['NS_GLOBAL_VALUE'])[1])
budget=int(os.environ['SIMAI_DCI_EXPERIMENT_MS']);complete=not(mode=='spray_random' and irn==0)
code=0 if complete else 3;time_ns=2000000 if complete else budget*1000000
lines=['maxRtt=10 maxBdp=8','FIG2_QP_CONFIG qps_per_channel='+os.environ['SIMAI_FIG2_QPS_PER_CHANNEL']]
for ch in range(int(os.environ['SIMAI_NCCL_CHANNELS'])):lines.append('FIG2_QP_CREATE channel='+str(ch))
if irn:lines.append('IRN_CONFIG bdp_bytes=8 rto_low_ns=20 rto_high_ns=40')
for node in [6,7]:
 lines.append(f'DCI_ROUTING_CONFIG node={node} mode={mode} ports=3,4 candidate_count=2 rng_seed={seed} rng_run={rng}')
 for port in [3,4]:lines.append(f'DCI_ROUTING_PORT node={node} port={port} selected_packets=10')
for node in range(4):
 ack=100 if complete else 50;out=0 if complete else 50
 lines.append(f'DCI_TRANSPORT_STATS node={node} irn={irn} completed={int(complete)} retrans_bytes=0 retrans_packets=0 timeouts=0 nack_count=0 outstanding_bytes={out} acked_bytes={ack} posted_bytes=100 received_contiguous_bytes={ack} qp_errors=0')
 if irn:lines.append(f'IRN_STATS node={node} rx_ooo_packets=0 rx_duplicate_packets=0 rx_window_drops=0')
if complete:lines.append('all passes finished at time: 1000000')
lines+=['DCI_EXPERIMENT_CONFIG budget_ms='+str(budget),f'DCI_EXPERIMENT_END completed={int(complete)} time_ns={time_ns} reason='+('collective_done' if complete else 'simulation_budget')]
(folder/'logs/simulator.log').write_text('\n'.join(lines)+'\n')
manifest=dict(inputs=inputs,threads=1,environment={'AS_SEND_LAT':'3','AS_NVLS_ENABLE':'0'},result={'return_code':code})
(folder/'outputs/manifest.json').write_text(json.dumps(manifest));sys.exit(code)
'''


class DciTools(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'experiments/scripts').mkdir(parents=True)
        (self.root/'experiments/scripts/run.py').write_text(FAKE_RUNNER)
        (self.root/'workload.txt').write_text('synthetic control-flow fixture\n')
        (self.root/'topology.txt').write_text('synthetic topology fixture\n')
        self.template='FLOW_FILE __OUTPUT_DIR__/flow1.txt\nCC_MODE 1\nRDMA_TRANSPORT_MODE 0\nRC_ACK_RETRY_ENABLE 0\nL2_BACK_TO_ZERO 0\nDCI_ROUTING_MODE spray_rr\nDCI_ROUTING_NODES 6,7\n'
        (self.root/'config.conf').write_text(self.template)
        self.sim=self.root/'simulator';self.sim.write_text('#!/bin/sh\nexit 0\n');self.sim.chmod(0o755)
        self.options=dict(group='test',workload=str(self.root/'workload.txt'),topology=str(self.root/'topology.txt'),
            config_template=str(self.root/'config.conf'),routing=['ecmp'],irn=[1],rng_runs=[1],seed=1,
            channels=4,qps=4,budget_ms=1000,wall_seconds=5,gpu_nodes=[0,1,2,3],dci_nodes=[6,7],
            diagnostics=False,diag_src=0,diag_dst=3,diag_sport=0,max_events=200000)
        self.spec=next(dci.case_specs(self.options));self.study=self.root/'experiments/runs/_batches/test'
        spec=self.root/'experiments/configs/dci-routing/experiment.json';spec.parent.mkdir(parents=True)
        spec.write_text(json.dumps(self.options))

    def tearDown(self):self.temp.cleanup()

    def execute(self,options=None,spec=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return dci.execute(self.root,options or self.options,spec or self.spec,self.sim,self.study)

    def test_execute_and_reuse_without_rerun(self):
        row=self.execute();self.assertEqual(row['measurement'],'OK');self.assertEqual(row['outcome'],'COMPLETE')
        cached=dci.find_result(self.root,self.spec);self.assertEqual(cached['run'],row['run']);self.assertEqual(cached['source'],'cached')
        with mock.patch.object(dci,'execute',side_effect=AssertionError('must reuse')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dci.main(self.root,['run','--simulator',str(self.sim)]),0)

    def test_changed_qps_is_not_reused(self):
        self.execute();other={**self.options,'qps':8};self.assertIsNone(dci.find_result(self.root,next(dci.case_specs(other))))

    def test_changed_input_is_not_reused(self):
        self.execute();(self.root/'topology.txt').write_text('changed')
        self.assertIsNone(dci.find_result(self.root,next(dci.case_specs(self.options))))

    def test_strict_build_checks_binary(self):
        row=self.execute();original=hashlib.sha256(self.sim.read_bytes()).hexdigest()
        self.assertEqual(dci.find_result(self.root,self.spec,original)['run'],row['run'])
        self.sim.write_text('#!/bin/sh\nexit 1\n')
        self.assertIsNone(dci.find_result(self.root,self.spec,hashlib.sha256(self.sim.read_bytes()).hexdigest()))

    def test_legacy_import_and_rto_validation(self):
        row=self.execute();folder=self.root/'experiments/runs'/row['run'];(folder/'inputs/dci-case.json').unlink()
        self.assertEqual(dci.probe_historical(folder,self.spec)['source'],'historical')
        log=folder/'logs/simulator.log';log.write_text(log.read_text().replace('rto_low_ns=20','rto_low_ns=999'))
        self.assertIsNone(dci.probe_historical(folder,self.spec))

    def test_legacy_channel_validation(self):
        row=self.execute();folder=self.root/'experiments/runs'/row['run'];(folder/'inputs/dci-case.json').unlink()
        log=folder/'logs/simulator.log';log.write_text(log.read_text().replace('FIG2_QP_CREATE channel=3','IGNORED channel=3'))
        self.assertIsNone(dci.probe_historical(folder,self.spec))

    def test_budget_expiry_is_valid_and_cached(self):
        options={**self.options,'routing':['spray_random'],'irn':[0]};spec=next(dci.case_specs(options))
        row=self.execute(options,spec);self.assertEqual(row['measurement'],'OK');self.assertEqual(row['outcome'],'INCOMPLETE')
        self.assertNotIn('global_exit_ms',row);self.assertNotIn('ooo_packets',row)
        self.assertEqual(dci.find_result(self.root,spec)['outcome'],'INCOMPLETE')

    def test_wall_timeout_is_not_simulation_expiry(self):
        (self.root/'timeout.flag').touch();options={**self.options,'wall_seconds':1}
        row=self.execute(options);self.assertEqual(row['outcome'],'WALL_TIMEOUT');self.assertEqual(row['measurement'],'INVALID')
        self.assertIsNone(dci.find_result(self.root,self.spec))

    def test_dry_run_and_summary_never_launch(self):
        with mock.patch.object(dci,'execute',side_effect=AssertionError('must not launch')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dci.main(self.root,['run','--dry-run']),0)
            self.assertEqual(dci.main(self.root,['summary']),1)

    def test_summary_reads_saved_plan_even_if_original_input_removed(self):
        row=self.execute();self.study.mkdir(parents=True,exist_ok=True)
        (self.study/'study.json').write_text(json.dumps(dict(options=self.options,cases=[dict(spec=self.spec,run=row['run'],source='cached',exit_code=0)])))
        (self.root/'workload.txt').unlink()
        with mock.patch.object(dci,'execute',side_effect=AssertionError('must not launch')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dci.main(self.root,['summary']),0)

    def test_interruption_preserves_full_plan(self):
        with mock.patch.object(dci,'execute',side_effect=KeyboardInterrupt),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):dci.main(self.root,['run','--routing','ecmp,spray_rr,spray_random','--irn','0,1','--simulator',str(self.sim)])
        state=record_json((self.study/'study.json').read_text());self.assertEqual(len(state['cases']),6)

    def test_fresh_preserves_old_results(self):
        first=self.execute()
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(dci.main(self.root,['run','--fresh','--simulator',str(self.sim)]),0)
        self.assertTrue((self.root/'experiments/runs'/first['run']).is_dir())
        self.assertEqual(len([p for p in (self.root/'experiments/runs').iterdir() if p.name != '_batches']),2)


class OriginalEntry(unittest.TestCase):
    def test_legacy_single_run_named_dci_still_accepts_original_flags(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'experiments/scripts').mkdir(parents=True)
            script=root/'experiments/scripts/run.py';script.write_bytes((ROOT/'experiments/scripts/run.py').read_bytes())
            for name,data in [('workload.txt','fixture'),('topology.txt','fixture'),('template.conf','FLOW_FILE __OUTPUT_DIR__/flow1.txt\n')]:
                (root/name).write_text(data)
            p=subprocess.run([sys.executable,str(script),'dci','--workload',str(root/'workload.txt'),
                '--topology',str(root/'topology.txt'),'--config-template',str(root/'template.conf'),
                '--simulator','/bin/true','--dry-run'],cwd=root,capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr);self.assertTrue((root/'experiments/runs/dci/inputs/SimAI.conf').is_file())




"""Preflight contracts for runtime WAN inputs, not a duplicate transport model."""
import sys
from pathlib import Path
import tempfile
import unittest
from dci_common import record_json, relocated_path

ROOT = Path(__file__).resolve().parents[3]
from dci_common import describe_topology as describe, topology_preflight as preflight, quantity, RATE_UNITS, TIME_UNITS
from run_collective import config_text, DELAY_PROFILES_US


class RuntimeTopologyTest(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / 'experiments/topologies/dci-routing'
        self.template = ROOT / 'experiments/configs/dci-routing/spray_rr.conf'
        self.config = config_text(self.template, 10, 1024)

    def load(self, name, config=None):
        return preflight(describe(self.folder / name), config or self.config, 'topology', DELAY_PROFILES_US)

    def test_known_four_path_initialization(self):
        info = self.load('4gpu-2site-4x400g-245-745-1245-1745us.txt')
        self.assertEqual(info['paths_per_peer'], 4)
        self.assertEqual(info['mmu_by_node']['6']['reserved_bytes'], 597174576)
        self.assertEqual(max(p['rtt_bound_ns'] for p in info['expected_paths']), 3492243)
        self.assertEqual(max(p['bdp_bytes'] for p in info['expected_paths']), 174612150)

    def test_reordered_interfaces_need_not_match_across_gateways(self):
        info = self.load('4gpu-2site-3wan-mixed-reordered.txt')
        self.assertEqual([p['port'] for p in info['wan_by_node']['6']], [1, 3, 4])
        self.assertEqual([p['port'] for p in info['wan_by_node']['7']], [1, 2, 4])
        self.assertEqual({p['rate_bps'] for p in info['wan_by_node']['6']}, {100000000000, 200000000000, 400000000000})

    def test_new_path_count_needs_no_named_profile(self):
        self.assertEqual(self.load('4gpu-2site-5wan-mixed.txt')['paths_per_peer'], 5)

    def test_insufficient_buffer_is_not_silently_increased(self):
        with self.assertRaisesRegex(ValueError, 'buffer above'):
            self.load('4gpu-2site-4x400g-245-745-1245-1745us.txt', config_text(self.template, 10, 512))

    def test_missing_ecn_map_reports_config_action(self):
        text = (self.folder / '4gpu-2site-3wan-mixed-reordered.txt').read_text().replace('200Gbps', '300Gbps', 1)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'custom.txt'; path.write_text(text)
            with self.assertRaisesRegex(ValueError, 'no rebuild'):
                preflight(describe(path), self.config, 'topology', DELAY_PROFILES_US)

    def test_units_and_precision(self):
        self.assertEqual(quantity('0.000025ms', TIME_UNITS, 'delay'), 25)
        self.assertEqual(quantity('200Gbps', RATE_UNITS, 'rate'), 200000000000)
        with self.assertRaises(ValueError):
            quantity('0.5ns', TIME_UNITS, 'delay')

    def test_link_count_mismatch_rejected(self):
        text = (self.folder / '4gpu-2site-3wan-mixed-reordered.txt').read_text().replace('8 2 2 2 11', '8 2 2 2 12', 1)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bad.txt'; path.write_text(text)
            with self.assertRaisesRegex(ValueError, 'link count'):
                describe(path)




"""Reject false loaded-fault acceptance and malformed gradient inputs."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from dci_common import record_json, relocated_path

ROOT = Path(__file__).resolve().parents[3]
from dci_common import paced_workload, workload_info, validate_issued, fault_metrics


class FaultAcceptanceTest(unittest.TestCase):
    def fixture(self):
        args = SimpleNamespace(fault=True, fault_path_index=0, fault_start_us=50000,
            fault_end_us=100000, timeout_us=10000, interval_us=1000, probe_enable=1,
            topology_info=dict(paths_per_peer=2,
                wan_by_node={'6': [dict(port=1), dict(port=3)],
                             '7': [dict(port=2), dict(port=4)]}))
        lines = [
            'DCI_PM_FAULT_CONFIG path_index=0 start_us=50000 end_us=100000',
            'DCI_PM_FAULT_EVENT action=blackhole time_ns=50000000 link_stays_up=1',
            'DCI_PM_FAULT_EVENT action=restore time_ns=100000000 link_stays_up=1',
            'DCI_PM_FAULT_END events=2',
        ]
        for node, ports in ((6, (1,3)), (7, (2,4))):
            target = ports[0]
            lines += [f'DCI_PM_FAULT_TARGET node={node} port={target}',
                f'DCI_PM_FAULT_DROPS node={node} port={target} data_packets=1 probe_packets=5 link_up=1']
            for path, port in enumerate(ports):
                lines += [f'DCI_PM_FAULT_TRAFFIC node={node} port={port} data_before=1 data_during=1 data_after=1',
                    f'DCI_PM_CANDIDATE node={node} path={path} interface={port}',
                    f'DCI_PM_STATE node={node} path={path} time_ns=0 to=UNVERIFIED',
                    f'DCI_PM_STATE node={node} path={path} time_ns=3500000 to=UP',
                    f'DCI_PM_STATS node={node} path={path} state=UP']
                if path == 0:
                    lines += [f'DCI_PM_STATE node={node} path=0 time_ns=60000000 to=DOWN',
                        f'DCI_PM_STATE node={node} path=0 time_ns=100500002 to=UP']
        return args, lines

    def test_paced_input_volume_and_compute_units(self):
        info = workload_info(paced_workload(12, 64, 20000))
        self.assertEqual((info['count'], info['bytes'], info['min_compute_ns']),
                         (12, 768 * 1048576, 240000000))
        with self.assertRaises(ValueError):
            workload_info(paced_workload(2,64,20000).replace('grad_bucket_1', 'grad_bucket_0'))

    def test_issued_buckets_alone_do_not_prove_completion(self):
        info = workload_info(paced_workload(2,64,20000))
        lines = [f"info: all-reduce weight grad collective issued for layer: {b['name']} with size: {b['bytes']}"
                 for b in info['buckets']]
        lines += [f'DCI_TRANSPORT_STATS node={node} posted_bytes={3 * 64 * 1048576}' for node in range(4)]
        validate_issued(lines, info)
        with self.assertRaisesRegex(ValueError, 'byte volume'):
            validate_issued(lines[:-1], info)

    def test_loaded_fault_with_recovery(self):
        args, lines = self.fixture()
        result = fault_metrics(lines, args, 1)
        self.assertEqual(result['fault_maintenance'], 'PASS')
        self.assertEqual(result['node6_fault_detect_ms'], 10)
        self.assertAlmostEqual(result['node7_fault_recover_ms'], 0.500002)

    def test_no_data_drops_is_not_loaded_acceptance(self):
        args, lines = self.fixture()
        with self.assertRaisesRegex(ValueError, 'no business DATA'):
            fault_metrics([s.replace('data_packets=1', 'data_packets=0') for s in lines], args, 1)

    def test_other_path_cannot_go_down(self):
        args, lines = self.fixture()
        lines.append('DCI_PM_STATE node=6 path=1 time_ns=70000000 to=DOWN')
        with self.assertRaisesRegex(ValueError, 'Unaffected'):
            fault_metrics(lines, args, 1)

    def test_recovery_requires_new_up_transition(self):
        args, lines = self.fixture()
        lines = [s for s in lines if 'time_ns=100500002' not in s]
        with self.assertRaisesRegex(ValueError, 'DOWN/UP'):
            fault_metrics(lines, args, 1)

    def test_recovery_cannot_precede_restore(self):
        args, lines = self.fixture()
        lines.append('DCI_PM_STATE node=6 path=0 time_ns=90000000 to=UP')
        with self.assertRaisesRegex(ValueError, 'resurrected'):
            fault_metrics(lines, args, 1)

    def test_late_detection_rejected(self):
        args, lines = self.fixture()
        with self.assertRaisesRegex(ValueError, 'Detection exceeds'):
            fault_metrics([s.replace('time_ns=60000000', 'time_ns=62000000') for s in lines], args, 1)

    def test_disabled_pm_not_credited_for_maintenance(self):
        args, lines = self.fixture()
        self.assertEqual(fault_metrics(lines, args, 0)['fault_maintenance'], 'NOT_RUNNING')







#!/usr/bin/env python3
"""Run controlled feedback contracts with actual Qbb return transmissions."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from dci_common import record_json, relocated_path


def check_cnp_main():
    p = argparse.ArgumentParser(description='Run CNP acceptance with the compiled checker')
    p.add_argument('--checker', type=Path, required=True)
    p.add_argument('--root', type=Path, default=Path.cwd())
    args = p.parse_args()
    binary = args.checker.resolve()
    if not binary.is_file():
        p.error('Checker not found: ' + str(binary))
    folder = args.root.resolve() / 'experiments/analysis' / (
        'cnp-feedback-checks-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True)
    cases = [(name, 1, 50, 5) for name in
             ('ect','ooo','burst','two_qps','pg1','cancel','late')]
    cases += [('burst',1,10,5), ('burst',1,50,250),
              ('burst',0,50,5), ('ooo',0,50,5)]
    env = {k:v for k,v in os.environ.items()
           if not k.startswith(('SIMAI_', 'TSRDMA_TEST_', 'AS_'))}
    for index, (scenario, mode, interval, delay) in enumerate(cases):
        command = [str(binary), '--scenario=' + scenario, '--mode=' + str(mode),
                   '--intervalUs=' + str(interval), '--delayUs=' + str(delay)]
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
        text = result.stdout + result.stderr
        path = folder / ('%02d-%s-m%d-i%d-d%d.log' % (index,scenario,mode,interval,delay))
        path.write_text(text)
        success = [line for line in text.splitlines() if line.startswith('CHECK_OK ')]
        if result.returncode or len(success) != 1:
            print('CHECK_FAILED log=' + str(path))
            print(text[-6000:])
            return 1
        print(success[0], flush=True)
    print('CNP_ACCEPTANCE_OK cases=%d logs=%s' % (len(cases),folder))
    return 0




if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'cnp':
        del sys.argv[1]
        sys.exit(check_cnp_main())
    unittest.main()
