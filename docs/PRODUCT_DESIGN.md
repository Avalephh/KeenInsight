> **v2 scope note (2026-11-19):** SQL-level tuning (query optimizer agent,
> hints, rewrites, index actions, SQL evidence pages) has been removed from
> this codebase.  This document records the pre-v2 artifact; the active product
> is the SysInsight system-parameter tuning path only.

# KeenInsight PostgreSQL 智能运维平台产品设计

> 产品版本：Prometheus + Grafana 监控架构设计稿 v2
>
> 文档状态：目标产品设计。文末单独列出当前代码已经实现的能力和需要新增的工程，避免把设计目标误认为现状。

## 1. 产品定位

KeenInsight 是一个面向 PostgreSQL 的自动化观测、诊断和调优平台。

新的整体分工是：

```text
Prometheus：采集、存储和查询时序监控数据
Grafana：展示监控大盘、趋势和告警状态
KeenInsight：理解监控事件、分析数据库证据、调用 LLM、执行安全调优
```

Prometheus 和 Grafana 负责稳定、通用的监控基础设施，KeenInsight 不再承担完整的监控存储和基础图表职责，而是作为数据库智能运维控制平面，处理普通监控系统难以完成的工作：

- 判断 TPS 波动是否是真实异常，而不是采样颗粒度造成的假波动；
- 结合 PostgreSQL 执行计划、锁、查询统计、参数和主机资源解释异常；
- 让大模型提出受约束的调优候选；
- 自动执行可逆、低风险的参数调优，并用同一指标验证和回滚；
- 持续分析每个 SQL 指纹的性能变化；
- 对经过验证的 SQL 改写建立规则，在下一次同类 SQL 到来时执行；
- 保存完整的证据、动作、结果和回滚记录。

产品的核心闭环是：

```text
Prometheus 观测 → KeenInsight 事件处理 → 证据诊断
       → LLM 生成受约束候选 → 校验/灰度执行
       → Prometheus + PostgreSQL 复测 → 提交或回滚
```

## 2. 目标用户和产品入口

| 用户 | 主要问题 | 入口 |
| --- | --- | --- |
| DBA/运维人员 | 当前数据库是否异常、是否需要立即干预 | Grafana 总览、KeenInsight 异常中心 |
| 性能工程师 | 哪条 SQL 变慢、执行计划哪里变化 | Grafana SQL 大盘、KeenInsight SQL 优化中心 |
| 应用开发者 | 下一次请求是否可以自动使用更优 SQL | SQL 改写网关/SDK、改写规则详情 |
| 管理员 | 哪些自动动作可以执行、风险是否可控 | 自动调优策略、审批和审计页面 |
| 论文演示观众 | 系统是否真实监控并完成自动恢复 | Grafana 大盘、KeenInsight 闭环报告 |

推荐的用户体验是：

1. 打开 Grafana，看到数据库和主机监控趋势；
2. TPS 波动或慢 SQL 告警出现在 Grafana/Alertmanager；
3. 点击告警中的 KeenInsight 链接进入诊断事件；
4. KeenInsight 展示证据、LLM 分析、候选动作和自动化状态；
5. 用户可以查看灰度测量、最终效果、回滚原因和审计记录。

## 3. 产品设计原则

### 3.1 监控和智能控制解耦

Prometheus 是监控数据的主要来源，Grafana 是监控数据的主要展示入口，KeenInsight 通过 Prometheus HTTP API、Alertmanager Webhook 和 PostgreSQL 证据接口获得数据。监控页面不依赖 KeenInsight 的 Python 进程才能显示基本曲线。

### 3.2 原始值、窗口平均值和事件值分开

TPS 必须从累计事务计数器和真实时间戳计算：

```text
TPS(W) =
  [(commit(t2) + rollback(t2))
   - (commit(t1) + rollback(t1))] / (t2 - t1)
```

其中 `W` 是 30 秒、60 秒或用户选定的时间窗口。图表同时保留：

- 原始采样点：用于精确定位突变；
- 窗口平均 TPS：用于观察稳定负载下的真实平均水平；
- 波动度和事件标记：用于解释为什么触发自动调优。

不能将几个已经四舍五入的 5 秒 TPS 点再次普通平均，也不能用中位数冒充平均 TPS。中位数可以用于异常鲁棒判断，但不能替代计数器窗口平均值。

### 3.3 LLM 只能提出候选，不能直接控制数据库

大模型读取经过筛选的指标、执行计划、查询历史、参数和知识证据，输出结构化候选。候选必须经过服务端白名单、范围、作用域、风险和实际测量校验。模型不能输出任意 SQL 并直接执行，也不能修改原始测量结果。

### 3.4 自动化必须有边界

