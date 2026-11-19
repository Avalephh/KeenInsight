# English.zip 原版展示与真实新增数据核验

核验日期：2026-10-27，Asia/Shanghai。

入口：`http://127.0.0.1:9108/keeninsight/`；
汇总页：`http://127.0.0.1:9108/keeninsight/template.html`。

## 展示对照

原包 `/root/English.zip` SHA256：
`4971365aa7780cb3697e6d259c062cf8a900a40131ce12856a6883410ad68bc3`。

- 保留的 11 页内嵌样式与原包逐页精确相同；只删除原要求删除的 realtime-monitor。
- 原导航、菜单图标、主内容宽度、列表表头、详情顶部/中间双栏/底部结构保留。
- Chromium 1440×1050 实际加载 11 页，分别保存原版/动态版截图和 DOM 布局测量。
- 主机列表原先被改成 Last Collected 的列恢复为 Created At；没有创建时间就显示 Not recorded，不把采集时间当作创建时间。
- 汇总页是填充原 template.html 的空白内容区域；原包没有同等的工作流汇总内容可供像素对照。新增内容采用既有字体、配色和卡片规范。
- 不是逐像素复制原型样例：真实记录、长 ID、数量、状态、刷新/导出/更新时间及折叠证据与原型演示文字不同；SQL 原型本身存在长内容挤宽，动态版保留防溢出约束。
- 不恢复原型的假主机、假优化指标、假 SHAP 或假 SQL 内部阶段。无采集能力的字段仍明确显示未知。

完整浏览器记录与截图在 `results/portal_audit_20261027/`：

- `presentation-audit.json`：原版/当前布局、内嵌样式、列标题与脚本/API 错误。
- `*-original.png` / `*-live.png`：11 页对照截图。
- `live-write-audit.json`：真实 TP 请求、打开页面的自动新增证明及最终证据。
- `new-sql-audit.json`：真实新 AP 执行、准确 Job、模型、重放、命中和采样的跨页核验。

## 新增实验

### TP：热点 Stock Level

修复后完整重复：

- Run：`lab-sysinsight-20261027163227-dc1a0f2088`。
- Alert：`incident-20261027163237-cc07fb4eaac4`。
- 正常业务 TPS：基线 1466.90，压力下调优前 613.00，调优后 907.00。
- 原策略判定 comparison_valid=true，表面增幅 47.96%；实际外部压力吞吐下降 12.38%，不能将本次展示验证升级成严格同压稳定收益证明。
- 控制台候选仍来自历史验证文件，不能与告警链本次 LLM 输出拼接成同一次 TPS 验证。
- 新告警真实调用 `gpt-6.1-sol`，10 条 API 调用证据，11 个流程阶段完成；告警链自己的应用/恢复状态 `applied_and_restored`。
- 保持打开的 Alert List 无页面重载看到新告警；保持打开的汇总页看到新 Run。
- 本次前后：incidents 7→8，lab_runs 3→4。最终状态写回后详情可查看调用链、SHAP、候选与应用恢复证据。

之前同日第一次完整 TP Run：
`lab-sysinsight-20261027160945-cbd6e0bdc0`，
基线 1610.05，压力前 577.50，压力后 897.40 TPS。其后监控健康误判导致部分排队告警中断，未把这些告警算成完成。

### AP：TPC-DS Q9

- Run：`lab-dream-20261027162606-c0a4e0d473`。
- Job：`dream-20261027162619-1577eec918`。
- 使用控制台真实 start/replay API，新调用 `gpt-6.1-sol`。
- 原 SQL 13551.12 ms；模型内部评估 3259.74 ms。
- 再次实际执行优化 SQL 3449.55 ms，耗时降低 74.54%。
- SQL 列表同时能检索原始/优化两条执行，详情准确关联这个新 Job。
- 方案 active，真实命中计数已增加；汇总页存在新 Run、新 Job。
- 原执行采样 profile：`sql-profile-ce1cf34adf5e46e18485`，532 个真实 CPU 样本，49 Hz。
- 这是显式控制台实验，trigger_source=manual，不冒称自动阈值触发。

## 核验中发现并修复的问题

1. 监控健康检查截断 exporter 的 2 MiB 响应。实际指标响应约 2.11 MB，pg_up 位于截断位置之后，健康数据库被误判，连续三次误判后整套服务重启。改为流式扫描 pg_up，保留真实 down 检查和有上限的读取保护。
2. 服务重启后旧告警保持 running。新启动显式结束被中断的告警，保留阶段证据和中断原因，不重放全局参数实验、不冒充成功。
3. 24 小时任务清理后，有效方案对应的模型证据仍在，但详情只查 live Job，导致模型/轨迹缺失。改为按这个 SQL 的 active 方案中保存的确切 job_id 回查 keeninsight_evidence；标注 history_expired，不重建旧 Job，也不伪造执行时间线。
4. 原浏览器回归硬编码历史 Q44；24 小时清理后无记录会误报。改为从当前保留记录选取真实 SQL 验证搜索与详情。

期间中断的两条告警保留为 failed，原因 bridge_restart_interrupted_workflow；不是删除历史来制造全绿。

当前 15 条有效知识方案逐条读取验证通过：14 条使用保留证据恢复真实模型与诊断轨迹，1 条对应本次 Q9 的现存 Job。修复加载时已无活动实验；服务重新加载后新增数据仍可正常查询。

## 验证方法

`test_keeninsight_archive.py`：原包样式、原列表列名、监控页移除。

`test_keeninsight_portal.py`：接口、筛选、证据、24h 活跃证据关联、跨 SQL 隔离、重启中断处理。

`monitoring/test_deployment.py`：大于 2 MiB 的健康响应不能引起误重启；真正 pg_up=0 仍失败。

`test_keeninsight_browser.cjs`：11 页加载、查询、分页、弹窗、导出、详情、精确 Job、
真实 SHAP、历史保留证据、断连保留旧数据、恢复刷新、自动刷新、无 JavaScript 异常。

浏览器工具使用临时目录的 Playwright 和解包的依赖库；没有为测试更改系统库、数据库数据量或资源限制。

最终结果：展示/接口/部署相关 Python 31 项通过，保留策略/SQL 采样/SHAP 相关 26 项通过，共 57 项。11 页浏览器回归、原包对照、新 AP 跨页核验、新 TP 不重载自动新增核验全部通过。最后健康检查五个组件均正常，服务 NRestarts=0（最后一次有意加载修复之后）。
