# 先做功能等价：eBPF 接回原六场景 TP 调优

本入口取代“先压缩采集，再解释为什么识别不了”的实现顺序。
先恢复原系统级诊断和业务调优链，之后再单独研究轻量化。
旧的单 PID / 49Hz / 2 秒六场景 smoke 仅保留为历史实验，
不是此入口的验收标准。
真实六场景结果及逐项验收见
[EQUIVALENCE_VALIDATION.md](EQUIVALENCE_VALIDATION.md)：
6/6 功能闭环完成，与数值收益成功分开统计。

## 功能链路

```text
持久 resident：显式 PID/出生身份，PortalRun 活动/完成聚合
              + ProcSleep / CreateCheckPoint / BufFileCreateTemp 语义事件
              不常驻抓 CPU 栈
                           ↓
通用活动/长执行阈值触发，或显式选定对象（不以 spill 为唯一触发）
                           ↓
全部选定业务/压力 backend 的短窗 eBPF CPU 栈
              + 目标 backend/checkpointer 上的有界资源探针
                           ↓
先停止 CPU 采样，再卸载细粒度探针；resident 继续
                           ↓
原 DBEnv.get_perf_function_range
 → 原 compare_file_sample_rate（真实正常 profile 对照）
 → 原 find_top_and_matched_functions
 → 原 PostgreSQL 源码证据
                           ↓
原 ParameterLibrary / LLM_ACQ / LLM_DIS_SM
 → 原候选解析、结构及真实 PG 校验
                           ↓
原六场景 pgbench 脚本，固定 offered 外压下的业务 TPS
 → 原 TemporaryPostgresConfiguration
 → 临时应用 / 复测 / 恢复后复测 / 设置与业务数据不变量检查
                           ↓
原稳定 TPS 汇总与压力可比性检查
 → 记录“有收益”或“闭环完成但无收益”
 → 返回 resident，实验结束才整体卸载
```

## 与原实现对齐的地方

### 1. 恢复正常/异常差分，而不是新写一个 spill 判定器

正常与异常均使用相同的 eBPF on-CPU 采样配置。
默认每窗 **15 秒、300Hz、全部明确授权的业务/压力 backend**，
正常参考默认采两窗，形成每函数 min / max / average 包络。
不再受旧 smoke 的一 PID、49Hz、5 秒限制。

eBPF 使用 task-clock perf-event FD 作为触发器，不运行 `perf record` 或
`perf script`。它采集的是真实 user/kernel CPU 栈，不是函数入口次数。
Linux perf-event 是内核机制，使用该机制运行 BPF **不等于退回 perf 命令采集**。
这里用的是 on-CPU task-clock 样本，不能称为硬件 cycles；与旧 perf 默认
硬件采样事件也不是逐样本数值等同。正常/异常两侧的事件配置保持一致。

多 PID 使用多个 task-bound、`cpu=-1` 的 FD，统一注册并统一关闭。
不能重复调用旧版 BCC 会覆盖 FD 注册表的封装，导致只关闭最后一个 PID。
符号化前停止采样，完成后关闭本轮全部采样与 SQL 关联探针。

原函数转换、异常对比和函数—参数匹配直接调用既有源码，不复制出另一个
相似算法。没有把调用次数填进采样率列，也没有将旧 perf 正常基线与新
eBPF 异常 profile 混在一起。

保留原算法的一个实际边界：它忽略正常 profile 从未出现的函数。
本实现另外审计这些 anomaly-only 函数，**不静默补零改变原算法**。

### 2. 通用触发和持久 resident

原演示 Prometheus 规则是“活跃会话数 ≥ 3”，不是锁、WAL、spill 的根因规则。
新 resident 以 PortalRun 的活动占用作通用阈值，并支持长执行阈值；
无须等到临时文件或锁事件出现，读/CPU 场景同样能够进入差分诊断。

PortalRun 占用不包含完整 parse/plan 等所有 `pg_stat_activity` 活跃状态，
所以这里声明的是**通用触发功能对齐，不是原 SQL 状态量的逐位相等替身**。
显式选择是独立入口，不在自动触发失败时悄悄降级使用。

resident 在模型调用、候选验证和复测期间不退出。
实验 worker 创建/结束时动态增加/撤销 PID 授权；checkpointer 单独授权。
退出后使用本 owner 保存的 uprobe 句柄卸载，不依赖已退出 PID 的符号查找。
查询边界探针的常驻成本在功能等价阶段先接受，**尚未宣称低开销**。

语义事件在内核累计，异常通知合并冷却，采集时及时消费；
常规计数不按通知数量反推。采样不完整、map 容量或通知丢失显式记录。

### 3. 论文中的按需函数/资源观测

异常诊断中临时附着 VFS 读写、实际锁等待、WAL flush/sync、
checkpoint、临时文件读取、hash spill 等针对性探针。CPU 差分采样时正常/异常
两侧使用相同的探针配置，CPU/查询关联采样卸载后才启动独立至多 5 秒的资源窗口，
避免只在异常 profile 中采到额外资源探针的自身开销。
后台 checkpoint 不能只观察客户端 PID。
有 PID/出生身份、执行开始时间和可获得的 PG query ID 时做 SQL 级关联；
执行器外的资源保留 backend scope，不靠 SQL 文本猜测归属。

