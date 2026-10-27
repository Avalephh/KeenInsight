# 从纯源码到真实演示：联网准备、离线搬运与数据重建

本手册的目标是让部署输入可获取、可校验、可重新生成，**不要求访问原演示
目录，也不携带旧成绩**。准备好依赖不等于全部业务缺口已修复：特别是 TP
新 LLM 候选与控制台 TPS 实验尚未统一，见末尾的验收边界。

## 1. 需要准备什么

| 输入 | 获取或重建方式 | 是否进入 Git |
|---|---|---|
| KeenInsight 集成源码与页面 | 本分支 | 是 |
| SysInsight、DREAM、RCRank、PG source、FlameGraph | 固定 commit/版本下载，DREAM/RCRank 应用分支内补丁 | 只保存来源/补丁 |
| pg_hint_plan | 固定 PG12/1.3.10 源码，针对目标 `pg_config` 编译 | 只保存来源 |
| TPC-DS generator | 固定 tpcds-kit commit，目标机编译 | 只保存来源 |
| 监控组件 | `monitoring/versions.lock` 固定包及 SHA256 | 否 |
| Python 依赖 | 联网同平台构建三个 wheelhouse，离线解析和安装 | 否 |
| DREAM 模型/训练输入 | 上游获准来源下载，按 8 项真实文件的大小/SHA256 校验 | 否 |
| TP 测试数据 | 本分支 SQL + 固定 seed 生成；默认 5 仓库 | 只保存生成代码 |
| AP 测试数据 | 固定 generator + scale/seed 生成，再 COPY 导入 | 只保存生成代码 |
| PostgreSQL/Python/编译器/系统库 | 目标发行版软件仓库或批准的离线系统介质 | 否 |
| DB 凭据、模型 API、Grafana 密码 | 操作者私下配置 | 绝不 |
| SHAP 本地训练模型、可选 QED | 额外准备；无资产时功能明确不可用/降级，不伪造 | 否 |

参考目标为 Linux x86_64、PostgreSQL 12、Python 3.10。不要把别的 CPU/
Python minor/glibc 平台上的 wheel 或 `.so` 直接搬过来。OS 包和 Python
解释器必须先就绪；依赖缓存不捆绑可搬移的虚拟环境。
标准库也要完整：`python3.10 -c 'import ssl,sqlite3,bz2,lzma,zlib,venv'`
必须成功。自编译 Python 缺 `_sqlite3`/SSL 时，pip 安装普通同名包不能补齐，
应安装发行版完整解释器或带相应开发库重新构建；安装脚本会提前拒绝。

## 2. 联网机器：创建依赖缓存

先审核 [NOTICE](../NOTICE) 和 [外部依赖授权](DEPENDENCIES.md)。下面的
确认选项只表示操作者已审核条款，不代替任何上游授权。使用与目标一致的
Linux/Python 3.10 构建机，在本分支根目录执行：

```bash
# 此目录可以在任意传输盘上，但不要提交到 Git。
python3 scripts/reproduction.py plan
python3 scripts/reproduction.py download \
  --directory /transfer/keeninsight-cache --accept-upstream-terms
```

`download` 只下载锁定源码压缩包和监控压缩包，不安装服务、不读 API key、
不连接数据库、不自动下载整个 Google Drive 文件夹。可用
`--group source` 或 `--group monitoring` 分开准备。

已下载且校验匹配的文件可复用。失败的 `.partial` 文件保留供检查；
先将它移出缓存再重试，脚本不会覆盖未知文件，也不关闭 TLS 验证。
GitHub source 以精确 commit 定位；下载得到的归档哈希写入本地 manifest。
PG/监控还有分支内预先固定的 archive SHA256。

如果下载必须在浏览器/代理机进行，可先按 `plan` 的 URL 取得原始归档，
再逐个导入；不需要改代码路径：

```bash
python3 scripts/reproduction.py import-archive \
  --directory /transfer/keeninsight-cache --name postgresql \
  --from-file /path/to/postgresql-12.22.tar.bz2
```

