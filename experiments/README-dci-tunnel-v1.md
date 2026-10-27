# DCI 透明隧道：动态路径维护第一版

基于 GitHub 主仓库 `renyue0411/simAI-optics` 的 `bde7cfd`，其 ns-3 子模块提交为 `3552fabc906bf78147e169ac44b7eea5e0d77a59`。源码已读取并按接口静态核对；没有运行 CMake、编译、runner 或仿真，因此尚未验证编译通过或实验结果。

这份包包含完整第一版，直接安装即可，不依赖之前下载的静态路径代码包。如果已经安装旧的 `dci-tunnel` 包，这份包会替换同名文件。GitHub 基线中没有这个新模块；不会改动已有 QUIC、RDMA、OCS 核心文件或顶层 CMakeLists.txt。

## 本版的实现范围

四节点：Host A(0)、DC1 网关(1)、DC2 网关(2)、Host B(3)。本地各为 400 Gbps / 单向 2 us。WAN0 为 100 Gbps / 10 ms 传播 RTT，WAN1 为 200 Gbps / 20 ms 传播 RTT。

数据路径：源端现有 RDMA 模型生成完整 PPP/IP/自定义 RDMA 模拟报文；源网关封装为一个 UDP datagram；目的网关解封装后发给目的主机。主机 QP、ACK/NAK、Go-Back-N、超时与 DCQCN 保持端到端。没有提前 ACK，没有外层重传或外层拥塞窗口。

**采用 MPQUIC 的路径管理思路，独立实现一个组件；没有直接链接 MpQuicPathManager，也不是 QUIC/MPQUIC 兼容的报文实现。**使用已有 MPQUIC 类会同时依赖 QuicSocketBase、subflow、流缓冲和外层恢复/拥塞状态。本版将路径维护与透明 RDMA 数据面分开，便于后续修改。

已核对的 MPQUIC 参考入口：

- `simulation/src/quic/model/mp-quic-path-manager.cc`：新增 subflow 和 Validating 状态。
- `simulation/src/quic/model/quic-socket-base.cc`：SendPathChallenge、OnReceivedPathResponseFrame、OnReceivedAckFrame。
- `simulation/src/quic/model/mp-quic-subflow.h`：逐路径状态、ACK 和定时器。
- `simulation/src/quic/model/quic-congestion-ops.cc`：RTT 更新逻辑。

对应固定版本源码：
https://github.com/renyue0411/optics-rdma-aisim-ns3/tree/3552fabc906bf78147e169ac44b7eea5e0d77a59/simulation/src/quic/model

该分支的 challenge/response 值匹配仍有 TODO，本组件补充独立随机 token 和探测序号检查。RTT 平滑直接使用现有 ns-3 `RttMeanDeviation`，不复制参考代码中的整数权重表达式。

## 路径维护如何工作

1. **登记候选路径**：配置只给接口、源/目的地址和线速。管理器没有接收拓扑 RTT 的接口。
2. **初始验证**：每条路径发送 CHALLENGE；对端在同一路径回复 RESPONSE。只有匹配当前 token 和探测序号的响应才完成验证。收到对方 CHALLENGE 本身不会验证自己的出站路径。
3. **动态 RTT**：探测响应和符合条件的数据 ACK 根据本地保存的实际发送时间产生 RTT 样本，维护 latest/min/smoothed RTT 和 RTT variation。正常 DATA RTT 样本会在合理范围内扣除 ACK delay。MIN_RTT 策略使用测量的 SRTT。
4. **持续探测**：即使该路径无业务也继续进行 challenge/response。默认在一次成功响应或超时后间隔 10 ms 再发起下一次探测；同时最多一个探测在途，不是强制每 10 ms 叠加新探测。
5. **超时和恢复**：首次探测超时初值 200 ms；有 RTT 样本后使用 `SRTT + max(4*RTTVAR, 1ms) + max_ack_delay`，本模型设 20 ms 下限。一次未获反馈进入 SUSPECT，默认连续三次进入 FAILED；新的有效回复恢复 ACTIVE。若同一探测期间有新的数据 ACK 证明双向通达，就不因单个探测丢失累计失效次数。
6. **发送资格**：只有 ACTIVE 且验证成功的路径才能发 DATA。控制报文仍可在 SUSPECT/FAILED 上发出，以支持重新验证。

状态日志为 `DCI_STATE node=... path=... time_s=... from=... to=...`。状态表示该接口/地址对在测量时是否有双向反馈，不包含链路层直接通知、路由发现或远端 OCS 拓扑推断。

## 网关 ACK 与 RDMA ACK

外层每方向、每路径有独立的 DATA 包号。32 B 私有头包含 magic/version/type/path、包号、类型相关值和 ACK delay。

新示例将内层 `L2AckInterval` 设置为 1 字节，使每个数据包（包括不足 MTU 的尾包）都产生 RDMA ACK，避免最小连通性检查被独立的尾 ACK 超时影响。它不是每 1 个包的配置单位，不修改原有 RDMA 实验的参数。

