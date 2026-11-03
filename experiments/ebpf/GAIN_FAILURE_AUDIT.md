# 未达收益门槛的原因追查：2026-11-03

## 结论

**和当前 eBPF 接入实现有关系，不能再用“采到了样本、六场景链路跑完”
来排除 eBPF 的影响。** 已确认常驻探针组合存在明显观察者效应，候选复测
额外执行了 SQL，正常/异常 CPU profile 的探针配置也不一致。
但不能把五个未过门槛的场景全部归因于 eBPF，更不能据此判断 eBPF 技术不可用。

上一轮 `ebpf-equivalence-six02` 的 6/6 是**流程贯通**，不是诊断证据等价、
稳定收益等价或低开销验收。Payment 的单轮 +6.53% 也没有稳定收益的因果证据。
原始 JSON/日志保持不变；本报告补充审计与新对照，不追改历史结果。

## 1. 实测：只开常驻探针就会影响业务

使用新建、最终销毁的独立五仓库 PG12 集群，原 Order Status SQL，
business 2 客户端、offered 1600 TPS；pressure 原客户端数、offered 2500 TPS。
每条件 12 秒、4 次；逐轮反向/轮换执行顺序。所有条件均**不启用 CPU
profiler 或细粒度资源探针**。本实验是原候选的明确回放，不是新的模型调用。

业务 TPS 采用原启动剔除/steady 中位数逻辑，下面再对四次 trial 取中位数：

| 参数与应用方式 | 无本实验 BPF 探针 | 只有常驻探针组合 |
|---|---:|---:|
| 原参数 | 1595.50 | 1322.50 |
| 同一原模型候选，连接启动时设置一次 | 1568.50 | 1349.55 |
| 同一候选，旧的每事务重复 SET | 1299.75 | 1010.25 |

同参数下，常驻组合对应的 TPS 下降分别约 **17.11%、13.96%、22.27%**。
原参数每轮的对应下降为 44.66%、18.87%、14.14%、14.66%，第一轮明显更噪；
不能把 17.11% 当成通用 eBPF 开销常数或带置信区间的精确结论。
压力 steady TPS 在全部 24 个 trial 中为 2520.90–2525.25，未靠减小外压取得改善。
业务聚合不变量未变，常驻探针均完成清理。

这里测的是**当前常驻探针组合**，不是 eBPF 机制本身的固定开销，也不是
CPU 栈采样开销。`PortalRun` 入口/返回覆盖每条 SQL，是常驻热路径；
“只聚合、不逐条输出”并不消除入口/返回探针触发成本。
本实验没有进一步分拆每种常驻探针的独立成本。

## 2. 已确认的候选应用偏差

旧 `equivalence.Workers` 把 session 参数的 `SET` 拼在 pgbench SQL 文件前面。
pgbench 每笔事务重复整个文件，所以 Order Status 候选的 8 个参数会增加
**每事务 8 条 SQL**；原参数基线没有这些语句。它还会增加常驻 PortalRun
探针的触发次数。这个偏差不是“候选参数无效”的证据。

新对照中，同参数改为连接启动时设置一次，开启常驻时 TPS 为 1349.55，
旧方式为 1010.25：旧方式比新方式低 **25.14%**。不启用 BPF 时也低
**17.13%**，证明额外 SQL 本身也是独立问题，不全是 BPF。

已改为经白名单、数值/布尔校验的 `PGOPTIONS`，连接建立时生效，
business/pressure 继续执行原 SQL，且不继承外部 PGOPTIONS。
真实 libpq 连接已验证 8 个候选值生效，包括 work_mem、成本参数和并行设置。
不是把 SET 去掉却忘记应用参数。

**修正偏差不等于候选产生收益：** 同一候选用新应用方式，相对本对照原参数，
无 BPF 时中位数之比为约 −1.69%，常驻开启时约 +2.05%，均不能证明稳定
超过原 5% 门槛。无 BPF 原参数已接近 offered 1600 TPS，也没有很大向上空间；
这不是无上限的系统吞吐容量测试。

## 3. 已确认的诊断输入不对称

上一轮正常 CPU profile 只开 resident + CPU/查询关联采样，
异常 CPU profile 还同时开 VFS、锁、WAL、checkpoint、临时文件、hash spill
等资源计时探针。频率和窗口相同，**不代表探针配置相同**。

原 Order Status 的 CPU 样本中，包含明确 uprobe 处理路径的比例：

- 正常两窗：2.11%、2.44%；
- 异常窗：8.28%；
- `uprobe_notify_resume`、`handle_swbp`、`uprobe_dispatcher`
  进入原差分前 20。Full Mix 和 WAL 也存在同类现象。