名称由 `plan` 列出。检查固定 archive hash（若锁中有）和源码根目录的
版本/commit 标识，再建立传输哈希；不会接受压缩后的旧运行目录替代依赖。

### 2.1 取得真实 DREAM 权重

上游给出的下载入口：

```text
https://drive.google.com/drive/folders/1mRkA_CJvKImJeHuaXm-W0h0riVmvB_2w
```

上面的 Drive 入口用于 RCRank checkpoint/CSV 与 Soft Q Retriever。
BERT 来自 RCRank README 指向的 `google-bert/bert-base-uncased` 官方仓库，
不是假定 Drive 包里已经包含它。从获准来源取得下表文件。
允许网页手动下载，或用你已授权的下载工具。
不要把 Google cookie、账号文件或任何 API key 放进依赖缓存。

| 文件 | 放置逻辑位置（相对于 DREAM checkout） |
|---|---|
| RCRank `best_model.pt` | `dream/agent/plan/model_res/GateComDiffPretrainModel slow_sql_data_gen eta0.07/best_model.pt` |
| `slow_sql_data_gen.csv` | `dream/agent/plan/slow_sql_data_gen.csv` |
| `soft_q_retriever.pt` | `dream/agent/memory/model/soft_q_retriever.pt` |
| BERT `config.json`、`model.safetensors`、`tokenizer.json`、`tokenizer_config.json`、`vocab.txt` | `dream/agent/plan/RCRank/bert-base-uncased/` |

BERT 可单独用此命令准备（不需要 Git LFS，也不克隆整个模型仓库）：

```bash
python3 scripts/reproduction.py download-bert \
  --output-dir /path/to/authorized-downloaded-files/bert-base-uncased \
  --accept-upstream-terms
```

下载的每个文件必须匹配分支内已固定 SHA256；URL 的 `main` 仅是发现入口，
不会因为上游返回 200 就接受不同内容。Hugging Face 不可访问时，在有网络
的机器下载同样的五个文件再搬运；不能拿 Git LFS 文本指针冒充权重。

清单的 8 个文件合计约 1.51 GB（十进制）。具体字节数和 SHA256 以
`integrations/dream/assets.json` 为准。源文件夹结构可以不同，以下工具先按
逻辑路径寻找，再按文件名、大小及完整 SHA256 匹配；只复制清单中的文件：

```bash
python3 scripts/reproduction.py add-models \
  --directory /transfer/keeninsight-cache \
  --from-dir /path/to/authorized-downloaded-files
```

缺失或不匹配就报错，不会生成随机模型。不相同的上游版本也不能通过
“忽略哈希”混用。哈希只保证与清单一致，不证明 pickle 可信或授予分发权；
未知来源模型不要加载。若上游链接不可访问，必须自行取得合法副本；代码
无法代替授权或恢复已失效的上游资源。

### 2.2 构建离线 Python 依赖

```bash
PYTHON_BIN=python3.10 bash scripts/prepare_wheelhouses.sh \
  /transfer/keeninsight-cache
python3 scripts/reproduction.py verify \
  --directory /transfer/keeninsight-cache --require-complete
```

脚本在独立临时构建环境中使用 pip wheel；需要的 sdist 会在联网构建机
编译成 wheel。SysInsight、DREAM、开发测试依赖分开存放；包含离线 pip。
构建结束会用 `--no-index --dry-run --ignore-installed` 验证三组解析，
记录 Python/架构/libc 信息并对每个 wheel 建哈希。

构建失败就没有“依赖已齐”的结论，不能只复制顶层 requirements 或
`site-packages`。先解决真实包版本/源/平台兼容性，再重新准备。
失败后保留已有 wheel 目录供检查；移走整个未完成的 `wheelhouses/` 后再执行，
避免把未封存的文件混入下一次构建。

### 2.3 搬运源码与依赖

将**同一次分支提交**的代码和缓存传到目标机。可以使用 Git clone，
也可在提交完成后的联网 checkout 中：

```bash
git archive --format=tar --output=/transfer/keeninsight-source.tar HEAD
```