- 外层 ACK：确认目的网关收到哪些隧道 DATA 包，用于路径观测，不完成源 RNIC 的 RDMA 操作，不触发外层数据重传。
- 内层 RDMA ACK/NAK：由目的主机生成，经隧道回到源主机，完成原有可靠性处理。

外层 ACK 默认每收到 16 个 DATA 包生成一次，或者在首个待确认包之后等待最多 50 us；它携带最大包号和向前 64 个包号的位图。超出位图范围的迟到包单独确认。这是有限窗口 ACK 机制，不是完整 QUIC ACK-range 编码。

ACK 控制报文本身不要求外层 ACK，因此不会形成 ACK 循环。承载内层 RDMA ACK 的隧道包仍属于 DATA，可以被外层 ACK 确认。

为了限制大 BDP 的状态成本，默认每 64 个 DATA 包保留一个发送时间样本，每条路径上限 65536 条。只有新 ACK 的最大包号有对应发送样本时才用该 ACK 更新 RTT；探测 RTT 始终独立采样。不会保存另一份待外层重传的 payload。过期或被容量淘汰的发送样本有单独计数，**不能将这些计数解释为实际丢包量或丢包率**；本版没有外层数据丢包估计算法。

控制和数据共用线速 pacing，控制队列优先，最多 4096 条控制报文/路径。每个包的 pacing 包含隧道、UDP/IPv4 和 PPP 开销。

## 流管理与调度

- 按规范化的 IP、端口和优先级识别该仓库中的 QP。
- `POLICY=0`：新 QP 在当前 ACTIVE 路径间轮询。
- `POLICY=1`：新 QP 对 ACTIVE 路径做固定哈希。
- `POLICY=2`：新 QP 选择测量 SRTT 最小的 ACTIVE 路径。
- 如果尚无 ACTIVE 路径，QP 先绑定一个候选并排队等待验证；不会在未验证路径发送 DATA。
- QP 生命周期内固定路径，目的网关学习对应路径，让内层 ACK/NAK 返回。独立 CNP 使用 fid/qIndex 形成单独控制流键；当前模型主要在 ACK 中携带 CNP 标志。
- 同一路径内的活跃 QP 按包轮询、每 QP FIFO。路径不可用时保留有限待发队列，恢复后继续排空。没有实现已有 QP 的跨路径迁移、乱序重排或无缝故障切换。

路径管理已从网关拆为 `DciPathManager`；本版流表和简单调度仍在 `DciTunnelGateway`，避免把基础版本拆成过多空壳类。

## 拥塞、缓存和 ECN

使用原有端点 `CcMode=1/DCQCN`。每路径网关 WAN 待发队列超过阈值时对 ECT 外层包标记 CE；对端解封装时传给内层，再由原目的 RNIC 反馈。网关的线速 pacing 是链路整形，不是新增的一套自适应拥塞控制。

原模型 `GetNxtPacket()` 生成 Not-ECT，而 `ReceiveUdp()` 把所有非零 ECN 位当作拥塞。新示例通过发送包装函数设置 ECT(0)，在目的接收包装函数中将正常 ECT 归一化为原模型的 0、保留 CE=3；不修改已有核心文件。这不是完整 L4S 或所有 ECN 模式的实现。

`GATEWAY_BUFFER_BYTES` 是所有待发 WAN 数据队列之和的上限；本地出口队列另外使用相同大小的上限。控制队列、流表、发送时间样本、IP 栈队列和模拟器对象开销不包含在其中，不能当作统一总 RAM 占用。超限丢包由端点 RC 恢复。PFC 不跨隧道，也没有实现本地 PFC/信用反压。

`RDMA_WINDOW_BYTES` 是固定端点字节窗口，没有自动按 BDP 放大。本示例采用原注入模式，不额外建模 WR 提交深度或真实 RNIC SRAM。

## 安装：新增 14 个文件

```
ns-3-alibabacloud/simulation/src/dci-tunnel/
  CMakeLists.txt
  model/dci-path-manager.h
  model/dci-path-manager.cc
  model/dci-tunnel.h
  model/dci-tunnel.cc
  examples/CMakeLists.txt
  examples/rdma-dci-tunnel.cc
experiments/
  README-dci-tunnel-v1.md
  configs/dci-tunnel/minimal.conf
  configs/dci-tunnel/probe-outage.conf
  topologies/dci-tunnel/two-path.txt
  workloads/dci-tunnel/single-flow.txt
  workloads/dci-tunnel/two-flows.txt
  scripts/run_dci_tunnel.py
```

将 `dci-path-v1.tar.gz` 下载到 `~/myrdma/simAI-optics`，执行：

```bash
cd ~/myrdma/simAI-optics
mkdir -p ns-3-alibabacloud/simulation/src/dci-tunnel/model
mkdir -p ns-3-alibabacloud/simulation/src/dci-tunnel/examples
mkdir -p experiments/configs/dci-tunnel experiments/topologies/dci-tunnel
mkdir -p experiments/workloads/dci-tunnel experiments/scripts
tar -xzf dci-path-v1.tar.gz
```

