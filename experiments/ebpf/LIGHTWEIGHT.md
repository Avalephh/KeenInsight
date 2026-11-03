# 轻量采集与性能影响：第二轮实测

日期：2026-11-03。分支：`experiment/ebpf-paper-20261103`。
第一版结果见 `VALIDATION.md`；本页是 v2 的轻量化修订与后续对照，
不能用第一版单 SQL 的 4.44% 数字代替这里的不同负载实验。

## 核心结论

1. **eBPF 不是零开销。** 成本取决于探针触发频率、每次执行的 BPF 操作、
   是否需要返回探针、是否逐次发送消息，而不只是用户态采集进程的 CPU。
2. **聚合消息只能减少消息路径成本，不能消除已附着探针的触发成本。**
   约 13 微秒的短 SQL，即使没有资源探针、只保留 SQL 执行边界，
   本机吞吐损失仍接近 40%。不能用这种模式默认全量监测极短 SQL。
3. **必须选择低 fan-out 的语义事件。** 相同排序落盘负载，对每次临时写入、
   spill 元组等高频函数附着，比只观测临时文件创建更昂贵。
4. **信息量与开销必须同时报告。** `system` scope 代价较小，但不提供每次
   SQL 的边界/耗时/正常 QPS。不能把“少采了数据”称作“同等信息量无损提速”。

## 实现变更

默认常驻：

```text
--profile events --detail-level lightweight --scope system
```

- 只为显式授权 backend 的 `BufFileCreateTemp` / `XLogFlush` 附着入口探针。
- 不采逐条 SQL 起止、不拷贝 SQL 文本、不采执行栈、不挂全局 sched_switch。
- 不为“只要次数”的事件附着返回探针，不读未使用的用户态结构。
- 在内核计数，周期导出；临时文件创建等异常立即通知，连续通知有冷却合并。
  **合并的是通知，不是原始计数。**
- 临时文件创建只是异常候选，不等于“这个 SQL 一定有错”；
  正常负载也可能合法产生临时文件。

按需 SQL 关联：

```text
--profile events --detail-level lightweight --scope query
```

- 增加 ExecutorRun 入口/返回以及错误清理。
- 普通短 SQL 不逐次发送开始/结束消息，在内核累计完成数、总耗时、最大值、
  log2 延迟直方图；异常、长 SQL 才发送详细执行记录。
- 完成数不是抽样，但持续开启边界探针的成本仍存在。
- 纯入口计数模式不声称知道函数耗时、返回字节数、on/off-CPU。
  缺失字段明确移除，不用零伪装。
- 原 `selected/full` 对照仍保留用于消融，而不是作为生产默认。

这符合论文“只采需要的数据、正常聚合、异常立即上报”的方向，但当前有明确差距：
默认 system 模式不能持续重建所有 SQL 的资源轨迹；自动进入/退出细粒度模式、
Lasso 真实训练和成本反馈控制尚未完成。晚开启的探针不能追溯过去的 SQL 开始。

## 测试方法

- PG12.22 / Linux 5.4 / BCC 0.12，同第一版机器。
- 全部业务在独立、测试后销毁的 PostgreSQL 集群；不接原 keeninsight 数据库。
- 已有展示服务保持运行，不停止它们，因此宿主机仍存在噪声。
- libpq 持久 Unix socket 连接；单客户端、闭环发请求；
  客户端直接测每次请求的墙钟延迟，没有额外 Python 数据库驱动。
- 对照中所有模式都由同一客户端记录耗时，不额外启用 SQL 日志、perf 或统计扩展。
- 每个 trial 5 次预热；模式顺序随机；测量开始后才统计 CPU；
  编译、附着和清理不混进稳态 SQL 计时。
- 默认 synchronous_commit / fsync 保持开启，JIT 与并行查询关闭。
- `cpu` 用 16MB work_mem，`spill` 用 64kB，避免把 generate_series
  的 CPU 测试意外变成临时文件落盘。
- 性能不是用 eBPF 自己测出的 SQL 时间来评估，而是独立客户端 QPS 和延迟。
- DB/collector CPU 从 `/proc` tick 计数读取，短 trial 分辨率有限。
  collector CPU 显示 0 不等于零消耗。

吞吐损失定义：

```text
QPS loss % = 100 × (1 − QPS_collected / QPS_baseline)
```

这是相同轮 baseline 的配对变化。它和延迟增幅百分数不同；
例如吞吐下降 40% 不等于延迟增加 40%。

### 消融实验：每组合 3 轮、1.5 秒

结果：`results/ebpf-light-ablation02/overhead.json`。
全部 72 个 trial（3 负载 × 8 模式 × 3 轮）完成。

| 模式 | 每次 SQL 边界 | 消息策略 | 资源模式 | CPU hook | 资源探针 |
|---|---|---|---|---|---|
| baseline | 无 | 无 | 无 | 无 | 无 |
| full | 有 | 逐次输出 | 入口+返回 | sched | 第一版 selected |
| aggregate | 有 | 正常聚合 | 入口+返回 | sched | 同 full |
| counts | 有 | 正常聚合 | 只入口计数 | sched | 同 full |
| no_sched | 有 | 正常聚合 | 只入口计数 | 无 | 同 full |
| events | 有 | 正常聚合 | 只入口计数 | 无 | 临时文件创建+WAL 刷盘 |
| boundary | 有 | 正常聚合 | 无 | 无 | 无 |
| system | 无 | 聚合/异常通知 | 只入口计数 | 无 | 同 events |

