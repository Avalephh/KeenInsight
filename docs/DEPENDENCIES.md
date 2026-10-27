# 依赖、来源与发布边界

本分支只发布原始集成源码、配置模板、版本标识与兼容补丁。不复制
外部 repository、训练资料、权重、数据库、监控二进制或旧 Git 历史。

| 依赖 | 版本/来源 | 准备用途 |
|---|---|---|
| SysInsight/WorkloadTune | 根目录 `external-sources.lock` 三个精确 commit | 原始检测/函数匹配/LLAMBO、参数参考资料 |
| PostgreSQL source | `postgresql-source.lock`，12.22 archive SHA256 | 可追溯参数/函数关联，匹配参考部署 |
| DREAM 集成基线 | `integrations/sources.json` | 原始诊断、动作、规则与 memory；固定的已有集成 snapshot |
| RCRank | 同一 JSON 的精确 commit + compatibility patch | 原模型结构/计划编码兼容 |
| DREAM 模型资产 | `integrations/dream/assets.json` | 真实权重/训练 CSV/tokenizer 的完整性元数据 |
| FlameGraph | `scripts/fetch_flamegraph.sh` 的精确 commit | stackcollapse-perf.pl 与其上游许可证 |
| 监控组件 | `monitoring/versions.lock` 的 SHA256 | Prometheus/exporters/Grafana 的独立发行包 |
| Python | 两份 requirements；DREAM 使用 CPU torch 2.4.1 | 与旧 SysInsight SDK 分离的两个环境 |
| 浏览器测试 | package.json / package-lock.json | 固定 Playwright，非产品运行依赖 |
| pg_hint_plan / TPC-DS generator | `integrations/reproduction-sources.json` 精确 commit | PG12 扩展的本地编译；可重建 AP 输入 |

联网准备、人工下载导入、离线缓存、模型匹配、Python wheelhouse、系统离线包
和 TP/AP 初始化步骤见 [REPRODUCING.md](REPRODUCING.md)。缓存本身不进入
源码发行，也不包含操作者的凭据或原实验数据库。

## 为什么没有把 DREAM 复制进来

本地 DREAM 根目录包含大量原模型/数据、日志和历史产物，并有自己独立的
Git 身份。新分支保存可重建整合所需的补丁和来源，下载在明确请求时进行。
没有复制旧 smoke bootstrap；缺模型必须失败，不生成随机权重或样例标签。

DREAM 基线为同一公开 repository 的
`backup/20260914-dream-linkage`，上游项目为
`https://github.com/HarneyHong/DREAM`。
RCRank 从 `https://github.com/HarneyHong/RCRank` 获取原始 commit 后应用
兼容补丁。获取器不依赖只有当前机器才有的本地 RCRank backup commit。

## 模型获取与完整性

上游 README 提供的下载入口：

`https://drive.google.com/drive/folders/1mRkA_CJvKImJeHuaXm-W0h0riVmvB_2w`

分别准备 RCRank `best_model.pt`、`slow_sql_data_gen.csv`、
Soft Q Retriever 与 BERT 目录。路径、大小和实物 SHA256 见 assets.json。
该清单不是数据集的再发布，也不是官方签名。下载权限、可用性和条款
由上游提供方控制；本项目不内置账号/cookie、不承诺下载链接永久有效。

如果你取得的资产确实来自另一个官方版本，不要跳过 hash 错误；先审核
来源、模型结构和兼容性，再有意更新 manifest，并重新执行真实推理验证。

## 许可证

- MIT 只覆盖本项目的原始集成代码和文档，见根目录 LICENSE/NOTICE。
- PostgreSQL、FlameGraph、各监控发行包及 Python 依赖保留各自许可；下载
  后保留其声明，不把它们统一标成 MIT。
- 审计的 DREAM 和 SysInsight snapshot 顶层未找到清晰的许可证声明。
  **公开可访问不等于授予开源许可或允许再分发。** 本分支不附带其完整
  源码或模型；兼容补丁不改变上游权利，使用/再分发整合包前仍需核实授权。
- 生成的参数资料与源码关联来自上游文档/代码，也只在用户本地生成，
  不带着历史本机生成文件在本分支重新分发。
- TPC-C/TPC-DS 名称用于描述测试 workload 类型；不是 TPC 官方审计结果，
  不包含数据库数据、官方 generator 或认证成绩。

因此可公开的是本分支明确授权的集成源码；不能据此宣称整个依赖组合的
所有资产已经取得相同的开源/商用授权。
