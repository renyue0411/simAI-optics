#!/usr/bin/env python3
"""Summarize one-QP diagnostic evidence, keeping unseen originals unresolved."""
import argparse
from pathlib import Path
import re


def fields(line):
    return dict(re.findall(r'([A-Za-z0-9_]+)=([^\s]+)', line))


def analyze(log_path):
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


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('log',type=Path)
    args=p.parse_args();analyze(args.log)