自动调优默认只允许动态、可逆、影响范围明确的参数。重启型参数、DDL、索引、连接池和 SQL 改写规则默认进入人工审核或灰度流程。每个自动动作必须有超时、冷却时间、最大变更幅度和回滚条件。

### 3.5 证据必须可追溯

每个告警和调优结果必须关联：Prometheus 查询表达式、查询时间范围、原始样本、PostgreSQL 计划、参数前后状态、动作 ID、LLM 输入摘要、候选校验结果、验证指标和回滚记录。

## 4. 总体架构

```text
┌──────────────────────────────────────────────────────────────┐
│ PostgreSQL                                                     │
│ pg_stat_database · pg_stat_statements · pg_locks · pg_settings │
│ EXPLAIN · pg_stat_activity · 业务 SQL                          │
└──────────────┬───────────────────────────────┬───────────────┘
               │                                 │
       PostgreSQL Exporter                 KeenInsight 控制连接
       Custom SQL Collector                计划/锁/参数/动作验证
               │                                 │
┌──────────────▼───────────────────────────────▼───────────────┐
│ Prometheus                                                     │
│ scrape · recording rules · range query · 24h demo retention    │
└──────────────┬───────────────────────────────┬───────────────┘
               │                                 │
       ┌───────▼────────┐                 ┌─────▼──────────────┐
       │ Grafana         │                 │ Alertmanager         │
       │ 监控大盘/趋势    │                 │ 告警路由/Webhook     │
       └─────────────────┘                 └─────┬──────────────┘
                                                  │
┌─────────────────────────────────────────────────▼─────────────┐
│ KeenInsight 智能控制平面                                       │
│                                                              │
│ 事件接收/归一化 → TPS/SQL 检测 → 证据关联 → RAG/LLM 分析       │
│       │                 │                 │                   │
│ 系统参数自动调优       SQL 优化器        策略/审批/审计         │
│       │                 │                                     │
│ 灰度执行/复测/回滚   SQL 改写规则注册中心                      │
└──────────────┬───────────────────────┬──────────────────────┘
               │                       │
       PostgreSQL 参数/计划         SQL Gateway / SDK
                                       │
                              ┌────────▼────────┐
                              │ 应用 → PostgreSQL │
                              └─────────────────┘
```

### 4.1 数据职责边界

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| `postgres_exporter` | PostgreSQL 标准统计指标 | 自动改参数、解释根因 |
| 自定义 SQL exporter | 查询级低基数指标、查询指纹 | 执行 SQL 改写 |
| `node_exporter` | CPU、内存、磁盘、网络 | 数据库级判断 |
| Prometheus | 时序采集、保留、PromQL、recording rule | LLM、DDL、回滚决策 |
| Grafana | 大盘、趋势、告警链接 | 数据库控制动作 |
| Alertmanager | 告警去重、分组、路由、Webhook | 深度诊断和调优 |
| KeenInsight | 证据关联、诊断、LLM、策略、调优闭环 | 替代 Grafana 的通用时序平台 |
| SQL Gateway/SDK | 下一次命中时按规则路由改写 SQL | 生成未经验证的改写 |
| PostgreSQL | 业务执行和动作结果 | 接收模型文本直接执行 |

## 5. 监控层设计：Prometheus + Grafana

### 5.1 Exporter 组成

#### PostgreSQL 指标

使用 `postgres_exporter` 暴露标准数据库指标，并通过 custom query 配置补充 KeenInsight 需要的字段：

- `pg_stat_database`：事务提交/回滚、块命中/读取、元组、临时文件、连接数；
- `pg_stat_statements`：query ID、调用次数、总执行时间、行数、共享块、临时块；
- `pg_locks`：等待会话和阻塞关系的聚合计数；
- `pg_settings`：少量关键参数的当前值；
- WAL、后台写入、检查点和复制状态。

#### 主机指标

使用 `node_exporter` 采集 CPU、内存、磁盘、文件系统、网络和负载。Prometheus 只保留稳定的低基数标签，如 `instance`、`job`、`cluster`、`database`。

#### 可选深度观测

eBPF/BCC、日志采集和 `pg_stat_monitor` 作为增强数据源：

- eBPF 用于 PostgreSQL 函数、内核写入和长查询关联；
- `pg_stat_monitor` 或应用侧 histogram 用于更严格的请求级 P95/P99；
- PostgreSQL 日志用于审计无法从统计视图恢复的单条语句。

这些数据不改变 Prometheus + Grafana 的主架构，KeenInsight 统一将其转换为证据。

### 5.2 指标命名与标签规则

产品定义逻辑指标名，具体 exporter 名称允许因版本不同做映射：

