# KeenInsight

**可追溯的 PostgreSQL 性能诊断、调优实验与结果展示。**

KeenInsight 将告警、数据库运行证据、SysInsight 参数建议、DREAM 慢 SQL
优化与统一结果页连接起来。保留每次任务的来源、阶段、候选、实际执行、
应用状态及恢复记录，让“后台做了什么”可查询，而不是只展示一个完成标记。

本分支是整理后的**纯源码发布版**：不含历史实验记录、数据库数据、API
密钥、模型权重、虚拟环境、监控发行包、截图或演示 PPT。

English: [README.en.md](README.en.md)

## 两条工作流

| 工作流 | 触发与目标 | 主要结果 |
|---|---|---|
| SysInsight | Prometheus 告警 → perf/调用链/源码关联 → 原始 SysInsight/LLAMBO 分析 → PostgreSQL 参数候选 | 阶段证据、候选校验、临时应用/复测/恢复；实验控制台展示业务 TPS |
| DREAM | 执行完成、达到阈值的可回放只读 AP SQL → 后台异步诊断与动作评估 | 优化前后 SQL 耗时、语义检查、候选/发布状态与后续实际命中 |

前端包含主机、告警、SQL、详情、参数手册、源码关联和调优知识页。SQL 级
CPU 火焰图只显示真实采样；SHAP 是可选的本地模型解释，不把 LLM 分数
或热点差分伪装为 SHAP。CPU/内存/TPS 时序继续由 Grafana 展示。

**重要边界：**当前 TP 控制台的候选回放与在线告警 LLM 输出仍不是同一次
TPS 闭环。开源版不携带旧候选，需显式提供候选文件才能做回放实验。
DREAM 的内部评估 SQL 隔离也存在已知缺口。详情见
[已知限制](docs/KNOWN_LIMITATIONS.md)，不要把研究演示当作生产无人值守承诺。

## 快速检查源码

使用 Python 3.10/3.11；3.10 的测试另需轻量 TOML 解析依赖，3.11 使用标准库。
Node.js 可额外检查前端语法。此步骤不启动服务、
不接数据库、不调用 LLM，也不制造压力：

```bash
git clone --branch opensource/clean-20261027 --single-branch \
  https://github.com/Avalephh/KeenInsight.git
cd KeenInsight
python3 -m pip install -r requirements-dev.txt
bash scripts/test.sh
```

依赖真实模型、生成参考资料的测试在资产缺失时明确跳过。核心存储、SQL
策略、前端契约、采样边界、保留策略和部署行为使用隔离的临时测试输入。

## 完整部署：分开准备，不隐式改数据库

已验证部署形态为 Linux/systemd、PostgreSQL 12、`pg_stat_statements`、
`pg_hint_plan`、`perf`、`pgbench`，以及两个独立 Python 环境。其他数据库
版本需要重新验证，不由参考 profile 文件的存在推断兼容。

1. 阅读 [部署手册](docs/DEPLOYMENT.md) 和 [安全说明](SECURITY.md)，准备专用
   PostgreSQL 实例及 TP/AP 测试数据。脚本**不会**创建、清空或导入数据库。
2. 获取锁定版本的外部源码、FlameGraph，生成参数/源码参考资料：

   ```bash
   bash scripts/fetch_keeninsight_sources.sh
   bash scripts/fetch_postgresql_source.sh
   python3 scripts/fetch_dream_source.py
   bash scripts/fetch_flamegraph.sh
   bash scripts/build_profiles.sh
   ```

   外部项目与模型的条款必须自行核实；
   [依赖清单](docs/DEPENDENCIES.md) 不将公开可下载等同于允许再分发。
3. 建立 SysInsight 和 DREAM 的独立环境，按文档另外取得真实模型/数据：

   ```bash
   PYTHON_BIN=python3.10 bash scripts/setup_python.sh
   DREAM_PYTHON_BIN=python3.10 bash scripts/setup_dream_python.sh
   python3 scripts/check_dream_assets.py
   ```

4. 准备私有配置，设置数据库、模型提供方、凭据与 Grafana 初始密码：

   ```bash
   cp config/env.example monitoring/.env.local
   chmod 600 monitoring/.env.local
   # 编辑 monitoring/.env.local；不要保留示例提供方或模型名。
   bash scripts/deploy.sh doctor
   ```

5. 前提检查通过后安装并启动：

   ```bash
   sudo bash scripts/deploy.sh install
   bash scripts/deploy.sh status
   ```

   `install` 会获取锁定的监控发行包、准备 postgres 可读的持久 worker
   runtime、安装并启用 systemd 服务；不会下载假模型代替缺失资产。

本地入口：

| 入口 | 默认地址 |
|---|---|
| 产品汇总页 | `http://127.0.0.1:9108/keeninsight/` |
| TP/AP 实验控制台 | `http://127.0.0.1:9108/lab` |
| 后台动作管理 | `http://127.0.0.1:9108/ui` |
| Grafana | `http://127.0.0.1:3000/` |
| Prometheus | `http://127.0.0.1:9090/` |

管理接口没有应用层鉴权，默认仅监听回环地址。外部访问需 SSH 隧道或受控
鉴权代理，不能将 9108 直接暴露到公网。不要在现有演示机上用此分支的
`install/start/stop` 覆盖正在运行的另一个 checkout。

## 目录

```text
app/             服务主链、SQL 策略、实验控制台与静态前端
monitoring/      监控配置、Grafana dashboard、持久 runtime 和服务管理
config/          无凭据的配置模板
integrations/    外部源码版本、模型哈希清单与兼容补丁
scripts/         获取依赖、构建参考资料、测试及发布检查
tests/           单元/契约测试与可选真实浏览器回归
docs/            架构、部署、配置、依赖、限制与逐文件用途清单
```

`dream/`、`third_party/`、`app/vendor/` 和 `app/db_profiles/postgresql/common/`
只在显式准备依赖后生成，均不进入源码发行。新的业务记录保留 24 小时；
有效方案所需证据单独保护，具体边界见 [架构](docs/ARCHITECTURE.md)。

## 文档

- [部署与故障排查](docs/DEPLOYMENT.md)
- [架构、数据流和证据](docs/ARCHITECTURE.md)
- [配置与实验约定](docs/CONFIGURATION.md)
- [页面与 HTTP 接口](docs/API.md)
- [依赖、来源、模型与许可证边界](docs/DEPENDENCIES.md)
- [已知限制与验证范围](docs/KNOWN_LIMITATIONS.md)
- [逐文件用途清单](docs/FILE_INVENTORY.md)
- [本次源码发布核验](docs/RELEASE_VALIDATION.md)
- [贡献说明](CONTRIBUTING.md) · [安全](SECURITY.md) · [许可证](LICENSE)

原始集成代码采用 MIT；外部依赖及补丁对应的上游代码不被本项目重新授权，
见 [NOTICE](NOTICE)。本项目不捆绑、发布或承诺任何固定性能提升。
