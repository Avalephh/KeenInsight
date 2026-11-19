> **v2 scope note (2026-11-19):** SQL-level tuning (DREAM SQL rewrite/hints,
> TPC-DS SQL lab, SQL log/detail pages) has been removed from this stack.
> This document records the pre-v2 artifact; the active product is the
> SysInsight system-parameter tuning path with the TPCC experiment console.

# KeenInsight 全流程与总结页面验证（2026-10-23）

## 结论与入口

总结页：<http://127.0.0.1:9108/keeninsight/template.html>。
告警、SQL、参数手册、源码关联、规则库和知识页面均使用真实 Bridge 数据，无原型示例记录兜底。

六组 TP 场景已逐组跑完；自动长 AP SQL、短 SQL 门槛、人工诊断、候选不发布、自动应用和实际浏览器按钮均完成检查。
不能将“所有流程可处理”理解为“每条 SQL 都有收益”或“所有动作都允许自动发布”。

## SysInsight 六组真实场景

每组 30 秒基线、120 秒压力（调优前后各 60 秒）；沿用资源护栏和原始压力配方，无强制告警、无手工制造告警记录。
每组实验后等待其后台分析恢复配置，再开始下一组。首次六组阶段开始于 2026-10-22 23:42，结束于 2026-10-23 00:17（北京时间）。

| 场景 | 正常业务 TPS | 压力前 TPS | 同压力调优后 TPS | 观测提升 |
| --- | ---: | ---: | ---: | ---: |
| 热点 Payment | 1613.10 | 1175.00 | 1595.00 | 35.7% |
| 热点 Stock Level | 1439.55 | 612.00 | 878.00 | 43.5% |
| Stock Level 突发 | 1531.95 | 606.00 | 880.45 | 45.3% |
| 混合事务高并发 | 1603.00 | 594.00 | 973.00 | 63.8% |
| WAL / checkpoint | 1603.00 | 453.50 | 743.00 | 63.8% |
| 热点 Order Status | 1465.50 | 1238.55 | 1610.00 | 30.0% |

六组均观测到实际 firing，均在 SQLite 和产品告警列表可查；每组至少一条新告警完成真实 perf、源码关联、LLM、应用、复测和恢复。六组的外部压力比较均通过现有 comparison_valid 检查。

重要的测量边界：上表是实验控制台使用**历史已验证 LLM 候选**回放所得，来源字段为 verified_sysinsight_gpt5.6-sol_selection，不是声称本次 GPT-6.1-SOL 在线新生成候选带来了这些 TPS 收益。告警触发后的在线链路确实另行调用 GPT-6.1-SOL 并应用/复测/恢复，其结果在总结页的独立表格和告警详情中显示。
本轮为单轮功能验证，不作为 3～4 轮重复稳定性证明。

当前演示告警仍为 active sessions >= 3、for=0s，因此部分告警在基线或切换阶段出现；不能据此证明其只识别外部压力。采不到真实业务后端时明确 skipped，不伪造调用链和收益。

## DREAM 实际 SQL / LLM / 自动应用

| 测试 | 实际执行与结果 | 发布状态 |
| --- | --- | --- |
| Q31 长 SQL 自动触发 | 原 Q31 加 OFFSET 0 的分页等价变体，客户端基线 38.66s；完成前没有提前排队，完成后正常轮询自动创建 job，服务端完成观测约 37.97s | 实际 LLM 评估 4.81s，通过校验并 active |
| Q31 下一次匹配执行 | 调用同一 Bridge 解析/应用路径，真实执行约 4.64s，记录实际命中 | bridge_managed 改写/提示 |
| Q8 短 SQL 门槛 | 实际约 3.02s，观察后没有新增 DREAM job | 正确不触发 |
| Q44 人工新基线与 LLM | 新基线约 5.93s，真实模型候选评估约 2.84s | 包含索引 DDL，不支持自动发布，保留 candidate |
| Q88 人工新基线与 LLM | 新基线约 12.40s，真实模型候选评估约 2.03s | active；再次执行约 2.03s |

Q31 自动 job：dream-20261023004655-c423579223。
Q44 人工 job：dream-20261023005644-b5a90e283b。
Q88 人工 job：dream-20261023005802-b192474648。

自动任务保存 trigger_source=automatic_completed_sql 与 source_completion_ms，人工任务明确标记 manual。客户端墙钟耗时和服务端完成统计不强行混为同一测量。
Q31 的 OFFSET 0 不是人工延时，也不改变返回行，只用于创建没有已完成任务缓存的新查询形状；原版 Q31 也另外真实执行并调优过。

Q44 目前不列为自动应用推荐，仍可选择做诊断/候选评估。自动应用演示推荐 Q31、Q88；不为展示效果而放行索引 DDL。

## 真实实验控制台按钮与去重

实际 Chromium 打开 /lab，选择原版 Q31，并点击按钮，不是仅调用内部 Python 函数。

- 实验 lab-dream-20261023010608-dfc0671f72；新真实 LLM job dream-20261023010649-b511786dea。
- 原 SQL 真实 baseline：40854.00 ms；优化后实际重放：4742.13 ms，约下降 88.4%。
- 数据库执行记录恰好 baseline 1 条、optimized 1 条。
- baseline execution_id=lab-exec-418620fd26d93b40；optimized execution_id=lab-exec-e494d7ddfcff7cac。
- SQL 详情关联该次 job，执行时间线能查到两条执行；总结页数值与实验结果一致。

