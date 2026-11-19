# 告警后端采样与 DREAM 无方案诊断（2026-10-22）

## SysInsight：采样不能排在 LLM 后面

旧链路将 Prometheus 查询、瞬时 active PID 查询、perf 和 LLM 放进同一个单线程分析队列。前一个任务需要数分钟，后一个任务真正取 PID 时压力可能已结束；高频短事务也会在查询瞬间处于 idle。这不是 API Key 问题。

新链路在告警到达时通过独立采样池保存 backend_snapshot.json、active_pids.json、perf_capture.json 和 Prometheus 窗口，再将后续原始源码匹配、LLM、临时应用、复测、恢复串行执行。分析使用同一告警保存的真实 perf，不事后拿别的负载补证据。

PID 选择覆盖 active 和最近 5 秒处于事务间隙的业务连接，并排除监控、控制接口及 SysInsight 自己的验证连接。空窗口最多重试 5 秒；若业务已结束则告警保留、分析标记 skipped/no_workload_backend_at_alert，不伪造后端或成功。空函数采样不再算作成功。

真实验证（均通过实验控制台正常启动 Stock Level 场景）：

| 告警 | 告警时间 UTC | perf 开始 UTC | 函数记录数 | 结果 |
|---|---|---|---:|---|
| incident-20261022212707-9f525c8eb0b7 | 13:27:07.600 | 13:27:07.695 | 1459 | 完成真实源码匹配、10 次 LLM 调用、临时应用/复测/恢复 |
| incident-20261022213110-8fba67e7c327 | 13:31:10.961 | 13:31:11.046 | 1236 | 采样发生在前一分析 13:31:13.844 结束前；后续完整完成 |

采样提交延迟分别约 95ms、85ms。函数记录数不是事务数，也不代表调优收益。两次分析均有 source_evidence.status=completed；原始文件在 monitoring/data/sysinsight_dream_bridge/incidents/ 对应目录。

目前演示告警规则仍是 active session >= 3，且 for=0s；因此基线阶段、AP 测试和短时活动也可能触发，并非都代表业务劣化。本次没有修改这个告警阈值；针对自己验证连接的采样过滤用于避免递归分析自己的复测。

## DREAM：真实无方案的原因

修复前重跑 Q44，job dream-20261022212639-06ee4fbb91，模型 gpt-6.1-sol。记录到合法原始 JSON：missing indexes=0.30、query knobs=0.11、plan optimizer=0.15、poorly written queries=0.30，predicted_root_causes=[]。

根因是提示词要求置信度调整限制在 0.1～0.3，而动作选择要求最终置信度 >0.5。RCRank 四项初始分数接近零（约 0.00296/0.000035/0.000013/0.00371），单轮受限调整无法越过动作阈值。旧解析器又把四项真实分数丢掉、折叠成 normal=1，掩盖了原因。

此外独立发现解析缺陷：{}、缺字段或空 root_causes 也会被当作正常。不能据此断言过去每个 no_action 都是解析错误，因为旧记录未保留原始响应。

已修正：

- 最终四项分数独立取 [0,1]，不再被小模型低分或 0.1～0.3 的调整上限锁死；仍允许基于证据合法返回 no_action。
- 兼容数组中的未采集指标零值明确标注为 unknown，不再提示模型把它当成真实零 CPU/I/O。
- 严格验证四个标签、唯一性、有限数值 [0,1] 和 >0.5 的选择一致性；无效响应重试后明确失败，不再默认 normal。
- 保存 diagnosis_trace（模型、初始分数、实际提示词、原始响应、校验结果）；合法 normal 保留四项真实分数。
- 运行时同步增加 planner.py、prompt.py，避免源代码与 postgres 用户的 /tmp 运行副本不一致。

修复后真实 LLM 测试：

| SQL / job | 真实诊断 | 后续结果 |
|---|---|---|
| Q44 / dream-20261022213018-2a8f4811e2 | 缺索引 0.85，改写 0.96 | 生成索引与改写，但复测未通过收益校验；索引 DDL 也不属于自动发布类型，保留 candidate，不发布 |
| Q88 / dream-20261022213255-f343c6cafa | 改写 0.99 | 生成改写，真实执行 2.008s，收益校验通过，登记 active 的 bridge_managed 自动应用 |

随后调用原实验 lab-dream-20261022205542-c5b2ad685f 的正式 replay 接口，实际执行命中改写，耗时 2.056s，方式 DREAM automatic SQL rewrite。该实验保留的原 SQL 基线为 12.510s，界面显示约 83.6% 下降；基线是此前记录，并非本次同一负载窗口的新配对试验，不能据此宣称重复稳定收益。

Q44 的评估与 TP 验证存在并发，只能判断本轮候选未通过，不能据此断言该 SQL 无优化空间。测试后查询确认候选索引 store_sales_ss_store_sk_ss_item_sk_idx 未遗留在数据库中。

## 回归与代码保存

`python3 -m unittest discover -s perf-anomaly-demo -p 'test_*.py' -q`：33 项通过，覆盖分析队列阻塞时提前采样、近期 idle PID、真实空窗口跳过、空 JSON/越界/重复标签拒绝和原始诊断保留。

dream/ 是独立且被主仓库忽略的嵌套工作树。本次其中两个源文件的变更另存为主仓库文件 dream_diagnosis_fix.patch，防止以后只推主仓库时遗漏。新拉取的未修复 DREAM 可用 `git -C dream apply ../perf-anomaly-demo/dream_diagnosis_fix.patch` 应用；已经修复的工作树不要重复应用。

## 最终上线核对

测试期间共 5 条事件完整完成，全部确认 source_evidence=completed、真实 LLM 调用 10 次以及 applied_and_restored。为避免中断临时配置恢复，使用 SIGINT 停止采集后让现有执行队列自然收尾，再重启桥接服务。

上线时残留的一次演示告警已没有业务后端，记录为 skipped/no_workload_backend_at_alert，而不是 failed；监控元数据查询及自身复测连接不作为业务后端。空闲机器实查符合条件的业务 PID 为零。

最终控制台告警页 HTTP 200，采集无错误。通过正式 KeenInsight 告警详情 API 核对，两次主验证均显示 8 条运行时调用链、19 项参数源码证据，以及参数已恢复。Q88 SQL 详情 API 可读取调优结果与自动应用字段。Grafana 健康检查正常，数据库未重启。嵌套补丁通过 `git -C dream apply --reverse --check ...` 校验。本次未执行 Git push。
