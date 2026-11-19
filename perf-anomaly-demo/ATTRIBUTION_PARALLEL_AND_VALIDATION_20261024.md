# PG12 并行 SQL 采样、SHAP 可靠性与独立验证

## 结论

并行 worker 的 SQL 归属与采样已补齐并真实验证。SHAP 的计算与可靠性隔离可用，
但预测精度尚未达标；没有把误差较大的候选模型替换上线，也没有伪称精度问题已解决。
English.zip 的原版页面组织保持不变，不恢复已删除的 realtime-monitor。

## 并行采样的身份链

PG12 源码 `src/backend/access/transam/parallel.c` 为 worker 生成含 leader PID 的标题。
本机查询也确认 worker 与 leader 报告相同 query_start。关联须同时满足：

- worker 的 PG 进程标题中的 leader PID 完全匹配，且 backend_type 为 parallel worker。
- query_start、SQL、数据库匹配，状态 active；不靠相同 SQL 文本猜测归属。
- OS executable、uid、postmaster 父 PID 相同，存在独立进程 startTicks / backend_start。
- 持续重新验证各 PID；进程出生标识或 SQL 身份变化就丢弃未确认的尾部事件。

单个 perf recorder 同时附加 leader 和已验证 worker。worker 出现/退出时，先停止旧段再开新段，
不叠加 recorder；全局采样截止时间不重置。最多 8 段、8 个 worker，perf 数据总预算 16 MiB，
省略 PID 与覆盖范围明确记录。每个 PID 使用独立有效时间窗，图中按角色/PID 分组。
短命 worker 仍可能漏采；这是部分 on-CPU 窗口，不是完整耗时、I/O/锁等待或内存归因。

两次真实验证：

| 查询 | leader 样本 | worker 样本 | 总计 |
| --- | ---: | ---: | ---: |
| 单条 SF10 store_sales CPU 聚合 | 149 | 148 + 148 | 445 |
| 相同 SQL 并发隔离测试 | 175 | 135 + 119 | 429 |

第二次同时运行另一条完全相同的查询。其实际活跃 PID 为 48834、48845、48846；
目标采样 PID 为 48838、48843、48844，两组没有交集。没有将别的查询的 worker 混入。

真实详情（运行数据按既有 24h 策略保留）：

- [单查询](http://127.0.0.1:9108/keeninsight/sql-detail.html?id=lab-exec-e2cfb7b55d8afe3d)
- [并发隔离验证](http://127.0.0.1:9108/keeninsight/sql-detail.html?id=lab-exec-e564364739007c54)
- 隔离原始证据：`monitoring/data/sysinsight_dream_bridge/sql_profiles/sql-profile-07fc7986fc52461ca867/parallel_isolation_validation.json`。

## SHAP 数据与独立验证

新增真实采集共 18 次：六组现有 TP 场景各 3 轮。每次包含无压力基线与外部压力，
使用成功运行的前台 control worker 的真实 TPS，而不是 external TPS 或 LLM 估计。
TPC-C 脚本沿用现有 rollback 保护，不由采集脚本执行全局改参。

目标取前台压力 TPS / 前台基线 TPS；压力使用 perf 时间窗口内的完整 pgbench progress 区间，
保存原始日志、完整区间、counts 与 hash。采集期间既有在线告警服务保持运行，
因此这是本机实际后台条件下的结果，不声称为隔离、恒定硬件条件的实验。
后续采集器进一步改为读取实际 perf 单调时钟事件边界，避免将解码时间算入采样窗口。

在第三轮开始前冻结候选选择与验证规则，`protocol.json` 记录时间与 hash。
历史数据上按场景分组五折，只在训练折选特征：678 特征 MAE 11.1456 pp，
32 特征 11.522 pp，64 特征 11.302 pp；没有采用效果更差的减维方案。
候选用原 53 条加前两轮 12 条训练，第三轮 6 条只测试，未参加候选选择和训练。

| 独立第三轮 | MAE / 百分点 | R² | 处理 |
| --- | ---: | ---: | --- |
| 现有模型 | 28.4455 | -4.9642 | 保留，但低可信 |
| 补数据候选 | 17.4650 | -1.2697 | 不达冻结门槛，未上线 |

六个独立目标 TPS 保留率：Stock burst 44.33%、Full mix 23.43%、WAL 13.78%、
Order status 15.76%、Payment 45.64%、Stock hot 24.14%。仅六条验证样本不足以证明稳定精度。
负 R² 与较大误差说明补少量数据仍不足以使此预测可依赖，不用 SHAP 加和残差冒充预测准确率。

复核文件：`results/shap_aligned_20261024/audit.json`、
`results/shap_validation_20261024/protocol.json`、`evaluation.json`。
已发布的 `models/sysinsight_pg12_tps/external_validation.json` 固定本次独立指标及模型/数据 hash，
与历史 11.1456 pp 分开显示；历史告警不重写。

## 低可信隔离与页面

新结果保存 `reliability` 与 `independentValidation`。当前保守策略：验证 MAE >5 pp、
验证量不足 100 条/20 场景、特征越界、非 TPCC 或未确认压力阶段，任一命中则 display-only。
这些是产品辅助证据的使用门槛，不代表达到门槛就证明因果效果。

仍计算并保存真实 TreeSHAP，仍检查背景值 + 全部贡献 = 模型预测值。
前端显示 Low confidence、原因、历史与最新独立误差；LLM 提示只传排除原因，
不传低可信的 prediction、baseValue 或贡献条形值。原源码匹配、LLM、应用、复测与恢复继续运行。
没有重新引入人工审批。没有可靠性字段的旧记录明确标记未评估，不谎称当时已被隔离。

真实告警 `incident-20261024163121-794979c766b5` 的 source_prompt.txt 已检查包含
`excluded_low_confidence`，没有在该 SHAP 提示段传数值归因；原始源码/调用链证据仍保留。

## 可重复检查

`validate_parallel_sql_profile.py` 可真实复验并发相同 SQL 的采样隔离。
`collect_shap_windows.py` 可重采六组；`validate_shap_model.py prepare/evaluate/publish`
严格区分冻结规则、一次性独立验证、发布指标，不会自动换模型。
相同输出目录不得覆盖冻结规则或重复对同一测试集重新选优。

Python 回归包含：PID 复用、相同文本不同执行、worker 误关联、动态 worker 进出换段、
各 PID 窗口与尾部丢弃、样本总数、采样失败隔离、完整前台 TPS 区间、低可信提示数值隔离。
浏览器回归覆盖 11 页及真实 SQL 图、角色/PID、缩放、真实 SHAP、正负贡献、原版双栏与刷新恢复。

最终 Python **76 项通过**；11 页浏览器与新增真实归因检查通过，无 JS 错误。
后台分析排空后仅重载 bridge，PID 55842；`/healthz` 正常，采集无错误，DREAM gate ready。
线上概览已读取新并行采样能力描述，真实图形在重载后再次通过浏览器检查。
未重启 PostgreSQL，未清空调优历史，未替换不合格的候选模型，未 push。
