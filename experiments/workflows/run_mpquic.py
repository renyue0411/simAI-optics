#!/usr/bin/env python3
"""Run the one-flow, two-path MPQUIC DCI input-file scenario.

This deliberately small adapter accepts the existing RDMA text formats. It
requires two direct gateway links (source ports 0/1) and one static flow. The
logical OCS schedule gates both directions of path 0 in the ns-3 example.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXP = PROJECT_ROOT / "experiments"
DEFAULT_CONFIG = EXP / "configs/mpquic-dci/minimal.conf"
DEFAULT_TOPO = EXP / "topologies/mpquic-dci/minimal-topo.txt"
DEFAULT_FLOW = EXP / "workloads/mpquic-dci/minimal-flow.txt"
DEFAULT_BINARY = (
    PROJECT_ROOT
    / "ns-3-alibabacloud/simulation/build/src/quic/examples/"
    / "ns3.36.1-mpquic-dci-default"
)


def data_lines(path: Path) -> List[str]:
    return [line.split("#", 1)[0].strip() for line in path.read_text().splitlines()
            if line.split("#", 1)[0].strip()]


def resolve_input(value: Path, default_dir: Path) -> Path:
    raw = value.expanduser()
    candidates = ([raw] if raw.is_absolute() else
                  [Path.cwd() / raw, PROJECT_ROOT / raw, default_dir / raw])
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    raise ValueError(f"Cannot find input {value}; checked: {', '.join(map(str, candidates))}")


def config_file(path: Path) -> Dict[str, str]:
    allowed = {
        "SIMULATOR_STOP_TIME_S", "MPQUIC_SCHEDULER", "MPQUIC_CC",
        "MPQUIC_BUFFER_BYTES", "MPQUIC_SEED", "OCS_SCHEDULE_FILE",
        "FCT_OUTPUT_FILE",
    }
    values: Dict[str, str] = {}
    for line in data_lines(path):
        fields = line.split()
        if len(fields) != 2 or fields[0] not in allowed or fields[0] in values:
            raise ValueError(f"Invalid or duplicate MPQUIC config line: {line}")
        values[fields[0]] = fields[1]
    missing = allowed - values.keys()
    if missing:
        raise ValueError(f"Missing config keys: {sorted(missing)}")
    return values


def one_flow(path: Path) -> Dict[str, Union[int, float]]:
    lines = data_lines(path)
    if not lines or lines[0] != "1" or len(lines) != 2:
        raise ValueError("First MPQUIC scenario needs exactly one six-column flow")
    fields = lines[1].split()
    if len(fields) != 6:
        raise ValueError("Flow entry must be: src dst pg dport bytes start_time")
    src, dst, pg, port, size = map(int, fields[:5])
    start = float(fields[5])
    if src == dst or min(src, dst, pg) < 0 or not 1 <= port <= 65535 or size <= 0 or start < 0:
        raise ValueError("Invalid flow endpoint, port, size or start time")
    return {"src": src, "dst": dst, "pg": pg, "port": port, "bytes": size, "start": start}


def delay_ms(value: str) -> float:
    for suffix, factor in (("ms", 1.0), ("us", 0.001), ("ns", 0.000001), ("s", 1000.0)):
        if value.endswith(suffix):
            result = float(value[:-len(suffix)]) * factor
            if result <= 0:
                break
            return result
    raise ValueError(f"Invalid positive link delay: {value}")


def topology(path: Path, flow: Dict[str, Union[int, float]]) -> dict:
    lines = data_lines(path)
    header = lines[0].split() if lines else []
    if len(header) != 8:
        raise ValueError("Topology needs the existing 8-field SimAI+OCS header")
    node_count, _gpu_per_server, n_nv, n_eps, n_tf, n_ocs, n_links = map(int, header[:7])
    role_count = n_nv + n_eps + n_tf + n_ocs
    roles: List[int] = []
    cursor = 1
    while len(roles) < role_count and cursor < len(lines):
        roles.extend(map(int, lines[cursor].split()))
        cursor += 1
    if len(roles) != role_count or n_ocs != 1 or n_nv or n_eps or n_tf:
        raise ValueError("First DCI topology needs one logical OCS and no packet-switch nodes")
    ocs_id = roles[-1]
    src, dst = int(flow["src"]), int(flow["dst"])
    if len({ocs_id, src, dst}) != 3 or max(ocs_id, src, dst) >= node_count:
        raise ValueError("Gateways and logical OCS must be distinct valid node IDs")
    links = lines[cursor:]
    if len(links) != n_links or n_links != 2:
        raise ValueError("First DCI topology needs exactly two direct gateway links")
    paths: Dict[int, Dict[str, Union[str, float]]] = {}
    for line in links:
        fields = line.split()
        if len(fields) != 7:
            raise ValueError(f"Link needs the existing seven columns: {line}")
        u, v, u_port, v_port, rate, one_way, error = fields
        if (int(u), int(v)) != (src, dst) or u_port != v_port or u_port not in {"0", "1"}:
            raise ValueError("Direct links must use gateway ports 0/0 and 1/1")
        if float(error) != 0 or u_port in paths:
            raise ValueError("First DCI case requires distinct zero-error links")
        paths[int(u_port)] = {"rate": rate, "rtt_ms": 2.0 * delay_ms(one_way)}
    if set(paths) != {0, 1}:
        raise ValueError("Topology needs path 0 optical and path 1 EPS")
    return {"ocs_id": ocs_id, "paths": paths, "node_count": node_count}


def key_values(fields: List[str]) -> Dict[str, str]:
    return dict(field.split("=", 1) for field in fields if "=" in field)


def gate_events(path: Path, ocs_id: int, src: int, dst: int, stop_s: float) -> List[Tuple[float, int]]:
    schedule: Optional[dict] = None
    selected = False
    for line in data_lines(path):
        fields = line.split()
        if fields[0] == "SCHEDULE":
            kv = key_values(fields[1:])
            selected = str(ocs_id) in kv.get("OCS", "").split(",")
            if selected:
                if schedule is not None:
                    raise ValueError(f"OCS {ocs_id} occurs in two schedule blocks")
                schedule = {
                    "epoch_us": int(kv["EPOCH_US"]),
                    "slice_us": int(kv["SLICE_US"]),
                    "switch_us": int(kv["SWITCH_US"]),
                    "slices": {},
                }
        elif selected and fields[0] == "SLICE":
            if schedule is None:
                raise ValueError("SLICE without SCHEDULE")
            kv = key_values(fields[2:])
            pairs = {frozenset(map(int, item.split("-")))
                     for item in kv["PAIRS"].split(",") if item}
            index = int(fields[1])
            if index in schedule["slices"]:
                raise ValueError(f"Duplicate slice {index}")
            schedule["slices"][index] = frozenset({src, dst}) in pairs
        elif selected and fields[0] != "ROUND_ROBIN":
            raise ValueError(f"Unsupported schedule statement: {line}")
    if schedule is None:
        raise ValueError(f"No schedule for logical OCS {ocs_id}")
    epoch = schedule["epoch_us"]
    duration = schedule["slice_us"]
    switch = schedule["switch_us"]
    slices = schedule["slices"]
    if epoch < 0 or duration <= 0 or switch < 0 or switch >= duration or not slices:
        raise ValueError("Invalid epoch/slice/switch interval")
    if set(slices) != set(range(len(slices))):
        raise ValueError("Slice IDs must be contiguous from zero")
    stop_us = int(stop_s * 1_000_000)
    if (stop_us - epoch) // duration > 1_000_000:
        raise ValueError("Too many gate events for this first scenario")
    events: List[Tuple[float, int]] = []
    if epoch > 0:
        events.append((0.0, 0))
    for n in range(max(0, (stop_us - epoch) // duration + 1)):
        start = epoch + n * duration
        if start > stop_us:
            break
        up = int(slices[n % len(slices)])
        events.append((start / 1_000_000, up))
        if switch:
            cut = start + duration - switch
            if cut < stop_us:
                events.append((cut / 1_000_000, 0))
    if not events:
        raise ValueError("Schedule has no events before simulation ends")
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="New directory name under experiments/runs")
    parser.add_argument("--config-template", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--topology", type=Path, default=DEFAULT_TOPO)
    parser.add_argument("--flow-file", type=Path, default=DEFAULT_FLOW)
    parser.add_argument("--simulator", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--dry-run", action="store_true", help="Prepare and inspect without simulation")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.name):
        parser.error("name must use letters, numbers, dot, underscore or hyphen")
    try:
        config_path = resolve_input(args.config_template, EXP / "configs/mpquic-dci")
        topo_path = resolve_input(args.topology, EXP / "topologies/mpquic-dci")
        flow_path = resolve_input(args.flow_file, EXP / "workloads/mpquic-dci")
        cfg = config_file(config_path)
        flow = one_flow(flow_path)
        topo = topology(topo_path, flow)
        schedule_path = Path(cfg["OCS_SCHEDULE_FILE"])
        if not schedule_path.is_absolute():
            schedule_path = config_path.parent / schedule_path
        schedule_path = schedule_path.resolve()
        if not schedule_path.is_file():
            raise ValueError(f"Missing OCS schedule: {schedule_path}")
        stop_s = float(cfg["SIMULATOR_STOP_TIME_S"])
        if stop_s <= float(flow["start"]):
            raise ValueError("Stop time must exceed flow start time")
        events = gate_events(schedule_path, topo["ocs_id"], int(flow["src"]), int(flow["dst"]), stop_s)
        scheduler = int(cfg["MPQUIC_SCHEDULER"])
        cc = int(cfg["MPQUIC_CC"])
        buffer_bytes = int(cfg["MPQUIC_BUFFER_BYTES"])
        seed = int(cfg["MPQUIC_SEED"])
        if scheduler not in range(5) or cc not in (0, 1) or buffer_bytes < 4096 or seed <= 0:
            raise ValueError("Invalid scheduler, CC, buffer size or seed")
        if cfg["FCT_OUTPUT_FILE"] != "__OUTPUT_DIR__/flows.csv":
            raise ValueError("First scenario expects FCT_OUTPUT_FILE __OUTPUT_DIR__/flows.csv")
    except (ValueError, OSError, KeyError, IndexError) as exc:
        parser.error(str(exc))

    simulator = args.simulator.expanduser().resolve()
    if not args.dry_run and not simulator.is_file():
        parser.error(f"Build the MPQUIC DCI example first: {simulator}")
    run_dir = EXP / "runs" / args.name
    if run_dir.exists():
        parser.error(f"Run already exists: {run_dir}; choose a new name")
    inputs = run_dir / "inputs"
    logs = run_dir / "logs"
    outputs = run_dir / "outputs"
    for path in (inputs, logs, outputs):
        path.mkdir(parents=True, exist_ok=False)
    for source in (config_path, topo_path, flow_path, schedule_path):
        shutil.copy2(source, inputs / source.name)
    rendered = (config_path.read_text()
                .replace("__OUTPUT_DIR__", str(outputs.resolve()))
                .replace(cfg["OCS_SCHEDULE_FILE"], str((inputs / schedule_path.name).resolve())))
    (inputs / "MPQUIC.conf").write_text(rendered)
    gate_path = inputs / "optical-gate.txt"
    gate_path.write_text("".join(f"{at:.9f} {up}\n" for at, up in events))
    paths = topo["paths"]
    command = [
        str(simulator), "--paths=2",
        f"--rate0={paths[0]['rate']}", f"--rate1={paths[1]['rate']}",
        f"--rtt0Ms={paths[0]['rtt_ms']}", f"--rtt1Ms={paths[1]['rtt_ms']}",
        f"--bytes={flow['bytes']}", f"--flowStart={flow['start']}",
        f"--flowId=0", f"--port={flow['port']}",
        f"--stop={stop_s}", f"--bufferBytes={buffer_bytes}",
        f"--scheduler={scheduler}", f"--cc={cc}", f"--seed={seed}",
        f"--gateFile={gate_path.resolve()}",
        f"--resultFile={(outputs / 'flows.csv').resolve()}",
    ]
    manifest = {
        "name": args.name,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": "two direct gateway links, logical OCS gate on path 0",
        "inputs": {"config": str(config_path), "topology": str(topo_path),
                   "schedule": str(schedule_path), "flow": str(flow_path)},
        "command": command,
        "gate_events": len(events),
        "dry_run": args.dry_run,
    }
    manifest_path = outputs / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Run dir: {run_dir}")
    print(f"Command: {shlex.join(command)}")
    if args.dry_run:
        print("Dry run: files prepared; simulator not started.")
        return 0
    log_path = logs / "simulator.log"
    with log_path.open("w") as log:
        result = subprocess.run(command, cwd=outputs, stdout=log, stderr=subprocess.STDOUT,
                                check=False)
    with log_path.open(errors="replace") as log:
        summary = [line.rstrip() for line in log
                   if line.startswith("DCI_PATH ") or line.startswith("DCI_RESULT ")]
    for line in summary:
        print(line)
    if not summary:
        print(f"No result summary; inspect {log_path}", file=sys.stderr)
    manifest["result"] = {"return_code": result.returncode,
                          "finished_at_utc": datetime.now(timezone.utc).isoformat()}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
