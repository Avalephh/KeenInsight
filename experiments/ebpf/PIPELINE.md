# eBPF 到识别、调优、复测：完整采集链路

本实验的验收目标是链路贯通，不是证明某个 eBPF 开销百分比。
代码仍在 `experiment/ebpf-paper-20261103`，保留原在线服务，不修改运行中的数据库。
已完成的真实 PG/eBPF/原模型验证与边界见
[PIPELINE_VALIDATION.md](PIPELINE_VALIDATION.md)。

## 明确采用的流程

```text
常驻：指定 PostgreSQL backend 上少量关键事件
  首个落盘场景只保留 BufFileCreateTemp 入口计数，1s 聚合
  不抓所有 SQL，不抓栈，不常驻提交/WAL/逐元组函数
                       ↓
eBPF 临时文件事件触发，或操作者显式选择目标
                       ↓
按需：只为该 backend 临时增加 query 边界和诊断函数
  BufFileRead / ExecHashJoinSaveTuple / LockAcquireExtended 等
                       ↓
有足够的完成执行证据，或时间/事件数/输出量/collector CPU 预算超限
  卸载细粒度 collector；常驻 collector 不退出
                       ↓
eBPF 证据适配 → 原函数/参数匹配 → 真实 PostgreSQL 源码证据
                       ↓
原 ParameterLibrary → 原 LLM_ACQ / selector → 原候选解析及校验
                       ↓
显式会话参数白名单 → 原临时配置流程 → SET LOCAL 下复测
                       ↓
结果集比较、收益门槛、ROLLBACK、参数恢复核验、恢复后复测
                       ↓
冷却，丢弃旧/自身事件，回到常驻监控
```

一次命令处理一个 incident 后清理退出，适合独立实验。`resident_resumed` 表示
细粒度探针已卸载、常驻采集仍有效；随后实验结束才停止 resident。
尚未接替运行中的 bridge/Portal，不能将一次 incident 实验冒充完整生产守护进程。
这个场景使用新的 `resident` profile，不是旧开销对照的双事件 `events` profile。
后续扩展异常类型需要明确选择新的低频语义事件，不默认把全部候选加入常驻集合。

## 复用与改变的边界

复用：

- 原 SysInsight `matchFunctions.find_top_and_matched_functions`；
- 已有 PG12 函数/参数关联库和真实源码片段提取；
- 原 `ParameterLibrary`、`LLM_ACQ`、`LLM_DIS_SM` 和严格 API 原文候选解析；
- 原结构/数据库参数校验、`TemporaryPostgresConfiguration`。

替换/补充：

- 原 Prometheus 告警、perf 调用栈输入替换成真实 eBPF 事件；
- 不把事件次数伪装成 perf 样本率，不运行基于 CPU 样本率的旧差分；
  兼容文件的数字列明确是调用数及排序分数；
- 新的事件规则把临时文件与读取行为识别为物化/溢写候选，
  内存参数根因仍需源码匹配、模型与真实复测验证；
- 原始提示词数字格式不能处理缺失时，显示 `unknown (not collected)`，
  不补成零；
- 本次目标是**同一只读 SQL 的耗时**，原候选流程以 lower-is-better 接收；
  不伪造 baseline TPS，不把 SQL 加速宣称成业务 TPS 提升；
- 当前首先贯通 PostgreSQL 参数调优路径；DREAM SQL 改写/模型路径
  尚未通过这条控制器迁移，不能宣称两条原业务链都已切换。

## 运行前置条件

除 eBPF 的 BCC/内核/PG12 前置条件外，还需要原链路的私有运行依赖。
实验不会下载模型或把凭据写入仓库：

```bash
export SYSINSIGHT_SOURCE_ROOT=<原_SysInsight_WorkloadTune_目录>
export SYSINSIGHT_PYDEPS=<原_SysInsight_Python依赖目录>
export SYSINSIGHT_PROFILE_ROOT=<含_postgresql/12/profile.json_及生成资料的目录>
export POSTGRES_SOURCE_ROOT=<对应_PostgreSQL_12.22_源码目录>

export SYSINSIGHT_GPT_BASE_URL=<原项目的兼容模型API>
export SYSINSIGHT_GPT_MODEL=<原项目配置的模型>
export SYSINSIGHT_GPT_API_KEY=<私有凭据>
```

`SYSINSIGHT_PROFILE_ROOT` 是新的可选路径覆盖，不设置时原服务行为不变。
也可使用项目已有的 `monitoring/codex_api_launcher.py`，由操作者选择的私有
配置在子进程环境中提供 API 设置；不复制认证到结果文件或 argv。

原始库缺失、API 不可用、证据不完整、模型输出不合法等，都会记录失败，
**没有固定 work_mem、历史候选或“修好后再测”的兜底配置。**

