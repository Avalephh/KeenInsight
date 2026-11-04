# eBPF 告警，按需启动原 perf / PG / SQL 采集

本入口按最新需求，不再追求“每一项原始数据都必须由 eBPF 重造”。
**eBPF 负责升级采集的告警；原 perf 及数据库观测负责诊断证据。**
这是实验分支上的可运行闭环，不会替换正在运行的展示服务。

## 实际链路

```text
一次性、明确标记的健康校准
  ├─ 两个有界的原 perf 健康窗口，构造原差分所需 normal_profile
  └─ eBPF 聚合统计的健康基线
                  ↓
正常运行：只有 eBPF sentinel，无 perf、无堆栈、无逐 SQL 探针
                  ↓
相对健康基线，连续观察到资源/活动量异常
                  ↓
限定数据库、PID / 出生时间、时长、采样频率、输出预算
                  ↓
原 perf record → script → 原 FlameGraph → 原函数统计
       + pg_stat_database / bgwriter / locks / host
       + 活动 SQL / wait_event / pg_stat_statements
                  ↓
关闭 perf，保留轻量 sentinel；执行原差分与源码关联
                  ↓
原模型 → 原候选校验 → 安全域内临时应用 → 原业务 TPS 复测 → 恢复
                  ↓
incident 完成 / 失败 / 预算超限 → 释放门控 → 冷却 → 继续轻量监控
```

重采集结束后就关闭 perf，不为了等待模型响应继续采集。
模型/复测回调完成之前 incident 门控仍然占用，防止重复启动。
初始健康 perf 校准是显式例外，不把这两个窗口冒充“正常期永不采样”。
已有可信健康参考时，生产控制器可以复用参考；当前 lab 会重新采集。

## 常驻探针为什么不是之前的热路径

`hybrid_sentinel.py` 只对**选中 PG12 集群的 stats collector 进程**
挂 `recv` 入口/返回探针，在内核解析已经收到的原生 PG 统计消息：

- 事务提交/回滚；
- 元组返回/抓取、逻辑块获取/命中；
- 数据块读写耗时；
- 临时文件和死锁事件。

普通统计本来就由 PG12 按批上报；本实验不增加 SQL 轮询、不强制报告，
也不修改数据库源码。启动时查询 OID / data_directory、发现目标进程，
以及告警后发现目标 backend，属于控制面，不参与告警判定。

**没有常驻 PortalRun / ExecutorRun、逐元组、逐 ReadBuffer、每次 WAL flush、
调度全局 tracepoint、堆栈、SQL 文本或 pg_stat_statements 视图轮询。**

本机 glibc 2.31 的 `recv` 实际可由 libpthread 提供。实现检查目标进程
映射，挂到 libc/libpthread 中存在的实现，避免“探针附着成功但永远为零”。
消息 ABI 由指定 PG12 自身头文件编译获取，PG12 之外直接拒绝，不猜偏移。

这是一种 **PG12 原生批量事件 + eBPF 观测** 的实现，并不是纯内核、
跨数据库通用的异常检测器。聚合消息没有业务角色、SQL 文本或 query ID。
因此这里的事务速率是数据库事务速率，不冒充正常业务 TPS。

### 通用告警规则

所有六场景共用相同规则，检测器没有场景名、压力开始时间或压力进程标签：

- 默认 2 秒一个窗口，6 个健康窗口校准；
- 提交、元组、逻辑块和读写耗时的速率，相对健康中位数超过
  `max(1.6 × 中位数, 中位数 + 6 × MAD, 中位数 + 最小增量)`；
- 同一信号连续至少 2 个窗口才升级；
- 数据库事务速率低于基线 60%，还需持续至少 10 秒；
- 临时文件字节速率 > 1 MiB/s、死锁，也需连续窗口确认。

事务下降使用较长持续时间，是为了不把短暂统计延迟/空闲/主机停顿立即
升级。实验保留了第一版 4 秒下降规则在无额外压力时触发的记录；
它观察到了实际计数下降，不能直接称为检测错误，更不能删掉记录冒充
“正常期零误报”。10 秒规则是功能测试前的全场景统一策略修订，
不是按某个调优收益反复选规则。

**告警只表示“值得打开诊断的变化”，不证明业务 SLO 已违约或根因成立。**
合法负载增加也可能告警，纯 off-CPU 延迟、低活动量和未反映到这些统计的
异常可能漏检。精确误报/漏报率仍需独立标签与更长正常轨迹验证。
PG12 UDP 统计到达前可能丢失；本实现不能观测发送端丢失，不声称零丢包。
若解码/读内存出错、PID 被复用、健康基线没有真实活动，则失败并报告，
不把缺失当零，不用场景名或定时器伪造 eBPF 告警。

## 为什么告警后不只是一个 perf 命令

perf 解决原函数/调用栈证据的差异，但不会自动生成数据库全局计数、
SQL 统计或锁等待。因此告警后同时读取与原 exporter 相同的 PG 源视图，
保留原 `build_sysinsight_input`、差分、静态源码证据、模型、候选解析、
临时应用和真实 TPS 复测流程。

| 原来发现的缺口 | 本入口如何恢复 | 何时启用 |
|---|---|---|
| CPU 栈的采样事件/权重差异 | 不另造近似，调用原 perf + 原后处理 | 健康校准 / 告警窗口 |
| 数据库全局 counters，而非 backend 局部值 | 原 exporter 同源的 PG 全局视图 | 告警窗口 |
| 慢 SQL / 调用数 / 块访问统计 | 原 pg_stat_statements 观察入口 | 告警后查询 |
| CPU、内存、磁盘等 host 上下文 | 真实窗口的 /proc 观测 | 告警窗口 |
| 原诊断和调优消费接口 | 原 canonical input、源码关联、模型、校验与 TPS | 告警后 |

