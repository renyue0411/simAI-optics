#!/usr/bin/env python3
"""Prepare or summarize the SimAI qualitative Figure 4 sweep.

This is a channel-buffer model experiment, not an implementation of the
ConnectX adaptive-routing transport or NCCL 2.18.3's actual buffer protocol.
"""

import argparse
import csv
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "experiments/scripts/run.py"
RUNS = ROOT / "experiments/runs"
CONFIG = "experiments/configs/nccl-channel-buffer/figure4-50km.conf"
TOPOLOGY = "nccl-channel-buffer/4gpu-2site-800g-50km.txt"
WORKLOADS = {
    4: "nccl-channel-buffer/micro-allreduce4-4gib.txt",
    8: "nccl-channel-buffer/figure4-8gib.txt",
    12: "nccl-channel-buffer/figure4-12gib.txt",
    16: "nccl-channel-buffer/figure4-16gib.txt",
}
FINISHED = re.compile(r"all passes finished at time:\s*(\d+)")
DATA = re.compile(r"All data (sent from|received by) node (\d+) is (\d+)")


def integers(raw):
    values = [int(item) for item in raw.split(",")]
    if not values or any(item <= 0 for item in values) or len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("use distinct positive integers separated by commas")
    return values


def least_squares(points):
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        raise ValueError("at least two distinct message sizes are required")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    return slope, mean_y - slope * mean_x


def summarize(args):
    rows = []
    for buffer_mib in args.buffers:
        points = []
        for gib in args.sizes:
            name = f"{args.prefix}-{gib}gib-{buffer_mib}mib"
            log_path = RUNS / name / "logs/simulator.log"
            if not log_path.is_file():
                print(f"missing: {log_path}", file=sys.stderr)
                return 1
            log = log_path.read_text(encoding="utf-8", errors="replace")
            matched = FINISHED.findall(log)
            delivered = {(direction, int(node)): int(size)
                         for direction, node, size in DATA.findall(log)}
            if len(matched) != 1 or len(delivered) != 8 or any(
                delivered.get(("sent from", node)) != delivered.get(("received by", node))
                for node in range(4)
            ):
                print(f"incomplete or inconsistent run: {log_path}", file=sys.stderr)
                return 1
            # SimAI boosted ticks in these runs are nanoseconds.
            time_ms = int(matched[0]) / 1e6
            rows.append((buffer_mib, gib, time_ms))
            points.append((gib, time_ms))
        if len(points) > 1:
            slope, intercept = least_squares(points)
            # Four-rank ring AllReduce injects 2*(N-1)/N = 1.5 message bytes/rank.
            ring_bus_gbps = 1.5 * 8 * 1024**3 * 1000 / 1e9 / slope
            print(f"buffer_mib={buffer_mib} slope_ms_per_gib={slope:.6f} "
                  f"intercept_ms={intercept:.6f} ring_bus_gbps={ring_bus_gbps:.3f}")
    output = RUNS / f"{args.prefix}-times.csv"
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("buffer_mib", "array_gib", "completion_ms"))
        writer.writerows(rows)
    print(f"times_csv={output}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator", type=Path, help="Built AstraSimNetwork executable")
    parser.add_argument("--prefix", default="fig4-50km", help="Unique run name prefix")
    parser.add_argument("--sizes", type=integers, default=[4, 8, 12, 16])
    parser.add_argument("--buffers", type=integers, default=[4, 16, 64, 128],
                        help="Exploratory MiB values; replace with the article's legend for exact comparison")
    parser.add_argument("--fragment-mib", type=int, default=4,
                        help="Default 4 MiB exceeds the 2 MiB QP cap; smaller fragments never fill that QP window")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print commands without starting simulations")
    mode.add_argument("--run", action="store_true", help="Run each case in sequence")
    mode.add_argument("--summarize", action="store_true", help="Check runs and fit their completion times")
    args = parser.parse_args()
    if args.summarize:
        return summarize(args)
    if args.simulator is None:
        parser.error("--simulator is required for --run and --dry-run")
    if args.fragment_mib <= 0 or args.fragment_mib > min(args.buffers):
        parser.error("fragment-mib must be positive and no larger than the smallest buffer")
    if any(size not in WORKLOADS for size in args.sizes):
        parser.error("available workload sizes: 4,8,12,16 GiB")
    simulator = args.simulator.expanduser().resolve()
    if args.run and (not RUNNER.is_file() or not simulator.is_file()):
        parser.error("run.py or simulator is missing; build SimAI and check this checkout")
    for buffer_mib in args.buffers:
        for gib in args.sizes:
            name = f"{args.prefix}-{gib}gib-{buffer_mib}mib"
            env = os.environ.copy()
            env.update(AS_PXN_ENABLE="0", AS_NVLS_ENABLE="0",
                       SIMAI_NCCL_CHANNELS="4",
                       SIMAI_NCCL_BUFFSIZE=str(buffer_mib * 1024**2),
                       SIMAI_NCCL_FRAGMENT_BYTES=str(args.fragment_mib * 1024**2))
            command = [sys.executable, str(RUNNER), name,
                       "--workload", WORKLOADS[gib], "--topology", TOPOLOGY,
                       "--config-template", CONFIG,
                       "--simulator", str(simulator), "--nvls-enable", "0"]
            print(" ".join(f"{key}={shlex.quote(env[key])}" for key in
                           ("AS_PXN_ENABLE", "SIMAI_NCCL_CHANNELS",
                            "SIMAI_NCCL_BUFFSIZE", "SIMAI_NCCL_FRAGMENT_BYTES"))
                  + " " + shlex.join(command), flush=True)
            if args.run:
                if (RUNS / name).exists():
                    parser.error(f"run directory already exists: {RUNS / name}")
                result = subprocess.run(command, cwd=ROOT, env=env, check=False)
                if result.returncode != 0:
                    return result.returncode
                log = RUNS / name / "logs/simulator.log"
                if not log.is_file() or not FINISHED.search(
                    log.read_text(encoding="utf-8", errors="replace")
                ):
                    parser.error(f"simulator did not finish its workload: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