## 最小真实贯通实验

```bash
python3 -m experiments.ebpf pipeline-lab \
  --output results/ebpf-closed-loop
```

这个入口会：

1. 创建临时独立 PG12 集群；只使用私有 Unix socket，不接已有 keeninsight 库；
2. 构造可回放的排序数据，给实验工作负载显式设置 64kB work_mem
   作为**故障前提**，不是模型候选；
3. 启动常驻 collector 后再制造排序落盘，必须由真实事件触发；
4. 自动临时附着诊断探针，获取真实执行完成证据后卸载；
5. 暂停实验负载避免干扰复测，进入原 SysInsight 模型链；
6. 使用 API 返回的候选值，在独立的 managed 只读会话中临时应用；
7. 比较结果与耗时，验证恢复并销毁临时集群。

只验证显式选择目标分支：

```bash
python3 -m experiments.ebpf pipeline-lab \
  --select-target --output results/ebpf-selected-target
```

`--detail-max-events 1` 等参数可验证证据不足时的预算卸载失败路径；
该路径预期不调用模型、不调优，不能计为完整成功。即使超限批次里恰好
包含可识别事件，也不继续自动调优；时间窗结束仍未达到最小完成执行数
（默认 2 次）同样失败关闭。

## 接入已有实验工作负载

```bash
python3 -m experiments.ebpf pipeline \
  --pid <明确选定的实验backend_pid> \
  --db <实验数据库> --db-user <数据库用户> \
  --host <TCP或Unix_socket路径> --port <端口> \
  --sql-file <一条显式只读SQL文件> \
  --allowed-knobs work_mem \
  --knob-limits '{"work_mem":[64,65536]}' \
  --output results/ebpf-incident
```

默认等待 eBPF 临时文件事件；显式选对象使用 `--select-target`。
SQL 必须是操作者明确提供的可回放查询，不从截断的 `sourceText` 猜参数。
sourceText 前缀与 PID/出生身份/执行起始身份只用于限定观测窗口，
前缀不是完整 SQL 文本相等性的证明。

generic 入口的 libpq 验证连接必须具备目标数据库的认证条件。
root 的 BPF 权限不自动绕过 PostgreSQL peer 认证；
可用受控 TCP/密码连接或合适的 OS/数据库认证安排。
lab 使用私有 trust socket 仅为隔离实验，不修改现有认证。

## 默认边界和安全

- 一个显式 backend；细粒度窗口默认最多 5 秒、8MiB、10,000 条事件；
  至少获得两次匹配 SQL 的已完成执行，才能提前完成取证。
- 时间/数量/输出/collector CPU 都是采集资源预算，不宣称等于数据库端开销。
- SQL 中断、错误、丢失/容量错误、部分覆盖不用于无条件自动调优。
- 默认仅允许 `work_mem`，限定 PG 原生 kB 单位的 `[64,65536]` 范围。
  这是安全边界，不是预设优化值。模型仍需要实际生成候选。
- 不允许全局 GUC、重启参数、DDL 或任意客户端透明注入。
- 用 `BEGIN READ ONLY` / `SET LOCAL` / `ROLLBACK` 执行，
  失败也回滚；复测后核对设置，最后关闭连接。
- 结果对比保留重复行，比较本次数据上的结果多重集，限制 10,000 行 / 4MiB。
  它不是对未来所有数据的形式化语义证明，也不检查行序语义。
- 收益不足或结果不一致只记录 candidate，不声称优化成功。
- 耗时为 managed libpq 会话的 SQL 往返加有界结果哈希处理，采用 3 次中位数；
  不是纯服务端 ExecutorRun 时间或业务 TPS。
- 原工作负载里的故障配置不被永久修改；优化是在 managed 重放会话中
  临时验证，完成后恢复，不是生产参数发布。

## 证据文件

- `workflow.json`：有时间戳的各阶段及卸载原因；
- `resident/`：常驻采集原始数据及 manifest；
- `detail/`：有界诊断原始数据及 manifest；
- `diagnosis.json`：只由真实完成事件形成的诊断证据；
- `observed_functions.tsv`、`source_detection.json`：原函数/参数匹配输入与输出；
- `sysinsight_input.json`、`case_result.json`：原模型接口输入；
- `sysinsight_api/`：原模型请求/响应审计及候选解析；
- `candidate_validation.json`：原候选校验；
- `baseline.json`、`tuning_result.json`：真实重放、结果哈希、收益及恢复；
- `result.json`、`lab_checks.json`：最终状态和实验检查。

`tuned_and_restored` 才表示存在实际应用、结果一致且达到门槛的候选。
仅跑到 API、仅识别出 spill 或仅得到参数文本，都不算端到端调优完成。
