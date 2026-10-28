# 日常只使用一个入口

```bash
python3 experiments/scripts/run.py help
```

本次整理只改变 Python 工具组织，不修改 NS-3、IRN、负载、拓扑或现有实验结果。

## 当前 DCI 实验

```bash
# 先看哪些结果会复用、哪些还缺；不启动仿真
python3 experiments/scripts/run.py dci run --dry-run

# 自动复用匹配的已有结果，只补缺少的 ECMP/轮询/随机 × IRN 开/关
python3 experiments/scripts/run.py dci run

# 重新汇总该组最近一次计划；不启动仿真
python3 experiments/scripts/run.py dci summary
```

默认参数集中在 `experiments/configs/dci-routing/experiment.json`：4 GPU、
256 MiB AllReduce、4 channels、4 QPs/channel、两条 400G/245us WAN、
seed=1/run=1、1000ms 仿真预算、每组 600s 墙钟限制。
配置或命令行参数只决定要跑的矩阵，不再生成新的运行脚本。

```bash
# 只考虑 IRN 关闭的三种方案
python3 experiments/scripts/run.py dci run --irn 0

# 随机模式扩展为 5 个 RngRun；ECMP/轮询只用列表第一个 run
python3 experiments/scripts/run.py dci run --rng-runs 1,2,3,4,5

# 改 QP 数，自动产生不同的实验身份和结果目录
python3 experiments/scripts/run.py dci run --qps 8

# 指定新的输入文件；是否支持异构延迟取决于现有 NS-3 后端，工具不偷偷改协议
python3 experiments/scripts/run.py dci run --topology experiments/topologies/dci-routing/4gpu-2site-2x400g-50km.txt

# 协议代码改变后重新运行所选矩阵；保留原结果
python3 experiments/scripts/run.py dci run --fresh
```

不再手写 suffix。新结果自动命名；现有 `experiments/runs/` 中的文件不覆盖、不移动。
有效的“仿真预算内未完成”结果也会复用；墙钟超时或损坏的结果不会当作有效结果。

复用会核对 workload/topology 快照、规范化配置、QP/channel、IRN、seed/run、
预算和日志完整性，不能只按文件名判断。较旧 manifest 未记录完整环境和二进制哈希，
对此会明确标为 `historical`；代码更改后用 `--fresh`。
`--strict-build` 只复用统一入口新记录且可执行文件 SHA256 相同的结果。

结果索引及汇总位于：

- `experiments/studies/dualpath/study.json`：最近一次矩阵计划和每组对应的原始结果目录。
- `experiments/studies/dualpath/summary.csv`：该计划的汇总。
- `experiments/runs/<自动名称>/`：原始输入快照、日志和输出。

`--group NAME` 可维护另一份计划；同组新的运行选择会替换计划视图，但不会删除
任何原始结果。`dci summary --group NAME` 汇总该组最后保存的计划。

## IRN 诊断也复用同一入口

分析你已有的日志，不需要重新跑：

```bash
python3 experiments/scripts/run.py dci diagnose irn-diag-v1-spray_rr-003 irn-diag-v1-spray_random-003
python3 experiments/scripts/run.py dci ack-lag irn-diag-v1-spray_rr-003 irn-diag-v1-spray_random-003
```

需要采集新诊断时：

```bash
python3 experiments/scripts/run.py dci run --group diagnostic --irn 1 --routing spray_rr,spray_random --diagnostics
```

默认只跟踪 0->3 第一个 QP；可增加 `--diag-sport 10000`。
诊断分析仍按单 QP 解释；不要把它直接当作整个作业的原因占比。

## 其他既有实验

以下保留原脚本参数，只改变调用入口。完整参数可在命令末尾加 `--help`。

