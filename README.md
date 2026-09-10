# 数据库性能监控与 SysInsight Demo

这个 Git 工作区保存当前 Demo 的可复现源码和配置，包含：

- `monitoring/`：原生 Prometheus、node_exporter、postgres_exporter 和 Grafana OSS 的本机监控配置与启动脚本；
- `perf-anomaly-demo/`：TPCC 外部压力、Prometheus 告警触发 perf、原始 SysInsight 异常提取以及真实 GPT API 配置验证；
- `sysinsight-tuning-demo/`：与数据库隔离的 SysInsight 单步回放；
- `监控产品文档.md`、`产品性能展示.pptx`：当前产品文档和展示材料。

## Git 内容边界

Git 只提交源码、SQL、配置、输入样例、profile 和文档。Prometheus/Grafana 数据目录、运行日志、perf 二进制结果、原始 API 运行结果、下载包以及本地上游仓库快照均被 `.gitignore` 排除，避免把 3GB 级运行状态或密钥带入仓库。

SysInsight 使用的原始开源源码不是本项目重写的实现。复现所需的源码快照和 PostgreSQL 12.22 源码树见 [EXTERNAL_SOURCES.md](EXTERNAL_SOURCES.md)。API key 只通过环境变量传入，不写入文件或结果。

## 环境要求

- Linux；Python 3.8+；
- PostgreSQL 12.x，数据库 `keeninsight` 中存在 `keeninsight_tpcc` schema；
- `psql`、`pgbench`、`perf`、`curl`、`perl`；
- Prometheus、node_exporter、postgres_exporter 和 Grafana OSS。当前主机已有的发行包路径可通过 `monitoring/env.example` 中的变量指定；这些大文件不进 Git；
- 调用真实 SysInsight API 时，需要原始 WorkloadTune 源码快照和 Python 依赖。

安装 Python 依赖：

```bash
python3 -m pip install -r requirements-sysinsight.txt
python3 -m pip install -r requirements-ppt.txt  # 仅生成 PPT 时需要
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

## 运行真实 API 的 TPCC 验证

先准备环境变量，不要把 key 写入 Git：

```bash
export SYSINSIGHT_GPT_API_KEY='<your-api-key>'
export SYSINSIGHT_SOURCE_ROOT="$PWD/repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune"
export SYSINSIGHT_PYDEPS="$PWD/perf-anomaly-demo/.pydeps"
```

确保监控已启动后，使用真实 API 验证一个场景：

```bash
cd perf-anomaly-demo
python3 tpcc_api_recommendation_validation.py \
  --only d01_work_mem_sort \
  --api-base 'http://35.212.195.134:28317/v1' \
  --model 'GPT5.6-SOL' \
  --run-source-selector
```

这个入口会运行 TPCC 正常控制负载和外部压力，轮询 Prometheus firing 告警；告警触发后对当前 PostgreSQL backend 执行原生 `perf record`，再执行原始 SysInsight 文件分析和函数匹配，调用 API，严格解析 API 返回的配置，临时应用后复测并恢复。默认不会使用 preset repair 作为 API 结果。

输出只保存在本地 `perf-anomaly-demo/results/`，不会被 Git 提交。

`tpcc_external_cases.py` 保留用于旧的受控实验，其中的 `repair` 字段是预设对照实验值，不能作为真实 GPT 推荐证据；需要真实 API 时使用上面的 `tpcc_api_recommendation_validation.py`。

## 静态检查

```bash
python3 -m py_compile \
  perf-anomaly-demo/*.py \
  perf-anomaly-demo/db_profiles/*.py \
  sysinsight-tuning-demo/replay_onestep.py
bash -n monitoring/start.sh monitoring/status.sh monitoring/stop.sh
```

本仓库当前是本机 Demo 的源码快照；自动化调优会影响测试数据库的会话级配置和负载，运行前应确认目标数据库与测试窗口。
