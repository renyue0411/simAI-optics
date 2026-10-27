#!/usr/bin/env python3
"""Prepare and run an existing rdma-dci-tunnel executable; never builds it."""
import argparse
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "ns-3-alibabacloud/simulation"
FIELDS = {
    "PATHS": ("paths", int), "POLICY": ("policy", int),
    "RDMA_MTU": ("mtu", int), "RDMA_WINDOW_BYTES": ("windowBytes", int),
    "ACK_TIMEOUT_MS": ("rtoMs", float), "GATEWAY_BUFFER_BYTES": ("bufferBytes", int),
    "GATEWAY_ECN": ("ecn", int), "ECN_THRESHOLD_BYTES": ("ecnThresholdBytes", int),
    "PATH_PROBE_INTERVAL_MS": ("probeIntervalMs", float),
    "INITIAL_PATH_TIMEOUT_MS": ("initialPathTimeoutMs", float),
    "PATH_FAILURE_THRESHOLD": ("pathFailureThreshold", int),
    "TUNNEL_ACK_EVERY": ("tunnelAckEvery", int),
    "MAX_TUNNEL_ACK_DELAY_US": ("maxTunnelAckDelayUs", float),
    "RTT_SAMPLE_EVERY": ("rttSampleEvery", int),
    "OUTAGE_START": ("outageStart", float), "OUTAGE_END": ("outageEnd", float),
    "OUTAGE_PATH": ("outagePath", int),
    "WAN_LOSS": ("loss", float), "SIM_STOP": ("stop", float), "SEED": ("seed", int)
}


def rooted(value):
    p = Path(value).expanduser()
    return p.resolve() if p.is_absolute() else (ROOT / p).resolve()


def rows(path):
    return [row.split("#", 1)[0].strip().split() for row in path.read_text().splitlines()
            if row.split("#", 1)[0].strip()]


