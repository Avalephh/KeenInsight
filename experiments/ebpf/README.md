# 论文采集实验：独立于展示系统的 eBPF 路径

基线：`opensource/clean-20261027` / `54026fc`。实验分支：
`experiment/ebpf-paper-20261103`。

依据用户提供的 `KeenInsight_VLDB_Demo.pdf` 第 2.1、2.2 节（第 2–3 页）。
这里重建**采集层实验**，不是把原来 Prometheus 的数据换一种方式展示。
纯采集入口不启动/依赖 bridge、Prometheus、Grafana、postgres_exporter、perf record、
DREAM 或 LLM；新加入的闭环入口会显式调用原 SysInsight 的模型 API。
两类实验均不停止本机已经运行的服务，原展示路径保持不变。

## 当前优先目标：贯通原诊断与调优链路

已新增常驻事件触发、有界诊断、原 SysInsight 模型与候选流程、
临时应用/复测/恢复的独立入口。使用与边界见
[PIPELINE.md](PIPELINE.md)。下面的开销实验保留，但不作为当前优先工作。
真实贯通和预算卸载结果见 [PIPELINE_VALIDATION.md](PIPELINE_VALIDATION.md)。
原控制台六个 TP 场景的采集替换评估与真实 smoke 见
[SIX_SCENARIOS.md](SIX_SCENARIOS.md)：6/6 采集可运行不等于 6/6 调优成功。

## 轻量化修订 v2：不把“eBPF”本身当作低开销证明

默认常驻模式现在是：

- **system scope**：不附着每条 SQL 的 ExecutorRun 起止探针，不声称获得逐次 SQL 数据；
- **events profile**：只附着 `BufFileCreateTemp`、`XLogFlush` 两类语义事件，
  不默认附着 `MemoryContextAlloc`、逐元组 spill、每次文件写入等高频路径；
- **entry counts**：只用入口探针计次数，不为纯计数增加返回探针，
  不伪造函数耗时、返回字节数或 CPU 时间；
- **kernel aggregation**：计数在内核累计，定期读取；
  首次临时文件创建等指定异常立即通知，之后按 span/probe 冷却合并通知；
- 默认没有全局 sched_switch、VFS/TCP 探针，没有堆栈、SQL 字符串拷贝；
- `query` scope 才加入每次 SQL 执行边界；普通短 SQL 聚合为完成数与延迟直方图，
  只为异常/长查询保留执行明细。聚合不等于对计数抽样。

这是两级采集设计：常驻语义事件监控 → **按需**增加 SQL/资源细节。
不是靠每次进入高频函数后做一个 if，就声称未选中的调用“没有成本”；
探针附着本身的触发成本仍需 A/B 测量。
`--scope system` 不提供正常查询的 QPS/逐次关联，不能用“未采”冒充零。
独立 `collect` 命令的细粒度范围由操作者显式控制；新 `pipeline` 控制器已经
实现一次 incident 的事件触发/细粒度卸载/回到常驻链路，但不是完整生产守护进程。
晚开启探针不能追溯已经发生的 SQL 起始事件。闭环的首个落盘场景使用更小的
`resident` profile，只保留 `BufFileCreateTemp`，不常驻 WAL 函数。

主要开关：

| 开关 | 常驻默认 | 明细/消融对照 |
|---|---|---|
| `--scope` | `system` | `query`：每次执行起止均有 Uprobe 成本 |
| `--profile` | `events` | `boundary` 无资源探针控制组；旧 `core/selected/all` 用于对照 |
| `--query-output` | `aggregate` | `all`：逐次开始/结束消息 |
| `--metric-mode` | `counts` | `timed`：入口/返回成对探针 |
| `--cpu-mode` | `off` | `sched`：增加调度 tracepoint |
| `--detail-level` | `lightweight` | `full`：上述明细开关的预设，不会自动选择 profile |

