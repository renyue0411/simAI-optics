#!/usr/bin/env python3
"""Local experiment dashboard server for SimAI/NS-3 runs.

Usage from repository root:
    python3 dashboard/run_serve.py

Optional:
    python3 dashboard/run_serve.py --runs-dir experiments/runs --port 8080 --open

The server uses only Python's standard library. It scans each directory under
experiments/runs and exposes parsed experiment data through a small JSON API.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import mimetypes
import os
import re
import threading
import time
import urllib.parse
import webbrowser
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

DASHBOARD_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_DIR.parent
DEFAULT_RUNS_DIR = PROJECT_ROOT / "experiments" / "runs"

KEY_VALUE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")
ADDRESS_RE = re.compile(r"^0b([0-9a-fA-F]{4})([0-9a-fA-F]{2})$")


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.2f} TiB"


def read_text(path: Path, max_bytes: int | None = None) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        if max_bytes is None:
            return path.read_text(encoding="utf-8", errors="replace")
        with path.open("rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_text(path))
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def get_manifest_path(run_dir: Path) -> Path:
    preferred = run_dir / "outputs" / "manifest.json"
    return preferred if preferred.exists() else run_dir / "manifest.json"


def get_config_path(run_dir: Path) -> Path:
    preferred = run_dir / "inputs" / "SimAI.conf"
    return preferred if preferred.exists() else run_dir / "SimAI.conf"


def manifest_snapshot(manifest: dict[str, Any], key: str) -> str | None:
    inputs = manifest.get("inputs")
    if isinstance(inputs, dict):
        entry = inputs.get(key)
        if isinstance(entry, dict):
            value = entry.get("snapshot")
            if isinstance(value, str):
                return value
    legacy = manifest.get(key)
    return legacy if isinstance(legacy, str) else None


def parse_conf(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        values[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    return values


def resolve_reference(raw_path: str | None, run_dir: Path, experiments_root: Path) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if candidate.exists():
        return candidate
    name = candidate.name
    candidates = [
        run_dir / name,
        run_dir / "inputs" / name,
        run_dir / "outputs" / name,
        experiments_root / "topologies" / name,
        experiments_root / "workloads" / name,
        experiments_root / "configs" / name,
    ]
    for item in candidates:
        if item.exists():
            return item
    return None


def decode_node_address(value: str) -> int | None:
    match = ADDRESS_RE.match(value)
    if match:
        return int(match.group(1), 16)
    # Common fallback: dotted IPv4 where the second/third octets encode node id.
    try:
        parts = [int(part) for part in value.split(".")]
        if len(parts) == 4:
            return (parts[1] << 8) | parts[2]
    except ValueError:
        pass
    return safe_int(value)


def parse_fct(path: Path) -> dict[str, Any]:
    flows: list[dict[str, Any]] = []
    if not path.exists():
        return {"flows": [], "summary": {}, "source": None}

    for index, raw in enumerate(read_text(path).splitlines()):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        src_raw, dst_raw = parts[0], parts[1]
        sport = safe_int(parts[2], 0) or 0
        dport = safe_int(parts[3], 0) or 0
        size_bytes = safe_int(parts[4], 0) or 0
        start_ns = safe_int(parts[5], 0) or 0
        fct_ns = safe_int(parts[6], 0) or 0
        base_rtt_ns = safe_int(parts[7], None) if len(parts) > 7 else None
        end_ns = start_ns + max(fct_ns, 0)
        avg_gbps = (size_bytes * 8.0 / fct_ns) if fct_ns > 0 else 0.0
        slowdown = (fct_ns / base_rtt_ns) if base_rtt_ns and base_rtt_ns > 0 else None
        src = decode_node_address(src_raw)
        dst = decode_node_address(dst_raw)
        flow_id = f"{src if src is not None else src_raw}→{dst if dst is not None else dst_raw}:{sport}"
        flows.append(
            {
                "index": index,
                "id": flow_id,
                "src": src,
                "dst": dst,
                "src_raw": src_raw,
                "dst_raw": dst_raw,
                "sport": sport,
                "dport": dport,
                "size_bytes": size_bytes,
                "size_human": human_bytes(size_bytes),
                "start_ns": start_ns,
                "end_ns": end_ns,
                "fct_ns": fct_ns,
                "start_us": start_ns / 1000.0,
                "end_us": end_ns / 1000.0,
                "fct_us": fct_ns / 1000.0,
                "base_rtt_ns": base_rtt_ns,
                "avg_gbps": avg_gbps,
                "slowdown": slowdown,
            }
        )

    fcts = [item["fct_ns"] for item in flows]
    sizes = [item["size_bytes"] for item in flows]
    sports = sorted({item["sport"] for item in flows})
    summary = {
        "count": len(flows),
        "total_bytes": sum(sizes),
        "total_bytes_human": human_bytes(sum(sizes)),
        "min_fct_us": min(fcts) / 1000.0 if fcts else None,
        "max_fct_us": max(fcts) / 1000.0 if fcts else None,
        "avg_fct_us": (sum(fcts) / len(fcts) / 1000.0) if fcts else None,
        "sports": sports,
        "timeline_start_us": min((item["start_us"] for item in flows), default=0),
        "timeline_end_us": max((item["end_us"] for item in flows), default=0),
    }
    return {"flows": flows, "summary": summary, "source": str(path)}


def synthesize_flows_from_rx(fct: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if fct.get("flows") or not rows:
        return fct

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        src = safe_int(row.get("src"))
        dst = safe_int(row.get("dst"))
        sport = safe_int(row.get("sport"), 0) or 0
        dport = safe_int(row.get("dport"), 0) or 0
        pg = safe_int(row.get("pg"), 0) or 0
        start_ns = safe_int(row.get("t"), 0) or 0
        bucket_ns = safe_int(row.get("bucket_ns"), 100000) or 100000
        bytes_value = safe_int(row.get("bytes"), 0) or 0
        if src is None or dst is None or bytes_value <= 0:
            continue
        flow_id = f"{src}→{dst}:{sport}"
        grouped[flow_id].append(
            {
                "src": src,
                "dst": dst,
                "sport": sport,
                "dport": dport,
                "pg": pg,
                "start_ns": start_ns,
                "end_ns": start_ns + bucket_ns,
                "bytes": bytes_value,
            }
        )

    flows: list[dict[str, Any]] = []
    for index, (flow_id, samples) in enumerate(sorted(grouped.items())):
        start_ns = min(item["start_ns"] for item in samples)
        end_ns = max(item["end_ns"] for item in samples)
        size_bytes = sum(item["bytes"] for item in samples)
        fct_ns = max(0, end_ns - start_ns)
        first = samples[0]
        flows.append(
            {
                "index": index,
                "id": flow_id,
                "src": first["src"],
                "dst": first["dst"],
                "src_raw": str(first["src"]),
                "dst_raw": str(first["dst"]),
                "sport": first["sport"],
                "dport": first["dport"],
                "size_bytes": size_bytes,
                "size_human": human_bytes(size_bytes),
                "start_ns": start_ns,
                "end_ns": end_ns,
                "fct_ns": fct_ns,
                "start_us": start_ns / 1000.0,
                "end_us": end_ns / 1000.0,
                "fct_us": fct_ns / 1000.0,
                "base_rtt_ns": None,
                "avg_gbps": size_bytes * 8.0 / fct_ns if fct_ns > 0 else 0.0,
                "slowdown": None,
                "completion_source": "FLOW_RX_BYTES",
            }
        )

    fcts = [item["fct_ns"] for item in flows]
    sizes = [item["size_bytes"] for item in flows]
    return {
        "flows": flows,
        "summary": {
            "count": len(flows),
            "total_bytes": sum(sizes),
            "total_bytes_human": human_bytes(sum(sizes)),
            "min_fct_us": min(fcts) / 1000.0 if fcts else None,
            "max_fct_us": max(fcts) / 1000.0 if fcts else None,
            "avg_fct_us": (sum(fcts) / len(fcts) / 1000.0) if fcts else None,
            "sports": sorted({item["sport"] for item in flows}),
            "timeline_start_us": min((item["start_us"] for item in flows), default=0),
            "timeline_end_us": max((item["end_us"] for item in flows), default=0),
            "completion_source": "FLOW_RX_BYTES",
        },
        "source": "logs/simulator.log:[FLOW_RX_BYTES]",
    }


def parse_end_to_end(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"rows": [], "summary": {}}
    if not path.exists():
        return result
    lines = [line for line in read_text(path).splitlines() if line.strip()]
    if not lines:
        return result

    # The file contains multiple CSV sections. Preserve all rows and extract
    # the first one-line job summary plus the explicit total-time row.
    for row in csv.reader(lines):
        cleaned = [cell.strip() for cell in row]
        result["rows"].append(cleaned)
        if cleaned and cleaned[0] == "total exposed comm":
            for i in range(0, len(cleaned) - 1, 2):
                result["summary"][cleaned[i]] = safe_float(cleaned[i + 1], cleaned[i + 1])
        elif cleaned and cleaned[0] not in {"File name", "layer_name", "SUM"} and len(cleaned) >= 10:
            if cleaned[0] and cleaned[0] != "embedding_layer" and "job" not in result["summary"]:
                result["summary"]["job"] = cleaned[0]
                result["summary"]["total_time_us"] = safe_float(cleaned[-1])
                result["summary"]["total_exposed_comm"] = cleaned[8]
                result["summary"]["total_comp"] = cleaned[7]
    return result


def parse_key_values(line: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, raw in KEY_VALUE_RE.findall(line):
        if re.fullmatch(r"[-+]?\d+", raw):
            values[key] = int(raw)
        elif re.fullmatch(r"[-+]?\d*\.\d+", raw):
            values[key] = float(raw)
        else:
            values[key] = raw
    return values


def parse_log(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tags": {},
        "gate_mode": None,
        "multiplane": {},
        "time_hash": {},
        "gate_tables": [],
        "wr_summaries": [],
        "ocs_stats": [],
        "retransmission": [],
        "flow_rx": [],
        "injection_windows": [],
        "completion": {},
        "warnings": [],
    }
    if not path.exists():
        result["warnings"].append("simulator.log 不存在")
        return result

    tag_counts: dict[str, int] = defaultdict(int)
    latest_ocs: dict[int, dict[str, Any]] = {}
    retrans_lines: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            tag_match = re.match(r"^\[([^]]+)\]", line)
            if tag_match:
                tag_counts[tag_match.group(1)] += 1

            if line.startswith("[RDMA GATE MODE]"):
                result["gate_mode"] = parse_key_values(line)
            elif line.startswith("[MULTIPLANE ENABLED]"):
                result["multiplane"] = parse_key_values(line)
            elif line.startswith("[TIME HASH SUMMARY]"):
                result["time_hash"] = parse_key_values(line)
            elif "GATE TABLE INSTALLED]" in line:
                values = parse_key_values(line)
                values["layer"] = "userspace" if "USERSPACE" in line else "rnic"
                result["gate_tables"].append(values)
            elif line.startswith("[USERSPACE WR SUMMARY]"):
                result["wr_summaries"].append(parse_key_values(line))
            elif line.startswith("[FLOW_RX_BYTES]"):
                result["flow_rx"].append(parse_key_values(line))
            elif line.startswith("[INJECTION WINDOW]"):
                result["injection_windows"].append(parse_key_values(line))
            elif line.startswith("[OCS STATS]"):
                values = parse_key_values(line)
                node = safe_int(values.get("node"), -1)
                if node is not None:
                    latest_ocs[node] = values
            elif line.startswith("[RNIC RETRANSMISSION STATS]"):
                values = parse_key_values(line)
                retrans_lines.append(values)

            match = re.search(r"all passes finished at time:\s*(\d+)", line)
            if match:
                result["completion"]["all_passes_finished_ns"] = int(match.group(1))
                result["completion"]["all_passes_finished_ms"] = int(match.group(1)) / 1e6
            match = re.search(r"pass:\s*(\d+)\s+finished at time:\s*(\d+)", line)
            if match:
                result["completion"]["pass"] = int(match.group(1))
                result["completion"]["pass_finished_ns"] = int(match.group(2))
            match = re.search(r"Total streams injected:\s*(\d+)", line)
            if match:
                result["completion"]["streams_injected"] = int(match.group(1))
            match = re.search(r"Total streams finished:\s*(\d+)", line)
            if match:
                result["completion"]["streams_finished"] = int(match.group(1))
            match = re.search(r"Percentage of finished streams:\s*([0-9.]+)", line)
            if match:
                result["completion"]["finished_percent"] = float(match.group(1))

    result["tags"] = dict(sorted(tag_counts.items()))
    result["ocs_stats"] = [latest_ocs[key] for key in sorted(latest_ocs)]
    result["retransmission"] = retrans_lines
    return result


def parse_schedule(path: Path) -> dict[str, Any]:
    configs: dict[int, dict[str, Any]] = {}
    entries: dict[int, dict[int, list[list[int]]]] = defaultdict(lambda: defaultdict(list))
    if not path.exists():
        return {"configs": [], "entries": {}, "source": None}

    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "CONFIG" and len(parts) >= 6:
            node = int(parts[1])
            configs[node] = {
                "ocs": node,
                "epoch_start_us": int(parts[2]),
                "slice_duration_us": int(parts[3]),
                "switching_time_us": int(parts[4]),
                "num_slices": int(parts[5]),
            }
        elif len(parts) >= 4 and all(re.fullmatch(r"\d+", p) for p in parts[:4]):
            node, slice_id, port_a, port_b = map(int, parts[:4])
            entries[node][slice_id].append([port_a, port_b])

    return {
        "configs": [configs[key] for key in sorted(configs)],
        "entries": {
            str(node): {str(slot): pairs for slot, pairs in sorted(slots.items())}
            for node, slots in sorted(entries.items())
        },
        "source": str(path),
    }


def parse_port_bindings(path: Path) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    if not path.exists():
        return bindings
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        values = [safe_int(item) for item in parts[:7]]
        if any(item is None for item in values):
            continue
        bindings.append(
            {
                "node": values[0],
                "logical_port": values[1],
                "if_index": values[2],
                "peer_node": values[3],
                "peer_logical_port": values[4],
                "delay_ns": values[5],
                "bandwidth_bps": values[6],
            }
        )
    return bindings


def parse_topology_file(path: Path) -> dict[str, Any]:
    lines = []
    for raw in read_text(path).splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    if len(lines) < 3:
        return {}

    header = lines[0].split()
    if len(header) < 6:
        return {}
    nums = [safe_int(item) for item in header[:6]]
    if any(item is None for item in nums):
        return {}

    total_nodes, nvs_count, gpus_per_server, eps_count, ocs_count, link_count = nums
    gpu_type = header[6] if len(header) > 6 else "Unknown"
    non_ocs_ids = [int(item) for item in lines[1].split()]
    ocs_ids = [int(item) for item in lines[2].split()]
    nvs_ids = non_ocs_ids[:nvs_count]
    eps_ids = non_ocs_ids[nvs_count : nvs_count + eps_count]
    gpu_count = nvs_count * gpus_per_server
    gpu_ids = list(range(gpu_count))

    links: list[dict[str, Any]] = []
    for raw in lines[3:]:
        parts = raw.split()
        if len(parts) < 6:
            continue
        src = safe_int(parts[0])
        dst = safe_int(parts[1])
        if src is None or dst is None:
            continue
        links.append(
            {
                "source": src,
                "target": dst,
                # Preserve tokens such as <nic_id>-<plane_id>, e.g. 1-0 and 1-1.
                "source_port": parts[2],
                "target_port": parts[3],
                "bandwidth": parts[4],
                "delay": parts[5],
                "error": safe_float(parts[6]) if len(parts) > 6 else None,
            }
        )

    servers = []
    for server_id, nvs in enumerate(nvs_ids):
        begin = server_id * gpus_per_server
        servers.append(
            {
                "id": server_id,
                "nvswitch": nvs,
                "gpus": gpu_ids[begin : begin + gpus_per_server],
            }
        )

    return {
        "source": str(path),
        "counts": {
            "total_nodes": total_nodes,
            "gpu": gpu_count,
            "server": nvs_count,
            "nvswitch": nvs_count,
            "eps": eps_count,
            "ocs": ocs_count,
            "links": link_count,
        },
        "gpu_type": gpu_type,
        "ids": {"gpu": gpu_ids, "nvswitch": nvs_ids, "eps": eps_ids, "ocs": ocs_ids},
        "servers": servers,
        "links": links,
    }


def synthesize_topology(bindings: list[dict[str, Any]], schedule: dict[str, Any], fct: dict[str, Any]) -> dict[str, Any]:
    ocs_ids = sorted({item["ocs"] for item in schedule.get("configs", [])})
    ocs_set = set(ocs_ids)
    gpu_ids = sorted(
        {
            item["node"]
            for item in bindings
            if item["logical_port"] is not None and item["logical_port"] >= 65536
        }
        | {flow["src"] for flow in fct.get("flows", []) if flow.get("src") is not None}
        | {flow["dst"] for flow in fct.get("flows", []) if flow.get("dst") is not None}
    )
    eps_ids = sorted(
        {
            item["node"]
            for item in bindings
            if item["node"] not in ocs_set
            and item["node"] not in gpu_ids
            and item["logical_port"] is not None
            and item["logical_port"] < 65536
        }
    )

    links: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int | None, int | None]] = set()
    for item in bindings:
        source, target = item["node"], item["peer_node"]
        key = (
            min(source, target),
            max(source, target),
            item["logical_port"] if source <= target else item["peer_logical_port"],
            item["peer_logical_port"] if source <= target else item["logical_port"],
        )
        if key in seen:
            continue
        seen.add(key)
        links.append(
            {
                "source": source,
                "target": target,
                "source_port": item["logical_port"],
                "target_port": item["peer_logical_port"],
                "bandwidth": f"{item['bandwidth_bps'] / 1e9:g}Gbps",
                "delay": f"{item['delay_ns']}ns",
                "error": None,
            }
        )

    return {
        "source": "outputs/ocs_port_bindings_debug.txt (partial reconstruction)",
        "counts": {
            "total_nodes": len(set(gpu_ids) | set(eps_ids) | set(ocs_ids)),
            "gpu": len(gpu_ids),
            "server": None,
            "nvswitch": None,
            "eps": len(eps_ids),
            "ocs": len(ocs_ids),
            "links": len(links),
        },
        "gpu_type": "Unknown",
        "ids": {"gpu": gpu_ids, "nvswitch": [], "eps": eps_ids, "ocs": ocs_ids},
        "servers": [],
        "links": links,
        "partial": True,
    }


def classify_topology(topology: dict[str, Any]) -> dict[str, Any]:
    ids = topology.get("ids", {})
    gpu_set = set(ids.get("gpu", []))
    nvs_set = set(ids.get("nvswitch", []))
    eps_set = set(ids.get("eps", []))
    ocs_set = set(ids.get("ocs", []))
    adjacency: dict[int, set[int]] = defaultdict(set)
    for link in topology.get("links", []):
        source, target = link.get("source"), link.get("target")
        if source is None or target is None:
            continue
        adjacency[source].add(target)
        adjacency[target].add(source)

    nodes: list[dict[str, Any]] = []
    server_by_gpu: dict[int, int] = {}
    for server in topology.get("servers", []):
        for gpu in server.get("gpus", []):
            server_by_gpu[gpu] = server["id"]

    for node in sorted(gpu_set | nvs_set | eps_set | ocs_set):
        if node in gpu_set:
            node_type = "gpu"
        elif node in nvs_set:
            node_type = "nvswitch"
        elif node in eps_set:
            node_type = "eps"
        else:
            peers = adjacency.get(node, set())
            node_type = "leaf_ocs" if peers & gpu_set else "core_ocs"
        nodes.append(
            {
                "id": node,
                "label": f"GPU {node}" if node_type == "gpu" else f"{node_type.replace('_', ' ').upper()} {node}",
                "type": node_type,
                "server": server_by_gpu.get(node),
                "degree": len(adjacency.get(node, set())),
            }
        )
    topology["nodes"] = nodes
    return topology


def parse_exact_windows(run_dir: Path, log_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if log_rows:
        rows = []
        ordered = [
            "mode", "layer", "node", "rnic_port", "plane", "epoch_ns",
            "start_ns", "end_ns", "period_ns", "destinations",
        ]
        for item in log_rows:
            row = {key: item.get(key) for key in ordered if key in item}
            for key, value in item.items():
                if key not in row:
                    row[key] = value
            rows.append(row)
        return {
            "exact": True,
            "source": str(run_dir / "logs" / "simulator.log"),
            "rows": rows,
        }

    candidates = [
        run_dir / "outputs" / "injection_windows.txt",
        run_dir / "outputs" / "injection_window_table.txt",
        run_dir / "outputs" / "rnic_injection_windows.txt",
        run_dir / "outputs" / "userspace_injection_windows.txt",
    ]
    for path in candidates:
        if not path.exists():
            continue
        rows: list[dict[str, Any]] = []
        header: list[str] | None = None
        for raw in read_text(path).splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                possible = line.lstrip("#").strip().split()
                if possible and any(any(ch.isalpha() for ch in item) for item in possible):
                    header = possible
                continue
            parts = line.split()
            if header and len(header) == len(parts):
                rows.append(dict(zip(header, parts)))
            else:
                rows.append({f"field_{i + 1}": value for i, value in enumerate(parts)})
        return {"exact": True, "source": str(path), "rows": rows}
    return {"exact": False, "source": None, "rows": []}


def build_flow_throughput(
    rows: list[dict[str, Any]],
    fct: dict[str, Any],
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    bucket_ns_values: list[int] = []

    for row in rows:
        src = safe_int(row.get("src"))
        dst = safe_int(row.get("dst"))
        sport = safe_int(row.get("sport"), 0) or 0
        dport = safe_int(row.get("dport"), 0) or 0
        pg = safe_int(row.get("pg"), 0) or 0
        start_ns = safe_int(row.get("t"), 0) or 0
        bucket_ns = safe_int(row.get("bucket_ns"), 100000) or 100000
        bytes_value = safe_int(row.get("bytes"), 0) or 0
        if src is None or dst is None or bytes_value <= 0 or bucket_ns <= 0:
            continue
        bucket_ns_values.append(bucket_ns)
        flow_id = f"{src}→{dst}:{sport}"
        grouped[flow_id].append(
            {
                "start_us": start_ns / 1000.0,
                "end_us": (start_ns + bucket_ns) / 1000.0,
                "gbps": bytes_value * 8.0 / bucket_ns,
                "bytes": bytes_value,
                "src": src,
                "dst": dst,
                "sport": sport,
                "dport": dport,
                "pg": pg,
            }
        )

    if grouped:
        series = []
        for flow_id, samples in grouped.items():
            samples.sort(key=lambda item: item["start_us"])
            total_bytes = sum(item["bytes"] for item in samples)
            total_duration_ns = sum(
                max(0.0, item["end_us"] - item["start_us"]) * 1000.0
                for item in samples
            )
            series.append(
                {
                    "id": flow_id,
                    "samples": samples,
                    "max_gbps": max(item["gbps"] for item in samples),
                    "active_avg_gbps": (
                        total_bytes * 8.0 / total_duration_ns
                        if total_duration_ns > 0
                        else 0.0
                    ),
                }
            )
        series.sort(key=lambda item: item["id"])
        return {
            "kind": "flow_rx_log",
            "source": "logs/simulator.log:[FLOW_RX_BYTES]",
            "bucket_ns": min(bucket_ns_values) if bucket_ns_values else 100000,
            "series": series,
            # "note": "曲线来自 simulator.log 中按时间桶统计的 [FLOW_RX_BYTES] 接收有效字节。",
        }

    return {
        "kind": "fct_interval_average",
        "source": fct.get("source"),
        "bucket_ns": None,
        "series": [],
        # "note": "日志中没有 [FLOW_RX_BYTES]；曲线退化为 size/FCT 计算的完成区间平均吞吐率。",
    }


def aggregate_wr_summaries(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_node_port: dict[tuple[Any, Any], dict[str, Any]] = {}
    totals = {
        "flows": len(rows),
        "posts": 0,
        "bytes": 0,
        "safe_budget_limited_flows": 0,
    }
    for row in rows:
        totals["posts"] += safe_int(row.get("posts"), 0) or 0
        totals["bytes"] += safe_int(row.get("total_bytes"), 0) or 0
        totals["safe_budget_limited_flows"] += 1 if safe_int(row.get("safe_budget_limited"), 0) else 0
    totals["bytes_human"] = human_bytes(totals["bytes"])
    return {"totals": totals, "by_node_port": list(by_node_port.values())}


def config_card_data(conf: dict[str, str], topology: dict[str, Any], manifest: dict[str, Any], log: dict[str, Any]) -> list[dict[str, Any]]:
    counts = topology.get("counts", {})
    rdma_mode = safe_int(conf.get("RDMA_TRANSPORT_MODE"), 0)
    scheduler = safe_int(conf.get("SCALE_OUT_PLANE_SCHEDULER"), 0)
    rdma_labels = {0: "Default RDMA", 1: "RNIC", 2: "User-space"}
    scheduler_labels = {0: "Hash", 1: "Round Robin", 2: "Least-QP", 3: "Time Hash"}
    cards = [
        {"label": "GPU", "value": counts.get("gpu")},
        {"label": "Server", "value": counts.get("server")},
        {"label": "NVSwitch", "value": counts.get("nvswitch")},
        {"label": "EPS", "value": counts.get("eps")},
        {"label": "OCS", "value": counts.get("ocs")},
        {"label": "GPU Type", "value": topology.get("gpu_type")},
        {"label": "RDMA Mode", "value": f"{rdma_mode} · {rdma_labels.get(rdma_mode, 'Unknown')}"},
        {"label": "CC Mode", "value": conf.get("CC_MODE", "—")},
        {"label": "Plane Scheduler", "value": f"{scheduler} · {scheduler_labels.get(scheduler, 'Unknown')}"},
        {"label": "ACK Policy", "value": log.get("multiplane", {}).get("ack_policy", "—")},
        {"label": "OCS Schedule", "value": "Enabled" if safe_int(conf.get("OCS_SCHEDULE_ENABLE"), 0) else "Disabled"},
        {"label": "Threads", "value": manifest.get("threads", "—")},
    ]
    return cards


def parse_experiment(run_dir: Path, experiments_root: Path) -> dict[str, Any]:
    manifest_path = get_manifest_path(run_dir)
    config_path = get_config_path(run_dir)
    manifest = load_json(manifest_path)
    conf = parse_conf(config_path)
    log = parse_log(run_dir / "logs" / "simulator.log")

    fct_path = None
    configured_fct = resolve_reference(conf.get("FCT_OUTPUT_FILE"), run_dir, experiments_root)
    if configured_fct and configured_fct.exists():
        fct_path = configured_fct
    else:
        matches = sorted((run_dir / "outputs").glob("*fct*.txt")) if (run_dir / "outputs").exists() else []
        fct_path = matches[0] if matches else run_dir / "outputs" / "fct.txt"
    fct = parse_fct(fct_path)
    fct = synthesize_flows_from_rx(fct, log.get("flow_rx", []))

    schedule_path = run_dir / "outputs" / "ocs_schedule_expanded.txt"
    if not schedule_path.exists():
        schedule_snapshot = manifest_snapshot(manifest, "ocs_schedule")
        referenced = resolve_reference(
            schedule_snapshot or conf.get("OCS_SCHEDULE_FILE"),
            run_dir,
            experiments_root,
        )
        if referenced:
            schedule_path = referenced
    schedule = parse_schedule(schedule_path)

    bindings_path = run_dir / "outputs" / "ocs_port_bindings_debug.txt"
    bindings = parse_port_bindings(bindings_path)

    topology_path = resolve_reference(
        manifest_snapshot(manifest, "topology"),
        run_dir,
        experiments_root,
    )
    topology = parse_topology_file(topology_path) if topology_path else {}
    if not topology:
        topology = synthesize_topology(bindings, schedule, fct)
    topology = classify_topology(topology)

    end_to_end = parse_end_to_end(run_dir / "outputs" / "ncclFlowModel_EndToEnd.csv")
    exact_windows = parse_exact_windows(run_dir, log.get("injection_windows", []))
    injection = {
        **exact_windows,
        "mode": safe_int(conf.get("RDMA_TRANSPORT_MODE"), 0),
        "gate_tables": log.get("gate_tables", []),
        "wr": aggregate_wr_summaries(log.get("wr_summaries", [])),
    }

    warnings: list[str] = []

    if topology.get("partial"):
        warnings.append("Topology snapshot missing; Server/NVSwitch layout unavailable.")

    if not exact_windows["exact"]:
        warnings.append("Exact Injection Window data unavailable.")

    if safe_int(conf.get("OCS_STATS_ENABLE"), 0) == 0:
        warnings.append("OCS statistics disabled.")
    elif not log.get("ocs_stats"):
        warnings.append("No [OCS STATS] entries found.")

    if not log.get("retransmission"):
        warnings.append("RNIC retransmission data unavailable.")

        throughput = build_flow_throughput(log.get("flow_rx", []), fct)

        result = manifest.get("result", {}) if isinstance(manifest.get("result"), dict) else {}
        result_summary = {
            "status": result.get("status", "unknown"),
            "return_code": result.get("return_code"),
            "wall_duration_seconds": result.get("duration_seconds"),
            "finished_at_utc": result.get("finished_at_utc"),
            **log.get("completion", {}),
            **end_to_end.get("summary", {}),
        }

    return {
        "name": run_dir.name,
        "path": str(run_dir),
        "manifest": manifest,
        "config": conf,
        "config_cards": config_card_data(conf, topology, manifest, log),
        "result": result_summary,
        "topology": topology,
        "port_bindings": bindings,
        "flows": fct,
        "throughput": throughput,
        "schedule": schedule,
        "injection": injection,
        "ocs_stats": log.get("ocs_stats", []),
        "retransmission": log.get("retransmission", []),
        "log_tags": log.get("tags", {}),
        "warnings": warnings,
        "sources": {
            "manifest": str(manifest_path),
            "config": str(config_path),
            "log": str(run_dir / "logs" / "simulator.log"),
            "fct": fct.get("source"),
            "topology": topology.get("source"),
            "schedule": schedule.get("source"),
        },
    }


def experiment_summary(run_dir: Path) -> dict[str, Any]:
    manifest_path = get_manifest_path(run_dir)
    config_path = get_config_path(run_dir)
    manifest = load_json(manifest_path)
    result = manifest.get("result", {}) if isinstance(manifest.get("result"), dict) else {}
    timestamp = manifest.get("started_at_utc")
    if not timestamp:
        try:
            timestamp = datetime.fromtimestamp(run_dir.stat().st_mtime).astimezone().isoformat()
        except OSError:
            timestamp = None
    return {
        "name": run_dir.name,
        "status": result.get("status", "unknown"),
        "return_code": result.get("return_code"),
        "duration_seconds": result.get("duration_seconds"),
        "started_at": timestamp,
        "has_manifest": manifest_path.exists(),
        "has_log": (run_dir / "logs" / "simulator.log").exists(),
        "has_config": config_path.exists(),
    }


@dataclass
class DashboardContext:
    runs_dir: Path
    experiments_root: Path


class DashboardHandler(SimpleHTTPRequestHandler):
    context: DashboardContext

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/experiments":
            runs_dir = self.context.runs_dir
            if not runs_dir.exists():
                self.send_json({"experiments": [], "runs_dir": str(runs_dir), "error": "runs directory not found"})
                return
            experiments = [experiment_summary(item) for item in runs_dir.iterdir() if item.is_dir()]
            experiments.sort(key=lambda item: item.get("started_at") or "", reverse=True)
            self.send_json({"experiments": experiments, "runs_dir": str(runs_dir)})
            return
        if path.startswith("/api/experiment/"):
            encoded_name = path[len("/api/experiment/") :]
            name = urllib.parse.unquote(encoded_name)
            candidate = (self.context.runs_dir / name).resolve()
            try:
                candidate.relative_to(self.context.runs_dir.resolve())
            except ValueError:
                self.send_json({"error": "invalid experiment path"}, HTTPStatus.BAD_REQUEST)
                return
            if not candidate.exists() or not candidate.is_dir():
                self.send_json({"error": "experiment not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                payload = parse_experiment(candidate, self.context.experiments_root)
            except Exception as exc:  # Keep the dashboard useful when one run is malformed.
                self.send_json({"error": f"failed to parse experiment: {exc.__class__.__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_json(payload)
            return
        if path == "/healthz":
            self.send_json({"status": "ok", "runs_dir": str(self.context.runs_dir)})
            return
        super().do_GET()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the local SimAI experiment dashboard")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR, help="Path containing experiment run directories")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runs_dir = args.runs_dir.expanduser().resolve()
    experiments_root = runs_dir.parent.parent if runs_dir.name == "runs" else runs_dir.parent
    DashboardHandler.context = DashboardContext(runs_dir=runs_dir, experiments_root=experiments_root)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Dashboard: {url}")
    print(f"Runs dir: {runs_dir}")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