| 逻辑指标 | 来源 | 标签 |
| --- | --- | --- |
| `keeninsight_db_transactions_total` | commit + rollback 累计值 | cluster、instance、datname、type |
| `keeninsight_db_tps` | recording rule | cluster、instance、datname |
| `keeninsight_db_connections` | `numbackends` | cluster、instance、datname |
| `keeninsight_db_cache_hit_ratio` | blocks hit/read | cluster、instance、datname |
| `keeninsight_sql_calls_total` | `pg_stat_statements.calls` 增量 | instance、datname、queryid |
| `keeninsight_sql_exec_seconds_total` | 总执行时间增量 | instance、datname、queryid |
| `keeninsight_sql_temp_blocks_total` | 临时块增量 | instance、datname、queryid |
| `keeninsight_sql_rows_total` | 返回行数增量 | instance、datname、queryid |
| `keeninsight_sql_active` | `pg_stat_activity` 聚合 | instance、datname、queryid |

不把完整 SQL 文本作为 Prometheus label。完整 SQL、规范化 SQL、参数类型和来源保存在 KeenInsight 的查询证据库中，避免高基数和敏感信息泄漏。

### 5.3 TPS Recording Rules

示意规则如下，实际 metric 名称由 exporter 映射层统一：

```yaml
groups:
  - name: keeninsight-database-rates
    interval: 5s
    rules:
      - record: keeninsight:db_tps:30s
        expr: |
          sum by (cluster, instance, datname) (
            rate(keeninsight_db_transactions_total[30s])
          )
      - record: keeninsight:db_tps_avg:5m
        expr: avg_over_time(keeninsight:db_tps:30s[5m])
      - record: keeninsight:db_tps_volatility:5m
        expr: |
          stddev_over_time(keeninsight:db_tps:30s[5m])
          / clamp_min(keeninsight:db_tps_avg:5m, 1)
```

Prometheus 的 `rate()` 和 recording rule 适合图表及初筛。KeenInsight 收到事件后必须再次通过 range query 获取窗口首尾累计计数器和原始时间戳，计算可审计的窗口平均 TPS，避免只依赖已经聚合过的点。

### 5.4 Grafana 页面

Grafana 提供三类基础大盘：

1. **数据库总览**：窗口平均 TPS、原始 TPS、P95/P99、连接数、缓存命中率、临时空间、错误和告警；
2. **SQL 性能**：按 query ID 的调用量、平均/分位延迟、总耗时、临时块、计划变化和慢 SQL Top N；
3. **主机与数据库关联**：CPU、内存、磁盘 IO、网络、WAL 与 TPS/延迟叠加。

Grafana 面板只负责显示和链接，调优按钮放在 KeenInsight 控制台，防止监控面板直接触发数据库变更。

## 6. KeenInsight 事件处理层

### 6.1 事件入口

KeenInsight 支持两种输入：

- Alertmanager Webhook：告警达到规则条件后发送事件；
- Prometheus range query：KeenInsight 定期拉取关键指标，补齐窗口数据、计算基线和判断恢复。

推荐流程是“Webhook 触发 + range query 补证据”。Webhook 只携带告警标签和时间范围，不携带未经验证的调优命令。

事件归一化后统一为：

```json
{
  "event_type": "tps_volatility",
  "cluster": "demo",
  "instance": "postgresql:5432",
  "database": "keeninsight",
  "starts_at": "...",
  "ends_at": "...",
  "source": "alertmanager",
  "prometheus_alert": "KeenInsightTPSVolatility",
  "fingerprint": "..."
}
```

### 6.2 事件状态机

```text
normal
  ↓ Prometheus 告警/Webhook
detected → corroborating → diagnosed
                              ↓
                    candidate_generated
                              ↓
                    validated / rejected
                              ↓
                    canary_applying
                         ↓       ↓
                  improved    regressed
                    ↓           ↓
                 committed    rolled_back
```

同一个 fingerprint 在冷却窗口内只允许一个自动调优任务。数据库恢复后，事件进入 resolved，但历史证据不删除。

## 7. TPS 波动检测和自动调优路径

### 7.1 为什么要用窗口平均 TPS

5 秒采样下，低流量数据库会出现“一笔事务对应约 0.2 TPS”的量化效应；高并发压力又可能把短时间事务集中到一个采样区间。页面直接画区间点时，曲线会跳动，但不代表稳定负载的真实长期速率不稳定。

因此产品使用三种不同量：

| 量 | 计算 | 用途 |
| --- | --- | --- |
| 原始区间 TPS | 每个相邻采样点的累计计数差/真实间隔 | 精确定位突变 |
| 窗口平均 TPS | 窗口首尾累计计数差/窗口真实时长 | 展示稳定负载水平 |
| 波动度 | MAD、标准差或 EWMA 偏差 | 判断是否触发异常 |

