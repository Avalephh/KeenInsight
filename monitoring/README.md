# 本机 Prometheus/Grafana 监控 Demo

## 重启恢复与新机器部署

统一入口：`bash scripts/deploy.sh install|start|restart|stop|status|doctor`（从项目根目录执行）。
部署后使用独立开机服务 `keeninsight-demo.service`，不替换已有端口 8000 的服务。
Bridge 状态与 exporter 已改为持久目录，不依赖重启后会消失的 `/tmp`。
完整的依赖、模型/数据库迁移边界与验收步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

本目录负责本机 PostgreSQL 与主机指标的采集和展示，不改动旧项目，不创建数据库角色。常规监控启动不修改 PostgreSQL 配置；实验控制台的 TPCC 调优阶段会通过 TemporaryPostgresConfiguration 临时应用候选并在结束后恢复。发行包、二进制和运行数据不纳入 Git；启动脚本支持通过环境变量指定安装路径。

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
- SysInsight 系统调优联动大屏：<http://127.0.0.1:3000/d/sysinsight-dream-automation?orgId=1&refresh=15s>
- SysInsight 系统调优实验看板：<http://127.0.0.1:3000/d/sysinsight-experiment-lab?orgId=1&refresh=5s>
- 联动管理控制台：<http://127.0.0.1:9108/ui>
- 一页实验控制台：<http://127.0.0.1:9108/lab>
- KeenInsight 动态产品页（由 English.zip 页面接入真实结果）：<http://127.0.0.1:9108/keeninsight/>
- Prometheus：<http://127.0.0.1:9090/>
- Grafana 初始登录：`admin` / `admin`

Grafana 只监听本机回环地址，适合首期本机 Demo。Dashboard 由 `dashboards/` 自动导入，数据源由 `config/grafana/provisioning/` 自动配置。
主机 PostgreSQL 监控大屏顶部现在用折线图展示全部 27 个 TPCC 场景的目标业务事务 TPS；当前运行场景会连续显示基线、压力前和调优后压力三个阶段，其余场景固定为 0，默认保留最近 6 小时历史曲线。详细的外部压力强度、基线/压力前后和调优对比仍在实验看板中查看。这些指标来自实验控制台的真实 TPCC control worker，不是数据库全局事务速率；没有运行场景时当前值全部为 0，但已采集的历史曲线仍可回看。

## SysInsight 系统调优联动

KeenInsight 英文页面与实验控制台共用同一个 Bridge/SQLite，无需另起 Web 服务。
保留的主机、告警、规则、参数手册和源码页面数据来自 `/api/v1/keeninsight/`，支持分页、搜索、
10 秒自动刷新、手动刷新、JSON 导出和详情跳转；实时监控统一由现有 Grafana 展示。
系统调优的告警证据写入 `keeninsight_evidence`，与基础记录一起按 24 小时清理，
运行中的告警证据保留。未采集的 SHAP 等字段显示缺失原因，不用示例数据填充。

`start.sh` 会同时启动 `perf-anomaly-demo/sysinsight_dream_bridge.py`（文件名保留兼容），默认监听本机 `9108` 端口。它向 Prometheus 暴露 `sysinsight_dream_bridge_*` 指标，并向管理控制台提供告警动作时间线和采集状态。桥接状态持久化在 `monitoring/data/sysinsight_dream_bridge.sqlite3`，输出凭证在 `monitoring/data/sysinsight_dream_bridge/`。启动时默认给 `postgresql@12-main.service` 加运行时资源护栏：CPU 250%、内存 24GiB、任务数 512；可通过 `SYSINSIGHT_PG_*` 调整，设置 `SYSINSIGHT_PG_RESOURCE_GUARD=0` 才会关闭。