CPU 样本额外记录所在 ExecutorRun 的时间/可用 query ID；
关联不完整和嵌套深度溢出单独标记，不把缺失身份当成精确关联。
PID/出生时间与时间窗是关联基础，PG12 query ID 可能不可用。

这是有限窗口的真实细粒度观测；VFS 字节不等于物理磁盘流量，
CPU 栈不等于 off-CPU 等待，函数时间也不能无条件相加成 SQL 总时间。
本阶段不训练/宣称已有论文 Lasso/SHAP/多分类模型，也没有重建全部 LLVM 分析。
复用现有源码关联资产，不伪装成完整论文所有模型都已实现。

### 4. 调优目标恢复为六场景的业务 TPS

使用 `FOCUS_SCENARIO_IDS` 中原来的六个场景、原来的工作负载脚本，
以及 `scripts/tpcc_schema.sql` 生成的独立五仓库数据：

- `tp_payment_hot`
- `tp_stock_level_hot`
- `tp_stock_level_burst`
- `tp_full_mix_surge_high`
- `tp_wal_checkpoint`
- `tp_order_status_hot`

business worker 与外部 pressure worker 分开。
默认 offered rate 为 business 1600 TPS、pressure 2500 TPS；
同一轮的候选前、后、恢复阶段不得改变配置的外压或客户端数。
这是实验明确配置，不声称数值与每个旧演示记录的压力相同。

评分复用原 `_tpcc_tps_summary` 的启动剔除/中位数逻辑，以及
`_tpcc_comparison` 的压力可比性检查。来源为真实 pgbench 进度/完成日志，
不是 ExecutorRun 完成数，不是压力 TPS，也不是单条 SQL 耗时。
采样阶段不直接作为收益基线；另做前、后、恢复的无细粒度采样复测，
各阶段仍保留同样的 resident。
session 参数通过 PGOPTIONS 在连接启动时设置一次，不在事务 SQL 中重复 SET。
原压力比较门和 5% 收益门之外，恢复后 TPS 相对 before 漂移超过 5% 的候选
标记测量不确定，不计成功收益；这不是统计显著性证明。
已发现的旧复测偏差与当前 resident 的实测影响见
[未达收益门槛追查](GAIN_FAILURE_AUDIT.md)。

### 5. 候选必须来自本次原模型

模型看到原差分结果、真实源码证据、当前参数与明确的 TPS 目标。
参数域由原匹配结果与显式安全政策相交，数值边界是资源限制，
不是预设修复值。没有历史候选回放或固定 work_mem 兜底。

临时配置复用原执行器；允许受限 session/reload 参数。
不允许更改端点、降低持久性、改 SQL、DDL 或重启原数据库。
此隔离入口也没有实现重启参数的实验应用；需要重启的参数不计为已迁移。
`fsync`、`synchronous_commit`、`full_page_writes` 等不在候选白名单内。

复测后恢复设置，并检查仓库、地区、客户、库存、订单、订单行等业务聚合
不变量。检查不是全库形式化语义证明，且保存点回滚并不撤销 WAL、物理 tuple
变化或序列计数，这些不在“业务内容不变量”的承诺中。

## 运行

先准备 [PIPELINE.md](PIPELINE.md) 中原 SysInsight Python 依赖、PG 源码、
profile 和私有模型 API 环境。没有凭据或原资产时失败，不伪造候选。

```bash
# 原六场景。创建并最终销毁一个独立临时集群，不接现有数据库。
python3 -m experiments.ebpf equivalence-lab \
  --output results/equivalence-new

# 单个场景；自动活动阈值是默认入口
python3 -m experiments.ebpf equivalence-lab \
  --scenario tp_stock_level_hot \
  --output results/equivalence-stock-new

# 明确选择目标，不冒充自动告警
python3 -m experiments.ebpf equivalence-lab \
  --scenario tp_stock_level_hot --trigger-mode selected \
  --output results/equivalence-selected-new
```

测试集群只开私有 socket、20 个连接上限，不修改原服务。
原脚本是 rollback-protected 的 TP 交易模拟，数据生成器不是认证 TPC-C。
运行会实际消耗宿主机 CPU/I/O；共享宿主机不适合直接发表性能结论。

## 如何判定完成

- `pipeline_completed=true`：确有本次原模型候选完成应用、业务 TPS 复测、
  参数恢复、恢复后复测和不变量检查。
- `tuned_and_restored`：另外通过原压力可比性和收益门槛。
- `completed_without_validated_gain`：闭环成功执行，但没有验证出足够收益；
  **不算调优收益成功**。
- `failed` / `no_executable_candidate`：记录具体失败，不能计成等价闭环完成。

关键证据：

```text
resident/manifest.json、events.jsonl
normal-*/profile/{manifest.json,samples.json,profile.folded}
normal_profile.csv
anomaly/profile/、anomaly/detail/
source_detection.json、sysinsight_input.json
sysinsight_api/、candidate_validation.json
business-baseline.json、pressure-before.json
candidate-*-tuned.json、candidate-*-restored.json
tuning_result.json、workflow.json、result.json
```

实现不修改原在线 bridge/Portal 的部署路径。当前等价范围是
**六个 TP 系统级参数调优场景**，不是 DREAM SQL/模型调优的全路径替换。
