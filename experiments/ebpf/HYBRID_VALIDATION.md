# eBPF 告警 → 原 perf / PG / SQL：真实功能验收

日期：2026-11-04。分支：`experiment/ebpf-paper-20261103`。
实现与命令：[HYBRID.md](HYBRID.md)。

## 本轮验证什么，不验证什么

本轮先验证：不需要常驻逐 SQL 执行探针，也能用真实 eBPF 事件自动打开
原 perf 和原数据库观测，再执行原模型、临时应用、业务 TPS 复测和恢复。
**采集贯通、模型/TPS 贯通、有效正收益是三个不同的计数。**

这不是新的随机双臂 TPS 等价试验，没有因此宣称达到历史收益、
等于原 perf 收益或已证明低开销。历史双臂负结果保留在
[PAIRED_TPS_VALIDATION.md](PAIRED_TPS_VALIDATION.md)。

## 固定的功能试验条件

- 原控制台六个 SQL 配方，不改 SQL；5 仓、同一停机物理快照的独立克隆。
- 正常业务 2 连接、1600 offered TPS；原压力 2500 offered TPS，
  各场景原压力连接数分别为 3 / 5 / 5 / 8 / 10 / 6。
- 两个正常 perf 窗口和一个告警后窗口，均为原采集与原后处理，
  15 秒、300 Hz / backend；不再重造 eBPF CPU 栈来近似原 perf。
- 常驻只对同集群 PG12 stats collector 的 recv 入口/返回挂 BPF；
  6 个 2 秒窗口校准、3 个健康观察窗口，然后保持原业务连接不变，
  引入原压力。压力开始时间只用于实验记录，**不输入检测器**。
- 通用告警规则统一为 1.6 倍 / 6 MAD / 最小增量的持续上升；
  同一信号连续 2 窗口。事务速率低于基线 60% 时须持续 10 秒。
- 原 canonical input、原源码关联、真实原模型与原候选解析。
  原模型温度/采样策略不修改；本轮子进程 `PYTHONHASHSEED=0`。
- 同一 31 参数安全权限域和数值上限；不降低持久性，不重启，
  不改连接目标，不执行模型建议的业务 DML/DDL，没有历史预设候选。
- 评分使用另外一个同快照克隆；每场景 3 组 before / tuned / restored，
  每段 12 秒，设置在连接启动时送入，不每事务插入 SET。
- **评分数据库上没有 BPF/perf 诊断探针**。原诊断集群的 sentinel 在模型/
  复测回调期间仍存在，但不附着评分库；这不是 sentinel 开销 A/B。
- 原已完成压力偏差、恢复漂移 ≤5%、收益 ≥5% 门槛不放宽。
  配置和业务表聚合不变量都必须恢复；这不是逐行/物理文件相同证明。

运行产物（不提交到 Git）：

```text
results/hybrid-six-01/
  <case>/workflow.json
  <case>/sentinel/{manifest.json,windows.jsonl}
  <case>/anomaly/{phase.json,profile/}
  <case>/{case_result.json,sysinsight_input.json,candidate_validation.json}
  <case>/score/{blocks.json,block-*-before/,block-*-tuned/,block-*-restored/}
  <case>/result.json
  summary.json
  hybrid_audit.json
```

`hybrid_report.py` 只读重建阶段顺序、原模型的 10 项数据库指标 /
慢 SQL 入口、perf 进程组清理、恢复和收益门控，不生成诊断候选或伪造 TPS。

## 真实结果：6/6 贯通，0/6 可验证的 ≥5% 收益

`results/hybrid-six-01/summary.json` 和 `hybrid_audit.json`：

| 检查 | 结果 |
|---|---|
| 真实 eBPF 自动告警 → 原 perf / PG / SQL | **6/6** |
| 原差分、源码证据、原模型、候选应用/TPS/恢复 | **6/6** |
| 原模型请求记录 | 每场景 10 次，合计 **60 次** |
| 原模型数据库指标记录 | 每场景 **10 项**，均有真实 series |
| 原模型 `tuning_context.slow_queries` | 每场景 **10 条** |
| 正常 2 窗口 + 告警后 1 窗口 | **18/18** 原 perf 完成并清理 |
| `perf evlist` 复核实际事件 | **18/18 `cycles`** |
| BPF 解码错误 | **0** |
| 诊断后回到原 sentinel，并收到新统计 | **6/6** |
| 配置恢复 / 业务表聚合不变量 | **6/6** |
| 测量有效且收益 ≥5% | **0/6** |