默认参数：

- exporter scrape：5 秒；
- TPS 显示窗口：30 秒；
- 波动评估窗口：5 分钟；
- 至少连续 3 个异常判断点；
- 自动调优冷却：默认 10 分钟；
- 演示环境指标保留：24 小时。

### 7.2 TPS 告警条件

TPS 波动事件不能只用绝对阈值。建议组合以下条件：

```text
相对偏差 = |当前窗口 TPS - 5 分钟基线| / max(基线, 1)
波动系数 = robust_dispersion(最近 5 分钟窗口) / max(基线, 1)
```

当相对偏差、波动系数或突增持续时间达到策略阈值，并且连接数、延迟、临时块、CPU 或锁等至少有一个相关证据时，进入 KeenInsight 诊断。只有 TPS 单独波动而没有其他证据时，先标记为“待确认”，不立即改参数。

### 7.3 系统级自动调优闭环

```text
1. Alertmanager 把 TPS 波动事件发送给 KeenInsight
2. KeenInsight 查询 Prometheus 原始范围数据，重算窗口平均 TPS
3. 关联数据库连接、P95、临时块、锁、CPU、IO、WAL 和 SQL Top N
4. 查询 PostgreSQL 当前参数、版本、计划和最近调优历史
5. RAG 检索论文、文档、代码和历史成功经验
6. LLM 输出结构化参数候选及理由、预期影响和风险
7. 确定性校验候选的参数名、范围、上下文、权限和作用域
8. 先执行 session/canary 级变更，观察固定时间窗口
9. 使用 Prometheus 窗口平均 TPS、延迟、错误和资源指标验证
10. 达到效果门槛则提交，否则自动回滚并记录原因
```

LLM 候选格式示例：

```json
{
  "action_type": "set_database_parameter",
  "parameter": "work_mem",
  "value": "8MB",
  "scope": "database",
  "duration_seconds": 300,
  "evidence_ids": ["P-123", "Q-456"],
  "rollback_required": true
}
```

这个 JSON 只是候选，不是执行指令。服务端只接受固定的 `action_type` 和白名单参数，并重新从 PostgreSQL 读取当前状态。

### 7.4 自动调优风险等级

| 等级 | 示例 | 默认处理 |
| --- | --- | --- |
| L0 | 只读诊断、RAG、Grafana annotation | 自动执行 |
| L1 | session 级 `work_mem`、session 级并行参数 | 自动灰度，自动回滚 |
| L2 | database 级动态参数，影响新连接 | 策略允许时自动，必须有 canary 和回滚 |
| L3 | 重启参数、连接数、DDL、索引、全局资源策略 | 人工审核 |
| L4 | SQL 改写规则上线 | 结果等价验证后灰度，默认人工确认 |

不能把 `shared_buffers` 这类需要重启或影响范围很大的参数放进无条件自动修改路径。自动模式必须由管理员显式打开，并且可以按数据库、参数和场景配置白名单。

## 8. 每条 SQL 的持续监控

### 8.1 SQL 识别

数据库侧以 PostgreSQL `queryid` 为主键，并保存：

- 原始 SQL 文本和脱敏版本；
- 规范化 SQL 和参数类型；
- 数据库、用户、应用名、schema/search_path；
- 首次/最近观测时间；
- 当前执行计划摘要和计划 digest；
- 调用次数、总耗时、平均耗时、行数、块访问、临时块。

Prometheus 暴露 query ID 级低基数指标；完整 SQL 和详细历史存储在 KeenInsight 证据库。对于无法从 `pg_stat_statements` 区分的应用上下文，使用应用侧 instrumentation 或 SQL Gateway 补充。

### 8.2 慢 SQL 触发条件

慢 SQL 检测使用绝对阈值和相对基线结合：

- 延迟超过数据库/服务策略阈值；
- 相对于该 query ID 的历史基线增长超过比例；
- 调用量和总耗时足以产生实际影响；
- 执行计划 digest 变化；
- 临时块、磁盘排序、锁等待、shared read 或 rows 偏差异常。

`pg_stat_statements` 本身提供聚合均值，不提供完整请求级 P95。严格 P95/P99 应使用 `pg_stat_monitor`、应用 histogram 或 SQL Gateway 延迟样本。页面必须标识 P95 的来源和覆盖范围。

### 8.3 SQL 后台优化流程