主要结果，QPS 损失的中位数：

| 模式 | 短 SQL `SELECT 1` | 排序落盘 |
|---|---:|---:|
| full | 48.94%（通知提交失败，不能当成完整采集） | 40.51% |
| aggregate | 41.25% | 41.94% |
| counts | 40.72% | 33.79% |
| no_sched | 40.90% | 35.19% |
| events | 39.98% | -0.06% |
| boundary | 39.23% | 0.75% |
| system | -0.79% | -1.44% |

负值不是采集“提升性能”，是共享环境下的时序/噪声，不能作为收益。
full 短 SQL 试验每轮存在 5.6–6.1 万次 perf 提交失败；
因此不能拿它与其他完整采集模式作等信息量比较，不能忽略健康计数。
`aggregate` 等其他列未报告采集错误；累计完成数均与实际请求数对应。

说明：

- 短 SQL 从约 1,100 bytes/query 降到不足 0.1 bytes/query 的聚合输出，
  **边界探针仍造成约 40% 吞吐损失**。
- 落盘负载减少高频探针后成本明显下降；只减少输出并不解决高频路径成本。
- `events` 的落盘输出量可能比聚合高频 profile 多，因为它把每次临时文件创建
  保留为 SQL 异常候选明细。这再次说明“输出少”和“触发开销低”不是同一件事。

### 五轮确认：每组合 2 秒

第一组：

```bash
python3 -m experiments.ebpf.overhead \
  --repeats 5 --seconds 2 --workloads short,cpu,spill \
  --modes baseline,system,events \
  --output results/ebpf-light-confirm
```

第二组增加真正 logged table 的 autocommit 写入，不用 temp table 假装 WAL：

```bash
python3 -m experiments.ebpf.overhead \
  --repeats 5 --seconds 2 --workloads wal \
  --modes baseline,system,events \
  --output results/ebpf-light-wal-confirm
```

| 负载 | baseline 平均客户端耗时中位数 | system：QPS 损失中位数 [min,max] | query events：QPS 损失中位数 [min,max] |
|---|---:|---:|---:|
| 短 SQL | 13.22 μs | 2.01% [-1.88%, 2.57%] | 40.76% [38.70%, 40.93%] |
| CPU 查询 | 1.38 ms | -0.08% [-4.90%, 9.28%] | 2.12% [-1.96%, 13.50%] |
| 排序落盘 | 4.72 ms | -0.84% [-2.13%, 2.55%] | 2.16% [-0.19%, 16.26%] |
| WAL 写入 | 0.48 ms | 1.42% [-1.22%, 2.99%] | 3.15% [0.72%, 4.98%] |

这两组共 60 个 trial；全部 capture completed，未报告 BPF 健康错误、perf
丢失或中途探针卸载。query scope 的内核完成数与客户端请求数（含预热）一致。

短 SQL query 模式的平均延迟增幅中位数是 **69.71%**，不是 40.76%；
40.76% 是吞吐损失。其 p99 增幅中位数为 52.51%。
system 模式短 SQL p99 增幅中位数为 3.35%；WAL 写入为 2.58%。
完整分位数、DB/collector CPU、输出量及配置在原始报告中。

**不能宣称所有场景开销 <1% 或 <2%。** 这些是单客户端小规模共享主机结果，
范围较宽、trial 较短、没有严格隔离和置信区间，也不是多客户端 TPCC/TPCDS。
闭环客户端延迟也不能代替开放式到达模型下的尾延迟。

## 健康验证

```bash
python3 -m experiments.ebpf.lab smoke \
  --output results/ebpf-light-verified-smoke03
```

25 项真实内核检查通过，包含原错误/游标/PID 过滤测试，以及：

- 聚合模式 53 次顶层执行与直方图计数一致；
- 普通短 SQL 不逐条输出，真实落盘仍能产生指定异常事件；
- system 模式没有伪造 SQL 身份/完成数，仍保留临时文件创建事件；
- 入口计数不伪造函数时长，关闭 CPU hook 不伪造 CPU 数值；
- 各轻量模式无报告的健康错误。

## 仍需优化和下一轮实验

1. **不是所有函数都适合“持续插探针”。** 离线选择应考虑诊断价值、
   实际调用率和单次探针成本，而不只按 Lasso 系数绝对值。
2. 以故障场景验证低频语义事件的召回、漏报和检测延迟，再决定常驻集合。
   当前只验证临时文件事件，不宣称覆盖所有故障。
3. 做按时间窗口/指定查询的细粒度启停，并报告窗口占空比、冷却、
   首次事件和已有执行的不可追溯范围。不能把内核 if 过滤当成真实卸载。
4. 在独立机器或受控资源中跑更长、多客户端负载，报告 TPS/QPS、p95/p99、
   DB CPU/内存、collector CPU/内存、消息量、丢失和检测质量的置信区间。
5. 当前 BCC/Python 原型常驻 RSS 约 **85–86 MiB**，并不是内存已轻量化的最终实现。
   后续需要区分编译器/驱动与 BPF map 内存，再考虑更精简的部署实现。
6. 成本模型必须绑定实际 scope、计数/计时模式、消息策略与 CPU hook，
   不能把 timed 探针校准结果用于入口计数模式。

现在得到的是可验证的开销边界和可运行的轻量路径，不是“eBPF 天然无影响”的结论。
