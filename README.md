# 数据库性能监控与 SysInsight Demo

重启恢复／新机部署统一入口：`bash scripts/deploy.sh install`；
日常管理用 `start|restart|stop|status`。首次部署需要准备数据库、真实模型和私有 API 配置，
具体步骤见 [部署说明](monitoring/DEPLOYMENT.md)；脚本不会自动覆盖数据库或清空优化记录。

这个 Git 工作区保存当前 Demo 的可复现源码和配置，包含：

- `monitoring/`：原生 Prometheus、node_exporter、postgres_exporter 和 Grafana OSS 的本机监控配置与启动脚本；
- `perf-anomaly-demo/`：TPCC 外部压力、Prometheus 告警触发 perf、原始 SysInsight 异常提取以及真实 GPT API 配置验证；
- `sysinsight-tuning-demo/`：与数据库隔离的 SysInsight 单步回放；
- `监控产品文档.md`、`产品性能展示.pptx`：当前产品文档和展示材料；
- `perf-anomaly-demo/TPCC场景验证报告.md`：本轮 TPCC 外部压力与真实 SysInsight API 闭环实验报告。

## Git 内容边界

Git 只提交源码、SQL、配置、输入样例、profile 和文档。Prometheus/Grafana 数据目录、运行日志、perf 二进制结果、时间序列采样、数据库数据、下载包以及本地上游仓库快照均被 `.gitignore` 排除，避免把 3GB 级运行状态或密钥带入仓库。

本轮另外保留了 6 组达标 TPCC 场景的结构化 `summary.json`、`case_result.json`、`selected_api_configuration.json`、SysInsight 原始检测结果和真实 API `result.json`，用于复核 TPS、配置应用/还原和 API 返回；这些是精选的实验凭证，不包含原始 perf 大文件或 API key。其余运行目录仍按上述规则留在本地。

SysInsight 使用的原始开源源码不是本项目重写的实现。复现所需的源码快照和 PostgreSQL 12.22 源码树见 [EXTERNAL_SOURCES.md](EXTERNAL_SOURCES.md)。API key 只通过环境变量传入，不写入文件或结果。

## 环境要求

- Linux；Python 3.8+；
- PostgreSQL 12.x，数据库 `keeninsight` 中存在 `keeninsight_tpcc` schema；
- `psql`、`pgbench`、`perf`、`curl`、`perl`；
- Prometheus、node_exporter、postgres_exporter 和 Grafana OSS。当前主机已有的发行包路径可通过 `monitoring/env.example` 中的变量指定；这些大文件不进 Git；
- 调用真实 SysInsight API 时，需要原始 WorkloadTune 源码快照和 Python 依赖。

## 从干净检出准备环境

下面的步骤只获取源码和开源运行组件；不会获取数据库数据，也不会把 API key 写入文件：

```bash
./scripts/fetch_keeninsight_sources.sh
./scripts/fetch_postgresql_source.sh       # 只有要重建 PG profile artifact 时必需
./monitoring/install_open_source.sh
INSTALL_PPT_DEPENDENCIES=1 ./scripts/setup_python.sh
./scripts/check_reproducibility.sh --strict
```

只运行监控时可跳过两个源码脚本和 Python 依赖安装。`psql`、`pgbench`、`perf` 等系统工具以及
PostgreSQL 实例需要由运行环境另行提供；检查脚本会验证它们是否存在。

安装 Python 依赖：

```bash
./.venv/bin/python -m pip install -r requirements-sysinsight.txt
./.venv/bin/python -m pip install -r requirements-ppt.txt  # 仅生成 PPT 时需要
```

## 运行本机监控

先复制环境变量模板并按本机安装路径修改：

```bash
cp monitoring/env.example monitoring/.env.local
source monitoring/.env.local
monitoring/start.sh
monitoring/status.sh
```

Grafana：<http://127.0.0.1:3000/>；Prometheus：<http://127.0.0.1:9090/>。Grafana 大屏的 JSON 在 `monitoring/dashboards/`，Prometheus 告警规则在 `monitoring/config/prometheus/rules/`。

联动状态大屏：<http://127.0.0.1:3000/d/sysinsight-dream-automation?orgId=1&refresh=15s>；后台动作管理控制台：<http://127.0.0.1:9108/ui>。`monitoring/start.sh` 会自动启动 SysInsight bridge，并把其状态作为 Prometheus 指标接入 Grafana。

一页实验控制台：<http://127.0.0.1:9108/lab>；Grafana 实验看板：<http://127.0.0.1:3000/d/sysinsight-experiment-lab?orgId=1&refresh=5s>。控制台可重置实验参数、触发 TPCC 基线→压力→恢复，并查看压力下调优前后的目标业务 TPS 对比。

## 运行真实 API 的 TPCC 验证

先准备环境变量，不要把 key 写入 Git：

```bash
export SYSINSIGHT_GPT_API_KEY='<your-api-key>'
export SYSINSIGHT_SOURCE_ROOT="$PWD/repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune"
```