通过 SSH/传输盘搬运 `keeninsight-source.tar` 和整个
`keeninsight-cache/`。缓存不含本机 API 配置、原任务数据库或 Git 身份。
建议额外通过可信通道传递源码包/manifest 的 SHA256；攻击者同时修改
manifest 和内容时，自带哈希不能证明发布者身份。第三方原始源码归档
可能含其上游自带的示例，不是本机历史实验数据，也不应用于伪造结果。

## 3. 离线机器：导入、校验、安装

先从可信源码包解出本分支到新的目录，如 `/opt/keeninsight`。不要覆盖
正在运行的旧部署，也不要将外来未知 tar 直接以 root 解到系统根目录。
进入新目录：

```bash
python3 scripts/reproduction.py verify \
  --directory /transfer/keeninsight-cache --require-complete
python3 scripts/reproduction.py install \
  --directory /transfer/keeninsight-cache
```

`install` 不发起网络请求。它先校验缓存，再展开源码、应用兼容补丁、复制
已校验的模型和监控归档。拒绝穿越路径、链接、特殊文件、过大的源码归档
和已有未标记的源目录。重复运行只接受自身标记的同一套版本。
不要在离线导入后再执行旧的联网 `fetch_*` 脚本。

缓存可分批使用：`verify` 不带 `--require-complete` 只验证已有资产，
**不表示全部依赖齐全**。`install --only tpcds-kit pg_hint_plan` 可仅展开
指定源码。缺其他部分就不能运行完整产品。

### 3.1 系统离线依赖

必须事先安装：

- PostgreSQL 12 服务、client、server development headers、`pg_stat_statements`；
- Python 3.10 及 venv；构建原生包时对应 Python headers；
- C/C++ 工具链、make、flex、bison、libpq development headers；
- systemd、perf（匹配目标内核）、git、curl、perl、psql、pgbench、runuser。

不要在不同 Ubuntu/Debian 版本之间搬 `.deb`。PostgreSQL 12 软件仓库需由
管理员配置受信任的、仍提供该版本的源；本项目不暗自加源/升级主库。
以已安装 apt-offline 且配置好源的 Debian/Ubuntu 为例，先在离线机获取
更新签名，联网机下载，离线机导入：

```bash
# 离线机
sudo apt-offline set /transfer/os-update.sig --update
# 联网机
apt-offline get /transfer/os-update.sig --bundle /transfer/os-update.zip
# 离线机
sudo apt-offline install /transfer/os-update.zip
```

然后按目标机实际包名做第二轮。例如发行版提供以下名称时：

```bash
# 离线机：基于它自己的 apt 状态生成签名，以包含依赖。
sudo apt-offline set /transfer/os-packages.sig --install-packages \
  postgresql-12 postgresql-client-12 postgresql-server-dev-12 \
  python3.10 python3.10-venv python3.10-dev build-essential flex bison \
  libpq-dev linux-tools-generic git curl perl
# 联网机
apt-offline get /transfer/os-packages.sig --bundle /transfer/os-packages.zip
# 离线机
sudo apt-offline install /transfer/os-packages.zip
sudo apt-get --no-download install \
  postgresql-12 postgresql-client-12 postgresql-server-dev-12 \
  python3.10 python3.10-venv python3.10-dev build-essential flex bison \
  libpq-dev linux-tools-generic git curl perl
```

包名和 perf 内核工具包必须由目标发行版确认；签名无法解析包时先修复源，
不把上述示例当成所有 OS 通用安装器。apt-offline 本身需从批准的系统
介质引导安装。这一步会改系统，由管理员执行，不由应用自动执行。

### 3.2 离线 Python 安装与解析

```bash
export KEENINSIGHT_WHEELHOUSE_ROOT=/transfer/keeninsight-cache/wheelhouses
PYTHON_BIN=python3.10 bash scripts/setup_python.sh
DREAM_PYTHON_BIN=python3.10 bash scripts/setup_dream_python.sh
python3 scripts/check_dream_assets.py
.venv/bin/python scripts/reproduction.py offline-check \
  --directory /transfer/keeninsight-cache --python .venv/bin/python
bash scripts/build_profiles.sh
```

