# 同条件 perf / eBPF 采集与 TP 调优收益对照

本入口检验**只替换采集方式后，原诊断/模型生成的候选能否产生接近的业务 TPS
收益**。不是把六场景“流程跑完”当成六场景收益成功，不预设两路必须相等，
不回放旧成功候选冒充本次模型生成结果。

## 固定的共同条件

1. 一次生成原五仓库 fixture，用 `pg_basebackup` 生成同一物理快照。
   每场景、每采集路径的诊断数据库均从该快照重新复制。
2. 收益测量再使用**全新快照副本**和相同的无探针 warmup。
   不把诊断阶段不同的采集耗时、WAL/死元组积累或模型等待时间带入收益基线。
3. 同一份原 business/pressure SQL、随机种子 1729、business 2 客户端、
   各场景原 pressure 客户端数、offered business 1600 / pressure 2500 TPS。
   哈希、数据业务聚合、初始配置、模型配置哈希和权限范围保存到审计文件。
4. 正常两窗、异常一窗，每窗 15 秒、每 backend 300Hz，均使用 hardware cycles。
5. 两路均按 `sum(sample_period)` 生成 folded 权重，再执行原
   `DBEnv.get_perf_function_range / compare_file_sample_rate /
   find_top_and_matched_functions`。保留原算法递归帧重复累计和忽略
   anomaly-only 函数的行为，不为了看起来接近而另造差分算法。
6. 同一原 `ParameterLibrary / LLM_ACQ / LLM_DIS_SM`、provider/model 配置、
   候选预算（原选择器选中的一个有效候选）、源码知识、参数域与数值边界。
   两路**不是各按 matcher 结果另设不同权限**，共同允许
   `POLICY ∩ PG12 profile.constraints`；匹配结果仅作为诊断证据。
7. 同一 PG12 配置、32MB shared_buffers、初始关闭 JIT/查询并行；
   两路均启用 pg_stat_statements 和 track_io_timing，以提供原 SQL 统计来源。
   不允许降低 fsync / synchronous_commit / full_page_writes、重启、更换端点、
   改 SQL 或 DDL。不是与旧演示中可能降低持久性的涨幅做跨环境比较。
8. 参数通过 PGOPTIONS 在连接时应用一次，global reload 参数复用原
   TemporaryPostgresConfiguration，复测后核验恢复。

相同模型配置不意味着响应确定：原模型调用独立完成，保留真实请求/响应。
单次候选采集不能消除模型随机性，不强制两路生成同一个参数来制造等价。

## 采集路径与范围

### 原采集语义参考臂

CPU 栈直接使用原 `sysinsight_live_chain._capture_perf`，包括 perf
record/script、原 stackcollapse-perf 和原函数转换器。

数据库统计直接读取**原 exporter 的来源视图**：pg_stat_database、
pg_stat_bgwriter、pg_stat_activity / pg_locks，以及 pg_stat_statements 的 SQL
统计。继续通过原 `build_sysinsight_input` 构造模型输入。
没有查询原生产数据库的 Prometheus，也没有新建/重启监控服务。

这复现原核心采集数据语义，不声称逐位复刻部署系统的 Prometheus 传输、
历史保留窗口、Alertmanager 触发或历史 SHAP 模型适用性。
两路均由操作者明确选择同一场景，**不是自动异常检测器对照**。

### eBPF 臂

- BPF perf-event 程序采 hardware cycles 用户/内核栈，并保存 sample_count
  和 sample_period 两套原始量；folded 使用后者的和，匹配原 FlameGraph 权重。
- 在同一个有界诊断窗口，以每 backend 49Hz 读取导出的 `pgBufferUsage`
  全局变量。按目标进程 PIE load bias 定位，使用 PG12/64位 long 的已知前缀。
- 稀疏的 `pgstat_report_tempfile(size)` 事件累计真实临时文件报告字节数。
- 不使用 PortalRun / ExecutorRun 高频入口返回探针，不为了取得缓存命中率而
  对每次 ReadBuffer 调用设探针；CPU 样本是 backend scope，不伪称精确 SQL 关联。
- 读块速率、命中率和临时文件报告字节速率实际送入原模型资源字段。
  它们是**目标 backend 的逻辑块/临时文件统计**，不等于数据库全局统计或物理盘流量。