| 原脚本 | 固定调用方式 |
|---|---|
| `run_mpquic.py` | `python3 experiments/scripts/run.py workflow mpquic ...` |
| `run_dci_tunnel.py` | `python3 experiments/scripts/run.py workflow tunnel ...` |
| `run_rdma_distance.py` | `python3 experiments/scripts/run.py workflow distance ...` |
| `run_rdma_longhaul.py` | `python3 experiments/scripts/run.py workflow longhaul ...` |
| `run_nccl_figure4.py` | `python3 experiments/scripts/run.py workflow nccl-buffer ...` |
| `analyze_nccl_figure2.py` | `python3 experiments/scripts/run.py workflow bandwidth RUN_DIR --ports 3,4` |
| `plot_fig2_timeseries.py` | `python3 experiments/scripts/run.py workflow plot-bandwidth`，仍是原 Figure 2 固定数据集作图 |

这些不同研究方向的实现保留在 `experiments/workflows/`，不是把它们强行改成同一个
物理模型。日常通过 `run.py` 调用，无需记住底层文件名。

## 历史脚本

后加的 baseline/matrix 脚本及根目录的 DCI v1/v2 包会移入 `experiments/legacy/dci/`，
保留 prepared、备份和其他原文件。没有安装过的包会跳过；整理不会删除旧结果。

如果确实需要按原始命名读取某次旧汇总，可转发给归档版本：

```bash
python3 experiments/scripts/run.py legacy baseline --suffix 002 --summarize
python3 experiments/scripts/run.py legacy irn-matrix --suffix 004 --random-runs 1 --summarize
python3 experiments/scripts/run.py legacy diagnostics --suffix 003 --summarize
```

只有已经安装并被归档的旧工具才有对应入口；新实验使用 `dci run`。

## 目录职责

| 目录 | 内容 |
|---|---|
| `scripts/` | 唯一日常入口 `run.py` |
| `lib/` | 共用的 DCI 运行、复用、结果校验和诊断实现 |
| `workflows/` | MPQUIC、隧道、距离等独立工作流的既有实现 |
| `configs/`、`topologies/`、`workloads/` | 可复用实验输入 |
| `studies/` | 矩阵计划、结果索引和汇总 |
| `runs/` | 每次运行的原始数据 |
| `legacy/` | 历史工具和一次性整理的备份 |

验证工具整理本身（使用合成日志和测试进程，不启动 NS-3）：

```bash
python3 -m unittest discover -s experiments/tests -p test_dci_tools.py
```

---

# SimAI Experiment Framework

本目录用于统一管理 SimAI + NS-3 仿真的输入文件、运行脚本和实验结果，避免 workload、topology、配置文件以及运行输出散落在项目根目录。

## 文件夹说明

### `scripts/`

存放实验运行和管理脚本。

当前统一实验入口为：

```text
experiments/scripts/run.py
```

该脚本负责：

* 检查 workload、topology、配置模板和模拟器是否存在；
* 创建独立的实验运行目录；
* 根据配置模板生成本次实验使用的 `SimAI.conf`；
* 设置 SimAI 运行所需的环境变量；
* 启动 `bin/SimAI_simulator`；
* 将标准输出和错误输出实时写入日志；
* 保存运行命令、Git commit、环境变量和执行结果；
* 将所有生成文件集中到对应的实验目录中。

批量运行、汇总和诊断统一使用 `run.py dci ...`。不要再为每轮实验新增一个
`run_*.py`。公共实现位于 `lib/`，其他研究方向的既有工具位于 `workflows/`，
由同一个入口转发调用。历史 DCI 脚本归档于 `legacy/dci/`。

### `configs/`

存放 SimAI 和 NS-3 的实验配置模板。

默认模板为：

```text
experiments/configs/SimAI-template.conf
```

模板中的输出目录使用占位符：

```text
__OUTPUT_DIR__
```

例如：

```text
FLOW_FILE __OUTPUT_DIR__/flow1.txt
TRACE_FILE __OUTPUT_DIR__/trace1.txt
FCT_OUTPUT_FILE __OUTPUT_DIR__/llama_hpn7_fct.txt
```

运行 `run.py` 时，`__OUTPUT_DIR__` 会自动替换为：