设置 wheelhouse 后两份安装脚本只用 `--no-index`，缺包不回退联网。
`offline-check` 用**目标解释器**重新解析三组完整依赖，拒绝仅凭文件数
认定兼容。其通过不证明原生库实际可导入、模型推理或业务性能已通过。

### 3.3 编译 pg_hint_plan、启用采集

```bash
bash scripts/build_external_tools.sh pg-hint-plan
# 显式系统写入，先审阅将使用的 pg_config：
sudo make -C third_party/pg_hint_plan USE_PGXS=1 \
  PG_CONFIG=/usr/lib/postgresql/12/bin/pg_config install
```

检查目标实例已有 `shared_preload_libraries`，**保留现有扩展**，由管理员
加入 `pg_stat_statements,pg_hint_plan` 后安排重启。不要直接用一个 SET/
ALTER SYSTEM 抹掉原列表，也不要为了演示重启共享业务实例。
在新的实验数据库中显式执行：

```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pg_hint_plan;
SHOW shared_preload_libraries;
SELECT extname,extversion FROM pg_extension;
```

本锁定源码仅用于 PG12；其他主版本需重新选版本和验证。hint 实际生效、
hint table 配置和新连接行为还需要业务查询验证，扩展存在不等于 hint
已经命中。

## 4. 重建 TP/AP 数据，不复制旧数据库

以下写操作只用于**新建的专用实验库**。脚本默认 plan-only，必须同时
`--apply` 和正确的 `--confirm-database` 才写库；已有目标 schema 会报错，
没有 DROP/TRUNCATE/重置旧数据的选项。连接用 PGHOST/PGPORT/PGUSER/
安全的 `.pgpass`，不把密码写在命令行或 Git 中。

```bash
# 管理员先建库并授权；示例库名不是当前演示库。
createdb -U postgres keeninsight_repro
export PGUSER=postgres PGHOST=/var/run/postgresql PGPORT=5432
```

Unix peer 认证需使用有授权的 OS 用户；其他认证按目标实例规则设置。

### 4.1 TP：固定 seed、表、索引和初始数据

```bash
python3 scripts/prepare_workloads.py tp \
  --database keeninsight_repro --warehouses 5 --seed 42
python3 scripts/prepare_workloads.py tp \
  --database keeninsight_repro --confirm-database keeninsight_repro \
  --warehouses 5 --seed 42 --apply
```

生成 5 仓库、50 地区、15 万 customer、10 万 item、50 万 stock、
15 万 orders、150 万 order_line、4.5 万 new_order 和15万初始 history。
包括 workload 读取的字段、主键、索引及 `load_info` provenance。
创建/装载处于单个事务，失败回滚。不改变 PostgreSQL 参数。

这是适配现有 SQL 的**确定性合成工作负载 fixture**：每单固定 10 行，
不是官方完整 TPC-C 分布/认证成绩，也不是原演示机的数据逐行复制。
同 seed 可重建相同初始业务值，不保证跨机器、缓存/统计状态的 TPS 相同。
现有 workload 会引用仓库 1..5、地区 1..10、客户 1..3000、item 1..100000，
不能为了小测试把这些范围删小后继续声称在跑同一 workload。

### 4.2 AP：生成 TPC-DS SF10

本入口固定的是 tpcds-kit 2.10.0；旧演示机使用过另一个 generator 版本。
它提供可重复的数据重建路径，不声称与旧机数据逐行相同；需要逐行一致时
必须另行保存并验证原 generator、seed 和输入 manifest，不能猜测补齐。

```bash
bash scripts/build_external_tools.sh tpcds
mkdir -p /srv/keeninsight-inputs
python3 scripts/prepare_workloads.py ap-generate \
  --scale 10 --seed 42 --output /srv/keeninsight-inputs/tpcds-sf10 --apply
```