```text
SQL 变慢
  ↓
保存 queryid、规范化 SQL、时间窗口和执行计划
  ↓
与历史成功经验、PostgreSQL 文档和源码知识检索关联
  ↓
生成候选：session hint / 安全 SQL 改写 / 参数化建议 / 索引建议
  ↓
候选校验：只读、语法、结果等价、计划变化、实测延迟
  ↓
形成待审核或待灰度的 SQL 改写规则
```

SQL 改写的候选包括安全的等价改写，例如经过解析和结果验证的谓词重写、冗余条件消除或等价 `OR`/`IN` 变换。任意复杂重写都必须被拒绝或转人工审核。

## 9. 下一次 SQL 命中时的改写架构

Prometheus 和 Grafana 只能监控，不能拦截下一次 SQL。因此“下一次遇到时改写”需要一个实际的执行接入点。

### 9.1 推荐方案：SQL Gateway 或应用 SDK

```text
应用发起 SQL
   ↓
Gateway/SDK 计算 query fingerprint
   ↓
查询 KeenInsight Rewrite Registry
   ├─ 无 active 规则：执行原 SQL
   └─ 有 active 规则：执行已验证 rewrite SQL
   ↓
记录原 SQL、改写 SQL、版本、耗时、错误和结果校验
   ↓
回传 Prometheus 和 KeenInsight
```

规则键至少包含：

```text
(cluster, database, role, application_name,
 queryid/normalized_sql, parameter_types, schema_version, rule_version)
```

这样可以防止同一个 SQL 在不同 schema、用户权限或参数类型下错误复用改写规则。

### 9.2 改写规则生命周期

```text
candidate → validated → canary → active → suspended/retired
```

上线前必须完成：

1. SQL 语法和只读校验；
2. 原 SQL 与改写 SQL 结果集行数、哈希或业务等价性校验；
3. 参数绑定和类型一致性校验；
4. `EXPLAIN`/`EXPLAIN ANALYZE` 计划证据；
5. 固定比例或固定次数 canary；
6. 延迟、错误率和结果不一致监控。

任一条件失败，规则自动暂停，命中请求回退到原 SQL。每次命中都记录规则版本，保证可以回答“哪个改写造成了变化”。

### 9.3 透明改写的边界

PostgreSQL 原生并没有一个可以安全地把任意 SQL 文本交给外部服务改写的标准配置项。若不引入 Gateway、SDK 或 PostgreSQL planner hook，KeenInsight 只能生成改写建议，不能保证下一次数据库请求自动使用改写结果。因此 SQL Gateway/SDK 是该产品能力的必要组件，不应在文档或演示中省略。

## 10. 诊断、RAG 和 LLM

### 10.1 证据优先级

诊断按以下顺序组合证据：

1. Prometheus 原始范围数据和 recording rule；
2. PostgreSQL 计划、查询统计、锁、参数和活动会话；
3. 主机资源、WAL、eBPF 或日志；
4. 经过验证的历史调优反馈；
5. 论文、设计、代码和 PostgreSQL 文档 RAG；
6. LLM 对上述证据的解释和候选生成。

LLM 不可以凭经验补写缺失的 TPS、延迟或改善百分比。

### 10.2 RAG 数据

RAG 收集：

- 论文、评审意见和产品设计；
- PostgreSQL 官方文档和外部技术资料；
- 当前代码、源码/LLVM 分析和原始静态页面；
- Prometheus/Grafana 告警和监控说明；
- 运行、异常、调优、SQL 改写和反馈记录。

检索支持 PostgreSQL 全文检索、BGE 向量检索和 hybrid RRF。LLM 回答必须包含有效 `[R#]` 引用，否则回退为抽取式证据摘要。

### 10.3 LLM 调用安全边界

模型收到的是脱敏后的时间窗口、指标摘要、计划摘要、参数状态和引用证据。模型可以：

- 总结异常原因；
- 给出候选参数或候选 SQL 改写；
- 解释证据之间的关系；
- 生成面向运维人员的报告。

模型不可以：

- 直接执行 SQL 或 shell；
- 直接写入 Prometheus、PostgreSQL 或 Rewrite Registry；
- 绕过人工策略、灰度、结果校验和回滚；
- 把候选结果描述成已经生效；
- 修改原始监控数据。

## 11. 工作负载和真实验证

工作负载实验室用于验证 KeenInsight 的诊断和调优因果，不替代生产监控。

当前已有的非索引案例继续保留：

| 案例 | 观测证据 | 验证动作 |
| --- | --- | --- |
| Hash Join 溢出 | 多批次 Hash、临时块 | `work_mem` 灰度/复测 |
| 分组聚合溢出 | 外部排序、临时磁盘 | `work_mem` 灰度/复测 |
| 窗口排序溢出 | external merge | `work_mem` 灰度/复测 |
| 并行 worker 不足 | Planned/Launched worker | 有界并行参数复测 |
| 统计信息过期 | 估计/实际行数偏差 | `ANALYZE` 后复测 |
| 锁竞争 | waiter/blocker/lock graph | 释放演示锁会话 |

