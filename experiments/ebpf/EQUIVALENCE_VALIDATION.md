# 功能等价真实验收：2026-11-03

> **后续审计更正：** 下文记录的是历史流程验收，不是诊断证据或收益等价。
> 已发现常驻探针的实测影响、每事务 SET 偏差和正常/异常采样探针配置不一致。
> Payment 的单轮 +6.53% 也不能作为稳定收益证明。详见
> [未达收益门槛追查](GAIN_FAILURE_AUDIT.md)。旧 96/96 不包括后来新增的
> instrumentation 配置一致性检查，不能用它排除上述问题。

结论：原六个 TP 场景 **6/6 完成 eBPF 触发/采集、原差分、原模型、新候选应用、
业务 TPS 复测和恢复**。这不是 6/6 调优收益成功，更不是低开销结论。

设计与复现前置条件见 [EQUIVALENCE.md](EQUIVALENCE.md)。

## 本次真实执行

```bash
# 先按 EQUIVALENCE.md 配置原源码、依赖、profile、PG 源码和私有 API。
python3 -m experiments.ebpf equivalence-lab \
  --output results/ebpf-equivalence-six02 \
  --measure-seconds 8 --measure-repeats 2

python3 -m experiments.ebpf.equivalent_acceptance \
  --input results/ebpf-equivalence-six02
```

没有 mock、历史候选或预设修复参数。每个场景实际调用原
`ParameterLibrary / LLM_ACQ / LLM_DIS_SM`，各保留 10 次 API 请求/响应审计，
六场景合计 60 次。所用模型是操作者私有配置，不是本实验新增或内置的模型。
认证只通过私有环境传给子进程，不保存到仓库或命令行。

环境：

- Linux 5.4.0-216-generic、BCC 0.12.0、PostgreSQL 12.22；
- 全新临时 PG 集群，私有 Unix socket，shared_buffers 32MB；
  20 个连接上限，JIT 和并行查询初始关闭；
- 仓库原始五仓库生成脚本，包含 150 万行 order_line、50 万行 stock，
  数据/索引约 602MB，不是认证 TPC-C；
- 原六份工作负载文件，2 个 business 客户端，各场景使用原 pressure 客户端数；
- 固定 offered rate：business 1600 TPS、pressure 2500 TPS，
  调优前、候选、恢复阶段保持相同策略；
- 正常 profile 两窗，每窗 15 秒、300Hz；异常窗也是 15 秒、300Hz；
  不再使用之前一 PID、49Hz、2 秒的 smoke 预算；
- profiler 使用 eBPF task-clock on-CPU 事件，不启动 `perf record/script`；
  它不是硬件 cycles 的逐样本等价替代，正常/异常两侧事件保持一致。

## 六场景结果

六个场景均由 `ebpf_active_executor_threshold` 实际触发，未使用显式选择兜底，
也未要求出现 spill 事件。

| 原场景 | 正常两窗样本数 | 异常样本数 | 原差分/原模型/应用复测恢复 | 本轮收益状态 |
|---|---:|---:|---|---|
| 热点 Payment | 3,379 / 4,371 | 7,398 | 完成 | 达到实验门槛 |
| 热点 Stock Level | 4,338 / 3,666 | 11,792 | 完成 | 无有效收益 |
| Stock Level 突发 | 4,283 / 3,720 | 11,124 | 完成 | 无有效收益 |
| 五类交易混合突发 | 4,284 / 3,656 | 10,661 | 完成 | 无有效收益 |
| WAL/checkpoint 压力 | 3,614 / 3,639 | 11,869 | 完成 | 压力可比性未通过，不计收益 |
| 热点 Order Status | 4,410 / 3,363 | 7,488 | 完成 | 无有效收益 |

所有异常采样的授权客户端 PID 都取得了 CPU 样本，没有只观测一个 backend。
原差分返回 545–650 个超出正常范围的函数，再由原匹配器处理，
不是通过“有 BufFileRead 才算识别”的自定义判定器进入模型。
这证明功能链路恢复，不证明其中每个函数都是正确根因。

### 实际业务 TPS

下表为真实 pgbench 进度，经原 `_tpcc_tps_summary` 剔除启动阶段后，
再对两次 trial 取中位数。保留原 `_tpcc_comparison` 的压力检查。

