# 部署、启动与故障排查

新的联网/离线依赖缓存、pg_hint_plan 编译、TP 固定种子初始化和 TPC-DS
生成/导入入口见 [REPRODUCING.md](REPRODUCING.md)。它们都需要显式执行；
部署服务本身仍不暗自导入或清空数据库。

先读 [安全边界](../SECURITY.md)。所有示例仅针对专用测试数据库；管理接口
无应用层认证，不能直接公网开放。不要在当前演示机上用新 checkout 的同名
systemd 服务或端口替换正在运行的服务。

## 1. 系统前提

参考形态：Linux x86_64、systemd、PostgreSQL 12、`psql`、`pgbench`、
`perf`、`curl`、`git`、`perl`、`runuser`，Python 3.10/3.11。
监控发行包由 `monitoring/versions.lock` 锁定，此分支不重新定义系统 PG
安装方式或承诺所有发行版兼容。

需自行准备：

- 专用 PostgreSQL 实例与访问授权；源码声明的端口/用户不是授权配置。
- `shared_preload_libraries` 中的 `pg_stat_statements`，相应数据库扩展。
  preload 修改通常需要数据库重启，应由操作者计划执行。
- **与 PostgreSQL 主版本匹配**的 pg_hint_plan。参考上游
  `https://github.com/ossc-db/pg_hint_plan` 的版本分支与编译说明，
  不把跨版本 `.so` 复制到运行库。
- 可访问后端 perf 的权限，以及需要时 PostgreSQL 对应符号/source。
- TP 的 `keeninsight_tpcc` schema 和事务表、AP 的 `tpcds` schema 和数据。
  可按复现手册执行 `scripts/prepare_workloads.py` 初始化新实验库；
  TP 是确定性合成 fixture，不是官方完整 TPC-C 数据生成器。

在目标库只读确认：

```bash
psql -d keeninsight -c 'SHOW server_version;'
psql -d keeninsight -c 'SELECT extname FROM pg_extension;'
psql -d keeninsight -c '\dt keeninsight_tpcc.*'
psql -d keeninsight -c '\dt tpcds.*'
```

若使用 root + Unix socket peer，worker 默认 OS 用户为 postgres。
其他认证方式需自行配置，不能把密码放在命令行或 Git。安装入口不导入/
重置数据库，不替你授予超级用户权限。

## 2. 准备源代码与参考资料

在仓库根目录：

```bash
bash scripts/fetch_keeninsight_sources.sh
bash scripts/fetch_postgresql_source.sh
python3 scripts/fetch_dream_source.py
bash scripts/fetch_flamegraph.sh
bash scripts/build_profiles.sh
```

执行前审阅 [外部依赖与条款](DEPENDENCIES.md)。获取器拒绝覆盖不符合标识/
版本的非空目录；版本变化要修改 lock，不能悄悄改为最新 HEAD。
网络失败后可能留下未完成 checkout，先检查再移走该目录，不要让脚本
自动删除未知目录。
若网络的 GitHub HTTPS 传输不稳定且操作者已有 SSH 访问权限，
可显式改用 `python3 scripts/fetch_dream_source.py --transport ssh`；
这不会改变精确 commit 或复制任何 SSH key。

生成的 profile 包含参数描述、约束、默认值、源码关联和 provenance。
默认构建**不读取本机 PG 配置**；仅在明确需要时传
`--capture-live-settings`。生成文件和外部 checkout 都不属于源码分支。

## 3. 两个独立 Python 环境

```bash
PYTHON_BIN=python3.10 bash scripts/setup_python.sh
DREAM_PYTHON_BIN=python3.10 bash scripts/setup_dream_python.sh
```

SysInsight 的旧 API/ConfigSpace 依赖与 DREAM 的新版依赖不能混装。
`requirements-dream.lock.txt` 是已用环境的精确快照，不代表所有平台均有
wheel，也没有在本次发布过程中重装整套依赖。遇到包解析/平台问题应明确
失败，不自动换版本或用空模型绕过。

## 4. 单独获取可信模型