TPC-C 形状数据继续作为真实业务负载来源，但必须标注为 demo seed，不把它宣传为合规 TPC-C benchmark。

每个验证实验至少保存：

- Prometheus 事件前后范围数据；
- 相同 SQL、参数、warm-up 和重复次数；
- PostgreSQL 计划和结果摘要；
- 调优前后窗口平均 TPS、延迟、错误率和资源指标；
- 动作应用、提交、回滚和失败原因。

## 12. 数据模型设计

### 12.1 Prometheus 存储

Prometheus TSDB 保存高频监控时序。演示环境默认保留 24 小时，生产环境由容量和合规策略决定。Grafana 直接查询 Prometheus；KeenInsight 不复制全部时序，只保存事件窗口和审计所需的证据快照/查询引用。

### 12.2 KeenInsight 证据库

现有 `keeninsight` schema 继续作为控制平面和证据库，并新增/扩展以下目标实体：

| 实体 | 作用 |
| --- | --- |
| `metric_sources` | Prometheus、exporter、Alertmanager 的连接和版本 |
| `metric_queries` | 记录 PromQL、时间范围、返回摘要和查询 digest |
| `anomaly_events` | 统一 Prometheus 告警、TPS/SQL 异常和状态机 |
| `sql_fingerprints` | queryid、规范化 SQL、应用上下文和基线 |
| `sql_observations` | SQL 时间窗口、调用、耗时、临时块、计划 digest |
| `tuning_policies` | 自动调优开关、风险级别、阈值、冷却和范围 |
| `tuning_candidates` | LLM/规则生成的结构化候选及校验结果 |
| `tuning_executions` | canary、正式应用、指标复测和回滚 |
| `sql_rewrite_rules` | 原指纹、改写文本、版本、状态和等价性证据 |
| `sql_rewrite_executions` | 每次命中、原/改写延迟、结果和错误 |
| `audit_events` | 所有状态变化、操作者和系统动作 |
| `rag_documents/chunks/embeddings` | 产品、论文、代码和运行知识 |

原有 `snapshots` 可以作为迁移期间的兼容证据表，但新架构的主监控来源改为 Prometheus。KeenInsight 自身只在事件窗口保留必要的原始样本，以便复现告警和自动调优结论。

## 13. 前端信息架构

### 13.1 Grafana：看监控

- 数据库总览；
- TPS 原始点/30 秒窗口平均/5 分钟基线；
- P95/P99 和来源说明；
- 连接、缓存、临时空间、锁和资源；
- SQL Top N 和慢 SQL 趋势；
- 告警列表和跳转 KeenInsight。

### 13.2 KeenInsight：做分析和控制

| 页面 | 职责 |
| --- | --- |
| 事件中心 | 告警、证据、状态机、诊断报告 |
| 自动调优 | 参数候选、LLM 理由、策略、canary、回滚 |
| SQL 优化 | 慢 SQL、执行计划、候选改写和测量 |
| 改写规则 | 规则版本、命中记录、暂停和回退 |
| 工作负载实验室 | 真实压力、异常触发和同条件复测 |
| RAG 知识 | 论文、代码、文档和运行经验问答 |
| 证据与审计 | PromQL、原始样本、动作和结果导出 |
| 系统设置 | Prometheus、Alertmanager、数据库和策略配置 |

原始 `English.zip` 页面作为兼容演示入口保留，但新产品主入口应是 Grafana + KeenInsight 控制台，而不是把 Grafana 图表再次复制到静态页面中。

## 14. 关键 API 设计

### 14.1 监控和事件

```text
GET  /api/monitoring/status
POST /api/events/alertmanager
GET  /api/events
GET  /api/events/{id}
GET  /api/events/{id}/evidence
GET  /api/prometheus/query
GET  /api/prometheus/query-range
```

### 14.2 自动调优

```text
GET  /api/autotune/policies
PATCH /api/autotune/policies/{id}
POST /api/events/{id}/diagnose
POST /api/events/{id}/generate-candidate
POST /api/candidates/{id}/validate
POST /api/candidates/{id}/canary
POST /api/executions/{id}/rollback
GET  /api/executions/{id}
```

### 14.3 SQL 优化和改写

```text
GET  /api/sql/fingerprints
GET  /api/sql/fingerprints/{queryid}
POST /api/sql/{queryid}/analyze
POST /api/sql/{queryid}/generate-rewrites
POST /api/sql/rewrites/{id}/validate
POST /api/sql/rewrites/{id}/activate
POST /api/sql/rewrites/{id}/suspend
POST /api/sql/rewrite/lookup
POST /api/sql/rewrite/report
```