这些比例是**采样栈包含探针处理路径的比例，不是测得的吞吐开销百分比**。
它们证明诊断输入包含观察者路径；不能精确量化模型因此选错了多少参数。
还没有严格同负载的旧 perf / 新 eBPF 诊断质量 A/B。

已改为正常/异常用同配置采 CPU 栈，**CPU 采样/关联探针全部卸载后**，
再在独立、至多 5 秒的窗口启用异常对象的资源探针；没有简单删除资源采集。
manifest 记录比较时的 probe 配置，离线验收新增配置一致性检查；
缺少此证据的旧记录不能通过这项新检查。

真实修复后组件验证：

- 原 Order Status 正常两窗 3393 / 4369 个样本，异常 6773 个样本；
- 正常/异常 CPU 比较时 instrumentation 相同；
- CPU 卸载在资源探针附着之前；
- 独立资源采集约 5.04 秒，无通知丢失或资源 collector health 错误；
- 原差分/原匹配执行完成，返回 604 个超范围函数。

**尚未消除自干扰：** 修复后正常 uprobe 路径占比约 2.62% / 2.56%，
异常约 5.79%，`uprobe_notify_resume` 仍在前 20；
常驻与 CPU 采样自身的查询关联仍会影响被观察进程。
这是配置对齐和时序验证，不是已恢复六场景诊断/调优效果的证明。

## 4. 旧收益结果本身不稳定

| 场景 | 旧候选相对调优前 | 恢复原参数后相对调优前 | 解释限制 |
|---|---:|---:|---|
| Payment hot | +6.53% | −20.71% | 单轮过线不代表稳定收益 |
| Stock Level hot | −7.12% | −18.29% | 基线漂移，不宜全归因候选 |
| Stock Level burst | −4.36% | −4.93% | 部分 trial 内 TPS CV 约 69%–70% |
| Full mix surge | −0.47% | −33.95% | 明显漂移 |
| WAL/checkpoint | +91.21% | +82.36% | 压力完成量还增加 23.32%，原压力门拒绝计收益 |
| Order Status hot | −20.41% | −18.08% | 额外 8 SET 偏差已另行实测确认 |

旧测量每 trial 8 秒、每阶段 2 次，模型调用把 before/tuned 间隔拉长，
同一集群串行复用，宿主机也不是独占。恢复配置正确，不代表吞吐恢复稳定。
不能把明显漂移的比较简单解释为“没采到问题”或“参数没有效果”。

新增保守收益 veto：恢复阶段业务 TPS 相对 before 漂移超过 5% 时记
`inconclusive_measurement / baseline_drift`，流程仍可完成，但不记成功收益。
原 5% 收益门和原压力比较门没有降低/取消。这个 veto 只是必要条件，
**不是统计显著性检验**；尚未实现置信区间、长期负载稳定性判定或完整配对实验。

另外，部分候选仅调 effective_cache_size 或少量提交参数。
effective_cache_size 是规划器估计，不会增大真实 shared_buffers 或预留 OS cache；
这里的 fixture 数据规模、32MB shared_buffers、安全参数范围与旧演示不相同。
不能假设旧成功率应自动复现，也不能用历史候选收益替代新模型的复验。

## 复现与审计文件

```bash
# 只读旧记录；不调用模型、不启动数据库。
python3 -m experiments.ebpf.gain_audit \
  --source results/ebpf-equivalence-six02 \
  --output results/gain-audit-new

# 明确回放旧 Order Status 候选，仅用于拆分复测偏差与常驻探针影响。
python3 -m experiments.ebpf.gain_audit \
  --source results/ebpf-equivalence-six02 \
  --output results/gain-controls-new --controls --seconds 12 --repeats 4

# 原资产/依赖环境按 EQUIVALENCE.md 配置；不需要模型凭据。
python3 -m experiments.ebpf.gain_audit \
  --source results/ebpf-equivalence-six02 \
  --output results/gain-capture-new --capture-check
```

本次原始数据：

- `results/ebpf-gain-audit-20261103/audit.json`
- `results/ebpf-gain-audit-20261103/control.json`，各 trial SQL/log/result
- `results/ebpf-gain-capture-check-20261103/capture-check.json`
- 后者的 `workflow.json`、正常/异常 profile 和独立资源窗口 manifest

只读核对的 PostgreSQL 12 原始文档：
`doc/src/sgml/ref/pgbench.sgml`、`doc/src/sgml/libpq.sgml`、
`doc/src/sgml/config.sgml`（effective_cache_size）。未变更原演示数据库和服务。

**后续验收边界：** 先降低/替换常驻热路径观测，并把必要资源证据真正接入模型，
再在稳定、同条件下完成六场景新候选复验。本轮没有重新调用六场景模型，
没有证明六场景都达到原收益，也没有把当前实现宣称为论文最终轻量版本。
