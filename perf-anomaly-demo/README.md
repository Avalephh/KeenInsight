> **v2 scope note (2026-11-19):** SQL-level tuning (DREAM SQL rewrite/hints,
> TPC-DS SQL lab, SQL log/detail pages) has been removed from this stack.
> This document records the pre-v2 artifact; the active product is the
> SysInsight system-parameter tuning path with the TPCC experiment console.

# PostgreSQL 突发异常与 perf 采样测试

这个目录独立于仓库中的 `monitoring/` 和 `sysinsight-tuning-demo/`。

栈折叠使用 FlameGraph 官方开源脚本 `stackcollapse-perf.pl`，来源为
`https://raw.githubusercontent.com/brendangregg/FlameGraph/master/stackcollapse-perf.pl`，本地副本位于 `vendor/FlameGraph/`。

`run_demo.py` 会在本机 `keeninsight` 数据库上执行一组可控的只读计算。Prometheus 告警到 perf 的连接是本 demo 明确增加的触发扩展；告警触发之后的函数统计和异常函数提取使用 SysInsight 原始源码：

1. 先运行较轻的 `generate_series + sqrt` 基线负载；
2. 再运行更多并发、包含 MD5 计算的异常负载；
3. 每秒记录 PostgreSQL 活跃连接、数据库累计计数器、worker backend CPU 和时间戳；
4. 轮询 Prometheus `/api/v1/alerts`，收到 `SysInsightDemoAnomaly` firing 告警后，对当前 PostgreSQL backend 进程执行原生 `perf record -F 300 -g`；
5. 使用 FlameGraph 官方 `stackcollapse-perf.pl` 生成折叠栈；
6. 直接执行 SysInsight 原始 `DBEnv.get_perf_function_range`，生成函数采样文件。

测试不会创建表、修改参数、写入业务表或重启数据库。由于当前 Prometheus 抓取和规则评估周期为 15 秒，验证告警触发时建议把异常负载设置为至少 45 秒。

运行：

```bash
cd /path/to/checkout/perf-anomaly-demo
python3 run_demo.py --anomaly-duration 45
```

结果保存在 `results/<时间>/`：

- `baseline_samples.jsonl`：基线时间序列；
- `anomaly_samples.jsonl`：异常时间序列；
- `summary.json`：触发时间、数据库计数器增量和安全说明；
- `*_perf.log` / `*.perf.data` / `*.perf.script`：perf 各阶段产物（若可用）；
- `*_counts_tpcc.txt`：SysInsight 原始 `get_perf_function_range` 生成的函数采样结果；文件名后缀由原始源码的 `result_flag=2` 逻辑产生；
- `anomaly_counts_tpcc_btFunctions.txt`：SysInsight 原始 `analyzeException.compare_file_sample_rate` 生成的全部异常函数结果；
- `sysinsight_source_detection_result.json`：原始异常提取和原始函数/参数匹配的返回值记录。

当前主机如果仍找不到 `perf`，脚本不会安装软件或调整 `perf_event_paranoid`，而是照常验证异常触发和数据库观测部分，并在 `preflight.json` 与 `summary.json` 中记录原因。

## 原始 SysInsight 异常提取

对某次已经完成的测试运行原始 SysInsight 检测和原始函数匹配：

```bash
cd /path/to/checkout/perf-anomaly-demo
python3 sysinsight_detection.py --run-dir results/<时间目录>
```

这个脚本只调用以下原始源码函数，不增加筛选步骤：

- `DBEnv.get_perf_function_range`：从 folded stack 统计函数采样率；
- `analyzeException.compare_file_sample_rate`：按原始基线范围输出所有越界函数；
- `matchFunctions.find_top_and_matched_functions`：执行仓库原有的函数到 knob 匹配逻辑。

因此输出的异常函数数量可能较多，这是原始源码行为。使用 PG profile 时，匹配器读取固定
PostgreSQL 12.22 源码构建的 276 条文档化 GUC 关联，其中 231 条数值/枚举/布尔 GUC 可作为
自动搜索维度；仓库原有的 5 条 PG 关系在记录中保留并明确标注为 legacy，新增关系均可追溯到
源码证据。

