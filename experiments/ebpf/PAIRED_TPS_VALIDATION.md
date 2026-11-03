# 同条件六场景 TPS 与输入缺口：2026-11-03

## 结论：未证明收益等价，不计为 6/6 调优成功

已执行六组 perf / eBPF 配对实验，正式两臂共 12 次原模型 acquisition/selector，
每臂 10 次真实 API，合计 120 次。每臂实测三组 before/tuned/restored。
一臂发生技术性恢复核验失败，保留原失败记录后用**同一快照、同一新候选**
补完测量，没有重新选模型候选，也没有重跑已完成的低收益结果来挑好数字。

**结果没有达到“六场景获得与原来相近且良好的 TPS 提升”。**
在本次共同安全权限下，原采集参考臂和 eBPF 臂都没有验证出稳定 ≥5% 收益。
不能把“两边都没调好”当作复现旧成功收益，也不能把原参考臂的不稳定结果
当成正确答案，再把全部差异归为 eBPF 漏采。

本报告记录的是 [PAIRED_TPS.md](PAIRED_TPS.md) 的**原核心采集语义参考路径**
与 eBPF 的受控对照，不是原线上部署全部组件、传输、历史模型的逐位复刻。
运行时核心方法对应提交 `1522b33`；`experiment.json` 另存实际源码哈希。

## 共同条件确实相同

- 一份物理 PG12 快照，每臂诊断与评分分别开全新副本。
  快照 SHA256：
  `24e427afae2074fa6c386ed7a9f3009feeddcb10e40c0fb8de63ec182859f214`。
- 同一原五仓库 fixture、业务/外压 SQL、随机种子 1729；
  business 2 客户端、offered 1600 TPS；
  pressure 各场景原客户端数、offered 2500 TPS。
- 共同允许 31 个安全参数，数值边界、初始配置、模型/provider 配置、
  acquisition/selector 预算相同。不按两侧 matcher 另设不同权限。
- 同一原函数差分/匹配、源码知识、原模型与候选校验、临时应用/恢复代码；
  session 参数连接时设置一次，不在事务内重复 SET。
- 两路正常两窗、异常一窗，15 秒/300Hz/hardware cycles，
  folded 均按 `sum(sample_period)` 加权。
- TPS 测量使用新副本和相同 warmup，全部诊断/常驻探针卸载；
  每段 12 秒、三组夹心 before/tuned/restored，使用原 steady TPS 统计。
- `summary.json` 的六组 fairness 检查均通过；
  所有最终测量设置恢复、业务聚合不变量核验完成。

参考臂使用真实原 `_capture_perf`，数据库统计读取原 exporter 的来源视图，
不读取生产 Prometheus。两路均由明确选定同一场景进入诊断，
不是自动异常触发对照，也不包含部署状态 resident 开销结论。
宿主机原展示服务保持运行，并非独占性能机；本轮没有消除系统级 I/O/调度波动。
“同负载”指受控目标 SQL、客户端和 offered 策略相同，不代表宿主机背景活动逐位相同。

## 实际业务 TPS

表中三阶段 TPS 是各阶段三次 steady TPS 的中位数。
**收益不是直接用表中两个中位数相除**：
逐组计算 `tuned / mean(before, restored) - 1`，再取三组中位数。
这样保留恢复阶段作为邻近控制，不跨模型等待时间拿旧 before 作唯一基线。

`不确定` 表示至少一组恢复漂移超过 5%，或原压力比较检查不通过。
这些原始数字保留展示，但不计成功收益。

| 场景 / 采集 | Before TPS | Tuned TPS | Restored TPS | 三组收益中位数 | 测量有效性 |
|---|---:|---:|---:|---:|---|
| Payment / perf | 1430.90 | 1446.05 | 1442.00 | +3.50% | 不确定 |
| Payment / eBPF | 1449.45 | 1403.00 | 1422.95 | −3.41% | 有效，未改善 |
| Stock hot / perf | 1170.00 | 1205.00 | 1188.50 | +2.04% | 有效，未过 5% |
| Stock hot / eBPF | 1199.50 | 1170.95 | 1202.50 | −2.18% | 有效，未改善 |
| Stock burst / perf | 1212.45 | 1248.10 | 1224.00 | −1.10% | 不确定 |
| Stock burst / eBPF | 1171.00 | 1197.50 | 1202.00 | +0.97% | 不确定 |
| Full mix / perf | 1070.85 | 1017.00 | 885.50 | +3.04% | 不确定 |
| Full mix / eBPF | 1084.50 | 804.50 | 1002.50 | −13.20% | 不确定 |
| WAL / perf | 928.50 | 1011.50 | 936.00 | +8.40% | 不确定 |
| WAL / eBPF | 1070.00 | 727.50 | 979.90 | −9.22% | 不确定 |
| Order Status / perf | 1466.50 | 1536.00 | 1525.45 | +3.26% | 不确定 |
| Order Status / eBPF | 1536.00 | 1500.45 | 1574.00 | −3.70% | 有效，未改善 |

