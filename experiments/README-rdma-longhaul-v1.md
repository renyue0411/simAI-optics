# RDMA long-haul reproduction v1

This experiment isolates the transport-window observation in HPE/Juniper's
"Training over 50km 800GE Links with PTX Routers" TechPost.

The first baseline represents one 400G ConnectX-facing host link across an
800G DCI link.  The article's 800G aggregate is formed by two 400G endpoints;
it is not modeled as one 800G QP in this first step.

Topology:

```text
host A -- 400G/1us -- DCI router A -- 800G/245us -- DCI router B -- 400G/1us -- host B
```

The default sweep uses a 2 MiB window per QP, corresponding to approximately
512 packets with 4096-byte payloads.  The expected window-limited goodput is
roughly 34 Gbit/s per QP at the simulated 494 us propagation RTT.  Aggregate
goodput should rise with QP count and approach the roughly 390 Gbit/s payload
limit of the 400G host interface around 13--16 QPs.

The two forwarding nodes represent the PTX routers used in the article.  The
simulation reuses the repository's `SwitchNode` implementation as the packet
forwarding primitive; this implementation choice does not make them EPS
devices.

The first experiment intentionally disables RC timeout/retry, ECN, PFC, loss,
packet spraying, and NCCL buffering.  This makes a failed reproduction easier
to diagnose: only link serialization, propagation RTT, cumulative ACKs, and
the finite per-QP window remain.

Files to copy into the repository:

```text
ns-3-alibabacloud/simulation/scratch/rdma-longhaul.cc
experiments/configs/rdma-longhaul/article-50km.conf
experiments/scripts/run_rdma_longhaul.py
experiments/README-rdma-longhaul-v1.md
```

Build from `ns-3-alibabacloud/simulation`:

```bash
cmake -S . -B build-dci
cmake --build build-dci --target scratch_rdma-longhaul -j1 \
  > rdma-longhaul-build.log 2>&1
tail -n 12 rdma-longhaul-build.log
```

Prepare the sweep without starting ns-3:

```bash
cd ~/myrdma/simAI-optics
python3 experiments/scripts/run_rdma_longhaul.py article-50km-dry --dry-run
```

Run the QP sweep:

```bash
python3 experiments/scripts/run_rdma_longhaul.py article-50km-001
```

Inspect only the compact summaries:

```bash
grep -aE '^LH_RESULT ' experiments/runs/article-50km-001/logs/qps-*.log
column -s, -t experiments/runs/article-50km-001/outputs/summary.csv
```

The first validation criteria are:

1. All runs have `success=1`, `switch_drops=0`, `retrans_bytes=0`, and
   `timeouts=0`.
2. One QP reaches approximately 32--34 Gbit/s.
3. Goodput increases approximately linearly for 1, 2, 4, and 8 QPs.
4. The 13--16 QP cases approach the payload limit of a 400G host interface.
5. Additional QPs no longer materially increase goodput.

The `LH_RESULT` line and each result CSV include both `goodput_gbps` and
`expected_goodput_gbps`.  With the default 494 us RTT and 2 MiB/QP window,
the latter is approximately:

| QPs | Expected goodput (Gbit/s) |
| ---: | ---: |
| 1 | 33.96 |
| 2 | 67.92 |
| 4 | 135.84 |
| 8 | 271.69 |
| 13 or more | 390.65 (400G payload ceiling) |

Exact agreement is not expected because the ns-3 model does not reproduce the
ConnectX firmware pipeline or NCCL.  A large mismatch should be diagnosed
before adding those mechanisms.
