> **v2 scope note (2026-11-19):** SQL-level tuning (DREAM SQL rewrite/hints,
> TPC-DS SQL lab, SQL log/detail pages) has been removed from this stack.
> This document records the pre-v2 artifact; the active product is the
> SysInsight system-parameter tuning path with the TPCC experiment console.

# Codex 配置复用与 GPT-6.1-SOL 实测

## 生效配置

- 模型：`gpt-6.1-sol`，保持当前 Codex 的精确模型 ID。
- Provider 与认证：启动时读取 `/root/.codex/config.toml`、`/root/.codex/auth.json`。
- 本机开关：`monitoring/.env.local` 的 `SYSINSIGHT_CODEX_CONFIG_DIR=/root/.codex`。
- 启动器：`monitoring/codex_api_launcher.py`。只将认证放入 Bridge 子进程环境，
  不把 Key 写入 JSON、Git、命令行或本报告。
- 当前 provider 实测支持现有 Chat Completions 客户端，无需更改两套调优程序的请求协议。
- DREAM 根因、诊断和修复 Agent 的模型统一由运行时选择覆盖旧示例配置。

## API 实测

- 新 provider 的 `/models` 返回 200，模型列表包含 `gpt-6.1-sol`。
- `/chat/completions` 返回 200，实际回答 `OK`，响应模型为 `gpt-6.1-sol`。
- 原超时请求使用的旧地址与当前 Codex provider 地址不同；当前地址已连通。

## SysInsight 真实告警闭环

通过现有控制台执行热点 Stock Level：

- 实验：`lab-sysinsight-20261022204915-ef67793ec8`。
- 告警：`incident-20261022204926-cdb6db714b8f`。
- 结果：`completed`。
- 真实 perf 采样、原版异常函数识别、源码参数匹配、LLM 候选生成、结构/PG 参数验证、
  基线复测、临时应用、调优复测、恢复及恢复后复测全部完成。
- 原始 LLM 审计中有 10 次真实 API 调用，包含候选采集与选择；请求模型全部为
  `gpt-6.1-sol`。没有复用历史候选作为本条在线闭环的 LLM 生成结果。
- 临时配置状态：`applied_and_restored`。
- 在线验证 SQL 中位耗时：基线 `1013.18 ms` → 候选 `1134.70 ms` → 恢复 `1010.48 ms`。
  本次候选反而慢约 12%，故仅证明 API 与完整执行链恢复，不计为优化收益。

同次实验另有告警排队后才进入分析，此时负载已结束，仍报 `no_active_backend`。
该问题与 API 连接无关，本次没有将其伪装成 completed，也没有清理这些真实失败记录。

## DREAM 实测

| SQL | 实验 ID | Job | 原 SQL 耗时 | 结果 |
| --- | --- | --- | --- | --- |
| Q88 | lab-dream-20261022205542-c5b2ad685f | dream-20261022205555-852b07a1a4 | 12510.39 ms | completed / no_action |
| Q44 | lab-dream-20261022205848-dd224a8e36 | dream-20261022205854-ed293136e4 | 5687.86 ms | completed / no_action |

两次没有 API 超时；运行时 job 配置中三个 Agent 均为 `gpt-6.1-sol`。
本次诊断返回 normal，没有生成改写或应用方案，不计为优化成功。
Q44 为显式控制台测试；自动 DREAM 的 10 秒阈值保持不变。

## 检查

- Codex 配置、脱敏输出、旧环境覆盖、DREAM 多 Agent 模型同步：4 项测试通过。
- DREAM 自动应用路由：8 项测试通过。
- KeenInsight 页面与证据持久化：7 项测试通过。
- 启动脚本语法和 Git diff 格式检查通过。

页面：`http://127.0.0.1:9108/keeninsight/alert-list.html`。
