# HTTP 与前端入口

默认 bridge 仅绑定 `127.0.0.1:9108`。这些管理接口**没有应用层鉴权**，
不应通过公开监听或未经鉴权的代理暴露。这里是现有接口索引，不承诺所有
内部字段永久稳定；展示 contract 使用 `schemaVersion`。

## 页面与健康

| GET | 含义 |
|---|---|
| `/healthz` | bridge 进程身份与基本存活；不代表调优成功 |
| `/keeninsight/` | 产品汇总入口 |
| `/lab` | TP/AP 实验控制台 |
| `/ui` | 后台动作与自动应用管理 |

## 只读展示

前缀 `/api/v1/keeninsight/`：

- `overview`：配置状态、计数、最近实验、告警、Job 与能力边界。
- `hosts`：当前主机采集状态，未知值不是 0。
- `alerts`、`alerts/<incident_id>`：列表与精确告警证据。
- `sql`、`sql/<identity>`：SQL 指纹或单次 lab execution；详情可以带
  `job_id=<exact_job>`，不把旧方案归给新任务。
- `manual`、`code`、`diagnosisRules`、`alertRules`、`knowledge`：
  显式生成的资料、已加载规则或实际方案。
- `schema`：字段、来源与类别说明。

列表一般支持 `limit`、`offset`、`q`；SQL 支持
`kind=all|execution|observation`。服务端筛选在分页之前执行，
不同资源支持的额外筛选见 `KeenInsightPortal.get()`。

示例（只读）：

```bash
curl -fsS 'http://127.0.0.1:9108/api/v1/keeninsight/overview'
curl -fsS 'http://127.0.0.1:9108/api/v1/keeninsight/sql?kind=execution&limit=20'
```

## 实验状态

- GET `/api/v1/lab/catalog`
- GET `/api/v1/lab/status`
- GET `/api/v1/lab/runs/<run_id>`
- GET `/api/v1/jobs/<job_id>`
- GET `/api/v1/improvements/<improvement_id>`

`completed` 只表示处理结束；是否有收益还要看原/优化耗时、策略校验、
active 状态、实际重放命中。TP 要看业务 TPS、实际外压和候选来源。

## 会改变状态的操作

以下 POST 只应由受控操作者在专用实验库执行。请求 body 为 JSON：

| 路由 | 用途 |
|---|---|
| `/api/v1/actions/lab/tpcc/start` | `scenario_id`、`normal_clients`、`pressure_clients`、`baseline_seconds`、`pressure_seconds` |
| `/api/v1/actions/lab/stop` | 请求停止 `run_id` |
| `/api/v1/actions/lab/dream/start` | `sql_id`、可选 `request_id`；显式实验是 manual 触发 |
| `/api/v1/actions/lab/dream/replay` | 对已有 `run_id` 实际执行已发布优化方案 |
| `/api/v1/actions/lab/dream/clear` | 清理 DREAM 记录；不是普通刷新 |
| `/api/v1/actions/lab/reset-database` | 参数重置；不是只读操作 |
| `/api/v1/actions/jobs/<job_id>/retry` | 显式任务重试 |
| `/api/v1/actions/improvements/<id>/activate` | 请求方案激活，仍需服务端策略校验 |
| `/api/v1/actions/improvements/<id>/rollback` | 撤销已发布方案 |

重复点击/请求 ID 幂等不等于跨所有故障的事务性 exactly-once 保证。
开始真实实验前阅读资源限制和 [已知问题](KNOWN_LIMITATIONS.md)。