def seconds(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ns|us|ms|s)", value)
    if not match:
        raise ValueError("delay must include ns/us/ms/s: " + value)
    return float(match[1]) * {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1}[match[2]]


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--config-template", default="experiments/configs/dci-tunnel/minimal.conf")
    parser.add_argument("--flow-file")
    parser.add_argument("--paths", type=int, choices=(1, 2))
    parser.add_argument("--policy", type=int, choices=(0, 1, 2))
    parser.add_argument("--binary")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.name):
        parser.error("name must be a simple directory name")
    config_path = rooted(args.config_template)
    config = {}
    for row in rows(config_path):
        if len(row) != 2 or row[0] in config:
            parser.error("invalid or duplicate config row: " + " ".join(row))
        config[row[0]] = row[1]
    if set(config) != set(FIELDS) | {"TOPOLOGY_FILE", "FLOW_FILE"}:
        parser.error("config keys must match the supplied minimal.conf")
    values = {dest: convert(config[key]) for key, (dest, convert) in FIELDS.items()}
    if args.paths is not None:
        values["paths"] = args.paths
    if args.policy is not None:
        values["policy"] = args.policy
    if not (values["paths"] in (1, 2) and values["policy"] in (0, 1, 2)
            and values["ecn"] in (0, 1) and 128 <= values["mtu"] <= 1000
            and 0 <= values["windowBytes"] <= 0xffffffff
            and 4096 <= values["bufferBytes"] <= 0xffffffff
            and 0 < values["ecnThresholdBytes"] <= values["bufferBytes"]
            and values["rtoMs"] > 0 and 0 <= values["loss"] < 1
            and values["stop"] > 0 and 0 < values["seed"] <= 0xffffffff
            and values["probeIntervalMs"] > 0 and values["initialPathTimeoutMs"] > 0
            and values["maxTunnelAckDelayUs"] > 0 and 1 <= values["tunnelAckEvery"] <= 32
            and 0 < values["pathFailureThreshold"] <= 0xffffffff
            and 0 < values["rttSampleEvery"] <= 0xffffffff
            and 0 <= values["outagePath"] < values["paths"]
            and ((values["outageStart"] == -1 and values["outageEnd"] == -1)
                 or 0 <= values["outageStart"] < values["outageEnd"] < values["stop"])
            and all(math.isfinite(v) for v in values.values())):
        parser.error("invalid protocol/timing/buffer parameter")
    topology_path = rooted(config["TOPOLOGY_FILE"])
    topology = rows(topology_path)
    if len(topology) != 6 or topology[0] != ["4", "2", "4"] or topology[1] != ["1", "2"]:
        parser.error("v1 requires 4 nodes, gateways 1/2, and four links")
    links = topology[2:]
    if any(len(row) != 5 for row in links):
        parser.error("topology link rows require five columns")
    pairs = [(int(row[0]), int(row[1])) for row in links]
    if pairs != [(0, 1), (1, 2), (1, 2), (2, 3)]:
        parser.error("link order must be A-GW1, WAN0, WAN1, GW2-B")
    if links[0][2:4] != links[3][2:4] or any(float(row[4]) != 0 for row in links):
        parser.error("v1 requires symmetric local access links; use WAN_LOSS for WAN loss")
    if any(seconds(row[3]) <= 0 for row in links[1:3]):
        parser.error("invalid link delay")
    values.update(accessRate=links[0][2], accessDelayUs=seconds(links[0][3]) * 1e6,
                  rate0=links[1][2], rtt0Ms=seconds(links[1][3]) * 2000,
                  rate1=links[2][2], rtt1Ms=seconds(links[2][3]) * 2000)
    flow_path = rooted(args.flow_file or config["FLOW_FILE"])
    flow_rows = rows(flow_path)
    if not flow_rows or len(flow_rows[0]) != 1:
        parser.error("flow file needs a count header")
    count = int(flow_rows[0][0])
    if not 1 <= count <= 1024 or len(flow_rows) != count + 1:
        parser.error("flow count mismatch")
    for row in flow_rows[1:]:
        if len(row) != 6:
            parser.error("flow rows require src dst pg dport bytes start")
        src, dst, pg, port, size = map(int, row[:5])
        start = float(row[5])
        if not ((src, dst) == (0, 3) and 1 <= pg <= 7 and 1 <= port <= 65535
                and 0 < size <= 1024**3 and math.isfinite(start) and 0.001 < start < values["stop"]):
            parser.error("invalid flow; v1 supports only host A(0) to host B(3)")
    candidates = sorted((SIM / "build-dci/src/dci-tunnel/examples").glob("ns3*-rdma-dci-tunnel-*"))
    candidates = [p for p in candidates if p.is_file() and os.access(p, os.X_OK)]
    if args.binary:
        binary = rooted(args.binary)
    elif len(candidates) > 1:
        parser.error("multiple executable profiles; specify --binary")
    else:
        binary = candidates[0] if candidates else SIM / "build-dci/src/dci-tunnel/examples/ns3.36.1-rdma-dci-tunnel-default"
    if not args.dry_run and (not binary.is_file() or not os.access(binary, os.X_OK)):
        parser.error("build target rdma-dci-tunnel first")
    run = ROOT / "experiments/runs" / args.name
    if run.exists():
        parser.error("run directory exists; choose a new name")
    for folder in ("inputs", "outputs", "logs"):
        (run / folder).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, run / "inputs/config.conf")
    shutil.copyfile(topology_path, run / "inputs/topology.txt")
    shutil.copyfile(flow_path, run / "inputs/flows-original.txt")
    normalized = run / "inputs/flows.txt"
    normalized.write_text("\n".join(" ".join(row) for row in flow_rows) + "\n")
    values.update(flowFile=str(normalized), resultFile=str(run / "outputs/flows.csv"),
                  pathResultFile=str(run / "outputs/paths.csv"))
    command = [str(binary)] + ["--{}={}".format(k, v) for k, v in values.items()]
    manifest = {"name": args.name, "created_at_utc": now(), "command": command,
                "dry_run": args.dry_run, "parameters": values,
                "model": "UDP transparent RDMA; MPQUIC-inspired path validation, ACK and measured RTT; fixed QP path",
                "flow_count": count,
                "source_baseline": {"main": "bde7cfd", "ns3": "3552fabc906bf78147e169ac44b7eea5e0d77a59"}}
    manifest_file = run / "outputs/manifest.json"

    def save():
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")

    save()
    print("Run dir:", run)
    print("Command:", shlex.join(command))
    if args.dry_run:
        print("Dry run: inputs prepared; simulator not started.")
        return 0
    log_path = run / "logs/simulator.log"
    try:
        with log_path.open("wb") as log:
            result = subprocess.run(command, cwd=run, stdout=log, stderr=subprocess.STDOUT, check=False)
    except OSError as error:
        manifest["result"] = {"error": str(error), "finished_at_utc": now()}
        save()
        raise
    summary = None
    with log_path.open(encoding="utf-8", errors="replace") as log:
        for line in log:
            if line.startswith(("DCI_BIND ", "DCI_FLOW ", "DCI_GATEWAY ", "DCI_PATH ", "DCI_WIRE ", "DCI_RESULT ", "DCI_STATE ", "DCI_PATH_STATE ", "DCI_OUTAGE ")):
                print(line.rstrip())
                if line.startswith("DCI_RESULT "):
                    summary = line.strip()
    result_rows = []
    if (run / "outputs/flows.csv").is_file():
        with (run / "outputs/flows.csv").open(newline="") as stream:
            result_rows = list(csv.DictReader(stream))
    path_rows = []
    if (run / "outputs/paths.csv").is_file():
        with (run / "outputs/paths.csv").open(newline="") as stream:
            path_rows = list(csv.DictReader(stream))
    success = result.returncode == 0 and summary is not None and len(result_rows) == count
    success = success and len(path_rows) == 2 * values["paths"]
    success = success and all(row.get("validated") == "1" for row in path_rows)
    success = success and all(row.get("success") == "1" for row in result_rows)
    manifest["result"] = {"return_code": result.returncode, "success": success,
                          "summary": summary, "finished_at_utc": now()}
    save()
    if not success:
        print("Incomplete/failed; inspect", log_path, file=sys.stderr)
    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, IndexError, OSError) as error:
        sys.exit("Input/run error: " + str(error))
