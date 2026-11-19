# KeenInsight 服务恢复与跨机器部署

统一入口：在项目根目录执行 `bash scripts/deploy.sh <命令>`。
默认保持本机环回监听，不主动开放数据库、监控或管理端口到公网。

## 当前机器：日常操作

```bash
cd /root/new
bash scripts/deploy.sh status
bash scripts/deploy.sh start
bash scripts/deploy.sh restart
bash scripts/deploy.sh stop
```

已安装的开机服务名是 **keeninsight-demo.service**，包含 Prometheus、node_exporter、
postgres_exporter、Grafana 和 SysInsight/DREAM Bridge。不替换已有端口 8000 的
`keeninsight.service`，也不停止或重启 PostgreSQL。

```bash
systemctl is-enabled keeninsight-demo.service
systemctl status keeninsight-demo.service
journalctl -u keeninsight-demo.service -n 100 --no-pager
```

开机服务在 PostgreSQL 与网络启动后运行。前台 supervisor 检查各进程出生标识和 HTTP
健康；连续三轮失败会退出，由 systemd 重启整套 demo，10 分钟内最多启动三次，
避免错误配置导致无限重试。PID 文件不匹配身份时绝不按旧 PID 杀进程。

`stop` 停止本次服务进程，开机启用状态保留；如需取消开机启动，应明确执行：

```bash
systemctl disable --now keeninsight-demo.service
```

## 新机器：需要先具备什么

目前支持与本机相同的 **Linux x86_64 + systemd + PostgreSQL 12** 演示环境。
其他 PG 版本、ARM、Docker/Kubernetes 不声称已验证，需要单独适配。
脚本的服务安装、peer 用户切换与资源护栏需要 root。

1. 取得**包含本次改动**的项目源码。当前运行目录、DREAM 上游快照和数据库都不在主仓库 Git
   中，不能仅 `git clone` 后就认为已有数据和模型。
2. 准备 PG12、`psql`、`pgbench`、匹配内核的 `perf`、`curl`、`perl`、Python venv/pip、
   `runuser`、systemd。按目标系统安装；脚本不修改 apt 源或自动替换数据库版本。
3. 准备数据库 `keeninsight`，TPC-C 的 `keeninsight_tpcc` 和 TPC-DS 的 `tpcds` 数据。
   安装并加载 `pg_stat_statements`、`pg_hint_plan`；数据可以通过已有备份迁移。
   `doctor` 只读检查扩展和表存在，不自动建库、导入、覆盖或执行 DDL。
4. 准备原始 SysInsight 源码和对应 PG 源码：

   ```bash
   bash scripts/fetch_keeninsight_sources.sh
   bash scripts/fetch_postgresql_source.sh
   ```

   原有目录含定制补丁时应迁移已验证快照，而不是用上游覆盖。也可配置
   `SYSINSIGHT_SOURCE_ROOT`、`POSTGRES_SOURCE_ROOT` 指向已准备目录。
5. 迁移当前**包含集成修复的** `dream/` 源码、配置及真实资产，至少包括：

   - `dream/dream/agent/plan/model_res/.../best_model.pt`（当前约 893 MiB）；
   - `dream/dream/agent/plan/slow_sql_data_gen.csv`；
   - `dream/dream/agent/plan/RCRank/bert-base-uncased/`；
   - `dream/dream/agent/memory/model/soft_q_retriever.pt`；
   - RCRank 模型代码、`pretrain/*.py`、规则库与适配器依赖的包代码。

   不使用 smoke 随机权重替代。跨机器不要直接依赖旧 venv 中指向 `/root/new` 的绝对
   symlink，应在目标机器重建两个独立 Python 环境：

   ```bash
   PYTHON_BIN=python3.10 bash scripts/deploy.sh install-python
   DREAM_PYTHON_BIN=python3.10 bash scripts/deploy.sh install-dream-python
   ```

   SysInsight 保持旧版 API 依赖，DREAM 使用独立新版依赖，不能装进同一个 venv。
   `requirements-dream.lock.txt` 为本机工作环境的精确依赖快照；新系统仍需验证 wheel、
   Python、glibc 可用性，不保证无网络离线安装。没有执行过目标机器安装时不能宣称迁移已验收。
