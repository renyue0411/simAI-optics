#!/usr/bin/env python3
"""Read existing IRN diagnostic logs; measure observed confirmation lag, not a performance counterfactual."""
import argparse
from bisect import bisect_right
from collections import Counter
from pathlib import Path
import re
import statistics


def fields(line):
    return dict(re.findall(r'([A-Za-z0-9_]+)=([^\s]+)',line))


def describe(label, values):
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


def analyze(path):
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
    describe('receiver_already_had_data_to_sender_confirmation',delays)
    print('first_covering_feedback cumulative={} selective_only={} not_observed={} earlier_cumulative_anomalies={}'.format(
        cum_evidence,sack_evidence,pending,earlier_cum))
    print('NOTE: observations use actual ACKs in this run, including possible feedback from retransmissions.')
    print('NOTE: these delays do not predict performance after introducing a wait or NACK threshold.')
    return dict(delays=delays,pending=pending,earlier_cum=earlier_cum)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs',nargs='+',type=Path)
    args=parser.parse_args()
    for path in args.logs:
        try:analyze(path)
        except (OSError,KeyError,ValueError) as error:parser.exit(1,str(path)+': '+str(error)+'\n')
