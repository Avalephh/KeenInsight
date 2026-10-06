# 本机 Prometheus/Grafana 监控 Demo

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
- SysInsight / DREAM 联动大屏：<http://127.0.0.1:3000/d/sysinsight-dream-automation?orgId=1&refresh=15s>
- SysInsight / DREAM 实验看板：<http://127.0.0.1:3000/d/sysinsight-experiment-lab?orgId=1&refresh=5s>
- 联动管理控制台：<http://127.0.0.1:9108/ui>
- 一页实验控制台：<http://127.0.0.1:9108/lab>
- Prometheus：<http://127.0.0.1:9090/>
- Grafana 初始登录：`admin` / `admin`

Grafana 只监听本机回环地址，适合首期本机 Demo。Dashboard 由 `dashboards/` 自动导入，数据源由 `config/grafana/provisioning/` 自动配置。
主机 PostgreSQL 监控大屏顶部现在用折线图展示全部 27 个 TPCC 场景的目标业务事务 TPS；当前运行场景会连续显示基线、压力前和调优后压力三个阶段，其余场景固定为 0，默认保留最近 6 小时历史曲线。详细的外部压力强度、基线/压力前后和调优对比仍在实验看板中查看。这些指标来自实验控制台的真实 TPCC control worker，不是数据库全局事务速率；没有运行场景时当前值全部为 0，但已采集的历史曲线仍可回看。

## SysInsight / DREAM 联动

`start.sh` 会同时启动 `perf-anomaly-demo/sysinsight_dream_bridge.py`，默认监听本机 `9108` 端口。它向 Prometheus 暴露 `sysinsight_dream_bridge_*` 指标，并向管理控制台提供动作时间线、SQL 观测、DREAM 队列和 Hint 管理。桥接状态持久化在 `monitoring/data/sysinsight_dream_bridge.sqlite3`，输出凭证在 `monitoring/data/sysinsight_dream_bridge/`。启动时默认给 `postgresql@12-main.service` 加运行时资源护栏：CPU 250%、内存 24GiB、任务数 512；可通过 `SYSINSIGHT_PG_*` 调整，设置 `SYSINSIGHT_PG_RESOURCE_GUARD=0` 才会关闭。

联动大屏适合查看趋势和状态；要执行“立即检测、重试 DREAM、启用候选 Hint、回滚活动 Hint”等操作，点击面板链接进入 `http://127.0.0.1:9108/ui`。演示默认每 5 秒采集最多 300 条 SQL、100 条活动 SQL；只有已经结束且观测均值或最大执行时长达到 10 秒的长 AP SQL，才允许进入 DREAM 队列，仍在 `pg_stat_activity` 中运行的 SQL 只记录不调优。5 秒采样间隔用于确保 10 秒以上的长 SQL 能被采到活动态原文，避免 `pg_stat_statements` 完成后只剩参数化文本而无法重放。Prometheus 告警 firing 负责启动 SysInsight 联动；长 AP SQL 即使没有告警，也会在结束后的下一轮采集中异步进入 DREAM，短 SQL不会进入。桥接状态、已完成告警/任务/实验、SQL 样本、运行归档和监控日志统一保留最近 24 小时，默认每 15 分钟清理一次；日志按小时压缩轮转，运行中的任务和生效中的 Hint 不会被清理。未发生调用变化的 `pg_stat_statements` 行不会重复写时间序列，SQLite 使用增量空间回收，样本另有 50 万行安全上限。Prometheus 自身同样配置为 24 小时 TSDB 保留。

从旧版本升级、且状态库已经包含大量 SQLite 空闲页时，需要在停止监控服务后执行一次 `python3 monitoring/compact_bridge_state.py --state-db monitoring/data/sysinsight_dream_bridge.sqlite3 --replace`。新建状态库和完成过该迁移的状态库会由后台维护增量回收空间，不需要重复做整库压缩。

实验看板的“打开实验控制台”链接进入 `http://127.0.0.1:9108/lab`。这一页集中提供数据库参数重置、6 组当前链路已实测有效的 TPCC 外部压力场景选择和“基线→压力调优前→压力调优后”控制，以及 TPC-DS SQL 选择、DREAM 记录清空、原 SQL 分析和优化 SQL 再执行；当前关注场景为热点 Payment、热点 Stock Level、Stock Level 突发、五类 TPCC 高强度混合、WAL/checkpoint 写入压力和热点 Order Status。低收益或压力不可比的 Payment 中等突发、Delivery 突发、混合读、普通 Order Status 和高强度 Payment 仍保留在完整场景目录中，但不再作为主要演示入口。默认基线为 60 秒，压力总观察为 180 秒并在调优前后各观察 90 秒。Prometheus/Grafana 同时记录目标业务 TPS、压力注入吞吐、连接、告警、压力下调优前后 TPS 及实际 SQL 耗时。目标业务 TPS 是 `tp_normal.sql` 控制负载的 TPS，结果中的稳态值剔除启动升温窗口后取中位数；实验控制台现在对正常控制负载和外部压力都使用有上限的固定 pgbench 速率，默认约为 1600 TPS 和最高 4000 TPS，避免压测把整机 CPU 打满。调优候选仍会应用安全的 session/连接级 planner 参数，`synchronous_commit=off` 也只通过 PGOPTIONS 进入调优控制连接；但会审计并跳过 `shared_buffers`、`work_mem`、`maintenance_work_mem`、并行 worker、`max_connections`、`fsync`、`max_wal_size` 等会重启集群、占满内存或改变全局持久性语义的危险项，因此调优期间不会为实验重启 PostgreSQL。压力注入吞吐只用于验证施压强度；调优候选的 session 参数在连接建立时通过 `PGOPTIONS` 注入，不会被写入 SQL 文件后按事务重复执行，外部压力连接保持原始负载配方；调优提升指标只有在压力仍施加时才有意义。

大屏中的“LLM API”卡片会直接反映 bridge 进程是否拿到共享的
`SYSINSIGHT_GPT_API_KEY`（也兼容 `SYSINSIGHT_API_KEY`、`OPENAI_API_KEY`）。
如果显示“未配置”，可以在 `monitoring/.env.local`（该文件已被 git 忽略，建议权限为
`600`）中设置 key、`SYSINSIGHT_GPT_BASE_URL` 和 `SYSINSIGHT_GPT_MODEL`，然后重启
bridge；也可以在启动 `monitoring/start.sh` 的同一个 shell 中导出这些变量。key 不会写入
SQLite、日志或 Grafana。

当自动调优已开启但 API key 不可用时，bridge 会在“DREAM 调度闸门”中记录阻塞原因，
并跳过自动入队，不会按轮询周期重复制造 blocked 任务。历史 blocked 记录仍保留在审计表中；
配置 key 后重启 bridge，闸门恢复为 ready，新的慢 SQL 才会进入 DREAM。

## 已验证组件

- Grafana OSS 13.2.1 官方发行包。
- Prometheus 2.15.2、node_exporter 0.18.1、postgres_exporter 0.8.0，均为开源官方发行物。
- Prometheus 的 `prometheus`、`node`、`postgres` 三个采集目标均已验证为 `up`。