```text
experiments/runs/<experiment-name>/outputs
```

配置模板必须包含 `__OUTPUT_DIR__`，否则 `run.py` 会终止并报告错误。

### `workloads/`

存放 SimAI workload 文件。

例如：

```text
experiments/workloads/microAllReduce16.txt
```

workload 文件主要描述：

* workload 类型；
* GPU/NPU 总数；
* tensor parallel、pipeline parallel 等并行配置；
* collective 类型；
* collective 数据量；
* computation time；
* layer 和依赖关系。

运行时可以只提供文件名：

```bash
--workload microAllReduce16.txt
```

脚本会自动在 `experiments/workloads/` 中查找该文件。

也可以提供显式路径：

```bash
--workload /path/to/workload.txt
```

### `topologies/`

存放网络拓扑文件。

例如：

```text
experiments/topologies/Spectrum-X_16g_8gps_100Gbps_A100
```

拓扑文件可以通过 SimAI 提供的 topology generator 生成：

```bash
python3 \
  ./astra-sim-alibabacloud/inputs/topo/gen_Topo_Template.py \
  -topo Spectrum-X \
  -g 16 \
  -gt A100 \
  -bw 100Gbps \
  -nvbw 2400Gbps
```

生成后将拓扑文件移动到：

```text
experiments/topologies/
```

运行时可以只提供文件名：

```bash
--topology Spectrum-X_16g_8gps_100Gbps_A100
```

也可以提供显式路径。

### `runs/`

存放所有实验运行结果。

每次运行对应一个独立目录：

```text
experiments/runs/<experiment-name>/
```

例如：

```text
experiments/runs/microAllReduce16-baseline/
```

每个实验目录中包含：

```text
SimAI.conf
manifest.json
logs/
outputs/
```

#### `SimAI.conf`

由 `SimAI-template.conf` 自动生成。

其中所有 `__OUTPUT_DIR__` 均已替换成本次实验的绝对输出路径。

#### `manifest.json`

记录实验的复现信息，包括：

* 实验名称；
* workload 路径；
* topology 路径；
* 配置模板路径；
* 自动生成的配置路径；
* 模拟器路径；
* 完整执行命令；
* 环境变量；
* SimAI Git commit；
* NS-3 backend Git commit；
* Git working tree 状态；
* 程序返回码；
* 开始和结束时间；
* 实际运行时长。

#### `logs/`

存放模拟器运行日志：

```text
logs/simulator.log
```

终端输出会实时显示，同时写入该文件。

#### `outputs/`

存放模拟器和 NS-3 生成的输出文件，例如：

```text
flow1.txt
trace1.txt
send.txt
ncclFlowModel_detailed_*.csv
ncclFlowModel_EndToEnd.csv
llama_hpn7_fct.txt
llama_hpn7_pfc.txt
llama_hpn7_qlen.txt
llama_hpn7_bw.txt
llama_hpn7_rate.txt
llama_hpn7_cnp.txt
```

`run.py` 会将模拟器的工作目录设置为 `outputs/`，因此使用相对路径生成的 CSV 和日志文件也会进入该目录，而不会出现在项目根目录。

---

# 使用 `run.py`

## 基本语法

在 SimAI 项目根目录执行：

```bash
python3 experiments/scripts/run.py \
  <experiment-name> \
  --workload <workload-file> \
  --topology <topology-file>
```

也可以直接执行：

```bash
./experiments/scripts/run.py \
  <experiment-name> \
  --workload <workload-file> \
  --topology <topology-file>
```

首次使用前需要赋予执行权限：

```bash
chmod +x experiments/scripts/run.py
```

## 最小运行示例

```bash
cd ~/myrdma/simAI-optics

python3 experiments/scripts/run.py \
  microAllReduce16-baseline \
  --workload microAllReduce16.txt \
  --topology Spectrum-X_16g_8gps_100Gbps_A100
```

脚本会自动创建：

```text
experiments/runs/microAllReduce16-baseline/
```

## 完整运行示例