仅新增/更新包内文件。模块由 ns-3 自动发现，无须编辑已有顶层 CMakeLists.txt。安装不执行 Git、构建或仿真。

## 构建

```bash
cd ~/myrdma/simAI-optics/ns-3-alibabacloud/simulation
cmake -S . -B cmake-cache-dci \
  -DNS3_MPQUIC=OFF \
  '-DNS3_ENABLED_MODULES=core;network;internet;point-to-point;dci-tunnel' \
  -DNS3_OUTPUT_DIRECTORY=build-dci \
  -DNS3_EXAMPLES=ON \
  -DNS3_TESTS=OFF \
  -DNS3_PRECOMPILE_HEADERS=OFF \
  -DNS3_WARNINGS_AS_ERRORS=OFF \
  -DNS3_MPI=OFF \
  -DNS3_MTP=OFF \
  -DNS3_GSL=OFF \
  -DNS3_GTK3=OFF \
  -DNS3_SQLITE=OFF

cmake --build cmake-cache-dci --target rdma-dci-tunnel -j1 \
  > dci-tunnel-build.log 2>&1
tail -n 12 dci-tunnel-build.log
```

`NS3_MPQUIC=OFF` 是因为本组件复现路径维护机制，而没有链接现有 QUIC 栈。这次构建不依赖 Eigen。原有 `src/quic` 和 MPQUIC 构建保留。

可执行文件通常为 `build-dci/src/dci-tunnel/examples/ns3.36.1-rdma-dci-tunnel-default`，版本/编译配置后缀以实际构建为准。报错提取：

```bash
grep -n -B 3 -A 8 -m 3 -E 'error:|fatal error:|CMake Error' dci-tunnel-build.log
```

## 由你构建后执行的最小检查

以下命令本交付没有执行。

```bash
cd ~/myrdma/simAI-optics
python3 experiments/scripts/run_dci_tunnel.py path-dry-001 --dry-run
python3 experiments/scripts/run_dci_tunnel.py path-single-001
python3 experiments/scripts/run_dci_tunnel.py path-two-001 \
  --flow-file experiments/workloads/dci-tunnel/two-flows.txt
python3 experiments/scripts/run_dci_tunnel.py path-probe-outage-001 \
  --config-template experiments/configs/dci-tunnel/probe-outage.conf
```

使用新的 run 名，脚本拒绝覆盖已有目录。`--dry-run` 只准备输入快照和命令，不启动模拟器。

- 单流检查：完整接收和源端最终 ACK；单 QP 只使用一条数据路径。另一条路径也应获得探测 RTT。
- 双流检查：POLICY=0 下两个 QP 分配到两条路径，具体绑定顺序取决于首包顺序。
- 可选中断检查：`probe-outage.conf` 在 0.5~0.9 s 对路径 0 的双向接收实施 100% 丢包。数据流提前在 0.1 s 开始，目标是检查空闲路径探测的失效和恢复，不是数据故障切换实验。接口保持 up，没有 OCS 模型或切换通知。

没有丢包和额外排队时，测量 RTT 预期接近 10/20 ms 加上传输开销，不应将这些预期当作已运行结果。

16 MiB 默认流小于 WAN 的传播 BDP（100G×10ms≈125 MB；200G×20ms≈500 MB）。此版本用于验证网关和路径维护，不能据此得出稳态吞吐、缓解 BDP 或论文性能结论。

## 输出与判定

每次运行保存到 `experiments/runs/<name>/`：输入快照、`logs/simulator.log`、`outputs/flows.csv`、`outputs/paths.csv`、`outputs/manifest.json`。终端仅打印 DCI_ 汇总和状态变化，不打印逐包日志。

- `DCI_BIND`：源网关的 QP 路径绑定。
- `DCI_STATE`：路径状态转换。
- `DCI_PATH_STATE`：各网关/路径的 RTT、数据/探测采样、ACK 和超时统计。
- `DCI_PATH`：封装数据、控制报文、ECN 和队列统计。
- `DCI_FLOW`：目的端累计唯一接收字节、目的接收 FCT、源最终 ACK FCT、内层重传字节和超时。
- `DCI_WIRE`：WAN 线缆字节，含两种 ACK 和探测等控制开销；drops 仅统计正向接收丢弃。
- 模拟器 `DCI_RESULT success=1`：所有流在截止前接收完整、源端完成且无网关协议/发送错误。它不保证所有路径最终都处于 ACTIVE。
- runner 还检查两个 CSV 的记录数、各流 success 和所有路径曾完成验证；仍应检查最终路径状态和中断时的状态日志。

`paths.csv` 为最终快照，状态变化时间在日志中，本版没有导出完整的逐样本 RTT 时序。

这是该仓库的网络协议抽象模型，不是真实 RoCE/QUIC 互通实现。没有 TLS/AEAD、ICRC、注册内存权限、完整 Read/Atomic、PCIe、CPU 或真实 RNIC/网关处理成本建模。路径 token 检查是仿真连通性验证，不能当作 QUIC 的认证与加密保证。