`/api/sql/rewrite/lookup` 供 Gateway/SDK 查询 active 规则；`report` 回传真实命中结果。接口不允许客户端上传一段模型文本直接变成 active 规则。

### 14.4 兼容当前代码的接口

当前项目已有的 `/api/dashboard`、`/api/dashboard/metrics`、`/api/incidents`、`/api/workloads`、`/api/actions`、`/api/rag/*`、`/api/models/*`、`/api/ebpf/*` 和 `/api/audit` 继续保留，作为迁移期兼容接口。迁移后：

- dashboard 图表数据逐步改为 Prometheus query-range；
- incidents 增加 Prometheus 告警来源和 query digest；
- actions 增加 canary、策略和自动回滚字段；
- workloads 结果同时写入 Prometheus annotation 和 KeenInsight 证据库；
- optimizer 增加 Rewrite Registry/Gateway 对接。

## 15. 底层技术栈

| 层 | 技术 | 作用 |
| --- | --- | --- |
| 监控采集 | Prometheus | scrape、TSDB、PromQL、recording rule |
| 监控展示 | Grafana | 大盘、告警视图、annotation、跳转 |
| 告警路由 | Alertmanager | 去重、分组、静默、Webhook |
| PostgreSQL exporter | postgres_exporter + custom queries | 数据库统计指标 |
| 主机 exporter | node_exporter | 主机资源 |
| SQL 深度观测 | pg_stat_statements、可选 pg_stat_monitor | 查询指纹和分位延迟 |
| 控制平面 | Python、FastAPI、Uvicorn | 事件、API、策略和执行器 |
| 数据访问 | psycopg 3、PostgreSQL | 计划、参数、证据和审计 |
| 智能分析 | RAG、BGE、XGBoost、SHAP | 知识检索、预测、归因 |
| 大模型 | OpenAI 兼容 Chat Completions | 解释和候选生成 |
| 深度观测 | BCC/eBPF、Kafka | 可选函数/内核事件流 |
| SQL 接入 | Gateway 或应用 SDK | 下一次命中时执行已验证改写 |
| 源码分析 | Clang、LLVM IR | 参数到 PostgreSQL 函数的源码证据 |
| 部署 | Docker Compose、systemd | 本地隔离和服务器持久化 |

## 16. 安全、权限和自动化护栏

### 16.1 Prometheus/Grafana

- Prometheus 对 PostgreSQL 只使用最小监控权限；
- Grafana 使用只读数据源；
- Alertmanager Webhook 使用签名或内网认证；
- query ID 标签不暴露完整 SQL 和敏感参数；
- 外部访问使用 HTTPS、身份认证和网络访问控制。

### 16.2 KeenInsight 执行器

- LLM token 只放在受限环境变量或密钥管理器；
- 动作必须有类型、目标、前置状态和回滚状态；
- 自动动作使用数据库级 advisory lock/幂等键避免并发重复调优；
- 每次变更设定最大幅度和观察窗口；
- 指标恶化、错误增加、结果不一致或超时立即回滚；
- DDL、索引、重启参数和 SQL 改写默认不进入无审核自动路径；
- 任何自动动作都写入审计，不能静默执行。

### 16.3 SQL 改写

- Gateway/SDK 默认 fail-open：KeenInsight 不可用时执行原 SQL；
- 只对 active 且未过期规则改写；
- schema、权限、参数类型或 query fingerprint 变化时自动失效；
- 结果不一致或延迟恶化时自动暂停规则；
- 改写文本不直接来自前端或 LLM，必须经过验证器和注册中心。

## 17. 当前代码与目标产品的差距

当前仓库已经实现了较完整的 PostgreSQL 证据、工作负载、诊断、类型化调优、RAG、LLM、模型、LLVM、eBPF 和原始页面兼容能力，但仍处于“直接 PostgreSQL 采集器 + FastAPI Canvas 控制台”阶段。

### 已有能力

- FastAPI、PostgreSQL 统计视图和 24 小时快照保留；
- 当前页面展示真实数据库指标；
- 真实工作负载、TPC-C 形状数据和非索引异常案例；
- 执行计划、锁、查询统计、规则诊断；
- 类型化参数调优、同条件复测、反馈和回滚；
- RAG、向量检索、外部 LLM 和引用校验；
- 查询 hints、保守 SQL 改写候选和结果等价测量；
- eBPF/Kafka、XGBoost/SHAP、LLVM 和经验闭环的可选实现；
- systemd 持久化服务。

### 尚未完成、不能提前宣称的目标能力

