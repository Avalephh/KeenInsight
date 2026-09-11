# 外部源码与复现依赖

本仓库不把数十 MB 的上游源码、监控发行包或本地数据库数据复制进 Git。它们都有明确的获取入口和版本锁定文件；运行数据、TPCC 数据、API key 和原始结果仍只留在本机。

## KeenInsight / WorkloadTune

`external-sources.lock` 锁定了本 Demo 实际依赖的三个分支和提交：

- `WorkloadTune`：原始 SysInsight/LLAMBO 运行时源码；
- `WorkloadTune_new`：PostgreSQL 官方参数资料；
- `dev`：重建 PostgreSQL 关联库时保留的原始五条 legacy 关联来源。

在干净检出中执行：

```bash
./scripts/fetch_keeninsight_sources.sh
```

默认获取到：
`repositories/Avalephh-KeenInsight/branch-sources/<branch>`。
脚本拒绝覆盖已有的非 Git 快照，并在 checkout 后核对精确 commit。也可以用
`SYSINSIGHT_REPOSITORY_ROOT=/path/to/branch-sources` 指定外部目录。

运行时的 `SYSINSIGHT_SOURCE_ROOT` 默认指向其中的 `WorkloadTune`；需要自定义位置时设置该环境变量即可。

## PostgreSQL 12.22 source tree

`postgresql-source.lock` 锁定 PostgreSQL 官方仓库 tag `REL_12_22`、commit
`498f30a8b7025a2a7bd3715acc1d1692122ba542`，以及官方 source tarball 的 SHA-256。
获取并校验：

```bash
./scripts/fetch_postgresql_source.sh
```

脚本使用官方 tarball（避免 partial clone 在源文件很多时产生不稳定的碎片请求），默认目录是
`third_party/postgresql-12.22/`，也可通过 `POSTGRES_SOURCE_ROOT` 指定。
它只在重建 `perf-anomaly-demo/db_profiles/postgresql/common/` 的源码关联和 provenance
时需要；普通运行使用已提交的 profile artifact，不要求每次启动都重新扫描 PG 源码。

重建命令：

```bash
./scripts/fetch_keeninsight_sources.sh
./scripts/fetch_postgresql_source.sh
python3 perf-anomaly-demo/db_profiles/build_profile_artifacts.py
```

构建器会根据源码和官方资料机械生成关联、约束、默认值与 provenance，不会把生成机器的绝对路径写入新的 provenance。

## 开源监控组件

`monitoring/versions.lock` 锁定 Prometheus、node_exporter、postgres_exporter 和 Grafana OSS 的版本、官方下载地址及 SHA-256。干净检出后执行：

```bash
monitoring/install_open_source.sh
```

发行包下载到被忽略的 `monitoring/downloads/`，二进制安装到被忽略的
`monitoring/vendor/usr/bin/`，Grafana 安装到被忽略的
`monitoring/grafana-13.2.1/`。这一步不下载数据库数据，也不创建数据库角色。

FlameGraph 的 `stackcollapse-perf.pl` 是已提交的开源脚本副本，来源和用途见
`perf-anomaly-demo/README.md`。

## 复现检查

只检查仓库内源码、JSON、profile 和脚本：

```bash
./scripts/check_reproducibility.sh
```

连同锁定的外部源码和当前监控运行时一起检查：

```bash
./scripts/check_reproducibility.sh --strict
```

`--strict` 不会伪造或生成缺失的数据；如果缺少 PostgreSQL、`perf`、TPCC schema 或
系统工具，会明确报出缺项。数据库实例及其 TPCC 数据是本项目有意排除的外部输入。
