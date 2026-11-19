# KeenInsight 页面与真实运行数据对齐

入口：`http://127.0.0.1:9108/keeninsight/`。HTML 导航、品牌与基础样式来自用户提供的
`English.zip`，动态脚本使用 Bridge 同源 API；无前端框架、无 CDN 依赖。
原压缩包不改动；导入后的文件位于 `keeninsight_web/`，不包含原型的示例业务记录。
现保留 11 个动态页面；实时监控由现有 Grafana 负责，已移除 realtime-monitor 页面、
菜单、指向该页的主机操作入口及专用曲线查询接口。

## 数据类别与存储归属

| 类别 | 页面 | 数据来源 / 保存位置 | 主键与关联 |
| --- | --- | --- | --- |
| hosts | host-list | 本机标识、Bridge 数据库配置、采集时间及健康状态；Prometheus 主机利用率瞬时快照 | 本机实例 ID，当前单实例 |
| alerts | alert-list / alert-detail | incidents：触发器、原始告警、诊断、工作流状态 | incident_id |
| sql | sql-log / sql-detail | sql_observations：指纹累计统计；lab_sql_executions：独立执行结果与错误 | sql_key / execution_id；run_id 关联实验 |
| diagnoses | sql-detail | dream_jobs.result_json 与 keeninsight_evidence：根因、解释、模型、实际置信度、规则证据、执行计划 | job_id → sql_key |
| profiles | alert-detail / sql-detail | 告警差分采样、真实 SHAP；SQL 级 CPU 样本写 sql_profiles，原始文件按 24h 清理 | incident_id / execution_id / SQL 指纹 |
| tuning | sql-detail / 原自动联动管理 | improvements：动作、改写、GUC、耗时、验证、应用方式、状态、命中次数 | improvement_id → sql_key；validation.job_id |
| alertRules | alert-rules | Prometheus **已加载**的实际告警规则与状态 | group + rule name |
| diagnosisRules | dba-rules | DREAM rewrite_rules.jsonl：条件与变换 | rule.name |
| code | code-source | PostgreSQL function_parameter_association.json：函数、参数、真实源码行和版本来源 | evidence hash；函数/参数/文件/行 |
| manual | manual | parameter_metadata.json：参数说明、类型、单位、默认值、范围、context | 参数名 |
| knowledge | tuning-knowledge | 已实测 improvements 派生的调优知识；保留候选/生效/回滚状态 | improvement_id；原始 SQL 与验证证据 |

SQL 日志中的 `kind=observation` 是 `pg_stat_statements` 指纹累计统计，`executionTime`
对应 `mean_time_ms`；不是逐次 SQL 审计日志。`kind=execution` 是实验控制台实际执行记录，
耗时为该次测量，失败也展示。没有全量逐条数据库日志采集时，不将累计值拆成虚构执行。

## 兼容原型的字段

统一版本：`schemaVersion=keeninsight.ui.v1`，API 总有 `updatedAt`。
列表返回 `{items,total,limit,offset,schemaVersion,updatedAt}`，单页最多 200 条（UI 默认 50）。
`q` 为服务端关键字搜索，`offset` 分页。详情通过 URL 编码后的实际 ID 查询。
SQL 列表可用 `kind=all|execution|observation` 筛选，默认把真实单次执行放在前面，
避免高频采集的指纹统计淹没刚完成的前台实验记录。

SQL 保留原页面字段：`id,time,sql,type,executionTime,hasError,result,lifecycle,flameGraph,
diagnosis_results,tuning_results`。

- `diagnosis_results`：`query_id,root_causes,confidence,explanation,rule_matches_detail`。
- `tuning_results`：`query_id,fix_action,rewrite_sql,action_explanation,root_causes,old_time,new_time,
  improvement_ratio,validation,status,applyKind,applyScope,hitCount,sessionSettings`。
- 新增：`kind,sqlKey,database,source,plan,job,statistics,execution,availability`。
- 单位：`executionTime` 为毫秒，`old_time/new_time` 为秒，`improvement_ratio` 为 0–1 比例。
  时间下降比例 = `(old_time-new_time)/old_time`；不能和 TPS 提升或倍数混用。

告警保留：`id,time,host,cluster,reason,severity,status,rootCause,diagnosis,detailAnalysis`。
其中 `status/rawStatus` 直接表示真实工作流状态（running/completed/failed），**完成诊断不等于告警已恢复**。
触发时的 firing 状态保存在 `detailAnalysis.triggeredRule.state`；当前告警状态仍由原联动页显示。
`detailAnalysis` 保存 `triggeredRule,runtimeData,differentialProfiling,shapAnalysis,hotFunctions,
knobs,runtimeCallChains,functionCodeExamples,comparison,application,stages`。
运行栈采样次数是 samples，不当作函数调用次数。

## 补齐的持久化

新增表 `keeninsight_evidence`：