v2 的 `schema=keeninsight.ebpf.v2` 新增临时文件创建探针、完成聚合与缺失语义；
旧 v1 结果保留在原目录，不拿新开关的结果替代旧结果。

## 与论文逐项对应

| 论文设计 | 实验实现 | 边界 |
|---|---|---|
| 数据库 Uprobe + 内核 Kprobe | 指定 DB 函数、按需执行边界及 VFS/TCP | 第一阶段仅 PG12；内核资源探针不默认常驻 |
| query ID + 时间区间关联 | 内核维护执行栈，单调时钟；PID + OS 出生时间 + 执行开始时间构成 execution ID | PG query ID 未提供时为 null，不伪造 |
| 只附着有诊断贡献的函数 | 固定候选白名单；可读离线 Lasso 模型选择非零系数探针 | 无训练模型时明确是 bootstrap，不是 Lasso |
| 在线调整探针集合 | 按 Lasso 重要性/静态 bootstrap 优先级，压力下动态卸载非核心探针 | 当前单向剪枝；不实现在线扩展/重训练 |
| 正常聚合，异常立即上报 | 每次真实调用更新内核累计值；定期读聚合快照；阈值或长查询触发 perf-buffer 通知 | query scope 也不默认发送普通 SQL 边界；通知合并不丢弃计数 |
| Kafka 消息队列 | 同一事件 schema 可选 Kafka producer；本地 JSONL 是默认实验落地 | Kafka broker/故障恢复需另外验证；没有伪造 ACK |
| 低开销 | 独立集群上的随机顺序 baseline/core/selected/all A/B 入口 | eBPF 不自动等于低开销；需正式多轮实测 |

## 数据路径

```text
显式 backend PID allowlist（不自动扫描生产库）
  │
  ├─ 默认 Uprobe: BufFileCreateTemp / XLogFlush ── 系统级关键事件计数
  ├─ 按需 Uprobe/return: ExecutorRun ── execution span / PG query ID
  ├─ 按需 Uprobe: BufFileWrite / ExecHashJoinSaveTuple / LockAcquireExtended
  ├─ 可选 Uprobe: MemoryContextAlloc
  ├─ Kprobe/return: vfs_read / vfs_write / 可选 tcp_sendmsg / tcp_recvmsg
  └─ 可选 sched_switch: on-CPU 计时；进程退出时解除 PID 授权始终保留
          │
  ├─ 普通调用：内核 map 中累计次数/时间/资源量
  │            同时保留授权进程的系统级总量与 SQL 内的细分量
          ├─ 异常调用：perf buffer 立即输出通知
          └─ 用户态：周期聚合快照 + 最终完成/中断记录
                    ├─ 私有 JSONL + manifest（默认）
                    └─ Kafka（显式配置才启用）
```

这不是 CPU 栈采样，也不查询 `pg_stat_activity` / `pg_stat_statements`。
开启时的 `sched_switch` 和 Kprobe 是全局 hook，但在 map 查找后仅处理授权范围；
不能把它们说成“对系统内其他进程绝对零成本”。Uprobe 按显式 PID 附着。
执行器外的提交/WAL/锁等调用保留在 backend 累计统计中，不强行绑定 SQL。
后台进程也必须显式授权，不自动推断其工作属于哪条 SQL。

## 前置条件与安全

- Linux、root 或等效 BPF/perf 权限；tracefs 可用；匹配内核 headers；
  BCC Python bindings、clang、C 编译器、binutils。
- PG12 二进制及其 server headers / `pg_config`。`pg_layout.c` 从真实 headers
  生成 `QueryDesc` / `PlannedStmt` 偏移；运行记录保存 headers 版本与二进制 SHA256。
  不硬编码猜测偏移。跨大版本拒绝运行；同主版本定制 ABI 仍需操作者核对。
- 默认不采 SQL 原文；需要时显式 `--capture-sql`，只保留最多 191 字节的
  `sourceText` 前缀。它可能含整个多语句 batch，不是精确语句切片。