没有部署/查询 Prometheus；数据运输直接使用同一 PG 源视图。
活动 SQL / wait_event 原始快照会保存，但**不声称原模型已消费每个原始字段**；
原模型实际的紧凑输入和慢 SQL 入口保持原样。

`pg_stat_statements` 为恢复原证据，在 lab 快照中预先启用。它本身仍会在
正常期累计统计，**只是不再由外部采集器常驻查询**；本实验没有证明
“正常期除了 eBPF 之外数据库绝无统计开销”。若现场没有此扩展，
不能靠打开 perf 追溯此前不存在的 SQL 历史，也不会自动重启生产库补装。
原可选 SHAP 资产/质量门控也没有被伪造或绕过。

## 预算与失败处理

`hybrid_perf.py` 在**独立、仅本次拥有的进程组**调用原
`sysinsight_live_chain._capture_perf`，不重写原采样和函数抽取算法：

| 预算 | lab 默认 | 可配置上限 |
|---|---:|---:|
| perf 范围 | 当前数据库的显式 backend PID + birth | 16 PID |
| 一个窗口 | 15 s | 60 s |
| 采样 | 300 Hz / backend | 1,000 Hz |
| 一次输出 | 128 MiB | 512 MiB |
| 额外处理墙钟 | 30 s | 120 s（采集器 API） |
| 并发 incident | 1 | 始终为 1 |
| incident 次数 | 1 | lab 最多 10 |
| 冷却 | 30 s | lab 最多 600 s |

单文件 `RLIMIT_FSIZE` 强制限制；总输出每 50 ms 检查，**不是字节精确的
文件系统配额，可能有检查间隔内的超调**。超时、超量、异常、中断会停止
本次 perf / sleep / 后处理子进程；不使用 `pkill perf`，不动别人的采集。
采集未完成或未确认清理，拒绝继续模型。controller 的 finally 释放门控，
回到 resident；冷却和次数预算阻止持续压力导致无限重采集。

目前仍是有界实验控制器，不是具备重启恢复、HA、跨集群调度、
长期日志轮转的生产守护进程。常驻端本身的开销/长期输出预算需另外验收。

## 可运行入口

从仓库根目录，使用原源码、profile 和依赖目录的环境变量：

```bash
export SYSINSIGHT_SOURCE_ROOT=/path/to/original/WorkloadTune
export SYSINSIGHT_PYDEPS=/path/to/original/python-dependencies
export SYSINSIGHT_PROFILE_ROOT=/path/to/original/db_profiles
export POSTGRES_SOURCE_ROOT=/path/to/postgresql-12-source
export PYTHONPATH="$SYSINSIGHT_PYDEPS${PYTHONPATH:+:$PYTHONPATH}"

# 不调用模型：六场景自动 eBPF 告警 → 原 perf/PG/SQL → 原差分和输入
python3 -m experiments.ebpf hybrid-lab \
  --output results/hybrid-collection \
  --stackcollapse /path/to/FlameGraph/stackcollapse-perf.pl

# 再执行真实原模型和 TPS 闭环；私有凭据仅由启动器通过环境给子进程
PYTHONHASHSEED=0 python3 monitoring/codex_api_launcher.py \
  --config-dir "$HOME/.codex" -- \
  python3 -u -m experiments.ebpf hybrid-lab \
    --output results/hybrid-tuning \
    --stackcollapse /path/to/FlameGraph/stackcollapse-perf.pl --tune
```

`--scenario tp_order_status_hot` 可先验证没有 spill 的 CPU/读类场景。
`--template /path/to/stopped-snapshot` 可复用停机物理快照；
实验总是复制后启动，不启动/写入该模板，不连接生产库。
没有模板时创建 5 仓 fixture 和带原 SQL 统计的物理快照。

主实现：

- `hybrid_sentinel.py` / `pg_stats_layout.c`：BPF 接收统计、ABI / PID 验证；
- `hybrid_policy.py`：健康基线、持续异常、冷却/并发/次数预算；
- `hybrid_controller.py`：可复用的真实告警→采集→诊断回调→恢复控制器；
- `hybrid_perf.py`：原 perf 的有界进程组监督；
- `hybrid_lab.py`：原六场景、物理克隆、真实模型与 TPS 复测。
- `hybrid_report.py`：只读核验阶段顺序、原输入覆盖、清理与收益门槛。

`python3 -m experiments.ebpf.hybrid_report --input results/hybrid-tuning`
会写 `hybrid_audit.json`，分别统计采集贯通、模型/TPS 贯通和有效收益，
不会把三个计数混成一个“成功率”。

## 验收边界

1. 先验收 eBPF 能自动触发并恢复原证据、原模型与应用/复测/恢复。
2. 再验收同数据、同负载、同权限下 TPS 收益是否稳定可比。
3. 最后单独做“无采集 / sentinel / 触发窗口”开销 A/B。

不能把 6/6 自动触发叫作 6/6 调优收益成功；也不能因为已经改用原 perf，
就跳过原模型随机性、权限域、压力可比性或 TPS 漂移门控。
历史双臂未达收益等价的结果仍见 [PAIRED_TPS_VALIDATION.md](PAIRED_TPS_VALIDATION.md)。