实际 TPS（正常业务 steady TPS，非 eBPF 事务计数）：

| 场景 | before | tuned | restored | 收益点估计 | 收益/测量门控 |
|---|---:|---:|---:|---:|---|
| Payment Hot | 1458.00 | 1476.50 | 1448.95 | +2.27% | <5%，且一组恢复漂移 −7.82% |
| Stock Level Hot | 1189.50 | 1247.50 | 1231.55 | +3.10% | <5%，且一组恢复漂移 +5.89% |
| Stock Level Burst | 1174.00 | 1244.50 | 1215.00 | +3.62% | <5%，且一组恢复漂移 +8.05% |
| Full Mix Surge High | 1138.55 | 842.50 | 981.50 | −18.24% | 压力不可比、恢复漂移 |
| WAL / Checkpoint | 1089.30 | 764.50 | 924.95 | −8.81% | 压力不可比、恢复漂移 |
| Order Status Hot | 1558.95 | 1531.45 | 1516.50 | +0.81% | 测量门控通过，但收益 <5% |

三个 TPS 列是各自三段的中位数；收益列是三组
`tuned / mean(before, restored) - 1` 的中位数，
**不是用表中三个中位数再计算一个比例**。
未通过测量门控的正/负点估计不能当作已确认的优化或退化。
也不能用这张单链路功能表代替与原采集链路的新随机配对对照。

所有 case 的已选配置都来自本次原模型，不是预设：

- Payment：`max_wal_size=3079`；
- Stock Hot：`max_wal_size=3079`；
- Stock Burst：`max_wal_size=3073`；
- Full Mix：`max_wal_size=3079, effective_cache_size=786431, work_mem=6143`；
- WAL：`max_wal_size=3587`；
- Order Status：`max_wal_size=3079`。

值均为原 PG 设置的 native units。没有 fsync / synchronous_commit 关闭，
没有扩大权限以追历史收益。

### 告警和常驻调用率

相对实验记录的负载切换时刻，Payment / Stock Hot / Stock Burst / Full Mix
约 4 秒，WAL 约 2 秒，Order Status 约 6 秒后升级。
**这不是标注真实异常发生时刻之后的检测时延或准确率证明。**
WAL 的读耗时上升信号可能跨越健康观察与压力切换边界；
检测器不在切换时重置或接收场景提示，不把此信号直接叫作 checkpoint 根因。

本轮相同 2 连接正常业务下，sentinel 观察到约 **43.88–44.55 次 recv/s**，
每次有入口和返回两个挂点。2 秒读取一次聚合不等于探针只触发 0.5 次/s。
这避开逐 SQL 热路径，但**不是已经测出某个 TPS 开销百分比**；
backend 数量/表数增加时原生统计消息也可能增加。

### 同数据、同 SQL 和清理复核

只读产物 `experiment-audit.json` / `perf-event-audit.json` 另外确认：

- 模板仍与上一轮物理快照相同，摘要为
  `24e427afae2074fa6c386ed7a9f3009feeddcb10e40c0fb8de63ec182859f214`；
- 六场景初始配置、初始业务表聚合一致；
- 每个原正常/压力 SQL 副本逐字节一致，没有每事务 SET 或改写 SQL；
- 六场景所有复测组均恢复配置，业务表聚合不变。

全部实验结束后，主试验 18 窗口、预算试验 1 窗口、最终版本补验 3 窗口，
合计 **22 个监督采集进程组没有存活成员**。
tracefs 中剩余 uprobe 定义为 **0**；未清空/改动别人已有的 kprobe。
原 PG12/main postmaster PID 758 和 stats collector PID 769 均仍为
2026-10-25 启动的原进程，没有重启展示数据库或服务。

