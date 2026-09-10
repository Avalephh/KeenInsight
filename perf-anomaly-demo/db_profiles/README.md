# SysInsight 数据库 / 版本 profile

入口脚本通过两个开关选择 profile：

```bash
python3 sysinsight_original_llm.py \
  --case-result results/tpcc_external/<run>/<case>/case_result.json \
  --dbms postgresql --db-version 12
```

也可以用环境变量：

```bash
export SYSINSIGHT_DBMS=postgresql
export SYSINSIGHT_DB_VERSION=12
```

当前登记的 profile：

- `mysql/8.0.36`：直接指向仓库原有 MySQL 参数、默认值、手册和规则文件。
- `postgresql/12`：本机 PostgreSQL 12 的当前配置快照，使用仓库已有的 PostgreSQL 参数手册和 PG 函数关联资料。
- `postgresql/13`：使用本地 PostgreSQL 13 官方参数文档默认值的 profile。
- `postgresql/14`：使用仓库已有 PostgreSQL 14.7 配置快照的 profile。

PG profile 的候选值采用 `pg_settings` 的原生数值单位。例如 `work_mem=4096`
表示 4096 kB，`shared_buffers=16384` 表示 16384 个 8 kB buffer。这样才能和
原始 SysInsight 的数值候选解析器保持一致。最终 prompt 同时会注明单位。

PG 参数—函数关联现在由固定的 PostgreSQL 12.22 开源源码构建，覆盖官方文档参数中
能在后端源码中找到真实绑定和使用的 276 个 GUC；其中 231 个数值/枚举/布尔 GUC 进入
自动搜索空间。仓库原有的 5 条 PG 记录没有被冒充成新发现，而是作为每条记录中的
`legacy_association` 原样保留；新增关系均带有 GUC 绑定、源码文件、行号、源码摘要和源码树 digest，详见
`function_parameter_association.provenance.json`。PG 手册知识来自
`WorkloadTune_new/sysinsight/library/` 下已采集的 `official_document.json`、
`structured_knowledge/` 和 `tuning_lake/`。
当前仓库没有 PG 历史规则文件，因此 PG profile 的规则文件为空，程序不会伪造规则。

首次使用前重新构建由上述原始资料机械提取的 profile 文件：

```bash
python3 db_profiles/build_profile_artifacts.py
```