- 仅允许显式提供 1–32 个、可执行文件与目标 postgres 一致的 backend PID。
  启动时二次核验进程出生身份；退出 tracepoint 删除授权，避免 PID 复用。
  应在宿主 PID namespace 内运行；容器 PID 映射未验证。
- 运行目录私有；事件文件 0600；不覆盖旧数据；默认 128 MiB 输出上限、
  最长 1 小时；输出满了报错停止而非悄悄截断。
- 不自动修改 `shared_preload_libraries`、数据库配置或系统 sysctl。
  `doctor` 不加载探针。`collect` 不发业务 SQL。只有 `lab` 显式创建可销毁的测试库。

本机已具备 PG12.22 / Linux 5.4 / BCC 0.12 的依赖；这不代表其他内核版本已验收。

## 运行

从仓库根目录执行：

```bash
# 只读检查目标版本、符号和 ABI offsets；不附着
python3 -m experiments.ebpf doctor \
  --pg-config /usr/lib/postgresql/12/bin/pg_config

# PID 必须由实验工作负载/控制连接明确提供，不会自行发现外部连接
python3 -m experiments.ebpf collect \
  --pid <实验连接的_backend_pid> \
  --scope system --profile events --duration 30 \
  --output results/ebpf-first

# 显式增加 SQL 关联：短 SQL 聚合，异常才有明细；仍需实测边界探针成本
python3 -m experiments.ebpf collect \
  --pid <实验连接的_backend_pid> \
  --scope query --profile events \
  --output results/ebpf-query

# 真正的内核 + PG12 验证：新建临时独立集群，仅 Unix socket，
# 测试目标过滤、临时文件、异常通知、错误中断、游标分段，最后停止并销毁。
# 不连接已有 keeninsight 数据库；不启停现有监控。
python3 -m experiments.ebpf.lab smoke --output results/ebpf-smoke

# 性能比较：每一轮随机排列四种模式，持久连接、相同 SQL、预热后计时。
python3 -m experiments.ebpf.lab benchmark \
  --repeats 5 --queries 200 --batches 20 --seed 20261103 \
  --output results/ebpf-ab

# 单独消融：消息输出 / 返回探针 / 调度 hook / 高频函数 / SQL 边界
# CPU 测试 work_mem=16MB，spill 测试为 64kB，避免把 CPU 测试意外变成 spill。
python3 -m experiments.ebpf.overhead \
  --repeats 5 --seconds 3 --workloads short,cpu,spill \
  --output results/ebpf-overhead

# 不需要 root、数据库或 BCC 的契约单测
python3 -m unittest discover -s tests -p 'test_ebpf_experiment.py' -v
```

`lab` 必须显式运行，依赖 postgres OS 用户和 PG12 工具。它在随机临时目录
启动独立集群：TCP 关闭、并行查询关闭、JIT 关闭、shared_buffers=32MB，
不继承展示系统的 pg_stat_statements、hint 或调优状态。fsync 保持默认开启。
测试使用本机共享 CPU，因此结果不能声称是无干扰的正式论文实验。

## 输出和计数语义

`manifest.json`：二进制/内核/ABI、探针集合、选择模型、配置、起止时间、错误、
丢失通知及 BPF 健康计数。

`events.jsonl`：

- `query_begin`：一次 `ExecutorRun` 调用开始。
  仅 `--scope query --query-output all` 输出；敏感 SQL 还需显式 `--capture-sql`。
- `query_snapshot`：尚未完成执行的累计计数，带当前经过时间和长查询标记。
  采用 sequence 首尾检查，拒绝明显撕裂的并发 map 快照。
- `metric_alert`：单次函数耗时/返回资源量/已运行时长超过门槛的立即通知。
  **已包含在累计值中，不要再加一次。**
- `query_end`：完成 span 的最终累计值，优先于中间快照。
  `aggregate` 模式只输出异常/长执行，不是所有正常执行的明细。