1. 尚未把 Prometheus 作为正式的监控数据源接入 FastAPI；
2. 尚未提供正式的 Grafana dashboard、recording rules 和 Alertmanager Webhook；
3. 当前 TPS 仍主要由应用快照接口计算，尚未统一为 Prometheus range query + KeenInsight 窗口重算；
4. 当前 LLM 自动闭环主要由工作负载运行接口显式触发，尚未做到 TPS 告警自动触发系统参数调优；
5. 当前 SQL 优化器可以产生和测量改写候选，但尚未提供 SQL Gateway/SDK，因此不能保证下一次真实业务请求自动使用改写；
6. 当前 `pg_stat_statements` 聚合数据不能单独提供严格请求级 P95/P99；
7. 当前系统尚未实现面向生产的多租户、认证、权限分级和 HTTPS 入口。

这些差距是产品下一阶段的工程任务，不应通过修改论文表述来掩盖。

## 18. 推荐实施顺序

### Phase 0：监控基础设施

- 部署 Prometheus、Grafana、Alertmanager；
- 部署 `postgres_exporter`、custom SQL collector 和 `node_exporter`；
- 建立数据库总览、SQL Top N 和主机关联大盘；
- 验证 TPS 窗口平均值与 PostgreSQL 累计计数器一致。

### Phase 1：KeenInsight 接入 Prometheus

- 增加 Prometheus HTTP API 客户端；
- 增加 Alertmanager Webhook；
- 把 PromQL、时间范围、原始样本和告警 fingerprint 写入证据库；
- 保留现有直连采集器作为兼容和交叉验证路径；
- Grafana annotation 显示 KeenInsight 事件和调优动作。

### Phase 2：TPS 自动调优

- 完成 TPS 波动基线、MAD/标准差、持续时间和冷却策略；
- 建立参数风险等级和自动调优白名单；
- 接入 LLM 候选生成和确定性校验；
- 实现 session/database canary、Prometheus 复测、提交和回滚；
- 用稳定负载、突增负载和回滚故障注入验证闭环。

### Phase 3：SQL 观测和后台优化

- 建立 query ID 元数据和 SQL 指纹基线；
- 接入 `pg_stat_statements` 增量和可选请求 histogram；
- 对慢 SQL 自动保存执行计划和证据；
- 接入 RAG、LLM 候选和同库结果等价验证；
- 将改写候选与参数调优候选分开管理。

### Phase 4：SQL Gateway/SDK 改写闭环

- 实现 query fingerprint 到 Rewrite Registry 的 lookup；
- 在 Gateway/SDK 中执行 active 改写；
- 回传命中、耗时、错误和结果校验；
- 实现 canary、自动暂停、原 SQL 回退和规则版本审计；
- 最后再评估 PostgreSQL planner hook 等透明接入方式。

## 19. 验收标准

### 监控准确性

- 固定速率负载下，30 秒窗口平均 TPS 曲线稳定；
- 5 秒原始点仍能标记短暂突增；
- 窗口平均 TPS 与累计 commit/rollback 差分除以真实时间一致；
- Prometheus、Grafana、KeenInsight 同一时间窗口数值可互相追溯。

### 自动调优真实性

- 告警由真实 Prometheus 指标触发；
- LLM 候选必须引用真实证据；
- 服务端拒绝越权、越界和未知参数；
- 调优前后使用固定窗口和相同负载测量；
- 指标变差时能够自动回滚；
- 结果、计划、参数和动作审计完整。

### SQL 改写安全性

- 慢 SQL 由真实 query ID 和数据库观测触发；
- 改写前后结果等价；
- 改写规则具备版本、作用域和过期时间；
- 下一次命中能看到原 SQL、改写 SQL 和实际收益；
- Gateway/SDK 或 KeenInsight 不可用时自动执行原 SQL；
- 任何回归都能自动暂停并回退。

## 20. 当前文档和运行入口

当前代码实现说明：

- [SERVER_SERVICE.md](SERVER_SERVICE.md)：服务器持久化运行；
- [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md)：真实工作负载和演示步骤；
- [ICDE_CLAIM_AUDIT.md](ICDE_CLAIM_AUDIT.md)：论文能力和证据边界；
- [RAG_OPERATIONS.md](RAG_OPERATIONS.md)：RAG 运行说明；
- [EBPF_OPERATIONS.md](EBPF_OPERATIONS.md)：eBPF/Kafka 运行说明。

目标产品的核心入口：

```text
Grafana：数据库监控、TPS、SQL 和主机大盘
KeenInsight：异常诊断、自动调优、SQL 改写、RAG 和审计
SQL Gateway/SDK：下一次请求命中已验证 SQL 改写规则
```

