# 原来六个 TP 场景：eBPF 替换可行性与实际验证

日期：2026-11-03。依据 `app/tpcc_transaction_cases.py::FOCUS_SCENARIO_IDS`，
不是另外挑六个容易成功的排序/落盘查询。

**历史评估页：**下面记录的是早期低预算 smoke 的边界。后续已按
“先做功能等价”新增 [EQUIVALENCE.md](EQUIVALENCE.md) 所述的多 backend
正常/异常差分、原模型与 TP 业务复测入口；不要把本页当作新入口的能力说明。

**结论：采集可以按少数几类机制替换，但目前不能宣称六个场景已经完成同等
调优效果的迁移，更不能承诺“很小代价”已经通过验收。**
原来的 `BufFileCreateTemp → work_mem → 只读 SQL 复测` 只覆盖首个落盘场景，
不能硬套在全部原始 TP 场景上。

## 逐场景最小采集方案

| 原场景 | 有诊断意义的常驻事件 | 按需取证 | 仍需验证的关键问题 |
|---|---|---|---|
| `tp_payment_hot` 热点 Payment | `ProcSleep`：实际重量级锁等待入口 | 目标 backend 的等待时长、必要时 `XLogFlush` 时长 | 锁热点不等于 WAL 慢；改变锁超时不等于解决热点 |
| `tp_stock_level_hot` 热点 Stock Level | 共用稀疏事件集合；纯 CPU 饱和时可能没有事件 | 选定少量 backend，短窗低频 CPU 栈采样；有依据再补 I/O 取证 | 是执行器 CPU、计划、缓存还是 I/O；不能靠 spill 规则猜测 |
| `tp_stock_level_burst` Stock Level 突发 | 同上 | 与上一个场景复用读/CPU 采集，不重写一套采集器 | 正常与异常的差异、并发分摊及样本量 |
| `tp_full_mix_surge_high` 五类交易混合突发 | 锁等待、checkpoint、临时文件事件 | 先采样定位，再选择锁/WAL 或读/CPU 探针 | 不能将“流量增加”或某个函数出现直接判成唯一根因 |
| `tp_wal_checkpoint` WAL/checkpoint 压力 | `CreateCheckPoint`，**包含 checkpointer PID** | `XLogFlush`、`pg_fdatasync`、checkpoint 的调用与时长 | WAL 等待不等于磁盘同步耗时；客户端观测不能替代后台进程 |
| `tp_order_status_hot` 热点 Order Status | 共用稀疏事件集合；允许显式选定目标 | 短窗 CPU 栈，必要时进一步查看计划/读取路径 | 小结果集也可能在计划、索引/缓存或并发路径上耗时，未必落盘 |

新 `six-resident` profile 只保留：

```text
ProcSleep / CreateCheckPoint / BufFileCreateTemp
```

计数在内核聚合，1 秒读取，通知冷却合并。它**不常驻附着**
`ReadBufferExtended`、`ExecScan`、`MemoryContextAlloc`、`LockAcquireExtended`、
`XLogFlush` 或 `sched_switch`。在热点函数里做一个 if 不能消除探针触发成本。
这些语义事件也不是任何负载下都必然低频，尤其锁竞争严重时仍需预算约束。

**没有伪装自动触发能力：**这组事件不能发现所有 CPU-only 读压力。
本次六场景实验使用显式选择场景/目标的入口；生产环境的 CPU 饱和触发仍需
单独验证低频哨兵。不能为了补齐告警，改成所有 SQL、所有 buffer hit 常驻探测。

## 这次实际运行了什么

入口：

```bash
python3 -m experiments.ebpf.six_scenarios audit \
  --output results/six-audit-new

python3 -m experiments.ebpf.six_scenarios lab \
  --association <原函数与参数关联JSON> \
  --output results/six-smoke-new
```

`audit` 读取原始六场景定义、工作负载文件 hash、探针符号及尚缺的迁移步骤。
不导入并启动原控制台。