联动大屏适合查看告警、采集和实验趋势；要执行“立即检测”或查看动作时间线，点击面板链接进入 `http://127.0.0.1:9108/ui`。演示默认每 5 秒采集最多 300 条 SQL、100 条活动 SQL，作为告警诊断证据；Prometheus 告警 firing 负责启动 SysInsight 系统调优分析，通过真实 perf 窗口、函数分析、源码关联和 LLM 生成 PostgreSQL 系统参数候选，经校验、临时应用、复测和恢复后记录证据。桥接状态、告警/实验记录、SQL 样本、运行归档和监控日志统一保留最近 24 小时，默认每 15 分钟清理一次；日志按小时压缩轮转，运行中的任务保留。Prometheus 自身同样配置为 24 小时 TSDB 保留。

实验看板的“打开实验控制台”链接进入 `http://127.0.0.1:9108/lab`。这一页集中提供数据库参数重置、6 组当前链路已实测有效的 TPCC 外部压力场景选择和“基线→压力调优前→压力调优后”控制；当前关注场景为热点 Payment、热点 Stock Level、Stock Level 突发、五类 TPCC 高强度混合、WAL/checkpoint 写入压力和热点 Order Status。低收益或压力不可比的 Payment 中等突发、Delivery 突发、混合读、普通 Order Status 和高强度 Payment 仍保留在完整场景目录中，但不再作为主要演示入口。默认基线为 60 秒，压力总观察为 180 秒并在调优前后各观察 90 秒。Prometheus/Grafana 同时记录目标业务 TPS、压力注入吞吐、连接、告警、压力下调优前后 TPS 及实际 SQL 耗时。目标业务 TPS 是 `tp_normal.sql` 控制负载的 TPS，结果中的稳态值剔除启动升温窗口后取中位数；实验控制台现在对正常控制负载和外部压力都使用有上限的固定 pgbench 速率，默认约为 1600 TPS 和最高 4000 TPS，避免压测把整机 CPU 打满。调优候选仍会应用安全的 session/连接级 planner 参数，`synchronous_commit=off` 也只通过 PGOPTIONS 进入调优控制连接；但会审计并跳过 `shared_buffers`、`work_mem`、`maintenance_work_mem`、并行 worker、`max_connections`、`fsync`、`max_wal_size` 等会重启集群、占满内存或改变全局持久性语义的危险项，因此调优期间不会为实验重启 PostgreSQL。压力注入吞吐只用于验证施压强度；调优候选的 session 参数在连接建立时通过 `PGOPTIONS` 注入，不会被写入 SQL 文件后按事务重复执行，外部压力连接保持原始负载配方；调优提升指标只有在压力仍施加时才有意义。

大屏中的“LLM API”卡片会直接反映 bridge 进程是否拿到共享的
`SYSINSIGHT_GPT_API_KEY`（也兼容 `SYSINSIGHT_API_KEY`、`OPENAI_API_KEY`）。
如果显示“未配置”，可以在 `monitoring/.env.local`（该文件已被 git 忽略，建议权限为
`600`）中设置 key、`SYSINSIGHT_GPT_BASE_URL` 和 `SYSINSIGHT_GPT_MODEL`，然后重启
bridge；也可以在启动 `monitoring/start.sh` 的同一个 shell 中导出这些变量。key 不会写入
SQLite、日志或 Grafana。

本机当前通过 `monitoring/.env.local` 中的
`SYSINSIGHT_CODEX_CONFIG_DIR=/root/.codex` 复用当前 Codex provider、model 和 API-key 认证。
`start.sh` 使用 `codex_api_launcher.py` 启动 Bridge，每次启动重新读取 `config.toml` 和
`auth.json`；不会复制 Key 到仓库、参数文件或命令行。当前 provider 已实测支持应用原有的
Chat Completions 请求，因此不需要更改调优协议。
可执行 `python3 monitoring/codex_api_launcher.py --config-dir /root/.codex --check`
检查非敏感配置；Python 3.11 之前需要 `tomli`。

当 API key 不可用时，bridge 仍会采集告警和观测证据，但不会执行 LLM 候选生成；
配置 key 后重启 bridge 即可恢复系统调优分析。

## 已验证组件

- Grafana OSS 13.2.1 官方发行包。
- Prometheus 2.15.2、node_exporter 0.18.1、postgres_exporter 0.8.0，均为开源官方发行物。
- Prometheus 的 `prometheus`、`node`、`postgres` 三个采集目标均已验证为 `up`。