Prometheus 演示规则位于 `../monitoring/config/prometheus/rules/sysinsight-demo.yml`：`SysInsightDemoAnomaly` 在外部压力阶段（`phase="pressure_before"`）的目标业务 TPS 低于基线最大值的 90% 并持续 10 秒时触发，用于把“TPS 下降”作为系统调优流程的异常入口；它不改变 SysInsight 的异常提取逻辑。

如果某次告警在函数采样之后、候选生成或验证之前被重启中断，incident 目录仍会保留 `anomaly/anomaly_counts_*.txt` 和 `anomaly/anomaly.folded`。此时运行：

```bash
python3 backfill_incident_analysis.py <incident-dir>
```

会基于这些原始 perf 数据事后生成差分画像、函数分析、运行时调用链、源码关联和 SHAP，并写入 `backfill_function_analysis.json`；门户会自动读取它。该工具不会伪造 LLM 候选、应用结果或调优前后对比。

Function Analysis 还会用 `function_locations.py` 为固定 PostgreSQL 12.22 源码树建立函数定义索引（缓存到 `results/function_locations.json`），为 perf 函数补全源码 `file:line`。生成代码、扩展聚合符号和内核符号无法在 PostgreSQL 源码中定位时，页面会明确说明。点击任一函数可查看源码注释、函数签名、相关参数/源码证据、运行时调用链和代码片段。

告警详情的“根因解释”会读取 `tuning_rationale.json`，把实际调优动作逐项列出：参数变化、为什么这样调、关联函数/源码证据和复测结果。该文件由 `tuning_rationale.py` 基于所选 LLM 候选理由、before/after 设置、源码证据和 comparison 生成；可用 `python3 tuning_rationale.py --scan` 为历史已完成告警补齐。未应用的参数会标明原因（例如需要重启）。

告警详情的 Tuning Results 会优先显示关联实验台同一次运行的目标业务 TPS：基线、压力调优前、压力调优后、调优提升和调优后/基线；告警链自己的只读 SQL 回放复测仍保留在折叠的 Workflow evidence 中，两种测量不会混在一起。

## 源码调用链证据

统一入口生成的 `sysinsight_input.json` 现在会保留两类真实证据：一类是异常阶段
`perf` folded stack 中与异常函数或匹配参数相关的采样调用路径；另一类是固定
PostgreSQL 12.22 源码中参数绑定变量的直接使用、文件、行号、上下文、源码摘要和
函数片段。`sysinsight_original_llm.py --dry-run` 会把同一份证据放入原始 LLAMBO
Prompt，随后真实 API 请求也复用这份 Prompt。

这部分证据表示“运行时采样路径”和“源码直接引用关系”，不是凭扫描结果推导出的完整
静态调用图，也不直接证明参数调整方向；方向仍必须由候选配置的真实 workload 复测确认。
如果缺少 perf folded stack，输入会明确记录调用链缺失，不会用预设调用链代替。

## 统一 SysInsight 主流程

`sysinsight_pipeline.py` 把监控输入、原始检测/匹配、LLAMBO 候选和候选实测串成一个入口：

```bash
python3 sysinsight_pipeline.py \
  --case-result results/tpcc_api_validation/<run>/<case>/case_result.json \
  --api-result results/tpcc_api_validation/<run>/<case>/sysinsight_api/result.json \
  --max-candidates 5
```

结果目录下的 `sysinsight_input.json` 固定包含八个部分：环境、负载、时间窗口、Prometheus 告警、主机资源、数据库观测、perf/异常函数和调优上下文。若不提供 `--api-result`，且环境中存在 `SYSINSIGHT_GPT_API_KEY`，入口会自动调用 `sysinsight_original_llm.py`；没有 key 时会明确记录候选生成被跳过。

候选的 `pg_settings` 名称和 profile 约束会先做静态校验，再做一次只读的 live `pg_settings` 校验。需要真实执行“候选配置→TPCC→指标比较”时显式增加：