Stock hot 是唯一两路测量均通过本轮控制检查的场景：
名义收益差 −4.22 个百分点，但三组短窗的差值 bootstrap 90% 区间约为
`[−11.50, −2.72]` 个百分点，没有落入预设 ±5 个百分点容差。
其余场景至少一侧测量不确定，不能用来证明采集造成了多少收益差。
三组短窗也不覆盖模型随机性或长期负载稳定性。

## 原来采了什么，eBPF 缺了什么

以下核对的是原始采集文件、canonical input、原模型 compactor 和真实 prompt，
不是根据探针已 attach 推断模型拿到了证据。

| 数据/语义 | 原采集参考路径 | 本轮 eBPF | 结论 |
|---|---|---|---|
| on-CPU 用户/内核栈 | perf cycles/callchain | BPF cycles/stack map | 同事件，不代表逐栈信息完全等价 |
| folded 权重 | sample_period 累加 | 已改为 sample_period 累加 | 旧版“每样本 +1”并不等价，已纠正 |
| 正常/异常函数对比、源码关联 | 原算法、原知识资产 | 相同算法/资产 | 下游方法相同，输入函数榜仍不同 |
| CPU/内存/主机磁盘窗口 | 真实主机 `/proc` 窗口 | 相同方法 | 共同上下文，不冒称全部来自 BPF |
| 读块速率、缓存命中率 | pg_stat_database 差值 | BPF 读取 backend pgBufferUsage | 已实际送模型；backend 与 DB 全局范围仍不同 |
| 临时文件报告字节速率 | pg_stat_database.temp_bytes | pgstat_report_tempfile(size) 事件 | 已送模型；本轮各场景均无临时文件报告增量 |
| DB 全局提交/回滚、返回/读取元组数 | 已采集并经修正后的 compactor 送模型 | 尚未提供同范围计数 | **仍缺**；不能用业务 TPS 或调用次数冒充 |
| DB 全局块/临时文件计数、锁数、大小 | canonical 中有真实记录 | 部分有 backend 资源替代，非同义全局记录 | **部分替代/部分缺失**，不填伪造零值 |
| 按 SQL 的 queryid/calls/total_time/mean_time/rows/块计数 | 每场景实际取得 10 条统计并送入模型 | 本入口没有该统计输入 | **明确缺口**，不仅是多采几个函数 |
| 活跃 SQL 的状态/等待事件快照 | 原始文件取得 | 本入口没有完整等价快照 | 但参考臂的完整等待快照也未送入模型 |
| bgwriter/checkpoint/backend 写入快照 | 原始文件取得 | 本入口未提供同范围快照 | 参考臂原始快照也没有完整进入模型，不能冒称原模型已利用 |
| 可用且可信的 SHAP 数值归因 | 本轮无 | 本轮无 | 不是已证实的单边优势，见下文 |

### 补齐资源字段后，实际数值已经接近，但未等价

两路均不再把资源 prompt 的 IO/cache 写成 unknown。
命中率是逻辑块命中率，readIO 单位是逻辑块/秒，不是物理盘读字节：

| 场景 | perf 命中率 | eBPF 命中率 | perf readIO | eBPF readIO | 异常函数 top20 交集 |
|---|---:|---:|---:|---:|---:|
| Payment | 87.8259% | 88.1327% | 21974.21 | 21281.20 | 11 |
| Stock hot | 99.4758% | 99.5458% | 12912.76 | 11156.13 | 10 |
| Stock burst | 99.5929% | 99.6086% | 10193.11 | 9738.76 | 10 |
| Full mix | 86.2887% | 86.5697% | 43764.95 | 45730.83 | 13 |
| WAL | 86.3074% | 86.4571% | 46579.59 | 44756.37 | 5 |
| Order Status | 89.7123% | 89.9407% | 18404.37 | 17156.97 | 11 |

top20 交集不能直接解释为“漏采率”：它还受短窗波动、两窗正常包络、
stack unwind/符号解析及原差分方法影响。它证明目前不能宣称诊断证据已等价。
BPF helper 失败计数、原始用户/内核帧和每 PID 样本均保存，
没有把“map 没满”当成“采样信息完整”的证明。

对同一 BPF 栈仅切换 sample_count / sample_period 加权，前列函数占比的最大
差异在各场景约 0.33–2.11 个百分点。加权错误是真实问题，但不能据此宣称
它单独解释了所有 TPS 差。

### 还发现“采了但没送模型”的公共适配问题

原 canonical builder 输出平铺 database_metrics，旧 compactor 只读嵌套
prometheus 字段，会丢掉已有指标。本轮已共同修正两路，不给某一路额外特权。

但完整活动/等待快照、bgwriter 原始快照仍没有完整进入模型。
例如参考臂原始文件确有 WALSync / WALWriteLock 等等待；
不能因此就说原 LLM 已利用这些证据，而 eBPF 没有。
本轮参考异常窗口的 checkpoint 请求/定时计数增量也都为 0，
场景名字不能代替真实 checkpoint 异常证据。

