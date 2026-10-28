from pathlib import Path
import csv
import re
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

root = Path(__file__).resolve().parents[2]
runs = root / "experiments/runs"
out = root / "experiments/analysis/fig2-original-ring"
out.mkdir(parents=True, exist_ok=True)

colors = ["#2878B5", "#E59B35", "#45A778", "#A879B6"]
fig, axes = plt.subplots(
    2, 2, figsize=(12, 7.5), sharex=True, sharey=True
)
max_time = 0

for ax, qp, letter in zip(axes.flat, [4, 8, 16, 32], "abcd"):
    run = runs / f"fig2-original-ring-qp{qp}-1gib-4x800g-001"
    candidates = [run / "outputs/bwconf", run / "outputs/bw.txt"]
    bw = next((p for p in candidates if p.is_file()), None)
    if bw is None:
        raise SystemExit(f"找不到带宽文件：{run}")

    log = (run / "logs/simulator.log").read_text(errors="replace")
    match = re.search(r"all passes finished at time:\s*(\d+)", log)
    if not match:
        raise SystemExit(f"找不到完成时间：{run}")
    finish_ns = int(match.group(1))
    finish_ms = finish_ns / 1e6
    max_time = max(max_time, finish_ms)

    series = {p: [] for p in range(3, 7)}
    with bw.open() as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 4:
                continue
            t, node, port, rate = map(float, row[:4])
            if int(node) == 6 and int(port) in series and t <= finish_ns:
                series[int(port)].append((t / 1e6, rate))

    for port, color in zip(range(3, 7), colors):
        points = sorted(series[port])
        if not points:
            raise SystemExit(f"{run.name}: router 6 port {port} 没有采样")
        t, rate = zip(*points)
        ax.plot(
            t, rate, color=color, linewidth=1.6,
            marker="o", markersize=3,
            label=f"Path {port-2} / port {port}"
        )

    ax.axhline(200, color="gray", linestyle="--", linewidth=1)
    ax.set_title(f"({letter}) {qp} QPs/channel")
    ax.text(
        .02, .96, f"Completion: {finish_ms:.3f} ms",
        transform=ax.transAxes, va="top", fontsize=9
    )
    ax.grid(alpha=.22)
    print(f"{qp} QPs/channel: completion={finish_ms:.6f} ms")

for ax in axes.flat:
    ax.set_xlim(0, max_time * 1.03)
    ax.set_ylim(0, 450)

for ax in axes[:, 0]:
    ax.set_ylabel("Bandwidth (Gbit/s)")
for ax in axes[1, :]:
    ax.set_xlabel("Simulation time (ms)")

handles, labels = axes[0, 0].get_legend_handles_labels()
fig.legend(handles, labels, loc="upper center",
           bbox_to_anchor=(.5, .91), ncol=4, frameon=False)
fig.suptitle(
    "ECMP bandwidth over time: 4 × 800G, 50 km\n"
    "Original SimAI Ring AllReduce, 1 GiB, 4 channels",
    fontsize=14
)
fig.text(
    .5, .025,
    "Router 6; recorded bandwidth samples. Dashed line: 200 Gbit/s reference. "
    "No synthetic samples added.",
    ha="center", fontsize=9
)
fig.tight_layout(rect=[0, .06, 1, .85])
for suffix in ["png", "pdf"]:
    target = out / f"qp-bandwidth-timeseries.{suffix}"
    fig.savefig(target, dpi=220)
    print(target)