查看 `integrations/dream/assets.json` 和 [依赖说明](DEPENDENCIES.md)。
从获准来源取得 RCRank checkpoint、训练 CSV、BERT tokenizer/权重、
Soft Q Retriever checkpoint，放在外部 `dream/` checkout 对应位置。
没有使用 bootstrap smoke 脚本或随机权重。

```bash
python3 scripts/check_dream_assets.py
```

manifest 只包含路径/字节数/SHA256，不包含训练行、权重或账号。
哈希验证只验证完整性，不证明 pickle 内容可信，也不产生再分发授权。

## 5. 私有配置

```bash
cp config/env.example monitoring/.env.local
chmod 600 monitoring/.env.local
```

编辑数据库、provider/model、API key 与强 Grafana 初始密码；不要使用示例
`api.example.com` 或 `your-model-name`。配置来源说明见
[CONFIGURATION.md](CONFIGURATION.md)。

默认 runtime 为 `/var/lib/keeninsight/dream-runtime`。它必须与源目录分离、
且 postgres 能遍历。工具只增量更新带有自身标识的 runtime，拒绝覆盖其他
非空目录；不会放开 `/root` 的权限。

默认资源限制取决于主机。配置文件样例的 250% CPU/24G 仅是示例，应删除
覆盖值以采用按主机缩放的默认值，或按自己的内存/CPU配置。
systemd 限制影响整个 PG service，先确认没有其他业务共用。

## 6. 前提检查与启动

```bash
bash scripts/deploy.sh doctor
sudo bash scripts/deploy.sh install
bash scripts/deploy.sh status
```

`doctor` 只验证前提；失败应按提示补齐。`install` 会获取锁定监控二进制、
准备持久 worker runtime、生成并启用 `keeninsight-demo.service`，然后启动。
它不创建数据库、不下载业务数据、不执行调优场景。

单独操作：

```bash
bash scripts/deploy.sh install-components
bash scripts/deploy.sh prepare
bash scripts/deploy.sh enable
bash scripts/deploy.sh start
bash scripts/deploy.sh restart
bash scripts/deploy.sh stop
```

服务名与旧端口 8000 的 `keeninsight.service` 不同；同一台机器上本项目的
两个 checkout 仍会争用默认 service/端口/runtime，不支持同时盲目 install。

## 7. 启动后验证

```bash
curl -fsS http://127.0.0.1:9108/healthz
curl -fsS http://127.0.0.1:3000/api/health
bash scripts/deploy.sh status
```

入口见 README。可选浏览器检查：

```bash
npm ci
npx playwright install --with-deps chromium
KEENINSIGHT_TEST_URL=http://127.0.0.1:9108 npm run test:browser
```

浏览器回归需要已有真实记录；空实例应先在实验窗口运行自己的场景，
不要向库中插入假业务记录来满足断言。此命令包含错误响应模拟，但不发起
TP 压力或 LLM 诊断。

## 排错

| 问题 | 检查方法 |
|---|---|
| 参考页为空 | 是否执行 build_profiles、是否安装外部 DREAM 规则；参考资料不是示例数据 |
| 模型不可用 | assets 验证、真实文件来源、worker 权限；不要调用随机模型 bootstrap |
| API 失败 | 私有 env 是否加载、base/model 是否匹配、额度/网络/超时；日志不可打印 key |
| pg_hint_plan 不可用 | PG 主版本/扩展安装、hint table、新连接 preload 状态 |
| 页面断连 | `journalctl -u keeninsight-demo.service`；健康检查流式找 pg_up，不截断大响应 |
| 历史少了 | 默认 24 小时清理；active 方案证据可保留并标注 history_expired |
| 采样为空 | perf 权限、采样窗口、符号、SQL 太短；空不等于 CPU 成本 0 |
| TP 无候选 | 没有打包旧实验文件；显式提供候选只会执行被标注的回放 |
| TPS 看似提升 | 核对真实外压、基线稳定性、候选来源、重复轮数，不能只看正百分比 |

日志、数据库和实验输出只属于本机，不应附到公共 issue。脱敏后报告必要
版本/错误与阶段即可。