确保监控已启动后，使用真实 API 验证一个场景：

```bash
./.venv/bin/python perf-anomaly-demo/tpcc_api_recommendation_validation.py \
  --only d01_work_mem_sort \
  --api-base 'http://35.212.195.134:28317/v1' \
  --model 'GPT5.6-SOL' \
  --run-source-selector
```

这个入口会运行 TPCC 正常控制负载和外部压力，轮询 Prometheus firing 告警；告警触发后对当前 PostgreSQL backend 执行原生 `perf record`，再执行原始 SysInsight 文件分析和函数匹配，调用 API，严格解析 API 返回的配置，临时应用后复测并恢复。默认不会使用 preset repair 作为 API 结果。

运行输出默认保存在本地 `perf-anomaly-demo/results/`；除本轮报告列出的 6 组结构化实验凭证外，完整运行目录不会被 Git 提交。

`tpcc_external_cases.py` 保留用于旧的受控实验，其中的 `repair` 字段是预设对照实验值，不能作为真实 GPT 推荐证据；需要真实 API 时使用上面的 `tpcc_api_recommendation_validation.py`。

## SysInsight 统一主流程

在已有 `case_result.json` 上运行统一编排入口：

```bash
python3 perf-anomaly-demo/sysinsight_pipeline.py \
  --case-result perf-anomaly-demo/results/<run>/<case>/case_result.json \
  --api-result perf-anomaly-demo/results/<run>/<case>/sysinsight_api/result.json \
  --max-candidates 5
```

入口会生成 Prometheus 采集记录、八段式 `sysinsight_input.json`、原始 SysInsight 检测/匹配状态、候选配置校验以及 `summary.json`/`summary.md`。设置 `SYSINSIGHT_GPT_API_KEY` 且不传 `--api-result` 时，会自动调用同一 GPT5.6-SOL API；显式增加 `--benchmark-candidates 1` 才会对候选执行真实 TPCC 复测。

只验证 Prometheus 到 SysInsight 的输入链路：

```bash
python3 perf-anomaly-demo/sysinsight_prometheus.py \
  --wait --output /tmp/sysinsight-prometheus-input.json
```

需要一次命令自动完成“压力负载→告警/perf/源码检测→GPT5.6-SOL 分析→候选配置实测调优”时，使用自动入口。它只要求选择一个已配置的 TPCC 场景；每个候选只在测试会话中生效，跑完后由现有临时配置生命周期清理，不留下持久配置：

```bash
python3 perf-anomaly-demo/sysinsight_auto.py \
  --only d01_work_mem_sort \
  --workload-module tpcc_external_cases \
  --benchmark-candidates 3
```

API key 只从环境变量读取，优先使用 `SYSINSIGHT_GPT_API_KEY`，也兼容 `SYSINSIGHT_API_KEY` 和 `OPENAI_API_KEY`；不会写入结果目录。输出目录中的 `auto_manifest.json` 汇总检测、分析和调优阶段，`pipeline/summary.json` 记录候选实测和最佳候选。

## SysInsight 系统调优在线联动

在线入口为 `perf-anomaly-demo/sysinsight_dream_bridge.py`（文件名保留兼容）：Prometheus firing 告警触发 SysInsight 观测/分析；同一后台循环持续记录 `pg_stat_statements` 时间统计和活动 SQL 样本，作为告警诊断证据。SysInsight 通过真实 perf 窗口、原始函数分析、源码关联、SHAP 和 LLM 生成 PostgreSQL 系统参数候选，经静态/live 校验后临时应用、复测并恢复。

```bash
python3 perf-anomaly-demo/sysinsight_dream_bridge.py \
  --db keeninsight --db-schema tpcds \
  --state-db /tmp/sysinsight-bridge.sqlite3 \
  --output /tmp/sysinsight-bridge
```

API key 只从环境变量读取，优先使用 `SYSINSIGHT_GPT_API_KEY`，也兼容 `SYSINSIGHT_API_KEY` 和 `OPENAI_API_KEY`；不会写入结果目录。告警时间线、最近采集状态和系统参数调优证据可在 `/ui` 查看；Grafana 联动大屏展示告警、采集和实验趋势。

这里的统计是 `pg_stat_statements` 的累计值与轮询间隔增量；要获得每次调用的精确耗时，还需同时打开应用侧 tracing 或 PostgreSQL `log_min_duration_statement` 日志采集。

## 静态检查

```bash
./.venv/bin/python -m py_compile \
  perf-anomaly-demo/*.py \
  perf-anomaly-demo/db_profiles/*.py \
  sysinsight-tuning-demo/replay_onestep.py
bash -n monitoring/start.sh monitoring/status.sh monitoring/stop.sh
```

本仓库当前是本机 Demo 的源码快照；自动化调优会影响测试数据库的会话级配置和负载，运行前应确认目标数据库与测试窗口。
