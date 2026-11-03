# 采集到调优的真实贯通验证：2026-11-03

本次先验收用户要求的链路：
**少量常驻事件 → 异常/选定对象 → 有界细粒度采集 → 卸载 → 原诊断调优 → 复测恢复**。
没有继续用开销微基准替代此目标，也没有预设“优化后配置”冒充模型结果。

## 版本和隔离

- 实验分支：`experiment/ebpf-paper-20261103`。
- 基线：2026-10-27 的 `opensource/clean-20261027`，
  `54026fc41c8c47eca28f3e4eae1fc1fc5e22c0e6`。
- 设计输入：本地 `KeenInsight_VLDB_Demo.pdf` 第 2.1–2.2 节。
- Linux 5.4.0-216-generic、BCC 0.12.0、PostgreSQL 12.22。
- 使用临时独立 PG 集群和私有 Unix socket，未连接原 keeninsight 数据库；
  结束后停止并删除集群。没有修改、重启、停用原展示服务。
- 采集、触发和诊断输入均不依赖 Prometheus、Grafana、postgres_exporter 或
  `perf record`。调优阶段显式复用原 SysInsight 库和真实模型 API。
- 宿主机仍有其他服务，**不声称整机是无人干扰的纯净性能环境**。

## 首个完整场景

创建 80,000 行排序数据，测试负载显式设置 `work_mem=64kB`，制造临时文件。
这是故障前提，不是优化候选。常驻采集启动后再运行负载，避免只检验离线文件。

当前实现的常驻 `resident` profile **只附着 `BufFileCreateTemp` 入口**，
计数内核聚合，1 秒汇总；不采集每条 SQL 起止、栈、CPU 调度或 SQL 原文，
也不让 WAL/提交或逐元组函数常驻。事件通知按 probe 冷却合并。

临时文件事件触发后，只为目标 backend 加入执行边界及诊断函数：
`BufFileRead`、`BufFileCreateTemp`、`ExecHashJoinSaveTuple`、
`LockAcquireExtended`。默认预算为 5 秒 / 8MiB / 10,000 条输出事件，
取得两次完整执行的证据即可提前卸载。

当前最终自动触发运行 `results/ebpf-resident-chain-final`：

1. 真正收到 `resident_temp_file_creation`。
2. 两次完成执行中观测到 `BufFileRead` 26,112 次、
   `BufFileCreateTemp` 2 次；这些是函数调用数，**不是 CPU 样本**。
3. 细粒度探针约 **0.846 秒**后卸载，原因
   `diagnosis_evidence_complete`；卸载发生在模型调用之前。
4. 原 `matchFunctions` 匹配到 `work_mem`，并取得真实 PG 源码证据。
5. 原 `ParameterLibrary → LLM_ACQ → LLM_DIS_SM → 候选解析` 实际执行；
   该次 API 审计记录 10 次调用，不是 mock，不使用固定候选回退。
6. 原候选校验和临时配置流程执行，候选经原解析落地为
   `work_mem=8191`（PG 原生 kB 单位）。在只读事务中 `SET LOCAL` 后复测。
7. 对比 SQL 结果哈希；`ROLLBACK` 后参数恢复到 64kB，并执行恢复后复测。
8. 细粒度采集保持关闭，常驻采集恢复；实验完成时才停止常驻进程。

### 实际耗时

每个阶段为同一 SQL、同一 managed libpq 会话的 3 次测量中位数；
计时包含 SQL 往返和有界结果哈希处理，不是纯服务端 CPU 时间。

| 运行记录 | 入口 | 基线 | 最佳候选 | 恢复后 | 相对基线耗时下降 |
|---|---|---:|---:|---:|---:|
| `ebpf-resident-chain-final` | 当前单事件常驻自动触发 | 85.586 ms | 49.940 ms | 92.928 ms | 41.65% |
| `ebpf-closed-loop-selected` | 显式选择 backend | 87.244 ms | 51.299 ms | 93.622 ms | 41.20% |
| `ebpf-closed-loop03` | 早期双事件常驻自动触发 | 87.314 ms | 54.682 ms | 85.945 ms | 37.37% |

后两次发生在常驻集合进一步缩减为单个事件之前，但用的是相同的诊断、
原模型、候选校验、临时应用和复测流程；最终行不用于宣称新版本的开销。
这些数字只证明这个受控场景能完成优化验证，**不是业务 TPS 收益、
不是 eBPF 开销结论，也不是跨负载普适收益**。
恢复后与基线的差异反映了单机实验波动，未进行统计显著性检验。