首次完整按钮测试发现 replay 防重锁设置在 await 状态请求之后，双击产生 2 次 POST，但后端幂等保护使 SQL 只执行 1 次。修复后锁在第一次 await 之前设置。
随后在同一个真实已完成实验上重复调用两个前端函数：start/replay 各只有 1 次 POST，后端返回 already_completed，没有增加 SQL。初次失败审计和修复后的审计均保留，不把历史失败改写成成功。

测试时仅暂时退役原版 Q31 的两个旧 active 方案，未清空全部历史。新方案 improvement-fdb3133b607ffb37 已真实校验并保留 active；旧方案保留为 rolled_back 便于追溯。Q31 OFFSET 0 和 Q88 的 active 方案不受影响。

## 本轮修复的缺口

1. 总结页新增两条链路的独立结果、三段 TPS、候选来源、模型、状态、恢复和证据入口，不能以 completed 冒充性能提升。
2. 告警详情补齐实际后端快照、perf 计数、热点函数、源码参数关联、候选、复测 SQL、比较和阶段时间；阶段变化立即写进度日志。
3. SQL 详情补齐真实诊断原文、动作模型输入/输出、规则候选、执行时间线、job 历史、当前已发布动作、未发布原因和实际语义校验路径。
4. 详情支持指定 job_id，不将历史成功方案误挂到新的失败/no_action 任务上。
5. 修复“只校验第一个候选”：非法候选仍拒绝，但同次 LLM 返回的其他真实候选不再提前丢弃。原失败告警保留；新任务实际校验 5 个候选并完成链路。
6. 修复 CASE NULL/布尔常量与 pg_stat_statements 参数化之间的指纹差异。历史 SQL ID、任务和生效方案关联保留。
7. 修复完成统计与 active→idle 轮询竞态：已完成信号持久保存到调度时消费，不被下一次零增量覆盖；自动任务保留触发耗时。
8. 人工任务的观测写入与排队加锁，避免自动采集抢走同一个信号；job_id 在进入模型分析前就写入，供前台实时跟踪。
9. 为 DREAM 工作连接、PG exporter 设置明确 application_name，排除其作为业务 perf 调优对象；空后端事件可立即结束，不等待长 LLM 队列。
10. 真实 QED/结果集比较分别保存。本机没有 nix 时走结果集比较，不将其称为形式化等价证明。
11. 源码上游写死的 100 仓/16 线程在 live alert 提示词中替换为真实采样/已声明实验配置，未知规模不伪造。
12. 统一导航，增加 Workflow summary、参数手册、源码、知识入口；修复连续点击的前端竞态与异常反馈。

## 明确仍未实现的能力

- SHAP 没有接入；实际展示差分采样，不冒充 SHAP。
- SQL 级火焰图及 SQL 内部 CPU/内存阶段归因没有接入。已补齐真实执行/job 时间线，并明确区分。
- 自动改写、执行期会话参数只作用于 Bridge 管理的执行链路，不会透明改写所有外部客户端的 SQL。直接 PostgreSQL 客户端只能用已支持的 pg_hint_plan 应用类型。
- 当前规则有库和实际提供给模型的结构候选，但没有证明每条候选变换都实际采用；页面不将规则库列表当执行证据。
- 99 条目录 SQL 是可选择的探索输入，不是 99 条都已逐条证明能优化。本轮 AP 覆盖 Q31/Q8/Q44/Q88 和原版 Q31 的实际按钮流程。

## 最终验证与恢复

51 项 Python 回归通过；11 页 Chromium 检查包含分页、搜索、详情关联、JSON 导出、定时刷新、接口失败保留旧数据和恢复，未出现 JavaScript 异常。
专门的按钮回归核对了实际 HTTP 次数和数据库执行次数，不只检查按钮状态。

AP 联动测试期间临时设置固定只读扫描探针给 SysInsight 复测；DREAM 始终实际执行原 SQL 和改写 SQL。另修复显式复测 SQL 必须优先于活跃 SQL 的配置优先级。
结束时等待所有临时参数恢复，然后移除临时 probe 参数，恢复原先选择复测 SQL 的运行策略并重启 Bridge。
PG 没有重启；仅 exporter 为连接标识更新做过单独重启，随后 pg_up=1、scrape_error=0。

两条 10 月 7 日无存活 worker 的旧 running 事件已保留历史并标记 service_interrupted，未删除证据。
最终业务任务无 queued/running 残留，接口采集无错误；页面健康检查通过。

## 证据文件

审计目录：results/product_e2e_validation/20261022_234207/。

- audit.json / 六个 *_lab.json / *_incidents.json：六组 TP 实际运行和告警。
- dream_audit.json：自动长 SQL、短 SQL 门槛、人工重试及实际应用。
- dream_audit_before_fingerprint_fix.json、dream_audit_before_completion_signal_fix.json：修复前的真实漏触发观察，未覆盖删除。
- lab_browser_audit.json：真实按钮完整运行及修复前重复 POST 的失败断言。
- lab_browser_postfix_audit.json：修复后 HTTP 去重、真实执行数量及总结页对应关系。
- browser/*.png：真实页面截图；不是设计稿。

静态页面数据模型详见 KEENINSIGHT_DATA_MODEL.md。嵌套 DREAM 修改另存 dream_diagnosis_fix.patch，已反向应用检查确认补丁完整。本轮未执行 Git push。
