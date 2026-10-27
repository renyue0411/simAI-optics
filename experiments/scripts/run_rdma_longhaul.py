#!/usr/bin/env python3
"""Run the HPE/Juniper 50 km single-NIC RDMA reproduction sweep."""

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
    EXPERIMENTS_ROOT / "configs" / "rdma-longhaul" / "article-50km.conf"
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


def command(binary: Path, config: dict[str, str], qps: int, result: Path) -> list[str]:
    mapping = {
        "hostRate": "HOST_RATE",
        "dciRate": "DCI_RATE",
        "accessDelayUs": "ACCESS_DELAY_US",
        "dciDelayUs": "DCI_DELAY_US",
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
    result_command = [str(binary), f"--qps={qps}"]
    result_command.extend(
        f"--{argument}={require(config, key)}" for argument, key in mapping.items()
    )
    result_command.append(f"--resultFile={result}")
    return result_command


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
    qps_values = [int(value) for value in require(config, "QPS").split(",")]
    if not qps_values or any(value <= 0 for value in qps_values):
        raise ValueError("QPS must be a comma-separated list of positive integers")

    run_dir = EXPERIMENTS_ROOT / "runs" / args.name
    logs = run_dir / "logs"
    outputs = run_dir / "outputs"
    inputs = run_dir / "inputs"
    for directory in (logs, outputs, inputs):
        directory.mkdir(parents=True, exist_ok=True)
    copied_config = inputs / "article-50km.conf"
    copied_config.write_text(config_path.read_text())

    commands = [
        command(binary, config, qps, outputs / f"qps-{qps}.csv")
        for qps in qps_values
    ]
    manifest = {
        "name": args.name,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "HPE 50 km single-400G-NIC finite-QP-window reproduction",
        "config": str(config_path),
        "binary": str(binary),
        "qps": qps_values,
        "commands": commands,
        "dry_run": args.dry_run,
    }
    (outputs / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"Run dir: {run_dir}")
    if args.dry_run:
        for item in commands:
            print("Command:", shlex.join(item))
        print("Dry run: files prepared; simulator not started.")
        return 0

    if not binary.is_file():
        raise FileNotFoundError(f"simulator binary not found: {binary}")

    rows: list[dict[str, str]] = []
    failed = False
    for qps, item in zip(qps_values, commands):
        completed = subprocess.run(item, text=True, capture_output=True)
        log = completed.stdout + completed.stderr
        (logs / f"qps-{qps}.log").write_text(log)
        summaries = [line for line in log.splitlines() if line.startswith("LH_RESULT ")]
        for line in summaries:
            print(line)
        result_path = outputs / f"qps-{qps}.csv"
        if completed.returncode != 0 or len(summaries) != 1 or not result_path.is_file():
            failed = True
            print(f"qps={qps}: failed; inspect {logs / f'qps-{qps}.log'}")
            continue
        with result_path.open(newline="") as handle:
            result_rows = list(csv.DictReader(handle))
        if len(result_rows) != 1:
            failed = True
            print(f"qps={qps}: invalid result CSV")
            continue
        rows.append(result_rows[0])

    if rows:
        with (outputs / "summary.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Summary: {outputs / 'summary.csv'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
