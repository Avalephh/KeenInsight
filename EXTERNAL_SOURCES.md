# 外部源码与复现依赖

本仓库不复制数十 MB 的上游代码和本地 PostgreSQL 源码树。代码仍调用原始 SysInsight/WorkloadTune 源码函数；下面的路径契约用于复现。

## KeenInsight / WorkloadTune

- 上游项目：<https://github.com/Avalephh/KeenInsight>
- 运行时目录：`repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune`
- 环境变量：`SYSINSIGHT_SOURCE_ROOT`
- 依赖文件：`DBTuner/utils/analyzeException.py`、`DBTuner/utils/matchFunctions.py`、`llambo/`、`db_configurations/` 以及 `library/`。

当前工作区的 `branch-sources` 是之前获取的各分支源码快照，未带 `.git` 元数据，因此 Git 只记录本 Demo 对它的路径约定，不把这些快照误标成当前仓库的提交。

## PostgreSQL 12.22 source tree

- 运行时目录：`/root/keeninsight-postgres/third_party/postgresql-12.22`，也可通过 `POSTGRES_SOURCE_ROOT` 指定；
- 用途：重建 `perf-anomaly-demo/db_profiles/postgresql/common/function_parameter_association.json` 和相关 provenance；
- 构建命令：

```bash
cd perf-anomaly-demo/db_profiles
POSTGRES_SOURCE_ROOT=/path/to/postgresql-12.22 \
SYSINSIGHT_REPOSITORY_ROOT=/path/to/branch-sources \
python3 build_profile_artifacts.py
```

已有 profile artifact 会随 Demo 源码提交；重新生成时必须保留 PostgreSQL 版本和上游资料版本，并检查 provenance。

## 开源运行组件

Prometheus、node_exporter、postgres_exporter、Grafana OSS 和 FlameGraph 的使用路径由 `monitoring/` 与 `perf-anomaly-demo/vendor/FlameGraph/` 说明。发行包、二进制和运行数据不提交；请通过发行包/官方发布物安装后，在 `monitoring/.env.local` 中指定 `MONITORING_BIN_DIR` 和 `GRAFANA_HOME`。
