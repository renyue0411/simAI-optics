# MPQUIC DCI: first file-driven case

Extract the archive from your existing `simAI-optics/` project root. It adds
files under `experiments/` and `ns-3-alibabacloud/` without replacing your
existing `src/quic/examples/CMakeLists.txt`. Append the block from
`CMakeLists.append.txt` to that existing file once.
These files are a local starter package, not commits in your GitHub repository.

The four inputs retain the old RDMA text syntax. `minimal-topo.txt` has two
gateway IDs (0 and 1), two direct links, and logical OCS ID 2. The first link
(gateway port 0) is the scheduled optical path; the second (port 1) is an
always-available electrical path. The logical OCS is **not a forwarding node**.
The ns-3 example uses ordinary IP point-to-point devices and drops packets
at the receiving gateway on both directions of path 0 during unavailable
intervals. This gate is an initial path-availability abstraction; packet
arrival timing differs from the original `OcsNode` ingress behavior. It is
not yet a Qbb/IP adapter, route-changing OCS, failover controller, or BDP
algorithm. Keep such distinctions explicit when interpreting results.

The example accepts one static flow from the original six-column format:
`src dst pg dport bytes start_time`. It uses `src`, `dst`, `dport`, `bytes`, and
`start_time`; `pg` is parsed and ignored. The first case requires one flow
and two direct gateway links. It does not yet read the 16-GPU All-to-All
topology or run an ASTRA AllReduce workload. `run_mpquic.py` validates and
translates these inputs to the example's command-line parameters.

The OCS text keeps `SCHEDULE OCS=... EPOCH_US=... SLICE_US=... SWITCH_US=...`
and `SLICE n PAIRS=...`. Consistent with the existing `OcsNode`, the gate
turns off for the last `SWITCH_US` of a slice and adopts the next slice's
pairing at the boundary. The 1-second starter slice means the short smoke
flow normally completes before a cutover; change flow size and slice length
only after confirming the input pipeline.

The runner follows your existing `experiments/runs/<name>/{inputs,logs,outputs}`
layout. It copies all four inputs, renders a config with resolved paths,
generates an `inputs/optical-gate.txt`, and writes a manifest. Detailed
stdout/stderr go to `logs/simulator.log`; the terminal prints only path and
result lines. It refuses to overwrite a run directory.

Once you have copied the files, the commands **for you to run on your WSL** are:

```sh
cd ~/myrdma/simAI-optics
tar -xzf /path/to/mpquic-dci-starter.tar.gz --strip-components=1
cat ns-3-alibabacloud/simulation/src/quic/examples/CMakeLists.append.txt >> \
  ns-3-alibabacloud/simulation/src/quic/examples/CMakeLists.txt

cd ~/myrdma/simAI-optics/ns-3-alibabacloud/simulation
cmake -S . -B cmake-cache-mpquic -DNS3_MPQUIC=ON -DNS3_EXAMPLES=ON
cmake --build cmake-cache-mpquic --target mpquic-dci -j1

cd ~/myrdma/simAI-optics
python3 experiments/scripts/run.py workflow mpquic minimal-dry --dry-run
python3 experiments/scripts/run.py workflow mpquic minimal-001
```

The generator and example have only been reviewed statically in this package.
No compilation or simulation was run while preparing it. The runner's
`--dry-run` mode itself creates an input snapshot under `runs/`, so give the
actual run a different name.