```bash
python3 experiments/scripts/run.py \
  microAllReduce16-baseline \
  --workload microAllReduce16.txt \
  --topology Spectrum-X_16g_8gps_100Gbps_A100 \
  --threads 1 \
  --send-lat 3 \
  --nvls-enable 1
```

---

# 参数说明

## 位置参数

### `name`

实验名称，同时用于生成实验输出目录。

格式：

```bash
run.py <name>
```

例如：

```bash
run.py microAllReduce16-baseline
```

对应输出目录：

```text
experiments/runs/microAllReduce16-baseline/
```

实验名称应能够反映主要实验配置，例如：

```text
microAllReduce16-baseline
microAllReduce16-nvls-off
ocs-plane2-auto
ocs-plane2-pinned
gate-mode-userspace
```

---

## 必需参数

### `--workload`

指定 workload 文件。

示例：

```bash
--workload microAllReduce16.txt
```

脚本会依次尝试从以下位置查找：

1. 当前工作目录；
2. SimAI 项目根目录；
3. `experiments/workloads/`。

也可以使用绝对路径：

```bash
--workload /home/keith/workloads/test.txt
```

### `--topology`

指定网络拓扑文件。

示例：

```bash
--topology Spectrum-X_16g_8gps_100Gbps_A100
```

脚本会依次尝试从以下位置查找：

1. 当前工作目录；
2. SimAI 项目根目录；
3. `experiments/topologies/`。

也可以使用绝对路径：

```bash
--topology /home/keith/topologies/custom-topology.txt
```

---

## 可选参数

### `--config-template`

指定 SimAI 配置模板。

默认值：

```text
experiments/configs/SimAI-template.conf
```

示例：

```bash
--config-template experiments/configs/SimAI-template.conf
```

配置模板中必须包含：

```text
__OUTPUT_DIR__
```

运行时该占位符会被替换为本次实验的输出目录。

不同实验机制可以使用不同模板，例如：

```text
SimAI-template.conf
SimAI-dcqcn.conf
SimAI-ocs.conf
SimAI-multiplane.conf
```

使用方式：

```bash
--config-template experiments/configs/SimAI-ocs.conf
```

### `--simulator`

指定 SimAI 模拟器可执行文件。

默认值：

```text
bin/SimAI_simulator
```

示例：

```bash
--simulator bin/SimAI_simulator
```

通常不需要显式设置该参数。

### `--threads`

设置仿真线程数，对应模拟器的 `-t` 参数。

默认值：

```text
1
```

示例：

```bash
--threads 1
```

线程数必须大于 0。

在验证功能正确性时，建议首先使用：

```bash
--threads 1
```

确认单线程运行正常后，再测试多线程模式。

### `--send-lat`

设置环境变量：

```text
AS_SEND_LAT
```

默认值：

```text
3
```

示例：

```bash
--send-lat 3
```

该参数用于控制 SimAI 网络发送路径中的发送延迟配置。

### `--nvls-enable`

设置环境变量：

```text
AS_NVLS_ENABLE
```

允许值：

```text
0
1
```

默认值：

```text
1
```

启用 NVLS：

```bash
--nvls-enable 1
```

禁用 NVLS：

```bash
--nvls-enable 0
```

该参数主要用于控制服务器内 NVLink/NVSwitch/NVLS 相关的 collective 实现。

### `--overwrite`

允许删除并重新创建已经存在的实验目录。

默认情况下，如果以下目录已经存在：

```text
experiments/runs/<experiment-name>/
```

脚本会停止执行，防止覆盖已有实验结果。

需要重新运行同名实验时：

```bash
python3 experiments/scripts/run.py \
  microAllReduce16-baseline \
  --workload microAllReduce16.txt \
  --topology Spectrum-X_16g_8gps_100Gbps_A100 \
  --overwrite
```

使用 `--overwrite` 后，原实验目录及其中所有文件会被删除。

对于需要保留历史数据的实验，建议使用新的实验名称，而不是覆盖旧结果。

