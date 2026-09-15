# 本机 Prometheus/Grafana 监控 Demo

本目录只负责本机 PostgreSQL 与主机指标的采集和展示，不改动旧项目，不修改 PostgreSQL 配置，也不创建数据库角色。发行包、二进制和运行数据不纳入 Git；启动脚本支持通过环境变量指定安装路径。

## 依赖

需要 PostgreSQL exporter 能访问目标数据库，并准备原生 Prometheus、node_exporter、postgres_exporter 和 Grafana OSS。当前目录中的 `vendor/` 与 `grafana-13.2.1/` 是本机运行时安装目录，已被 Git 忽略。

```bash
./install_open_source.sh
cp env.example .env.local
# 如果组件安装到了非默认目录，在 .env.local 中取消注释并修改路径
source .env.local
```

安装脚本按 `versions.lock` 下载并校验 Prometheus 2.15.2、node_exporter 0.18.1、postgres_exporter 0.8.0 和 Grafana OSS 13.2.1。所有发行包和运行目录均被 Git 忽略。

## 启停

```bash
./start.sh
./status.sh
./stop.sh
```

## 访问

- Grafana：<http://127.0.0.1:3000/>
- 监控大屏：<http://127.0.0.1:3000/d/local-postgresql-demo/582e36f?orgId=1&refresh=15s>
- SysInsight / DREAM 联动大屏：<http://127.0.0.1:3000/d/sysinsight-dream-automation?orgId=1&refresh=15s>
- SysInsight / DREAM 实验看板：<http://127.0.0.1:3000/d/sysinsight-experiment-lab?orgId=1&refresh=5s>
- 联动管理控制台：<http://127.0.0.1:9108/ui>
- 一页实验控制台：<http://127.0.0.1:9108/lab>
- Prometheus：<http://127.0.0.1:9090/>
- Grafana 初始登录：`admin` / `admin`

Grafana 只监听本机回环地址，适合首期本机 Demo。Dashboard 由 `dashboards/` 自动导入，数据源由 `config/grafana/provisioning/` 自动配置。

## SysInsight / DREAM 联动

`start.sh` 会同时启动 `perf-anomaly-demo/sysinsight_dream_bridge.py`，默认监听本机 `9108` 端口。它向 Prometheus 暴露 `sysinsight_dream_bridge_*` 指标，并向管理控制台提供动作时间线、SQL 观测、DREAM 队列和 Hint 管理。桥接状态持久化在 `monitoring/data/sysinsight_dream_bridge.sqlite3`，输出凭证在 `monitoring/data/sysinsight_dream_bridge/`。

联动大屏适合查看趋势和状态；要执行“立即检测、重试 DREAM、启用候选 Hint、回滚活动 Hint”等操作，点击面板链接进入 `http://127.0.0.1:9108/ui`。默认启用慢 SQL 无告警入队，以便后台持续发现 OLAP SQL；如只希望告警触发后调优，可设置 `SYSINSIGHT_TUNE_WITHOUT_ALERT=0` 并在单独启动 bridge 时不传 `--tune-without-alert`。

实验看板的“打开实验控制台”链接进入 `http://127.0.0.1:9108/lab`。这一页集中提供数据库参数重置、6 组已验证 TPCC 外部压力场景选择和基线→持续压力调优观察控制，以及 TPC-DS SQL 选择、DREAM 记录清空、原 SQL 分析和优化 SQL 再执行；默认基线为 60 秒、持续压力观察为 180 秒。Prometheus/Grafana 同时记录目标业务 TPS、压力注入吞吐、连接、告警和实际 SQL 耗时。目标业务 TPS 是 `tp_normal.sql` 控制负载的 TPS，结果中的稳态值剔除启动升温窗口后取中位数，压力注入吞吐只用于说明施压强度。

大屏中的“LLM API”卡片会直接反映 bridge 进程是否拿到共享的
`SYSINSIGHT_GPT_API_KEY`（也兼容 `SYSINSIGHT_API_KEY`、`OPENAI_API_KEY`）。
如果显示“未配置”，请在启动 `monitoring/start.sh` 的同一个 shell 中设置 key、
`SYSINSIGHT_GPT_BASE_URL` 和 `SYSINSIGHT_GPT_MODEL`，然后重启 bridge；key 不会写入
SQLite、日志或 Grafana。

当自动调优已开启但 API key 不可用时，bridge 会在“DREAM 调度闸门”中记录阻塞原因，
并跳过自动入队，不会按轮询周期重复制造 blocked 任务。历史 blocked 记录仍保留在审计表中；
配置 key 后重启 bridge，闸门恢复为 ready，新的慢 SQL 才会进入 DREAM。

## 已验证组件

- Grafana OSS 13.2.1 官方发行包。
- Prometheus 2.15.2、node_exporter 0.18.1、postgres_exporter 0.8.0，均为开源官方发行物。
- Prometheus 的 `prometheus`、`node`、`postgres` 三个采集目标均已验证为 `up`。
