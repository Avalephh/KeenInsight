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

Prometheus 测试规则位于 `../monitoring/config/prometheus/rules/sysinsight-demo.yml`，只用于验证“告警触发 perf”；它不改变 SysInsight 的异常提取逻辑。

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