6. 从 `monitoring/env.example` 建立本机私有 `monitoring/.env.local`，权限设为 600：

   ```bash
   cp monitoring/env.example monitoring/.env.local
   chmod 600 monitoring/.env.local
   ```

   配置数据库连接、PG service/版本、CPU/内存上限；默认内存取物理内存 80% 与 24GiB 的较小值，
   默认 CPU 取逻辑核数的 70% 与 250% 的较小值，仍应按机器上的其他业务调整。
   API 可配置 `SYSINSIGHT_GPT_API_KEY`、`SYSINSIGHT_GPT_BASE_URL`、
   `SYSINSIGHT_GPT_MODEL`，或设 `SYSINSIGHT_CODEX_CONFIG_DIR` 读取已有配置和 auth。
   不在脚本、unit、模型配置、Git 或日志里保存 API Key；不迁移整份私人账户目录。

配置齐全后：

```bash
bash scripts/deploy.sh doctor
bash scripts/deploy.sh install
bash scripts/deploy.sh status
```

`install`：检查前提 → 缺少时安装 `versions.lock` 固定版本的监控组件并校验 SHA256 →
准备持久 DREAM runtime → 安装并启用 systemd 服务 → 启动与健康检查。
它不自动下载第三方训练权重、不安装 PG、不导入测试数据，也不重置现有优化记录。
已有监控二进制时不重复下载；已有 runtime 增量同步，不重新训练模型。

若要单独执行各步骤：

```bash
bash scripts/deploy.sh install-components
bash scripts/deploy.sh prepare
bash scripts/deploy.sh enable
bash scripts/deploy.sh start
```

非默认解释器可设 `KEENINSIGHT_DEPLOY_PYTHON`；Bridge 默认优先项目 `.venv/bin/python`，
也可设 `SYSINSIGHT_BRIDGE_PYTHON`。DREAM 源 venv 可通过 `DREAM_SOURCE_VENV` 指定。
仅部署监控而不运行 demo 的既有用法仍可用 `SYSINSIGHT_BRIDGE_ENABLED=0 bash monitoring/start.sh`。

## 重启后不再依赖 /tmp

- DREAM 可执行代码、模型与 Python：`/var/lib/keeninsight/dream-runtime`；
- DREAM 运行时 memory/cache：该目录的 `state/`，仅 postgres 用户可写；
- exporter 程序与查询配置：`/var/lib/keeninsight/exporter`；
- PG 的 CPU/内存/任务数护栏：开机启动脚本重新应用，无需重启 PG；
- 已有 SQL/告警/优化记录和 Grafana/Prometheus 数据仍在 `monitoring/data/`，
  不因服务恢复而清空，既有 24H 清理策略继续执行。

runtime 仅对带有本工具标识的目录做增量同步，拒绝覆盖其他非空目录。代码、权重与依赖复制，
不采用共享 inode，也不放开 `/root` 权限。源文件中的 `api_key` 不写入生成配置。

验证实际 postgres 用户能加载原始模型（不调用 LLM、不执行 SQL）：

```bash
python3 monitoring/prepare_runtime.py --load-model
```

## 服务恢复后的验收

`status` 同时检查实际进程身份、Grafana DB、HTTP、exporter `pg_up=1`；
进程存在但连不到 PG 也判为失败。它**不等于**两类调优收益验收。

- 产品页：`http://127.0.0.1:9108/keeninsight/`
- 实验控制台：`http://127.0.0.1:9108/lab`
- Grafana：`http://127.0.0.1:3000/`
- Prometheus：`http://127.0.0.1:9090/`

服务健康后，性能验收仍按两部分：SysInsight 同压力下业务 TPS 前后对比；
DREAM 完成的长 SQL 超阈值触发、下一次执行应用和耗时对比。不要把健康检查或模型加载通过
当成已经重新验证六组场景的收益。

外部访问建议使用 SSH 隧道或已有鉴权反向代理，不直接公开当前管理 API。Grafana 默认初始
账户只适用于首次安装，已有数据库保留原密码；脚本不会重置。

## 本机恢复验证（2026-10-25）

- 开机服务已 enabled 且 active；受控重启、重复 start 后健康检查通过；
- Prometheus 的 node、postgres、prometheus、bridge 四个目标均 up；
- Grafana 的监控、自动联动和实验控制台三个 dashboard 可通过 API 查询；
- 产品页、实验控制台、主机数据与已加载告警规则 HTTP 检查通过；
- PostgreSQL 未重启；实际 cgroup CPU 配额 250%、内存 25769803776 字节（24GiB）、TasksMax 512；
- 真实 postgres 用户导入 DREAM 并加载原 RCRank/BERT 模型通过；
- 共享 API `/models` 返回 200，包含配置的 `gpt-6.1-sol`；不冒充已运行新一轮 LLM 调优；
- 部署测试 10 项、原有 Python 回归 76 项通过；这不是新机器实装或重新跑六组性能收益的证明。
