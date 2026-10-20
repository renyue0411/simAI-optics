#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"
WORKLOADS_DIR = EXPERIMENTS_DIR / "workloads"
TOPOLOGIES_DIR = EXPERIMENTS_DIR / "topologies"
CONFIGS_DIR = EXPERIMENTS_DIR / "configs"
RUNS_DIR = EXPERIMENTS_DIR / "runs"

DEFAULT_SIMULATOR = PROJECT_ROOT / "bin" / "SimAI_simulator"
DEFAULT_TEMPLATE = CONFIGS_DIR / "SimAI-template.conf"

CONFIG_LINE_RE = re.compile(r"^(?P<prefix>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<sep>\s+)(?P<value>\S+)(?P<suffix>.*)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one SimAI + NS-3 experiment."
    )

    parser.add_argument(
        "name",
        help="Experiment name, used as experiments/runs/<name>.",
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--workload",
        help=(
            "ASTRA workload filename under experiments/workloads, "
            "or an explicit path."
        ),
    )
    input_group.add_argument(
        "--flow-file",
        help=(
            "Static RDMA flow filename under experiments/workloads, "
            "or an explicit path."
        ),
    )
    parser.add_argument(
        "--topology",
        required=True,
        help=(
            "Topology filename under experiments/topologies, "
            "or an explicit path."
        ),
    )
    parser.add_argument(
        "--config-template",
        default=str(DEFAULT_TEMPLATE),
        help="SimAI configuration template containing __OUTPUT_DIR__.",
    )
    parser.add_argument(
        "--simulator",
        default=str(DEFAULT_SIMULATOR),
        help="Path to SimAI_simulator.",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Number of simulation threads. Default: 1.",
    )
    parser.add_argument(
        "--send-lat",
        type=int,
        default=3,
        help="AS_SEND_LAT value. Default: 3.",
    )
    parser.add_argument(
        "--nvls-enable",
        type=int,
        choices=(0, 1),
        default=1,
        help="AS_NVLS_ENABLE value. Default: 1.",
    )
    parser.add_argument(
        "--static-stop-time",
        type=float,
        default=0.4,
        help="Static-flow simulation stop time in seconds. Default: 0.4.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete an existing run directory before starting.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Prepare files and print the command without running.",
    )

    return parser.parse_args()


def resolve_input(value: str, default_dir: Path) -> Path:
    supplied = Path(value).expanduser()

    candidates = []
    if supplied.is_absolute():
        candidates.append(supplied)
    else:
        candidates.extend(
            [
                Path.cwd() / supplied,
                PROJECT_ROOT / supplied,
                default_dir / supplied,
            ]
        )

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved

    searched = "\n".join(f"  - {path}" for path in candidates)
    raise FileNotFoundError(
        f"Cannot find input: {value}\nSearched:\n{searched}"
    )


def resolve_config_reference(value: str, template: Path) -> Path:
    supplied = Path(value).expanduser()
    candidates: list[Path] = []
    if supplied.is_absolute():
        candidates.append(supplied)
    else:
        candidates.extend(
            [
                template.parent / supplied,
                Path.cwd() / supplied,
                PROJECT_ROOT / supplied,
                TOPOLOGIES_DIR / supplied,
                CONFIGS_DIR / supplied,
            ]
        )

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved

    searched = "\n".join(f"  - {path}" for path in candidates)
    raise FileNotFoundError(
        f"Cannot resolve configuration file reference: {value}\n"
        f"Searched:\n{searched}"
    )