- `query_aggregate` / `query_aggregate_final`：顶层 ExecutorRun 完成数、中断数、
  总耗时、最大值及 log2 微秒直方图；包含已输出的明细，不重复相加。
  仅 query scope；直方图不能冒充精确逐次 p99。
- `query_aborted`：事务/子事务中断或 backend 退出，不能算作成功完成样本。
- `incomplete_capture_stop`：采集停止时仍在执行，不能伪造成完成查询。
- `backend_snapshot` / `backend_final` / `backend_end`：授权进程系统级累计统计、
  采集停止时的终值、进程退出前的终值。包含执行器内外的调用，
  使用 `activity_id` 而非伪造 `execution_id`，不能再与 SQL 总量相加。
- `collector_health`：采集进程 CPU、探针调用计数、动态卸载及通知丢失。

入口计数模式移除未测量的函数耗时、返回字节数等字段；关闭 CPU 采集则移除
on/off-CPU 数值字段。manifest 保存实际开关和 `message_stats`，区分合并通知、
省略普通边界消息与真正 perf 通知丢失。合并通知保留完整累计计数。

每个执行记录带 `probe_coverage`；运行中卸载标记为 partial，
卸载后才开始的执行移除该指标，不把“没采”冒充测量值零。

重要区别：

1. `ExecutorRun` 是执行器**调用段**，不含完整 parse/plan；游标多次 FETCH
   对应多个 span，不等于一个客户端 SQL 的端到端延迟。DML 的 ExecutorFinish
   阶段、utility/background 工作不在 SQL span 内；其中被选择函数的调用
   可进入显式授权进程的 backend 系统级统计，不能当成完整业务事务。
2. 显式开启时，`on_cpu_ns` 由调度事件计时，包含嵌套执行；`off_cpu_ns = wall - on_cpu`，
   不能进一步冒称是 I/O 等待、锁等待或调度排队的精确分解。
3. 资源归给最内层活跃执行；函数时间是 inclusive，多个函数之间可能重叠，
   **不能相加成 SQL 总时间**。
4. VFS 返回字节可能命中页缓存，不是物理磁盘吞吐；TCP 返回字节不是线上字节；
   `MemoryContextAlloc` 请求量不是存活/峰值内存；
   `LockAcquireExtended` 包括无等待获取，不是纯锁等待时间。
5. 并行 worker 不会凭 SQL 文本猜测归属；本阶段只观测显式 PID，lab 禁用并行。
   操作符 plan-node ID、自动 worker 关联、后台 WAL/I/O 的因果归属待后续实现。
6. 最大嵌套深度 4，map/输出大小有界。容量、读取和丢失错误必须随实验结果报告。
   perf `lost` 与提交失败可能指同一压力现象，不应简单相加成精确丢失总数。
   kretprobe 自身的内核 missed-return 完整核算尚未实现，健康计数为零不是无损证明。
   采集开始/结束边缘尚未配对返回的函数可能不完整，backend 终值是本次采集窗口，
   不是进程一生的精确累计。正式 TPS 仍需事务提交边界或 workload 实测补充。
7. PostgreSQL longjmp 错误路径没有正常 uretprobe。事务/子事务回滚入口会保守地
   中止并清空该线程的全部活动 span，避免把旧资源归给后续 SQL；
   PL/pgSQL 捕获异常后继续执行的外层 span 不做虚构补全，完整恢复待扩展。

## Lasso：只在有真实标签后启用

训练输入是一行一个独立 trial 的 JSONL：

- `trial_id`：唯一的训练 trial ID；
- `features`：所有 trial 采用相同探针特征集，key 使用 catalog 的 probe name；
  推荐固定窗口的调用率或归一化资源量，不能混用单位；缺失不是零；
- `degradation`：独立测得的退化标签，例如相同工作负载相对正常配置的延迟增幅。

```bash
python3 -m experiments.ebpf train-selector \
  --input <真实训练_trials.jsonl> --alpha 0.05 --output <selector.json>

python3 -m experiments.ebpf collect \
  --pid <backend_pid> --selector <selector.json> \
  --output results/ebpf-lasso
```