```sql
entity_type TEXT, entity_id TEXT, schema_version TEXT,
captured_at TEXT, payload_json TEXT,
PRIMARY KEY(entity_type, entity_id)
```

`StateStore._init` 非破坏性建表。任务完成后，在更新基础状态的同一 SQLite 事务内保存
脱敏后的补充证据。SysInsight 保存固定 artifact 中的 input/live_chain；DREAM 保存 worker 的
真实计划、诊断及评估结果。启动时对保留中的历史任务补填（幂等、每类上限 500）。
不复制大量 SQL 时间序列，也不复制 Prometheus 样本。引用资产由 Git 版本管理，不在每次轮询重复入库。

保留规则：运行类证据 24h；queued/running 告警/任务及 active 改进方案支持的证据不清理。
参考文档/规则/源码不受运行历史清理影响。JSON 导出是手工下载的当前查询结果，便于归档；
列表导出只含当前页。如需全量归档，按 total/limit/offset 遍历 API。
原实验控制台执行 DREAM 清空时，同步归档并清空 SQL 补充证据，保留告警证据。

## 缺失字段的真实边界

- 历史 DREAM worker 没返回 plan 的记录显示 `plan=null`；升级后的新任务直接保存已经获取的真实 EXPLAIN 计划，不为页面追加一次重查询。
- `confidence` 只取模型真实返回值。没有则 `{}`，不把实测收益转换为置信度。
- 新告警完成 perf 后实际调用本机性能模型及 TreeSHAP；`shapAnalysis` 保存模型版本、预测目标、背景值、正负归因、加和校验、验证指标及输入摘要。旧任务缺少证据时不回填伪造 SHAP。
- SQL 级 CPU 栈采样已接入 `sql_profiles`，`flameGraph` 为真实样本数树。PG12 支持主进程与经过进程标题/查询身份/OS 出生标识联合校验的并行 worker；`sqlProfile.participants` 保存角色、PID、出生标识及各自样本数，`segments.windows` 保存各 PID 有效窗口。最多附加 8 个 worker，省略情况记录在 `omittedWorkerPids`，短命 worker 可能漏采。不能声称为完整延迟/等待/内存分解。`lifecycle` 仍是执行/任务时间。
- `shapAnalysis.reliability` 保存可信状态、允许用于辅助决策与否及原因；没有可信标记的旧记录不能自动视为可信。低可信数值不进入新 LLM 调优提示，原始 SHAP 计算结果仍保留供查看。`independentValidation` 保存最新独立批次误差和验证报告 hash，与历史分组交叉验证分开展示，失败的候选不替换现有模型。
- 规则库页面代表系统可用规则；只有诊断实际返回的规则证据才写入 `rule_matches_detail`。
- 知识记录来自真实优化结果，保留实际验证及状态，不使用原型中的示例置信度。

## 接口

基础路径 `/api/v1/keeninsight/`：
`overview,hosts,alerts,sql,alertRules,diagnosisRules,code,manual,knowledge,schema`。
详情：`alerts/{incident_id}`、`sql/{sql_key_or_execution_id}`。
Prometheus/Grafana 继续保存和展示主机、PG 与 TPS 指标，不在产品页重复提供曲线接口。
未知资源/ID 返回 404；无效分页返回 400；Prometheus 规则不可用返回 503。
刷新失败保留旧数据并显示错误，不回退静态数据。UI 每 10 秒刷新；隐藏页面、编辑筛选或展开详情时暂停自动刷新。

操作入口仍由 `/lab` 和 `/ui` 提供，产品页可直接跳转。无后端支持的编辑/开关不提供可执行操作；
原版上传、新建规则、批量启动/停止等按钮保留外观但禁用，并用提示说明尚未接管理 API。

验证：`python3 -m unittest discover -s perf-anomaly-demo -p 'test_keeninsight_portal.py' -v`。

## 2026-10-21 本机接入验证

- 7 项新增后端回归、8 项原 DREAM 自动应用回归通过。
- Chromium 实际访问产品页面（当前保留 11 页），验证服务端分页、SQL 类型筛选、搜索、详情跳转、JSON 下载、
  自动刷新；模拟接口 503 验证旧数据保留和重连；未出现浏览器 JavaScript 异常。
- 通过现有实验控制台实际执行 `tpcds_q44`，单次 SQL 为 `5651.646 ms`，
  `execution_id=lab-exec-47b60aa8c547c999`。
- 首次 DREAM 调用与一次重试均返回 `APITimeoutError: Request timed out.`，
  新页面如实显示 failed，不显示优化成功。重试 `dream-20261021164635-fc287df117`
  保存了真实执行计划，且能从上述执行记录详情关联查询。