def get_git_head(repository: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def get_git_status(repository: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "-C", str(repository), "status", "--short"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def copy_snapshot(source: Path, inputs_dir: Path) -> Path:
    destination = inputs_dir / source.name
    shutil.copy2(source, destination)
    return destination.resolve()


def render_config(
    template: Path,
    destination: Path,
    output_dir: Path,
    inputs_dir: Path,
) -> dict[str, dict[str, str]]:
    text = template.read_text(encoding="utf-8")
    if "__OUTPUT_DIR__" not in text:
        raise ValueError(
            f"{template} does not contain the __OUTPUT_DIR__ placeholder."
        )

    rendered = text.replace("__OUTPUT_DIR__", str(output_dir.resolve()))
    snapshots: dict[str, dict[str, str]] = {}
    output_lines: list[str] = []

    for raw_line in rendered.splitlines(keepends=True):
        content = raw_line.rstrip("\r\n")
        newline = raw_line[len(content):]
        match = CONFIG_LINE_RE.match(content)
        if not match or match.group("key") != "OCS_SCHEDULE_FILE":
            output_lines.append(raw_line)
            continue

        original_value = match.group("value")
        schedule_source = resolve_config_reference(original_value, template)
        schedule_snapshot = copy_snapshot(schedule_source, inputs_dir)
        snapshots["ocs_schedule"] = {
            "original": str(schedule_source),
            "snapshot": str(schedule_snapshot),
        }
        rewritten = (
            f"{match.group('prefix')}{match.group('key')}"
            f"{match.group('sep')}{schedule_snapshot}{match.group('suffix')}"
        )
        output_lines.append(rewritten + newline)

    destination.write_text("".join(output_lines), encoding="utf-8")
    return snapshots


def write_manifest(
    path: Path,
    *,
    args: argparse.Namespace,
    input_mode: str,
    input_original: Path,
    input_snapshot: Path,
    topology_original: Path,
    topology_snapshot: Path,
    config_template: Path,
    generated_config: Path,
    config_snapshots: dict[str, dict[str, str]],
    simulator: Path,
    command: list[str],
    environment: dict[str, str],
    started_at: str,
) -> None:
    backend_repo = PROJECT_ROOT / "ns-3-alibabacloud"

    manifest = {
        "experiment": args.name,
        "started_at_utc": started_at,
        "project_root": str(PROJECT_ROOT),
        "mode": input_mode,
        "inputs": {
            input_mode: {
                "original": str(input_original),
                "snapshot": str(input_snapshot),
            },
            "topology": {
                "original": str(topology_original),
                "snapshot": str(topology_snapshot),
            },
            "configuration": {
                "template_original": str(config_template),
                "snapshot": str(generated_config),
            },
            **config_snapshots,
        },
        # Legacy top-level fields are retained for older dashboard versions.
        "workload": str(input_snapshot) if input_mode == "workload" else None,
        "flow_file": str(input_snapshot) if input_mode == "static_flow" else None,
        "topology": str(topology_snapshot),
        "generated_config": str(generated_config),
        "simulator": str(simulator),
        "threads": args.threads,
        "environment": environment,
        "command": command,
        "command_string": shlex.join(command),
        "git": {
            "simai_commit": get_git_head(PROJECT_ROOT),
            "simai_status": get_git_status(PROJECT_ROOT),
            "ns3_backend_commit": get_git_head(backend_repo),
            "ns3_backend_status": get_git_status(backend_repo),
        },
    }

    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def update_manifest_result(
    path: Path,
    *,
    return_code: int,
    finished_at: str,
    duration_seconds: float,
) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["result"] = {
        "return_code": return_code,
        "status": "success" if return_code == 0 else "failed",
        "finished_at_utc": finished_at,
        "duration_seconds": round(duration_seconds, 3),
    }
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()

    if args.threads <= 0:
        print("error: --threads must be greater than zero.", file=sys.stderr)
        return 2
    if args.flow_file is not None and args.static_stop_time <= 0:
        print(
            "error: --static-stop-time must be greater than zero.",
            file=sys.stderr,
        )
        return 2

    input_mode = "workload" if args.workload is not None else "static_flow"
    input_value = args.workload if args.workload is not None else args.flow_file

    try:
        experiment_input = resolve_input(input_value, WORKLOADS_DIR)
        topology = resolve_input(args.topology, TOPOLOGIES_DIR)
        config_template = resolve_input(args.config_template, CONFIGS_DIR)
        simulator = resolve_input(args.simulator, PROJECT_ROOT)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    run_dir = RUNS_DIR / args.name
    inputs_dir = run_dir / "inputs"
    logs_dir = run_dir / "logs"
    outputs_dir = run_dir / "outputs"

    if run_dir.exists():
        if not args.overwrite:
            print(
                f"error: run directory already exists: {run_dir}\n"
                "Use another experiment name or pass --overwrite.",
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(run_dir)

    inputs_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    generated_config = inputs_dir / "SimAI.conf"
    manifest_path = outputs_dir / "manifest.json"
    log_path = logs_dir / "simulator.log"

    try:
        input_snapshot = copy_snapshot(experiment_input, inputs_dir)
        topology_snapshot = copy_snapshot(topology, inputs_dir)
        config_snapshots = render_config(
            config_template,
            generated_config,
            outputs_dir,
            inputs_dir,
        )
    except (OSError, ValueError, FileNotFoundError) as error:
        print(f"error: cannot prepare experiment inputs: {error}", file=sys.stderr)
        return 2

    # Some NS-3 configurations expect these files to exist during initialization.
    (outputs_dir / "flow1.txt").touch()
    (outputs_dir / "trace1.txt").touch()

    command = [
        str(simulator),
        "-t",
        str(args.threads),
    ]
    if input_mode == "workload":
        command.extend(["-w", str(input_snapshot)])
    else:
        command.extend(
            [
                "-f",
                str(input_snapshot),
                "-x",
                str(args.static_stop_time),
            ]
        )
    command.extend(
        [
            "-n",
            str(topology_snapshot),
            "-c",
            str(generated_config.resolve()),
        ]
    )

    experiment_environment = {
        "AS_SEND_LAT": str(args.send_lat),
        "AS_NVLS_ENABLE": str(args.nvls_enable),
    }
    environment = os.environ.copy()
    environment.update(experiment_environment)

    started_at = datetime.now(timezone.utc).isoformat()
    write_manifest(
        manifest_path,
        args=args,
        input_mode=input_mode,
        input_original=experiment_input,
        input_snapshot=input_snapshot,
        topology_original=topology,
        topology_snapshot=topology_snapshot,
        config_template=config_template,
        generated_config=generated_config.resolve(),
        config_snapshots=config_snapshots,
        simulator=simulator,
        command=command,
        environment=experiment_environment,
        started_at=started_at,
    )

    print(f"Experiment : {args.name}")
    print(f"Run dir    : {run_dir}")
    print(f"Inputs     : {inputs_dir}")
    input_label = "Workload" if input_mode == "workload" else "Flow file"
    print(f"Mode       : {input_mode}")
    print(f"{input_label:<11}: {input_snapshot}")
    print(f"Topology   : {topology_snapshot}")
    print(f"Config     : {generated_config}")
    print(f"Output dir : {outputs_dir}")
    print(f"Manifest   : {manifest_path}")
    print(f"Log        : {log_path}")
    print(f"Command    : {shlex.join(command)}")

    if args.dry_run:
        print("Dry run completed. Simulator was not started.")
        return 0

    start_time = time.monotonic()

    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                cwd=outputs_dir,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="")
                log_file.write(line)
                log_file.flush()
            return_code = process.wait()

    except KeyboardInterrupt:
        print("\nExperiment interrupted.", file=sys.stderr)
        if "process" in locals():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return_code = 130

    except OSError as error:
        print(f"error: failed to start simulator: {error}", file=sys.stderr)
        return_code = 127

    duration = time.monotonic() - start_time
    finished_at = datetime.now(timezone.utc).isoformat()
    update_manifest_result(
        manifest_path,
        return_code=return_code,
        finished_at=finished_at,
        duration_seconds=duration,
    )

    print()
    print(f"Experiment finished with return code {return_code}.")
    print(f"Duration: {duration:.3f} seconds")
    print(f"Results: {run_dir}")

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())