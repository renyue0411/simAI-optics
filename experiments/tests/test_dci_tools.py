"""Workflow tests use synthetic logs/processes; they never start NS-3."""
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

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'experiments/lib'))
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
        self.spec=next(dci.case_specs(self.options));self.study=self.root/'experiments/studies/test'
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
        state=json.loads((self.study/'study.json').read_text());self.assertEqual(len(state['cases']),6)

    def test_fresh_preserves_old_results(self):
        first=self.execute()
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(dci.main(self.root,['run','--fresh','--simulator',str(self.sim)]),0)
        self.assertTrue((self.root/'experiments/runs'/first['run']).is_dir())
        self.assertEqual(len(list((self.root/'experiments/runs').iterdir())),2)


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


if __name__=='__main__':unittest.main()
