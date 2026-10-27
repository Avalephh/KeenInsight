# 配置与实验约定

主配置模板为 `config/env.example`；运行时使用私有
`monitoring/.env.local`（0600），它是可信操作者的 shell 文件，不接收上传。

| 配置组 | 主要变量 | 说明 |
|---|---|---|
| 数据库 | `SYSINSIGHT_DB/DB_USER/DB_HOST/DB_PORT/DB_SCHEMA` | 目标测试库及 TPC-DS schema |
| PG service | `SYSINSIGHT_DB_VERSION/PG_CLUSTER/PG_SERVICE` | systemd 资源限制的准确作用目标 |
| 模型服务 | `SYSINSIGHT_GPT_API_KEY/BASE_URL/MODEL` | SysInsight 与 DREAM 共享；不得保留示例 endpoint/model |
| 可选凭据桥接 | `SYSINSIGHT_CODEX_CONFIG_DIR` | 仅显式选择时读取操作者配置；不复制 auth 文件 |
| worker | `SYSINSIGHT_DREAM_RUNTIME_ROOT/DREAM_RUN_AS` | postgres 可读、持久、与源码分离 |
| DREAM 输入 | `DREAM_SOURCE_VENV/DREAM_SOURCE_CONFIG` | 默认外部 dream/.venv 与本项目 config/dream.example.json |
| 采集 | `SYSINSIGHT_POLL_INTERVAL/STATS_LIMIT/ACTIVE_LIMIT` | 默认 5 秒、300 个统计、100 活跃后端 |
| AP 门槛 | `SYSINSIGHT_DREAM_TRIGGER_MS` | 默认 10000 ms；完成增量观测，不是保证每次调用逐条日志 |
| 保留 | `SYSINSIGHT_SQL_SAMPLE_RETENTION_DAYS/MAX_ROWS` | 默认 1 天、500000 样本；分批维护 |
| 资源 | `SYSINSIGHT_PG_CPU_QUOTA/MEMORY_LIMIT/TASKS_MAX` | 只对明确 PG service 应用；需与主机能力匹配 |
| SQL 采样 | `KEENINSIGHT_SQL_PROFILING` | 默认开启，有限窗口与频率，失败不重放业务 SQL |
| 系统链 | `SYSINSIGHT_FULL_CHAIN` | 告警后的原始源代码分析/LLM/应用复测链 |
| TP 回放 | `SYSINSIGHT_LAB_CANDIDATE_DIR` | 私有 `<scenario_id>.json` 候选，不随源码提供 |
| SHAP | `SYSINSIGHT_SHAP_MODEL_DIR` | 可选可信训练模型；默认没有模型就不可用 |
| Grafana | `GF_SECURITY_ADMIN_PASSWORD` | 首次启动必须提供强密码，无公开默认密码 |

更细的 timeout/批量/频率控制以 bridge `--help` 与 `monitoring/start.sh`
为准。示例：

```bash
python3 app/sysinsight_dream_bridge.py --help
python3 app/sysinsight_shap.py --help
python3 app/collect_shap_windows.py --help
```

后两项可能产生真实采样/压力或读取训练数据；只查看 `--help` 不等于执行实验。

## TP 六组压力场景

目录保留六组主要配方：热点 Payment、热点 Stock Level、突发 Stock Level、
高强度混合负载、WAL/checkpoint、热点 Order Status。底层还有用于组成
这些配方及兼容已有场景 ID 的 SQL workload。
`tpcc_external_cases.py` 的独立 CLI 还保留用于参数敏感性实验的受控配方，
其 `repair` / tuned SQL 是显式实验对照，不是 LLM 推荐或发布成绩。

业务控制 worker 与外部压力 worker 分开；TPS 是完成的事务速率，不是 SQL
请求数量或压力“设定值”。开放环 `-R` 设定相同不代表实际完成外压相同。
先看基线是否稳定，再看压力降幅、前后外压吞吐偏差和多次重复结果。

开源版不分发过去实验产生的候选。要执行明确的候选回放，将自己获取的
JSON 对象放到私有目录，并设置：

```bash
export SYSINSIGHT_LAB_CANDIDATE_DIR=/path/to/private/candidates
# 文件名例：tp_stock_level_hot.json
```

文件是实际参数对象，由操作者取得并审查；本项目不提供声称有收益的预置值。
来源标注 `operator_supplied_candidate_replay`。缺文件时状态 unavailable，
不把同配置的随机 TPS 变化称为调优成功。

## DREAM

自动触发针对**完成**且达到门槛的可回放只读 AP SQL，不会在仍活跃时
复制同一条业务 SQL 再跑一遍。显式控制台实验可人工要求诊断，记录 manual，
不得将它混称 automatic_completed_sql。

plan hint 可由 pg_hint_plan 直接匹配；改写/session settings 必须通过
bridge-managed 执行。详情区分：

- 原始执行、模型内部候选评估、后续实际优化执行；
- 本次 Job 的候选/错误与当前 active 方案；
- 未应用、策略拒绝、失败、no_action 与成功命中；
- QED 形式化验证与当前数据集结果集比较。

不要把“优化方案已生成”当成“下一次任意 SQL 客户端都会被透明改写”。