默认单进程，不运行 benchmark。SF10 默认检查数据目录文件系统至少 30 GiB
空闲；这只是起步保护，不是整个部署空间上限。数据文件、数据库、索引、
WAL、临时排序及模型/虚拟环境要分别留空间；DB 所在磁盘也需自行检查。
可先 SF1 功能验收，再 SF10 演示。改变 scale 会改变 SQL 耗时和触发概率。

生成后写 `generation.json`：scale、seed、generator/DDL 哈希、数据文件
大小和哈希。也可以在联网构建机生成整个目录，再与缓存分开搬运到目标机；
数据不是源码，不应提交 Git。生成器源码和数据条款仍需遵守。

### 4.3 导入 AP 数据

```bash
python3 scripts/prepare_workloads.py ap-load \
  --database keeninsight_repro --confirm-database keeninsight_repro \
  --data-dir /srv/keeninsight-inputs/tpcds-sf10 --apply
```

使用同一固定 generator 的 DDL，校验 provenance 和每个输入文件，再经
单连接 COPY 装入 `tpcds` schema，最后 ANALYZE。处理 dsdgen 的尾分隔符，
保留真实空列；不需要 server 读取操作者目录，也不安装 TPC-DS PG 扩展。
导入不自动建额外“调优索引”、不执行 LLM、不写假调优记录。
SF10 导入会占用真实 I/O、WAL 和磁盘，应在实验窗口进行。

确认 workload 能访问：

```bash
psql -X -d keeninsight_repro -c 'TABLE keeninsight_tpcc.load_info;'
psql -X -d keeninsight_repro -c 'SELECT count(*) FROM tpcds.store_sales;'
pgbench -n -c 1 -j 1 -t 20 -f app/tpcc_cases/tp_normal.sql keeninsight_repro
psql -X -d keeninsight_repro -c \
  "SET search_path=tpcds,public; SELECT count(*) FROM store_sales;"
```

## 5. 配置、启动和逐层验收

按 [DEPLOYMENT.md](DEPLOYMENT.md) 创建私有 env，改 DB 名、目标 LLM
endpoint/model/key 和 Grafana 密码。不要提交真实配置。

```bash
export KEENINSIGHT_OFFLINE=1
export KEENINSIGHT_WHEELHOUSE_ROOT=/transfer/keeninsight-cache/wheelhouses
bash scripts/deploy.sh doctor
sudo --preserve-env=KEENINSIGHT_OFFLINE,KEENINSIGHT_WHEELHOUSE_ROOT \
  bash scripts/deploy.sh install
bash scripts/deploy.sh status
```

离线监控安装缺包/哈希错就失败，不尝试 curl。doctor 失败必须补齐。
端口/service/runtime 会与旧部署争用；这段只用于新机器/独立部署。

**依赖离线 ≠ LLM 离线。** 配置公网模型 API 时必须允许对应网络访问；
完全隔离网络需提供协议兼容、产品代码可访问的内网模型服务。缓存没有
捆绑大模型服务，也不会复制任何 Codex auth。没有可用 LLM 时，不应标记
诊断/调优已真实完成。

建议按顺序验收并保留自己的真实记录：

1. 源码检查/隔离单测；
2. cache verify + 目标 Python offline-check；
3. DB 表/扩展、模型文件、doctor、服务健康；
4. TP 原负载能执行、Grafana 有真实 TPS；
5. 独立告警 LLM 链的输入、API 调用、候选、临时应用及恢复证据；
6. AP SQL 达阈值后真实 DREAM、方案评估、同 SQL 再执行命中和汇总页新增；
7. 多轮真实测量，比较实际外压、正确性和稳定性，不预填“加速倍数”。

当前 **TP 控制台候选回放仍需操作者另行提供候选文件**，见
CONFIGURATION.md。它不是新告警即时 LLM → TPS 的完整闭环。DREAM 内部
评估 SQL 的再采集问题也未在依赖工具中修复。这两项属于业务代码缺口，
**不能靠下载包解决**。完整限制见 KNOWN_LIMITATIONS.md。