## 最终版本追加验收

### 真实预算超限：通过

产物：`results/hybrid-budget-01/result.json`。

在真实原负载和真实持续 eBPF 告警后，把 perf 输出限为 1 MiB：

- `anomaly.perf.data` 实际达到 **1,048,576 字节**，原采集因限额中止；
- 没有进入诊断/模型回调：**0 次**；
- 记录 `incident_aborted → resident_resumed`；
- 本次 perf / sleep 进程组无存活成员；
- **同一个** eBPF sentinel 在中止后继续接收真实 PG 消息；
- 最终正常卸载 sentinel，解码错误 0，未决 recv 0。

这不是假数据、只检查日志字段或把 timeout 当作告警。
另有无 PG/BPF 的单元测试覆盖墙钟中止、单文件与总输出限制、
子孙进程清理、清理不确定时拒绝成功、模型异常、取消、冷却与次数预算。

### canonical 告警与最终采集器：通过

产物：`results/hybrid-current-order-01/hybrid_audit.json`。

最终代码重新运行 Order Status 自动告警和三个真实原 perf 窗口：
顶层 canonical 告警与真实 eBPF 告警一致，原 10 项数据库记录 /
10 条 SQL 统计仍在，原 perf 证据、进程组清理、回到 sentinel 均通过，
BPF 解码错误 0。最终采集器也在 perf 窗口前后检查 PID 出生时间，
拒绝期间退出/复用的目标。

该补验未重新调用模型、没有新 TPS 收益结论；不覆写首轮 60 次请求的输入。
回归命令为 `bash scripts/test.sh`：**222 项测试，218 通过、4 跳过**；
源文件和运行产物/凭据隔离检查同步通过。

## 调试记录与版本边界

1. 最初 libc-only 挂点没有收到消息，真实探针 smoke 发现计数为零。
   本机 PG12 实际 recv 来自 libpthread；现检查目标映射并挂两个可能的
   实现，且健康基线没有真实事务就直接拒绝校准，不能用零冒充已采到。
2. `hybrid-order-preflight-01` 中，未引入额外压力时，连续两个窗口的
   数据库事务计数确实下降，最初的 4 秒规则升级了告警。
   这不等于已有独立证据证明是“误报”；统计延迟、空闲和真实主机停顿
   都需要区分。因此统一增加事务下降的 10 秒持续条件，
   保留原失败记录，不按已完成调优收益重试或挑选规则。
3. `hybrid-order-preflight-02` 已验证没有 spill 的 Order Status 自动
   触发 perf、恢复原输入并返回 sentinel，不是操作者选择伪装成告警。
4. `hybrid-six-01` 的长运行在最终元数据修订之前启动。首轮模型输入的
   真实 eBPF 告警位于 `ebpf_observations`，但 canonical 顶层 `alert`
   仍沿用参考输入 helper 的空值。最终实现已同步顶层、step 4、capture
   和 case samples 的真实 eBPF 告警，避免保留 operator-selected 标签。
   **不追改已经调用模型的输入/结果，不把首轮 TPS 当作该元数据修订后的
   新收益证明。** 后续用最终版本追加采集与元数据核验。

## 已知边界

- eBPF 告警代表数据库级资源/活动量变化，不证明业务 SLO 或具体根因。
  合法负载增长可能升级，低活动量或短 off-CPU 异常可能漏检；
  六个注入场景不是误报率/漏报率评估集。
- 统计消息是 PG12 原生批量消息；消息到达前的 UDP 丢失不能由接收端
  观测证明不存在。解码错误与未采集不会冒充零。
- pg_stat_statements 在快照中预先启用，正常期仍会在数据库内部累计。
  这里只在按需窗口查询，不声称消除了它已有的统计成本。
- 同机原服务继续运行，未做硬件级独占或内核级隔离；TPS 漂移门控
  仍然重要，正的点估计不等于可验证收益。
- 原可选 SHAP 缺失/质量门控、原模型随机性、参数排序问题和历史宽权限
  不会因为换成混合采集就自动消失。
- 当前入口是隔离集群的有界实验控制器，不冒充完整生产守护进程。
