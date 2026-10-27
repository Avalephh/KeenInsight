# 架构与数据流

## 两条业务链，不制造第三种验收口径

```text
Prometheus 告警
  → bridge 收集当前数据库状态、活跃后端与告警
  → perf / 差分函数 / 源码关联 / 可选 SHAP
  → 原始 SysInsight/LLAMBO 参数候选
  → 结构与数据库校验 → 临时应用 → 复测 → 恢复 → 保存证据

业务只读 AP SQL 执行结束
  → pg_stat_statements 完成增量 + 执行中保留的字面 SQL
  → 时长门槛、活跃排除、可回放检查
  → DREAM 异步队列 → 真实模型/API/计划/动作评估
  → candidate 或 active → 下一次匹配执行实际应用 → 记录命中与耗时

两条链的记录 → SQLite + 限定目录的证据 → Portal API → 原风格静态页面
监控时序 → Prometheus → Grafana
```

SysInsight 是系统参数调优，目标业务指标是 TPS；DREAM 是 SQL 调优，目标
是同一语义查询的执行耗时。当前在线 SysInsight 实现使用只读 SQL 作候选
复测探针，这是**实现边界**，不是产品新增的第三条业务链，也不是同压力
业务 TPS 的替代证明。

## 主进程与 worker

- `app/sysinsight_dream_bridge.py`：单个 SQLite 状态库的 owner；采集循环、
  HTTP、线程池、自动调度、应用策略、保留维护。
- `sysinsight_executor`：串行在线系统分析；`capture_executor` 执行有边界
  的证据采集；`executor` 执行 DREAM；实验任务有独立 pool 和幂等状态。
- DREAM 使用单独 Python 环境和 postgres OS 用户可读的持久 runtime。
  每个 Job 有临时交换目录，完成后复制证据；临时目录不是长期依赖。
- `monitoring/deployment.py` 负责 systemd 和组件健康管理，不属于数据库
  诊断算法。HTTP/组件健康通过不等于调优收益验收。

## 数据类别

| 数据 | 保存位置 | 含义 |
|---|---|---|
| 告警 | `incidents` | 触发来源、阶段、最终状态和恢复证据 |
| SQL 指纹 | `sql_observations`、samples | 累计统计与采集间隔增量，不冒充逐次执行 |
| 原始字面样本 | `live_sql_samples` | 还原可执行 SQL；不从 `$1` 猜常量 |
| DREAM Job | `dream_jobs` | 确切 Job、SQL、触发类型、完成耗时、错误与结果 |
| 改进方案 | `improvements` | active/candidate、类型、作用域、校验、实际命中 |
| 实验与执行 | `lab_runs`、`lab_samples`、`lab_sql_executions` | 分开的 TP 阶段、原 SQL/优化 SQL 实测 |
| CPU 采样 | `sql_profiles` | execution/PID/query_start/后端出生身份绑定的采样 |
| 展示证据 | `keeninsight_evidence` | 完成时的版本化补充证据，保护有效方案的原始依据 |
| 参考知识 | 显式构建的 profile、外部 DREAM 规则 | 版本化来源资料，不是实验运行结果 |

运行库默认 `monitoring/data/sysinsight_dream_bridge.sqlite3`，
证据在 `monitoring/data/sysinsight_dream_bridge/`；全部被 Git 忽略。

## 自动应用的作用域

- pg_hint_plan hint：通过 PostgreSQL hint table 对匹配的新连接/查询生效；
  匹配计数与桥接实际命中计数具有不同语义。
- SQL 改写及执行期会话设置：只在 bridge-managed 路径应用，不会拦截
  任意外部 PostgreSQL 客户端。常量保留采用更严格的文本身份检查。
- DDL、未知动作、收益证据不足或策略未通过：留 candidate，不自动执行。
- 精确 Job 详情与“当前 active 方案”分开，不能把旧成功归给新失败任务。

## 采样和归因

火焰图是窗口内 **on-CPU 样本量**，不是墙钟耗时、锁等待/I/O 时长、
内存阶段或端到端时间轴。采样失败/零样本不能解释为 SQL 零成本。
并行 worker 必须通过身份与时间窗口验证；短生命周期 worker 仍可能遗漏。

可选 SHAP 对训练模型的预测作解释，不证明真实因果、也不解释 LLM 思考过程。
模型不存在、不可信或超出训练域时显示不可用/低置信度。本源码分支不附带
本机历史数据训练的模型。

## 24 小时保留与恢复

默认保留 24 小时运行历史，分批清理 SQL 样本、任务和限定目录的旧文件，
不在启动时执行全库 VACUUM。active 方案及其必需证据受保护。

- 旧 Job 超期但有效方案仍存在：按该 SQL 对应的 `validation.job_id`
  回读保留证据，明确 `history_expired`，不重建 Job 或伪造时间线。
- owner 重启：中断的告警结束为 failed 并注明原因，不盲目重放全局参数实验；
  DREAM 队列按现有恢复机制处理。
- 具体保护范围由 `StateStore`、Portal `prune` 与回归测试约束。备份策略
  另由部署者负责，清理策略不等于可恢复备份。
