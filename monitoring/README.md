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
- Prometheus：<http://127.0.0.1:9090/>
- Grafana 初始登录：`admin` / `admin`

Grafana 只监听本机回环地址，适合首期本机 Demo。Dashboard 由 `dashboards/` 自动导入，数据源由 `config/grafana/provisioning/` 自动配置。

## 已验证组件

- Grafana OSS 13.2.1 官方发行包。
- Prometheus 2.15.2、node_exporter 0.18.1、postgres_exporter 0.8.0，均为开源官方发行物。
- Prometheus 的 `prometheus`、`node`、`postgres` 三个采集目标均已验证为 `up`。