`lab`：

- 新建临时独立 PG12 集群，不连接原 keeninsight 数据库，不启停原服务；
- 使用仓库原 `scripts/tpcc_schema.sql`，5 个仓库，150 万行 order_line，
  50 万行 stock，数据和索引约 602MB；不是认证 TPC-C；
- 使用六场景的**原始 pgbench SQL 文件**及各自客户端数，不换成排序微基准；
- 同时运行两类 worker，保留 business / pressure 的角色边界；
- 试运行的 offered load 刻意限制为 business 200 TPS + pressure 500 TPS，
  **不是旧演示的完整压力，也不是新旧收益对照实验**；
- 常驻采集覆盖显式列出的客户端和 checkpointer；细粒度只选少量目标；
- 读/CPU 诊断按需使用 **单 backend、49Hz、2 秒**的 eBPF task-clock 栈采样，
  停止采样后才符号化，不调用 `perf record`；
- 锁/WAL 诊断采用短窗入口/返回探针，之后显式卸载；
- 不调用模型、不回放历史候选、不应用参数，不把这次 smoke 当作调优成功。

结果在 `results/ebpf-original-six-smoke-v2/summary.json`：
**6/6 完成原工作负载执行和采集 smoke**，没有报告 collector 丢失或 BPF 健康错误。
全部实验 collector/worker 结束，临时集群销毁。

### 实际证据与不足

| 场景 | 授权进程上的 resident `ProcSleep` 调用合计 | 选定一个压力 backend 的 CPU 样本 | 按需取证/判读边界 |
|---|---:|---:|---|
| 热点 Payment | 120 | 未额外采样 | 目标 backend 记录 13 次等待、224 次 `XLogFlush`；有语义信号，不等于已证明主因 |
| 热点 Stock Level | 1 | 5 | 样本不足，不能承诺原诊断质量 |
| Stock Level 突发 | 5 | 6 | 同上，需要在有界预算内扩大样本覆盖或窗口 |
| 五类交易混合突发 | 55 | 3 | 目标 backend 601 次 `XLogFlush`；稀疏 CPU 样本不能排除其他瓶颈 |
| WAL/checkpoint | 81 | 未额外采样 | 161 次 `XLogFlush`、1 次 `pg_fdatasync`；没有自然触发 checkpoint |
| 热点 Order Status | 2 | 1 | 明显不足以稳定区分计划/执行/缓存等原因 |

resident 合计包含 business、pressure 和后台目标，不等于某一条业务 SQL 的
等待数；原始记录保留 PID/出生身份。CPU 数据明确是 sample，不是函数调用数。

六个场景的观测窗口均没有临时文件创建，说明继续套用唯一的 spill 触发器
会漏掉这些运行。也不能为了证明 checkpoint 采集有效，就把不存在的
checkpoint 报成零成本诊断成功。

### 两个额外的探针语义对照

```bash
python3 -m experiments.ebpf.six_scenarios controls \
  --output results/six-controls-new
```

此入口在另一个临时集群构造约 200ms 的真实行更新阻塞，并显式执行一次
`CHECKPOINT`，验证 `ProcSleep` 的等待时长和 checkpointer 上的函数事件。
这是**探针存活/语义测试，不是原场景自然出现该根因的证明**。
本机 5/5 检查通过：捕获 1 次约 199.9ms 的真实等待，以及 checkpointer 上
1 次约 9.2ms 的 checkpoint。结果见
`results/ebpf-six-semantic-controls/controls.json`。

## 为什么现在还不能说“和之前一样好”

### 1. 旧目标是业务 TPS，不是单条只读 SQL

原控制台区分 normal business worker 和外部 pressure worker；
`app/lab_controller.py::_tpcc_comparison` 比较固定外压下的业务 TPS。
当前已跑通的 eBPF 调优控制器则以单条只读 SQL 耗时为目标，
默认只允许 `work_mem`。Payment/混合交易包含写事务和保存点，
不能当作一条只读 SQL 塞入同一个验证器。