```bash
python3 sysinsight_pipeline.py \
  --case-result results/tpcc_api_validation/<run>/<case>/case_result.json \
  --api-result results/tpcc_api_validation/<run>/<case>/sysinsight_api/result.json \
  --workload-module tpcc_transaction_cases \
  --benchmark-candidates 1 --tuned-duration 20
```

候选评测复用现有 PostgreSQL 临时配置生命周期模块；本入口不另行实现配置快照、应用或恢复机制。

如果需要让各环节自动衔接，不再手工准备 `case_result.json` 或 `--api-result`，运行：

```bash
python3 sysinsight_auto.py \
  --only d01_work_mem_sort \
  --workload-module tpcc_external_cases \
  --benchmark-candidates 3
```

这个入口先运行真实 TPCC 基线和压力场景，等待 Prometheus 告警后启动 perf 和原始 SysInsight 检测；随后自动调用 GPT5.6-SOL、解析并校验候选配置，逐个进行真实 TPCC session-only 调优测试，最后按实测控制 TPS 选择最佳候选并写入统一报告。候选测试结束后仍由现有临时配置模块负责清理和恢复。

Prometheus 侧也可以单独等待告警并导出最近窗口：

```bash
python3 sysinsight_prometheus.py --wait --output /tmp/sysinsight-prometheus-input.json
```

## SysInsight + DREAM 在线联动

`sysinsight_dream_bridge.py` 是在线链路入口：它持续轮询 Prometheus 告警，同时把每一条
`pg_stat_statements` 的累计调用数、总耗时、平均/最大耗时和活动会话样本写入 SQLite。告警
第一次进入 firing 时，后台生成 SysInsight 的八段式输入并异步分析；达到慢 SQL 阈值的只读
语句进入 DREAM 单 SQL 队列，不阻塞告警检测。

先为新数据库连接启用 `pg_hint_plan` 的 Hint 表（本机已安装 1.3.10）：

```bash
python3 sysinsight_dream_bridge.py \
  --configure-hint-table --once --no-api \
  --state-db /tmp/sysinsight-dream-bridge.sqlite3 \
  --output /tmp/sysinsight-dream-bridge
```

再启动常驻链路；API key 只从环境变量读取，SysInsight 和 DREAM 共用同一组变量及
`gpt-6.1-sol` endpoint：

```bash
export SYSINSIGHT_GPT_API_KEY='<your-api-key>'
export SYSINSIGHT_GPT_BASE_URL='https://api.gpt.ge/v1'
export SYSINSIGHT_GPT_MODEL='gpt-6.1-sol'
python3 sysinsight_dream_bridge.py \
  --db keeninsight --db-schema tpcds \
  --dream-config ../dream/config/tpcds_local_config.json \
  --alert-name SysInsightDemoAnomaly \
  --state-db /tmp/sysinsight-dream-bridge.sqlite3 \
  --output /tmp/sysinsight-dream-bridge
```

`gpt-6.1-sol` 的 responses compatibility 模式不支持 `n>1`；`sysinsight_original_llm.py`
会把一次 `n=5` 请求拆成 5 次 `n=1` 请求并合并 `choices`，调用方无需改动。

六个焦点场景可以用下面的入口顺序运行，并从告警详情审计函数/差分/SHAP/调用链和函数说明是否完整：

```bash
python3 run_focus_scenarios.py --scenarios tp_payment_hot,tp_stock_level_hot,tp_stock_level_burst,tp_full_mix_surge_high,tp_wal_checkpoint,tp_order_status_hot
```

DREAM 任务完成后，方案只有同时满足“只读、DREAM 真实复测成功、实测提升至少 10%”才会进入
active。合法 plan Hint 和只影响规划阶段的会话参数发布到 `hint_plan.hints`，下一次相同规范化
SQL 可由 PostgreSQL 直接套用；只读 SQL 改写以及 `work_mem` 等执行期参数登记到桥接执行路径，
实验控制台或接入该路径的应用再次发送相同 SQL 时会自动应用。DDL、未知动作、缺少优化后实测
耗时或收益不足的结果只保留为 candidate。PostgreSQL 本身不能透明替换任意客户端发来的 SQL，
所以改写类方案要求客户端经过桥接/应用集成路径；直接连接数据库的客户端只能透明获得 Hint
类方案。`pg_hint_plan` 的数据库设置只对新连接生效，已有业务连接需要重连。每轮观测、incident、
DREAM job、验证和应用记录都在 state SQLite 及 `output/incidents/` 中。