三次完整运行的 `lab_checks.json` 均为 12/12 通过：
真实触发、细粒度采集、模型前卸载、eBPF 识别、原源码关联、原模型候选、
真实收益、当前 SQL 结果一致、设置恢复、返回常驻、不使用 Prometheus/perf、
所有 collector 子进程已停止。collector manifests 为 completed，
本次没有报告 perf 通知丢失或 BPF 健康计数错误；这不等于证明所有路径无损。

## 预算超限：真实负向验证

当前单事件常驻版本的 `results/ebpf-resident-budget-final`：

```bash
python3 -m experiments.ebpf pipeline-lab \
  --select-target --detail-max-events 1 \
  --output results/ebpf-resident-budget-final
```

该命令**预期返回 1，而不是优化成功**。结果：

- 细粒度进程真实附着，然后以 `event_budget_exceeded` 卸载；
- 不进入 `original_model_acquisition`，没有模型调用或候选应用；
- 常驻与细粒度 manifest 均 completed，子进程已退出；
- `result.json` 为 failed，明确记载证据不足，不伪造优化结果；
- 单独的负向检查 `negative_checks.json` 为 7/7 通过。

另一次自动事件触发的预算测试 `ebpf-closed-loop-budget` 也为 7/7 负向检查通过。
超限与成功证据同时出现在同一输出批次时仍停止调优；
窗口结束只得到一次完成执行时也拒绝进入模型，这两点有独立单元测试。

## 原接口接入时实际修复的问题

- 第一次尝试的旧私有 API 地址不可达，保留真实失败及清理记录，没有伪造候选。
  后续使用项目已有私有配置 launcher，未修改运行中服务的配置或存储凭据。
- 原 `ParameterLibrary` 会对所有资源值应用数字格式，不能直接处理 eBPF 未采
  数据的 `None`。适配后显示 `unknown (not collected)`，没有用零补齐 I/O、
  命中率等缺失观测。
- 原流程以 TPS 为最大化目标；此实验显式传递
  `read_only_sql_duration_ms` 和 lower-is-better，不制造 TPS。
- 原 source evidence 的 perf 栈文案被 eBPF 输入的真实语义覆盖，
  不把独立函数事件伪装成完整动态调用链。
- 使用 `SYSINSIGHT_PROFILE_ROOT` 指向操作者已有参考资料；
  默认原服务行为不变，也没有复制或修改原演示目录的资产。

## 回归与证据位置

```bash
bash scripts/test.sh
python3 scripts/release_check.py --write-inventory
git diff --check
```

完整测试 138 项：134 通过、4 项明确跳过；其中闭环契约测试 17 项。
回归测试不加载 BPF、不连数据库、不调用 API。上面的真实 PG/BPF/API 验证
由显式 `pipeline-lab` 入口执行，不能用单元测试通过冒充真实链路通过。

运行产物保存在被 Git 忽略的 `results/` 中，不提交凭据、运行数据库或大体积日志。
`workflow.json`、`diagnosis.json`、`source_detection.json`、`sysinsight_api/`、
`candidate_validation.json`、`baseline.json`、`tuning_result.json` 保留各阶段证据。
复现条件和通用入口见 [PIPELINE.md](PIPELINE.md)。

## 本次没有完成的事情

- 这是**首个 PostgreSQL 排序落盘 → 参数调优**场景，不是所有根因都已覆盖。
  `BufFileRead` 只形成物化/溢写假设，不能独立证明最终因果。
- 单个指定 backend 和显式只读 SQL；不支持自动覆盖所有连接、并行 worker、
  Prepared Statement 参数恢复或自动追溯已执行完的 SQL。
- 优化临时应用于 managed 重放会话，恢复后退出，不永久改线上参数。
- 结果检查是当前数据上的返回结果多重集一致，不是形式化语义证明。
- 控制器一次运行处理一个 incident，尚未接管在线 bridge/Portal；
  DREAM 的 SQL/模型调优路径没有通过本控制器完成迁移。
- 没有重新实现论文全部 LLVM 分析、训练出有效 Lasso 根因/选择模型，
  或证明总数据库开销达到某个百分比。当前优先交付真实链路贯通。
