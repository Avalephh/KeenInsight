> **v2 scope note (2026-11-19):** SQL-level tuning (DREAM SQL rewrite/hints,
> TPC-DS SQL lab, SQL log/detail pages) has been removed from this stack.
> This document records the pre-v2 artifact; the active product is the
> SysInsight system-parameter tuning path with the TPCC experiment console.

# English.zip 展示恢复与解释能力边界

## 展示恢复

用户要求只删除 realtime-monitor，其余保留原版展示。本次恢复 11 个页面的原始 HTML 外壳、
菜单 SVG、配色、筛选区域、列表列结构和知识卡片；告警 / SQL 详情恢复原版顶部信息、
中间双栏、底部调优对比的布局，不再用通用 JSON 面板替换主内容。

- 逐页比较原压缩包与当前 HTML 的内嵌 `<style>`，11 页全部相同（仅规范化 CRLF）。
- 原型中的样例记录、随机数据脚本和硬编码根因不恢复。
- `original-layouts.js` 保存从原型提取的两种详情布局，仅插入已经转义的真实字段。
- `portal.css` 不再覆盖原版内容宽度、间距和双栏比例；仅为新增状态、弹窗及长真实数据提供补充样式。
- 差分分析仍展示原工具的 sample rate / difference；递归包含采样可能超过 100，不伪称 CPU 占比或 SHAP。
- 告警调优对比使用真实 SQL 复测毫秒数，不恢复原型中虚构的 CPU / IO / TPS 预测。
- SQL 优化耗时区分候选评估、当前发布状态和实际重放，未发布的候选不冒充自动应用。
- 新增证据（LLM 原始轨迹、调用链、阶段、应用恢复、EXPLAIN）保留在底部折叠区。
- 未接管理 API 的上传 / 新建 / 批量启停按钮禁用；详情弹窗、查询、重置、分页、刷新、导出使用真实接口。
- realtime-monitor 文件、菜单及专用曲线接口仍然不存在。原 Grafana 不替换。

原包 SHA256：`4971365aa7780cb3697e6d259c062cf8a900a40131ce12856a6883410ad68bc3`。

## 实际数据补齐

主机列表原版 CPU / 内存 / 磁盘进度条接现有 Prometheus 的瞬时快照，15 秒缓存，
仅匹配本机 node exporter。CPU 为 1 分钟窗口，内存为 MemAvailable，磁盘为根文件系统。
这些是整机指标，不是 PostgreSQL 单进程占比。采不到显示未知，不写成 0。
不增加时间序列采集或新的监控页。区域和主机创建时间不造数，分别显示未配置和实际采集时间。

列表恢复原有筛选控件并增加分页前服务端筛选：

| API | 筛选参数 |
| --- | --- |
| hosts | status, dbType, ip |
| alerts | status, severity, reason；q 同时匹配主机、规则和告警 ID |
| sql | type, result, duration=fast/medium/slow；保留 kind |
| code | db, category |
| diagnosisRules | category, status |

## SHAP 与 SQL 级火焰图

本次仅评估实现路径、恢复模块位置，**没有实现 SHAP 或 SQL 级采样后端**。
没有实际采样 / 归因数据时保留空态，不能为了原型外观填入示例条形图。

SQL 级火焰图相对容易：已有真实 perf record → script → folded 调用栈链路可复用。
需补齐 execution_id / SQL 指纹 / backend PID / query_start / 采样时间窗口的严格绑定，
处理并行 worker 和短查询；普通 CPU 火焰图不是 SQL 执行时间轴，更不是等待时间分解。
现有 DREAM 要求长 SQL **执行结束后**才触发分析，若要给该次执行采样，必须在执行中启动采样，
不能结束后再采同一个 PID，否则可能混入下一条 SQL。
建议限制并发、频率、采样时长；先支持受控的长 AP SQL，再扩展外部连接。

SHAP 工作更多：当前 SysInsight 是差分采样 / 源码关联 / LLM 候选链路，
没有可直接调用的 SHAP 解释器。必须先明确目标（例如异常预测模型的分数）、
真实输入特征、背景数据和输出类别，再验证解释与模型版本的一致性。
若解释 DREAM / RCRank，需要额外处理 SQL、计划、时序等多模态输入的有效屏蔽，
不能直接把 LLM 置信度或热点差分转换为 SHAP，也不能将模型归因声称为真实因果证明。
建议先做 SQL 级火焰图，再按具体解释目标实施 SHAP。

参考：[SHAP KernelExplainer](https://shap.readthedocs.io/en/latest/generated/shap.KernelExplainer.html)、
[Flame Graphs](https://www.brendangregg.com/flamegraphs.html)。

## 验证

- Python 全部 54 项回归通过，包括筛选在分页前生效、未知指标不是 0、新静态资源白名单、原版容器保留。
- Chromium 实访 11 个保留页面，验证原版详情双栏、SHAP 无虚构条目、查询、分页、弹窗、JSON 下载、
  断连保留旧数据、手动恢复、自动刷新，无浏览器 JS 异常。
- 主机列表确认 3 个真实进度条，1440 像素下文档无横向溢出，宽表只在自身容器滚动。
- Bridge 已重载；Grafana / PostgreSQL 未重启，未新发起 SQL 压测或 LLM 调优。

入口：<http://127.0.0.1:9108/keeninsight/>。
