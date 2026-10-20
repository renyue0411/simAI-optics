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

后续可在该目录中继续增加：

```text
sweep.py      # 批量参数实验
analyze.py    # 实验结果整理与分析
common.py     # 公共函数
```

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
