# SQL CPU 火焰图与 SHAP 实现、真实验证

> 后续补充与最新结论见 [并行采样与独立验证](ATTRIBUTION_PARALLEL_AND_VALIDATION_20261024.md)。
> 下文原始 Q44/SHAP 数值是早期实测；PG12 worker 已补齐。新增独立批次暴露更大预测误差，
> 不能只引用下文 11.15 pp 历史交叉验证误差声称线上模型可靠。

## 产品入口

- SQL 火焰图：SQL Logs → Details → 原版 SQL Lifecycle 区域。
- SHAP：Alert List → Details → 原版 SHAP Model Analysis Results 区域。
- 原版菜单、样式与双栏组织保留，realtime-monitor 仍删除；监控曲线仍在 Grafana。

真实可查看记录（24h 运行保留策略内有效）：

- Q44：[SQL 详情](http://127.0.0.1:9108/keeninsight/sql-detail.html?id=lab-exec-efb1de99d01c32e3)
- SHAP：[告警详情](http://127.0.0.1:9108/keeninsight/alert-detail.html?id=incident-20261023235623-3c8a4a8ab114)

## SQL 级 CPU 栈采样

`sql_profiling.py` 复用 Linux perf，但不使用机器级或告警级栈冒充 SQL 栈。
实验控制台通过原 `_execute_lab_sql` 路径执行，采样器只观察该连接；普通外部连接则从
既有 pg_stat_activity 采集周期发现。采样本身不执行或重放 SQL，也不调用 LLM。

每次采样绑定 PID、backend_start、query_start、SQL 原文和摘要，托管执行还绑定 execution_id。
每隔约 0.3 秒核对身份，SQL 变化/结束即停止。使用单调时钟，仅发布落在最后一次确认有效的
时间边界内的事件，丢弃尾部事件，防止复用同一连接后混入下一条 SQL。

- 默认单路 recorder，49 Hz，最多 10 秒；附加 SQL 主进程与最多 8 个身份验证的 worker。独立 watchdog 限时停止 perf，探测连接独立 2 秒超时，不受业务 SQL 的长超时拖延。
- 托管执行约 0.75 秒以上尝试，普通外部执行须已运行至少 3 秒且被轮询看见。
- 这不修改 DREAM 的规则：自动调优仍要求 AP SQL 完成且运行超过配置门槛（当前 10 秒）。
- 原始 perf 文件设大小上限，空闲空间不足 512 MiB 不采样；失败只记录原因，不重复执行 SQL。
- PG12 无 leader_pid，通过 PG 生成的 `parallel worker for PID` 标题、查询身份与 OS 出生标识联合关联，不能仅凭相同 SQL 文本合并。
- CPU 采样图不是 SQL 全程延迟、时间轴、锁等待、I/O 等待或内存归因。
- 树宽度来自实际事件数量；低占比栈合并仍保持样本总数，不归一化为虚构耗时。
- 悬停可看函数/样本占比，点击函数缩放，支持重置及放大查看。
- `sql_profiles` 保存精简树和证据。数据及专属原始文件按 24h 清理；DREAM 工作区清空时先暂停并排空采样器，再归档清理，不能反向停止用户 SQL。

验证结果：

| 真实执行 | 结果 |
| --- | --- |
| 实际 CPU 聚合 SQL | 26 个事件、10 条不同栈，先验证 PID/窗口/解码 |
| TPC-DS Q44，原控制台执行函数 | 8169.927 ms；339 个 CPU 样本、92 条不同栈、7.037 s 有效采样窗口 |
| 普通外部连接 Q44 | 8693.400 ms；自动发现并保存 109 个 CPU 样本，关联同 SQL 指纹页面 |
| Q44 独立限时保护复验 | SQL 实际 8233.463 ms；设置采样上限 2 s，实际有效窗口 1.980 s、91 个 CPU 样本，SQL 未被停止 |

Q44 验证使用会话级 `max_parallel_workers_per_gather=0`，使 CPU 主要由被采样后端消耗，
不改数据库全局参数。这里验证的是采样，不是优化提速；不与旧 Q44 优化耗时混算。
托管 profile：`sql-profile-d54cad6ecd714343bb22`；外部 profile：`sql-profile-b23fa532f1ae43238d93`。
按执行 ID 查询不回退另一执行的图；按指纹查看时明确标注“最近一次实际采样”，不是全部调用汇总。

## 真正的 SHAP

原仓库 SHAP 加载器指向缺失的 MySQL 模型文件，不能直接用于当前 PG。
本次新增 PostgreSQL 本地模型，并实际调用 SHAP 库，未将差分比例、置信度或 LLM 文本改名成 SHAP。

- 数据：已有真实压测产生的 53 份不同 perf 记录、37 个场景；没有生成训练样本或估计 TPS 标签。
- 特征：678 个源码关联库中的函数采样特征。
- 目标：压力下目标业务 TPS / 无压力基线目标业务 TPS；排除外部注入 TPS 标签混淆。
- 模型：RandomForestRegressor；不在用户 HTTP 请求中训练，推理在独立进程中执行并限制超时/线程。
- 归因：TreeSHAP，显式 tree_path_dependent 背景分布；正贡献增加模型的 TPS 保留率预测，负贡献降低预测。
- 验证：按场景 ID 分组五折，MAE **11.1456 个百分点**，R² **0.358289**。泛化能力有限，必须标为实验性，不能当成线上性能保证。
- 不适配 AP-only/TPC-DS 性能预测，不声称解释 RCRank、LLM 思维或因果效应。
- 保存模型及训练数据摘要、输入文件 hash、背景值、预测值、所有归因、范围偏离、误差和加和残差。
- Top12 与“其他函数合计”用于页面，完整结果单独保存；只向 LLM 传精简证据，避免数次复制大数组。
- 缺模型、校验失败等显示 unavailable，并继续原诊断链路；不捏造条形图。

模型：`pg12-foreground-tps-bfe2bbb0c93dc30c`。
本次新告警的背景值 0.51912354，预测值 0.64804097，加和残差 **-1.39e-16**。
注意这是模型预测，不是声称该告警窗口实测 TPS 保留率为 64.80%；且该窗口有特征超出训练范围。

## 线上链路核验

实际通过实验控制台 API 启动 `tp_stock_level_burst`：
`lab-sysinsight-20261023235610-dad750d63e`，30 秒基线、120 秒压力观察。

告警 `incident-20261023235623-3c8a4a8ab114` 真实完成：

`perf → SHAP → 原始差分/源码匹配 → LLM → 参数验证 → 应用 → 复测 → 恢复`

- `shap_model_attribution` 阶段约 981 ms（含子进程启动）。
- 实际 GPT-6.1-SOL 请求轨迹中包含该模型版本及 SHAP 正负贡献；原始调用记录 10 次、候选 10 个。
- 告警最终 completed，应用状态 applied_and_restored。
- 在线链路复测 SQL 中位耗时 1019.858 → 990.445 ms，恢复后 999.897 ms。
- 实验 TPS 1437 → 613 → 855；压力下恢复约 39.5%。实验复用了历史验证候选，
  与上面在线新 LLM 的单 SQL 复测是不同证据，不能将这 39.5% 全归功于新 SHAP 或新 LLM。
- 告警较早在基线阶段触发（既有宽松告警规则）；SHAP 解释其真实采样窗口，不冒充压力阶段采样。

最后检查：采集健康、pg_up=1，无运行中实验/调优任务，未重启 PostgreSQL，未清空现有优化结果。

## 回归

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python3 -m unittest discover -s perf-anomaly-demo -p 'test_*.py' -q
node perf-anomaly-demo/test_keeninsight_browser.cjs
node perf-anomaly-demo/test_attribution_browser.cjs
```

本机 Python **67 项通过**；浏览器检查包括 11 页、真实图形、身份关联、缩放/重置、正负归因、
加和验证、分页/筛选、导出、刷新/断连恢复及原版双栏。无浏览器 JS 异常。
浏览器脚本只读，缺少真实保留样本时明确要求先运行场景，不生成假数据通过测试。

配置：`KEENINSIGHT_SQL_PROFILING=0` 可关闭采样；
`SYSINSIGHT_SHAP_MODEL_DIR` 可指向受信任的本机模型目录；依赖已写入 requirements-sysinsight.txt。