### `--dry-run`

只准备实验目录和配置，不启动模拟器。

示例：

```bash
python3 experiments/scripts/run.py \
  microAllReduce16-test \
  --workload microAllReduce16.txt \
  --topology Spectrum-X_16g_8gps_100Gbps_A100 \
  --dry-run
```

该模式会完成：

* 参数检查；
* 输入文件路径解析；
* 实验目录创建；
* `SimAI.conf` 生成；
* `manifest.json` 生成；
* 命令打印。

但不会执行：

```text
bin/SimAI_simulator
```

该选项适合在正式运行前检查：

* workload 是否正确；
* topology 是否正确；
* 输出路径是否正确；
* 配置模板替换是否正确；
* 最终命令是否符合预期。

---

# 查看帮助

```bash
python3 experiments/scripts/run.py --help
```

或：

```bash
./experiments/scripts/run.py --help
```

---

# 查看实验状态

查看运行日志：

```bash
tail -100 \
  experiments/runs/microAllReduce16-baseline/logs/simulator.log
```

检查关键完成信息：

```bash
grep -E \
  "simulator run|all passes finished|Total streams|Percentage" \
  experiments/runs/microAllReduce16-baseline/logs/simulator.log
```

典型成功输出包括：

```text
simulator run
all passes finished
Total streams injected: 2
Total streams finished: 2
Percentage of finished streams: 100 %
```

查看程序返回码：

```bash
grep -A5 '"result"' \
  experiments/runs/microAllReduce16-baseline/manifest.json
```

成功运行时：

```json
{
  "return_code": 0,
  "status": "success"
}
```

---

# 重现实验

每次运行都会在 `manifest.json` 中记录：

```text
command
environment
simai_commit
ns3_backend_commit
```

因此可以通过以下信息复现实验：

1. checkout 对应的 SimAI commit；
2. checkout 对应的 NS-3 backend commit；
3. 使用相同 workload；
4. 使用相同 topology；
5. 使用相同 `SimAI.conf`；
6. 使用 `manifest.json` 中记录的环境变量和执行命令。

---

# 推荐工作流程

## 1. 编译

在项目根目录执行：

```bash
./scripts/build.sh -c ns3
```

## 2. 准备 workload

将 workload 放入：

```text
experiments/workloads/
```

## 3. 准备 topology

将 topology 放入：

```text
experiments/topologies/
```

## 4. 检查运行配置

```bash
python3 experiments/scripts/run.py \
  <experiment-name> \
  --workload <workload-file> \
  --topology <topology-file> \
  --dry-run
```

## 5. 正式运行

```bash
python3 experiments/scripts/run.py \
  <experiment-name> \
  --workload <workload-file> \
  --topology <topology-file>
```

## 6. 检查结果

```bash
cat experiments/runs/<experiment-name>/manifest.json
```

```bash
tail -100 \
  experiments/runs/<experiment-name>/logs/simulator.log
```

---

# 注意事项

1. 每个实验名称默认只能使用一次，避免意外覆盖已有结果。

2. `--overwrite` 会删除同名实验目录中的所有已有文件。

3. workload 中的 GPU/NPU 数量必须与 topology 中的 GPU/NPU 数量一致。

4. 配置模板中的输出路径必须使用 `__OUTPUT_DIR__`。

5. 不要直接将输出路径写死为：

   ```text
   /etc/astra-sim/simulation
   ```

   除非当前系统确实使用该安装路径。

6. 建议从项目根目录运行 `run.py`，虽然脚本内部使用绝对路径解析，但从根目录执行更便于管理。

7. 功能验证阶段建议使用：

   ```bash
   --threads 1
   ```

8. 生成的 `experiments/runs/` 通常不应提交到 Git，可在项目 `.gitignore` 中加入：

   ```gitignore
   /experiments/runs/
   ```

9. `workloads/`、`topologies/` 和 `configs/` 中用于正式实验的输入文件应提交到 Git，以保证实验可复现。