因此缺的是**TP 目标适配和六场景的完整模型/应用/复测接线**，
不只是再添加几个采集函数。

### 2. 低频能采到数据，不等于数据足以诊断

本次读场景只得到 1–6 个样本。需要在总时间、采样频率和目标数预算内按需
增加证据；达不到最小证据要求应停止，而不是提高解释的确定性。
单 PID 原型也尚未实现自动跨 backend 扩展和相应预算调度。

另外，不能把 CPU 栈全部祖先直接交给函数—参数关联库：
`PostmasterMain`、`PostgresMain` 等通用祖先在极少样本中就匹配出大量无关参数，
包括连接数、启动和日志参数。正式接线需要正常/异常对照、通用框架帧抑制、
场景安全白名单和证据置信度；匹配到参数并不等于存在调优因果。

锁路径也有同类限制：`ProcSleep` 会匹配锁超时/日志设置，
但调整它们通常不能消除热点行串行化。
晚附着的返回探针也不能补回已错过的入口；等待跨越整个诊断窗口时，
不能把尚未完成的时长报告成零。正式接线应保留稀疏等待事件的配对信息，
或明确输出不完整/下界证据。

### 3. 旧收益的可靠性条件必须一致

检查本机旧 TP 验证归档发现，部分旧候选包含 `fsync=off` 或
`synchronous_commit=off`，而且并非每次原场景验证都达标。
旧控制台还存在“历史候选回放”和“当前在线告警的新模型候选”不是同一条
业务 TPS 闭环的已知边界。

不能在 eBPF 一侧放松持久性，或使用旧候选预置修复，来制造“同样好的效果”。
新旧路径应使用相同数据、offered load、持久性/重启/内存政策与验收门槛。

## 后续真正的六场景验收

顺序应是：

1. 保留一套共享常驻采集，按读/CPU、锁、WAL 三类短窗细化，不建六套全量采集。
2. 将 eBPF 证据接回原函数/参数和源码链，保留 sample/count/wait 的真实语义；
   样本不足、范围不全或知识匹配有歧义时停止自动调优。
3. 增加以**固定外压下业务 TPS**为目标的实验控制器，
   复用原模型、候选校验及临时配置执行，不依赖 Prometheus 告警或旧候选文件。
4. 每个场景独立完成：识别 → 新候选 → 临时应用 → 业务复测 → 恢复，
   并验证没有降低持久性或改变压力目标；允许记录“没有安全有效候选”。
5. 再做同条件的采集替换对照：诊断一致性/漏报、业务收益与恢复、
   resident 的 TPS/p99 影响、诊断窗口影响及内存/输出预算分别验收。

尚未执行第 3–5 步，故不能把本页的 6/6 smoke 写成 6/6 调优成功。
共享宿主机和 BCC/Python 原型也不足以证明生产环境低开销，
当前没有发布新的开销百分比。

## 回归与代码

- `six_scenarios.py`：原场景核对、隔离 workload smoke、语义对照。
- `cpu_sample.py`：短窗、单 task FD、49Hz 上限的 eBPF CPU 样本原型。
- `catalog.py` / `collector.bpf.c`：稀疏哨兵和按需锁/WAL 探针。
- `runtime.py`：显式多 PID 参数及生命周期管理。
- `tests/test_ebpf_six_scenarios.py`：场景身份、采样预算、ABI、
  快照去重、非因果关联语义、多 PID 命令等契约。

扩展后的真实内核回归 `results/ebpf-six-expanded-kernel-smoke/smoke.json`
25 项通过，包含执行完成、错误中断、按需 SQL 文本、动态卸载、轻量计数、
system/query 隔离和丢失检查。它与六场景 smoke 一样，不替代业务收益验收。
完整回归 `bash scripts/test.sh` 为 146 项：142 通过、4 项明确跳过。
