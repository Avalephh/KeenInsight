# 首次本机验证：2026-11-03

这是实现验证，不是论文的完整效果/低开销结论。
本页记录 **v1 历史版本**；后续 v2 默认模式和多负载性能结论见
[LIGHTWEIGHT.md](LIGHTWEIGHT.md)，不要把本页的单负载数字作为新版本验收。
最新的采集→识别→原 SysInsight→临时调优链路另见
[PIPELINE.md](PIPELINE.md)；本页“未迁移”的结论只描述当时版本。

## 版本和环境

- 分支：`experiment/ebpf-paper-20261103`；
  基线：`54026fc41c8c47eca28f3e4eae1fc1fc5e22c0e6`。
- 设计输入：用户提供的 `KeenInsight_VLDB_Demo.pdf`，
  SHA256 `1ffa252559f7a3d772762e060edff4b2291e69b2c72a36409bd5a2be8d9339b3`；
  对应第 2.1–2.2 节。
- Linux `5.4.0-216-generic`，x86_64，BCC `0.12.0`，clang `10.0.0`。
- PostgreSQL `12.22`，二进制 SHA256
  `a2645922d96ae2104ed52307ba086b377fbf3987d1e14b2b6f71c1a80aca7f14`。
- 独立临时 PG 集群，Unix socket，无 TCP 监听；32MB shared_buffers；
  并行查询和 JIT 关闭；未安装展示系统扩展。测试结束停止并删除集群。
- 展示系统服务未启停、原 keeninsight 数据库未修改。因此采集链路无展示依赖，
  **但宿主机不是完全无其他负载的干净性能测试环境**。

## 已执行检查

```bash
bash scripts/test.sh
python3 -m experiments.ebpf doctor
python3 -m experiments.ebpf.lab smoke \
  --output results/ebpf-smoke-20261103-verified
python3 -m experiments.ebpf.lab benchmark \
  --repeats 3 --queries 100 --batches 20 \
  --output results/ebpf-ab-20261103-verified
```

### 单元/回归

共 116 项测试，112 项通过、4 项显式 skip；其中新增 21 项 eBPF 契约/选择器/安全测试。
回归测试不加载 BPF，真实内核验证通过显式 lab 命令进行。

### 真实内核 smoke

17 项检查通过，包含：

- 真正完成的 8 个 ExecutorRun span、1 个运行错误中断 span；
- 34,186 次真实 `BufFileWrite` 调用；
- 相同集群内未授权 backend 的事件被排除；
- 默认没有 SQL 原文，执行身份不依赖 SQL 文本猜测；
- 显式打开原文采集后能捕获测试 SQL 前缀；
- 运行错误后新的查询仍正确完成；
- 执行器外的 WAL 刷盘保留在系统级 backend 统计；
- backend 统计不冒充 SQL 统计；
- 真实动态卸载了 4 个非核心探针；核心探针保持；
- 卸载后未采集的指标不被报告为测量值零；
- backend 退出前输出系统级终值；
- 本次未报告 perf 通知丢失、map 容量错误或用户内存读取错误。

即时通知测试将 `event-bytes` 人为设为 1，以验证路径，不是生产告警门槛。
自适应 smoke 使用极低 collector CPU 预算**强制触发卸载**，验证的是机制，
不是已经校准的开销反馈控制。
健康计数为零不是全路径无损的证明，kretprobe missed-return 等仍需补齐。

### 最小性能对照

每个 trial 在同一个持久连接中执行 2,000 次：

```sql
SELECT sum(g) FROM generate_series(1,20000) AS g;
```

每轮四种模式随机排序，3 次重复；每个 trial 先预热 5 次。
表中增幅是**每轮相对该轮 baseline 的墙钟时间变化**，最后一列为其 median。

| 模式 | 第 1 轮 | 第 2 轮 | 第 3 轮 | median |
|---|---:|---:|---:|---:|
| eBPF core | +5.95% | +5.92% | +1.84% | +5.92% |
| eBPF selected（未训练 bootstrap） | +4.44% | +8.05% | +4.20% | +4.44% |
| eBPF all candidates | +8.69% | +12.90% | +11.42% | +11.42% |

该次全部 capture 的 manifest 均为 completed，未报告丢失/健康错误。
这只说明：在这一 CPU 微型负载上，选择性附着比附着全部候选的额外成本小，
**尚未达到可以直接宣称“低于 1%/2%”的结果**。
shared-host 噪声、单一 SQL、只有 3 轮且没有置信区间，不能泛化为生产开销。
core/selected 的细微次序也不能据此作显著性结论。
不把查询/事务数、客户端协议延迟和 ExecutorRun span 时长混为一个指标。

## 尚未验证/尚未完成

- 没有真实训练的 Lasso 根因贡献模型；训练器和模型加载是可执行入口，
  正式实验需独立标注数据、按 workload 分割训练/测试。
- 没有真实校准的 per-probe 成本模型；默认压力信号不等于总 DB 开销。
- Kafka 路径未连接真实 broker 验证 ACK、故障恢复和积压控制。
- 未实现并行 worker 自动归属、plan-node/operator ID、客户端完整 SQL 延迟、
  完整物理 I/O / 峰值内存 / 网络线速数据。
- 未替换原在线 SysInsight/DREAM 的输入适配层，不声称端到端诊断调优已迁移。
- 未做停止原展示采集的整机对照、TPCC/TPCDS 多场景与多轮开销实验。

原始结果在仓库被忽略的 `results/` 中，不提交数据库、SQL 运行记录或二进制证据。
本次数字包含最终的启动身份复核、系统级统计和 CPU 聚合快照。
后续修改应重新执行真实内核 smoke 和 A/B，不复用历史数字当作新版本验收。