- 该测试验证的是运行结果的同步、失败可见性和证据持久化；没有把此次调用称为新的成功优化样本。
- 当前产品页可检索 276 条参数手册、1490 条源码关联及 68 条真实 DREAM 规则。
- 浏览器脚本为 `test_keeninsight_browser.cjs`，需安装 Playwright Chromium；
  可通过 `KEENINSIGHT_PLAYWRIGHT_MODULE` 指定独立安装的模块，
  `KEENINSIGHT_TEST_URL` 指定 Bridge。SQL 搜索步骤使用上述保留中的 Q44 实验数据。

## 2026-10-23 全链路与总结页面补齐

总结入口：`/keeninsight/template.html`，恢复原版侧栏后，点击顶部 KeenInsight 品牌进入。
新增明确区分的汇总：

- `workflowCounts`：任务完成、失败、跳过与改进方案 active/candidate/no_action 分开统计，不能将 completed 当成收益成功。
- `recentRuns`：精简后的实验记录，包含 TP 基线/压力前/压力后 TPS、外部压力可比性、候选来源、恢复状态及关联告警；不再重复携带整套多兆证据。
- `recentAlerts`：真实在线 LLM 模型/调用数、复测指标、应用与恢复状态、跳过/失败消息。
- `recentDreamJobs`：按确切 job 关联 SQL、模型、诊断结论、评估耗时与发布状态。
- `automation`：10 秒自动触发门槛、当前 gate、模型、自动应用开关、采集错误及当前告警状态。
- `capabilities`：明确说明 SHAP、SQL 级火焰图和内部 CPU/内存阶段未接入；监控仍使用 Grafana。

以上为 10 月 23 日补齐时的状态；10 月 24 日 SHAP 与 SQL 级 CPU 火焰图已经接入，
实现和验证见 [ATTRIBUTION_VALIDATION_20261024.md](ATTRIBUTION_VALIDATION_20261024.md)。

告警详情新增 `backendSnapshot/perfCapture/llm/validationSql`，并展示实际阶段日志。
`workflow_progress.json` 在阶段变化时原子写入，保存真实 startedAt/finishedAt/durationMs；
没有采到的时间不补估计。告警补充证据迁移到 evidenceVersion=2，已有持久化证据即使原文件缺失也不会被空值覆盖。
跳过属于独立结果，不假装执行成功，也不当作模型/API 失败。

SQL 详情新增：

- `diagnosisTrace`：实际提示词、初始根因分数、模型原始 JSON 与校验结果。
- `actionTrace`：实际传给动作模型的空间、召回计数、规则候选和原始动作响应。
- `executionHistory/lifecycle`：真实实验执行和 DREAM 任务时间线；不是 SQL 内部 CPU 采样。
- `jobHistory` 及 `?job_id=...`：可明确查看某次诊断，不将新失败/no_action 与历史成功方案混为同次结果。
- `activeApplication`：当前真正已发布的动作，与所选 job 的候选结果分别展示。
- `evaluation.semantic_validation`：实际 QED/结果集比较路径、重试、失败回退。nix 不可用时，结果集比较通过不称为形式化等价证明。
- `diagnosis_results.rule_matches_detail`：实际提供给动作 LLM 的结构规则候选；`applied=null`，不宣称每个候选变换都已采用。

运行时同步覆盖 planner.py、prompt.py、action_manager.py、action_evaluation.py、pg_env.py 等接入文件。
嵌套 DREAM 源码修复由 `dream_diagnosis_fix.patch` 保存，部署不能只复制外层 adapter。

本轮发现并修复自动触发的标量常量匹配问题：pg_stat_statements 会把 CASE 中的 NULL/布尔常量替换为参数。
统一指纹处理同时保留 IS [NOT] NULL/TRUE/FALSE 谓词和双引号标识符；迁移更新 canonical_sql，保持历史 SQL ID/任务/改进方案关联不变。
活跃 SQL 样本因此可与执行完成后的统计关联，不再因为缺失 replay_sql 而错过自动调优。

完成信号也已改为持久交接：`sql_observations.pending_completed_ms` 保存已观测完成的耗时，
直到该 SQL 不再活跃且实际尝试调度时才消费。下一轮统计无增量不会擦掉它，避免“统计已完成但活跃会话仍短暂 active”时漏触发。
`dream_jobs.trigger_source/source_completion_ms` 区分人工与自动触发，并保存触发时的已完成区间均值/新最大值证据；自动任务不再用陈旧的全历史均值代替触发观测。
DREAM 自己的评估连接具名为 sysinsight-dream-worker，并从业务 perf 后端选择中排除，避免把诊断压测再次当作新的业务根因分析。

本轮还修复在线候选校验提前截断：`candidate_count=1` 是采集参数，不能据此丢掉同次原始源码返回的其他模板候选。
仍执行原有结构与 live pg_settings 校验，不猜测修正越界参数、不使用预设值顶替模型输出。

完整实跑审计保存在 `results/product_e2e_validation/20261022_234207/`。