已有另一个 BPF collector 能观测 ProcSleep / WAL 等有界事件，
**不等于本入口已把它们与 SQL/业务角色关联并送入原模型**。
下一步应补这个语义和数据流缺口，而不是声称 eBPF 技术无法采到。

## 固定同一采集证据，原链路自己的候选也会变

额外做一次 Stock hot 原采集输入的 A/A：

- 原 case_result / sysinsight_input 文件逐字节不变；
- 再走原 ParameterLibrary / acquisition / selector，10 次额外 API；
- 没有重新采集，也没有新 TPS 收益主张；
- source prompt 和首个真实 API 请求却变了，解析候选也不同。

源码 `llambo/extract_knob.py:301` 使用 `list(set(updateKnobs_names))`。
两份 prompt 各 1647 行：忽略顺序、缩进和 target 参数列表顺序后，行多重集相同。
也就是说，**同一证据会因为原链路的非确定排序变成不同顺序的提示**。
原 acquisition 还记录 temperature 0.8 / top_p 0.95，不能假定响应确定。

原候选中有 `effective_cache_size=786431, max_wal_size=3583`；
同证据重跑又产生含 `effective_cache_size=917503, work_mem=8191,
max_wal_size=3073` 等不同候选。
这不是字段消融实验，不能把变化全部归到顺序；
但足以说明“候选不同”不能自动归因为 eBPF 缺失某个信号。

本轮未改原优化器排序/温度来强凑结果。后续应先固定提示排序和共同模型设置，
用重复 acquisition 估计原链路自身方差，再比较采集方式；这尚未验收完成。

## 历史涨幅不是本次同权限参考

只读核对的两个历史成功例子：

- `20260912_052000_tp_stock_level_hot`：记录约 +87.24%，候选包含
  shared_buffers 1009253 blocks、fsync=off、synchronous_commit=off；
- `20260912_054000_tp_payment_hot`：记录约 +32.27%，候选包含
  shared_buffers 1638397 blocks、synchronous_commit=off。

本次共同安全权限不允许这些重启/持久性变更。不能要求一个 31 参数安全臂
复现更大权限历史候选的全部涨幅，再把差异当成 BPF 漏采。
这不证明上述几个字段是历史收益的唯一原因，也没有在本轮回放这些候选凑成功。
如果目标改为复现历史大权限收益，必须另做**两路共同授权、明确持久性语义**
的隔离实验，不能把它混进本报告。

## SHAP 资产核对边界

正式两路因工作树没有安装默认模型资产而记录 unavailable。
另外只读加载实际原部署的模型及关联资产做离线核对，两路均得到真实
TreeSHAP 结果，但原 `llm_evidence` 均返回 excluded_low_confidence /
decisionEligible=false。原模型独立验证记录 R² 约 −4.96、MAE 约 28.45 个百分点。

没有用不可信 SHAP 数值填补 eBPF 缺口，也没有据此重跑/改写正式 TPS 数字。
这仍是核心参考路径的范围限制，不能宣称完整线上模型链已经逐位复刻。

## 技术重试与清理

Stock burst / perf 首次在恢复核验处终止，原错误和部分 trial 未删除。
保留并核对同一物理快照哈希，用原 API 已选中的配置
`work_mem=16381, max_wal_size=3079` 重做未完成的测量阶段。
重试只加了至多 5 秒的真实设置重读，不重选候选、不新增 API、不放宽收益门。

重试完成，设置核验通过；没有出现需要等待的瞬态读数。
另做八次轻载 apply/restore 也未复现原失败，因此**不把原失败确定归因于
SIGHUP 竞态**。重试后第一组恢复漂移仍约 5.03%，照原门槛记不确定，没有改成成功。

18 个 BPF 栈采样窗口及 18 个资源窗口均保存真实记录；
采样 FD 全关闭，资源读取错误为 0。临时数据库/本次 worker/探针已清理，
未修改、重启原演示数据库或服务。

## 原始审计与复现

方法、运行环境和命令见 [PAIRED_TPS.md](PAIRED_TPS.md)。
本次原始记录位于忽略提交的 `results/ebpf-perf-paired-six-02/`：

- `experiment.json / summary.json / input_gap_audit.json`
- 每场景每臂正常/异常采集、输入、API、候选验证和全部 TPS trial；
- `tp_stock_level_burst/perf/result-attempt-1-technical-failure.json`
  与 `retest-attempt-2/`；
- `same-input-AA-stock-hot/result.json / prompt-diff-audit.json`；
- `historical_policy_audit.json / shared-original-SHAP-resolved-asset-audit.json`。

前一校准运行 `ebpf-perf-paired-six-01` 在发现 sample_period 权重差异后中止，
其采样和一次 acquisition（10 个 API 请求）不计入正式结果。
回归测试 190 项，186 通过、4 跳过；测试不代替真实 TPS 或模型效果验收。

**尚未完成的目标：** 六场景稳定 TPS 收益等价；完整同义 SQL/全局统计输入；
字段消融后的因果归因；最终低频常驻版本的部署性能验收。
本轮完成的是受控核心对照和可核实的缺口定位，不把未达标写成成功。