主机 `/proc` 窗口采样、声明的负载配置、控制面 PID 发现和参数读取对两路一致。
因此这里并未声称 Linux 主机静态资源和 SQL 管理控制面全部改成 BPF。

这是按需诊断数据与收益对照，不是最终常驻监控的性能/SLO 验收。
收益复测时两路均卸载全部诊断探针，不再让“常驻热路径开销不同”污染候选质量比较。
部署状态下的 resident 开销另有 [GAIN_FAILURE_AUDIT.md](GAIN_FAILURE_AUDIT.md) 的对照，
不能混作本实验的收益数字。

## 收益口径

每路、每候选做三组 `before → tuned → restored`，默认每段 12 秒：

```
每段真实 pgbench business 进度
 → 原 _tpcc_tps_summary 启动剔除和 steady 中位数
 → 每组 baseline = mean(before, restored)
 → 每组 gain = tuned / baseline - 1
 → 对三组 gain 取中位数
```

同时保留每组原始 before/tuned/restored TPS、CV、压力 TPS 和原比较器结果。
原 5% 收益门、原压力完成量 20% 可比性检查均保留；
任一组恢复后 TPS 相对 before 漂移超过 5%，该路测量记不确定，
不把漂移伪装成候选收益。

两路收益差另外使用**预设 ±5 个百分点探索性容差**及三组收益的 bootstrap
90% 区间作描述。它不是“允许降低 5% 收益门”，也不是完整统计等价性证明：
不涵盖模型响应随机性，三组短窗不足以代表长期稳定工作负载。
两路都约 0 收益，最多是“两路在这个设置下都没调好”，不能叫复现旧成功收益。
offered business 为 1600，本来已接近这个速率时，上升空间也有限；
它不是无限 offered load 下的饱和容量测试。

## 数据流修复与仍待核对的缺口

发现并修复的共同 adapter 问题：canonical builder 的 database_metrics 是平铺
指标记录，旧 compactor 只读取嵌套 prometheus 字段，可能把已采到的指标在进入
模型前丢掉。现在兼容两种格式，**同样作用于两臂**，不是给某一臂额外提示。
资源值也使用诊断期间保存的窗口，不在模型调用时重新采另一时间点的 CPU/内存。

`paired_report` 逐场景审计实际输入，而不是根据“已经 attach”推断模型看到了数据：

- 资源 prompt 字段是否仍有 unknown；
- 数据库全局事务/元组/块/临时文件/锁/大小指标的覆盖；
- SQL 活动快照及按语句 calls / total_time / mean_time / rows 等统计；
- SHAP 是否真的可用，不把不可用的历史模型归因当成已有证据；
- 原源码证据、top20 函数及匹配参数的交集；
- 对**同一份 BPF 原始栈**仅改变 sample_count / sample_period 加权后的函数占比。

BPF 的 backend 资源统计不能填成数据库全局计数。已有别的 collector 能观测
锁等待/WAL 等事件，也不等于本入口已经把它们转换成原数据库视图的同义字段。
因此缺口报告区分“没有观测”“范围/语义不同”“采了但没送入模型”。
看到缺口只证明输入不同，**没有消融实验时不能声称某个缺失字段必然造成 TPS 差**。

## 运行

原源码、依赖、PG12 profile、PG源码与私有 API 环境按 EQUIVALENCE.md 配置。
认证只在私有环境中传递，不写入命令行、源码、报告或 Git。

```bash
python3 -m experiments.ebpf.paired_tps \
  --output results/perf-ebpf-paired-new \
  --stackcollapse /path/to/FlameGraph/stackcollapse-perf.pl \
  --measure-seconds 12 --measure-repeats 3

python3 -m experiments.ebpf.paired_report \
  --input results/perf-ebpf-paired-new
```

输出：

- `experiment.json`：快照/共同条件/源码哈希；
- `summary.json`：逐场景两路实际 TPS 收益、可比性及不确定性；
- `<case>/<perf|ebpf>/`：正常/异常原始采集、模型输入、API 审计、原候选验证；
- 各 `block-*-before/tuned/restored/`：实际 SQL 和 pgbench 日志；
- `input_gap_audit.json`：逐项采集、模型输入和作用范围缺口。