实现采用标准化特征的 L1 坐标下降，记录训练输入 SHA256、trial IDs 和系数。
这是训练器，不是随代码附带的已验证模型。必须按 workload/故障场景划分训练与测试，
避免相邻窗口泄漏；需要留出集和多轮选择稳定性实验。
**不要把上述 monitoring overhead A/B 结果当作根因贡献训练集**，
否则会把探针本身的成本学成“异常原因”。

## 自适应预算与正式实验

压力指标是采集进程 `user+system CPU / wall` 和 perf 通知丢失。
它不包含发生在数据库进程中的所有探针成本，**不是论文的端到端开销结果**。
压力超过预算时，按 Lasso 绝对系数（或明确标注的 bootstrap 优先级）
逐个卸载非核心探针。旧 profile 的 VFS 核心探针保留用于历史对照；
events profile 只保护临时文件创建探针，不强制开启 VFS/CPU/SQL 边界。

可选 `--cost-model` 接收独立校准文件，要求：
`schema=keeninsight.probe-cost.v1`、`calibration_id`、匹配的
`binary_sha256` / `kernel`、每个所选探针的 `ns_per_call`。
该成本应来自独立的单探针开/关重复试验，而非手写“经验值”；
还须有与本次配置完全相同的 `collection`（scope、计数/计时、输出与 CPU 模式），
否则拒绝复用。
根据实际调用率估算额外 CPU 后使用 `--overhead-budget` 剪枝。
估算不能替代固定负载下的真实 TPS/延迟/CPU 开销报告。

建议正式矩阵：

1. 无采集 / 原展示采集 / eBPF core / Lasso-selected / all candidates；
2. 正常负载 / sort spill / hash spill / 锁竞争 / WAL 压力；
3. 固定采集集合 / 重要性剪枝 / 重要性 + 预算剪枝；
4. 正常聚合 / 全量消息 / 异常感知消息；
5. 报告 TPS、p50/p95/p99、DB/collector CPU、消息量、内存、丢失与目标事件召回，
   随机顺序、多次重复、独立 baseline、误差区间及探针集合变化。

当前 smoke 与 A/B 只验证采集器可运行、数据身份和比较入口，不宣称完成
论文的根因诊断、调优收益或低开销结论。

## Kafka（可选）

安装独立的 `kafka-python` 依赖后，为 collect 增加
`--kafka-bootstrap <broker:9092> --kafka-topic keeninsight-ebpf`。
始终保留本地 JSONL；按 execution ID 分区，要求 broker ACK，关闭时等待发送完成。
发送失败令本次采集失败。没有自动重放/事务去重，broker 故障测试尚未完成。

## 主要文件

- `collector.bpf.c`：执行上下文、资源探针、内核聚合及异常通知。
- `collector.py`：ABI 校验、PID 限制、附着/卸载、快照、落盘和可选 Kafka。
- `catalog.py`：探针白名单与选择/剪枝规则。
- `selection.py`：离线 Lasso 与训练数据来源。
- `lab.py`：可销毁 PG 集群中的真实采集 smoke 和随机顺序 A/B。
- `overhead.py`：libpq 持久连接、多负载、随机顺序逐项开销对照；
  分别报告 QPS 损失、客户端 p50/p95/p99、DB/collector CPU、输出量及采集完整性。
- `tests/test_ebpf_experiment.py`：无需 BPF 权限的契约测试。

实现接口参考：BCC 项目 `docs/reference_guide.md`；
PostgreSQL `REL_12_STABLE/src/include/executor/execdesc.h`。

首次真实内核验证与有边界的 A/B 数字见 [VALIDATION.md](VALIDATION.md)。
**v2 轻量化改动、逐项消融及五轮多负载结果见 [LIGHTWEIGHT.md](LIGHTWEIGHT.md)。**