| 场景 | 调优前 | 候选后 | 恢复后 | 判定 |
|---|---:|---:|---:|---|
| Payment hot | 1170.50 | 1246.95 | 928.05 | +6.53%，本轮压力检查通过 |
| Stock Level hot | 1009.95 | 938.00 | 825.28 | 未通过收益门槛 |
| Stock Level burst | 917.25 | 877.25 | 872.00 | 未通过收益门槛 |
| Full mix surge | 961.00 | 956.50 | 634.78 | 未通过收益门槛 |
| WAL/checkpoint | 429.50 | 821.25 | 783.25 | 表面 +91.21%，但压力完成量变化过大，**拒绝计为收益** |
| Order Status hot | 1348.30 | 1073.05 | 1104.50 | 未通过收益门槛 |

这里只做了短窗口功能验收，宿主机仍有其他服务，期间也有显式验证任务。
恢复后 TPS 的明显波动显示不能把这些数据当作稳定性能结果或因果证明。
没有与旧 perf 路径做严格同条件 A/B，没有置信区间，也不证明 Payment
在重复实验中稳定提升 6.53%。下一阶段才能讨论调优效果和采集开销优化。

“设置已恢复”来自真实设置核验，不是要求恢复后 TPS 数字恰好相同。
候选均未改 `fsync`、`synchronous_commit`、端点或业务 SQL。

## 功能验收：6 × 16 项，96/96

`acceptance.json` 逐场景验证：

1. 真实正常/异常 profile 都完成；
2. 采样事件、频率、窗口及 PG 二进制一致；
3. 所有 task-bound 采样 FD 已关闭；
4. 模型前细粒度探针已卸载；
5. 正常/异常不再退化成单 PID smoke；
6. 原差分和函数匹配确实执行；
7. 本次原模型真实 API 证据存在；
8. 原解析候选确有应用、复测及恢复；
9. 调优前、候选、恢复的 offered 外压/客户端政策一致；
10. 目标为业务 TPS，且使用原稳定 TPS 汇总；
11. 原压力可比性检查执行；
12. 没有降低持久性或更换连接端点；
13. resident 跨越模型阶段并恢复监控；
14. resident 最终完整卸载；
15. 不依赖 Prometheus、perf 命令或历史候选；
16. 功能完成与数值收益成功明确区分。

每个执行候选均核验设置恢复、业务聚合不变量保持，并执行恢复后的压力复测。
数据不变量是有界业务检查，不是形式化 SQL 等价或逐字节数据库相等证明。

## 错误与观测边界没有隐藏

- 早期调试暴露了 Linux 5.4 不支持较新的 `bpf_probe_read_user` helper，
  以及有限 workload 退出后不能重新通过 PID 查符号来卸载的问题。
  已改成兼容 helper 和保存 owner 的 uprobe event 句柄；相关旧失败目录保留，
  不计入本次六场景通过数。
- CPU profile manifest 仍保留 user/kernel stack helper 失败计数；
  kernel 栈并不是每次用户态采样都可获得，user 栈也有缺失。
  **没有把这些计数清零或宣称完整无损栈。**
- 本轮未报告 profile map 容量错误或执行关联嵌套深度溢出；
  各异常窗的关联完整性标记为 true，PG query ID 仍可能不可用。
  该标记不代表全部 SQL 生命周期/并行 worker 都已精确归属。
- 原采样率比较器会忽略正常窗未出现的函数；原行为未更改，
  anomaly-only 函数另外审计。未训练新的 Lasso/SHAP 根因模型。
- VFS、WAL、锁、checkpoint 的原始资源事件保存在 `anomaly/detail/`；
  模型主诊断证据是原 CPU 差分与源码关联，不能假装所有 I/O/cache
  观测都与旧 Prometheus 指标逐字段等价。未采指标仍为 unknown。

## 清理与回归

- 全部实验 profiler/collector/worker 已退出，临时 PG 集群已停止删除；
- tracefs 的实验 uprobe 定义最终为 0；
- 原 keeninsight 服务与原 PostgreSQL 服务仍运行，未启停或改配置；
- 扩展后的真实内核回归 `results/ebpf-equivalence-kernel-regression/smoke.json`
  25 项通过；
- `bash scripts/test.sh`：166 项，162 通过、4 项显式跳过；
  新增功能等价契约测试 20 项，不加载 BPF、不连数据库、不调用 API；
- 发布检查与 `git diff --check` 通过。

原始产物位于 Git 忽略的 `results/ebpf-equivalence-six02/`，不提交数据库、
凭据或模型交互大日志。此版本完成的是**独立实验入口中的六场景系统级功能
等价链路**；未接管在线 Portal/bridge，没有完成 DREAM 的 SQL/模型路径迁移，
也未覆盖需要重启的参数，不能宣称整篇论文所有组件或所有数值效果已等价。
