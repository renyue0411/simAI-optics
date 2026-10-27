#!/usr/bin/env python3
"""Run a single-QP RDMA distance sweep over the long-haul topology."""

from __future__ import annotations

import argparse
import csv
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS_ROOT = REPO_ROOT / "experiments"
DEFAULT_CONFIG = (
    EXPERIMENTS_ROOT / "configs" / "rdma-longhaul" / "distance-sweep.conf"
)
DEFAULT_BINARY = (
    REPO_ROOT
    / "ns-3-alibabacloud"
    / "simulation"
    / "build"
    / "scratch"
    / "ns3.36.1-rdma-longhaul-default"
)


def read_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value or key in values:
            raise ValueError(f"{path}:{number}: invalid or duplicate key")
        values[key] = value
    return values


def require(config: dict[str, str], key: str) -> str:
    if key not in config:
        raise ValueError(f"missing configuration key {key}")
    return config[key]


def compact_number(value: float) -> str:
    return f"{value:g}"


def file_label(distance_km: float) -> str:
    return compact_number(distance_km).replace(".", "p") + "km"


def command(
    binary: Path,
    config: dict[str, str],
    distance_km: float,
    propagation_us_per_km: float,
    result: Path,
) -> tuple[list[str], float]:
    dci_delay_us = distance_km * propagation_us_per_km
    mapping = {
        "hostRate": "HOST_RATE",
        "dciRate": "DCI_RATE",
        "accessDelayUs": "ACCESS_DELAY_US",
        "mtu": "MTU",
        "windowBytes": "WINDOW_BYTES",
        "ackEveryPackets": "ACK_EVERY_PACKETS",
        "bytesPerQp": "BYTES_PER_QP",
        "retryEnabled": "RETRY_ENABLED",
        "rtoUs": "RTO_US",
        "retryCount": "RETRY_COUNT",
        "flowStart": "FLOW_START",
        "stop": "STOP",
        "seed": "SEED",
    }
    result_command = [
        str(binary),
        f"--qps={require(config, 'QPS')}",
        f"--dciDelayUs={compact_number(dci_delay_us)}",
    ]
    result_command.extend(
        f"--{argument}={require(config, key)}" for argument, key in mapping.items()
    )
    result_command.append(f"--resultFile={result}")
    return result_command, dci_delay_us


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="run directory name")
    parser.add_argument("--config-template", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = args.config_template.resolve()
    binary = args.binary.resolve()
    config = read_config(config_path)

    distances = [
        float(value) for value in require(config, "DISTANCES_KM").split(",")
    ]
    propagation = float(require(config, "PROPAGATION_US_PER_KM"))
    qps = int(require(config, "QPS"))
    if not distances or any(value <= 0 for value in distances):
        raise ValueError("DISTANCES_KM must contain positive values")
    if len(set(distances)) != len(distances):
        raise ValueError("DISTANCES_KM must not contain duplicates")
    if propagation <= 0:
        raise ValueError("PROPAGATION_US_PER_KM must be positive")
    if qps != 1:
        raise ValueError("the distance-isolation experiment requires QPS=1")

    run_dir = EXPERIMENTS_ROOT / "runs" / args.name
    logs = run_dir / "logs"
    outputs = run_dir / "outputs"
    inputs = run_dir / "inputs"
    for directory in (logs, outputs, inputs):
        directory.mkdir(parents=True, exist_ok=True)
    copied_config = inputs / "distance-sweep.conf"
    copied_config.write_text(config_path.read_text())

    cases: list[tuple[float, float, str, list[str], Path]] = []
    for distance in distances:
        label = file_label(distance)
        result_path = outputs / f"distance-{label}.csv"
        item, delay = command(binary, config, distance, propagation, result_path)
        cases.append((distance, delay, label, item, result_path))

    manifest = {
        "name": args.name,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "single-QP RDMA distance/BDP sweep",
        "config": str(config_path),
        "binary": str(binary),
        "qps": qps,
        "distances_km": distances,
        "propagation_us_per_km": propagation,
        "commands": [item for _, _, _, item, _ in cases],
        "dry_run": args.dry_run,
    }
    (outputs / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"Run dir: {run_dir}")
    if args.dry_run:
        for _, _, _, item, _ in cases:
            print("Command:", shlex.join(item))
        print("Dry run: files prepared; simulator not started.")
        return 0

    if not binary.is_file():
        raise FileNotFoundError(f"simulator binary not found: {binary}")

    rows: list[dict[str, str]] = []
    failed = False
    for distance, delay, label, item, result_path in cases:
        completed = subprocess.run(item, text=True, capture_output=True)
        log = completed.stdout + completed.stderr
        log_path = logs / f"distance-{label}.log"
        log_path.write_text(log)
        summaries = [
            line for line in log.splitlines() if line.startswith("LH_RESULT ")
        ]
        if completed.returncode != 0 or len(summaries) != 1 or not result_path.is_file():
            failed = True
            print(f"distance_km={compact_number(distance)}: failed; inspect {log_path}")
            continue

        with result_path.open(newline="") as handle:
            result_rows = list(csv.DictReader(handle))
        if len(result_rows) != 1:
            failed = True
            print(f"distance_km={compact_number(distance)}: invalid result CSV")
            continue

        row = {
            "distance_km": compact_number(distance),
            "dci_delay_us": compact_number(delay),
            **result_rows[0],
        }
        rows.append(row)
        print(
            "LH_DISTANCE_RESULT"
            f" distance_km={row['distance_km']}"
            f" dci_delay_us={row['dci_delay_us']}"
            f" success={row['success']}"
            f" rtt_us={row['propagation_rtt_us']}"
            f" goodput_gbps={row['goodput_gbps']}"
            f" expected_goodput_gbps={row['expected_goodput_gbps']}"
            f" retrans_bytes={row['retrans_bytes']}"
            f" timeouts={row['timeouts']}"
            f" switch_drops={row['switch_drops']}"
        )

    if rows:
        summary_path = outputs / "summary.csv"
        with summary_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Summary: {summary_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