实验控制台的 TPCC 启动是幂等的：已有真实任务时重复点击会返回同一个 run 并继续跟踪；桥接
进程重启后，会把遗留的 `queued/running` run 标记为 `startup_recovery`，并只按该 run 生成的
`application_name` 精确终止孤儿数据库会话和 pgbench 进程组。这样异常退出后可以直接重新启动，
不会留下永久的 “already queued or running” 锁。

TPCC 实验本身采用两阶段窗口：先运行基线，再保持外部压力直到实验结束。默认基线为 60 秒、
持续压力观察为 180 秒；压力窗口同时承担告警触发、SysInsight 调优和目标业务 TPS 观察。目标
业务 TPS 是 `tp_normal.sql` 控制负载的吞吐，外部场景 TPS 只用于表示压力注入强度。结果会剔除
启动升温窗口，以稳态中位数和 CV 展示主指标；不再追加撤压后的自然恢复阶段，因此只有压力仍
在时目标业务 TPS 回升，才作为调优恢复证据。

实验控制台也提供连续模式，由操作员按钮控制，而不是固定结束时间：
`POST /api/v1/actions/lab/tpcc/continuous`，`{"mode":"baseline"}` 持续运行
`tp_normal.sql` 基线；`{"mode":"pressure","scenario_id":...}` 立即叠加所选场景的外部压力，
默认 120 秒后自动应用已校验的 SysInsight 候选，并继续以调优后配置运行。再次请求 baseline 模式
会先停止压力、恢复初始配置，再重新持续运行基线。切换模式通过停止旧 run、等待配置恢复并启动
新 run 完成；自动评测的 `/api/v1/actions/lab/tpcc/start` 三阶段入口保持不变。

如果本机 PostgreSQL 使用 Unix socket peer 认证，DREAM worker 需要以数据库 OS 用户运行，并
把 DREAM checkout 放在该用户可读取的位置；也可以改用 TCP/密码认证。例如本机验证可额外传入
`--dream-run-as postgres --dream-runtime-root <postgres 可读的 DREAM 根目录>`。捕获不到活动会话
中的实际参数时，参数化 `pg_stat_statements` 记录会暂存为 blocked，下一次捕获到可回放样本后
自动重新入队。

## 数据库 / 版本切换

原始 LLAMBO 配置流程现在通过 profile 选择数据库和版本，MySQL 默认文件仍直接使用仓库原有文件，PostgreSQL 使用固定源码生成的关联库以及仓库中已有的 PG 手册、结构化知识资料：

```bash
cd /path/to/checkout/perf-anomaly-demo
# 查看可用 profile
python3 sysinsight_original_llm.py --list-profiles

# 只生成 PostgreSQL 12 的原始 prompt，不调用模型
python3 sysinsight_original_llm.py \
  --case-result results/tpcc_external/<run>/<case>/case_result.json \
  --dbms postgresql --db-version 12 --dry-run

# 切换回原始 MySQL 8.0.36 profile
python3 sysinsight_original_llm.py \
  --case-result results/tpcc_external/<run>/<case>/case_result.json \
  --dbms mysql --db-version 8.0 --dry-run
```

异常函数匹配也接受相同开关：

```bash
cd /path/to/checkout/perf-anomaly-demo
python3 sysinsight_detection.py --run-dir results/<run>/<case>/anomaly \
  --dbms postgresql --db-version 12
```

PG profile 当前使用 276 条源码可追溯的 PG 参数—函数关联，其中 231 条进入自动搜索空间，
并保留仓库已有的 5 条关联作为 legacy 子集；仓库没有 PG 历史规则文件，PG profile 的规则段
保持为空。
