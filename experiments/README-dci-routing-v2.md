# 可配置的 DCI 数据包喷洒

配置文件支持：

```ini
DCI_ROUTING_MODE spray_rr
DCI_ROUTING_NODES 6,7
```

模式为 `ecmp`、`spray_rr`、`spray_random`。节点列表用逗号分隔，不加空格。
路径数量和出口编号来自各目的地的候选路由列表，不再固定为四个端口。
轮询为每 QP 独立计数，第一次使用原 ECMP 出口；随机为每包等概率选择。
只有候选路由中的有效点对点出口参与选择，单路径直接发送。
ACK/NACK/CNP 使用原有选路，不参与喷洒。

节点默认仍为 6、7，避免对已有实验的其他交换机开启喷洒；可通过
`DCI_ROUTING_NODES` 指定其他 DCI 节点。启用节点上的所有 UDP 数据流均按
该模式处理；如果要只控制特定链路组，应另行增加链路组配置。

显式配置优先于旧 `SIMAI_DCI_ROUTING` 环境变量。没有这两个配置项时，
保留原环境变量行为；两者均未设置时仍为原 ECMP。
旧四路径实验脚本不需要修改。旧的异构辅助代码
`SIMAI_DCI_HETEROGENEOUS=1` 仍专用于原四路径 profile，不能用于双路径扫描。
本次附带双路径等时延拓扑；不同延迟的加载、RTT/BDP 边界计算留给下一步实验修改。

随机数使用 ns-3 UniformRandomVariable，每个节点使用 stream `1700000 + node_id`。
可通过 `NS_GLOBAL_VALUE='RngSeed=1;RngRun=1'` 复现；重复实验改变 RngRun。
日志 DCI_ROUTING_CONFIG 输出实际候选出口、模式、stream、seed 和 run。

## 安装和增量构建

在项目根目录解压补丁包后：

```bash
python3 dci-routing-v2/install.py --check
python3 dci-routing-v2/install.py --apply
SIM_DIR=astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation
cmake --build "$SIM_DIR/cmake-cache-nccl" --target scratch_AstraSimNetwork -j2
```

安装器先检查全部文件版本，再备份并更新。它同时更新 NS-3 子模块、frontend
模板以及已经存在的实际构建源码副本，不会覆盖版本不同的文件。
子模块和父仓库的修改需要分别提交。

## 双路径等时延检查

沿用已有 NCCL 模拟器和工作负载；本次不是完整延迟扫描。
选择新的 run 名称，避免覆盖先前实验。

```bash
SIM_DIR=astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation
SIMAI_IRN_ENABLE=1 SIMAI_NCCL_CHANNELS=4 SIMAI_FIG2_QPS_PER_CHANNEL=4 \
SIMAI_NCCL_NET_MONITOR=1 AS_PXN_ENABLE=0 SIMAI_DCI_EXPERIMENT_MS=1000 \
NS_GLOBAL_VALUE='RngSeed=1;RngRun=1' \
python3 experiments/scripts/run.py dci-v2-rr-2path-001 \
  --workload nccl-channel-buffer/micro-allreduce4-256mib.txt \
  --topology dci-routing/4gpu-2site-2x400g-50km.txt \
  --config-template experiments/configs/dci-routing/spray_rr.conf \
  --simulator "$SIM_DIR/build-nccl/scratch/ns3.36.1-AstraSimNetwork-optimized" \
  --nvls-enable 0
```

随机喷洒使用相同命令，把运行名换成 `dci-v2-random-2path-001`，
配置文件换成 `experiments/configs/dci-routing/spray_random.conf`。
请在干净的终端环境执行；不要继承上一轮的 `SIMAI_DCI_HETEROGENEOUS=1`
或手工覆盖的 IRN BDP/RTO 参数。

```bash
python3 experiments/scripts/run.py workflow bandwidth \
  experiments/runs/dci-v2-rr-2path-001 --ports 3,4
```

此分析器输出端口带宽和 Jain 公平性，带宽包含重传，不能等同于有效吞吐。

## 选路行为检查

```bash
cmake --build "$SIM_DIR/cmake-cache-nccl" --target scratch_dci-routing-check -j2
"$SIM_DIR/build-nccl/scratch/ns3.36.1-dci-routing-check-optimized" spray_rr 2
```

输出可执行文件的后缀取决于你的 CMake build profile。该程序使用真实 ns-3
节点、点对点设备和随机数，检查非连续出口、每 QP 轮询、控制包旁路及统计。
