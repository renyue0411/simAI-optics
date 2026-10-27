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


def fields(line):
    return dict(re.findall(r"([a-z_]+)=([^\s]+)", line))


def analyze(run_dir, node, ports):
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
                item = fields(match.group(1))
                created[int(item["src"]), int(item["dst"])].add(
                    (int(item["channel"]), int(item["index"]), int(item["sport"])))
            match = CONFIG_LINE.match(line)
            if match:
                qp_config = fields(match.group(1))
            match = QP_LINE.match(line)
            if match:
                item = fields(match.group(1))
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--node", type=int, default=6)
    parser.add_argument("--ports", default="3,4,5,6")
    args = parser.parse_args()
    ports = tuple(int(value) for value in args.ports.split(","))
    if not ports or len(set(ports)) != len(ports):
        parser.error("--ports must contain distinct port IDs")
    analyze(args.run_dir, args.node, ports)


if __name__ == "__main__":
    main()
