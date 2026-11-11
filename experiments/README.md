# 当前跨 DC RDMA 实验

所有命令使用同一个入口：`python3 experiments/scripts/run.py`。

| 命令 | 用途 |
|---|---|
| `routing` | 固定 C4Q4 的 ECMP / 喷洒基线 |
| `collective` | 集合通信、路径维护开关及故障实验 |
| `replay` | 从已有记录重放单 QP 诊断 |
| `wan` | 分析指定网关、端口的带宽和 QP 分布 |
| `dci` | 原通用批量运行、缓存复用和汇总命令 |
| `check` | 工具检查，不启动 ns-3 |
| `check cnp` | 使用已编译的 checker 验收 CNP |

```bash
python3 experiments/scripts/run.py help
python3 experiments/scripts/run.py routing --dry-run
python3 experiments/scripts/run.py routing --simulator bin/SimAI_simulator
python3 experiments/scripts/run.py collective --help
python3 experiments/scripts/run.py replay --help
python3 experiments/scripts/run.py wan experiments/runs/运行名 --node 6 --ports 3,4
python3 experiments/scripts/run.py check
python3 experiments/scripts/run.py check cnp --checker /path/to/dcqcn-cnp-check
python3 experiments/scripts/run.py dci --help
```

`scripts/` 顶层只保留入口 `run.py`，其余实现分为两组：

| 子目录 | 文件 |
|---|---|
| `runners/` | `run_collective.py`、`run_routing.py`、`dci.py` |
| `tools/` | `dci_common.py`、`replay_irn_diag.py`、`check_tools.py` |

`__pycache__` 是 Python 自动生成的缓存。日常命令统一使用顶层 `run.py`。

输入放在 `configs/`、`topologies/`、`workloads/`。
原始运行放在 `runs/<运行名>/`，批次记录和汇总放在 `runs/_batches/<批次名>/`。
历史记录的原文件不改写，旧路径在读取时转换。

旧 MPQUIC、隧道、Figure 2/4 绘图和距离扫描的专用运行入口已退役；
这些实验已有的数据、仿真源码、配置和构建产物保留。
CNP/WAN 校准对照及故障验收仍可通过 `collective` 的对应参数执行。
故障验收原参数为：

```bash
python3 experiments/scripts/run.py collective \
  --simulator bin/SimAI_simulator \
  --config-template experiments/configs/dci-routing/pm-coexist-monitor-10us.conf \
  --topology experiments/topologies/dci-routing/4gpu-2site-4x400g-245-745-1245-1745us.txt \
  --buffer-mib 1024 --hash-seeds 0,15 --pm 0,1 \
  --paced-buckets 12 --bucket-mib 64 --bucket-gap-us 20000 --budget-ms 500 \
  --fault --fault-path-index 0 --fault-start-us 50000 --fault-end-us 100000
```

模型修正后的实验请另外指定对应的 `--cnp-mode`、`--wan-profile`、
`--irn-timeout-replay` 和 `--collective-fifo` 参数。整理工具不会更改这些参数默认值。
