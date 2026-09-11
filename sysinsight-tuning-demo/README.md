# SysInsight 调优过程隔离回放

这个目录独立于 `monitoring/`，用于验证 SysInsight 的调优链路，当前只做文件回放和大模型预测，不连接数据库，也不修改 Prometheus/Grafana。

## 已完成

已按仓库源码中的单步流程完成一次回放：

1. 用源码 `analyzeException.py` 将 perf 函数采样率与基准 profile 比较。
2. 用源码 `matchFunctions.py` 将关键函数匹配到数据库参数。
3. 复用仓库已有的单步推荐配置，调用兼容接口进行“未执行配置”的性能变化预测。

本次回放得到 43 个关键函数、30 个参数匹配项。实时接口结果保存在 `results/live_replay_result.json`；仅使用仓库记录结果的离线回放保存在 `results/replay_result.json`。

## 运行

只运行本地、无 API 请求的回放：

```bash
cd /path/to/checkout/sysinsight-tuning-demo
python3 replay_onestep.py
```

需要重新调用接口时，把密钥通过环境变量传入：

```bash
cd /path/to/checkout/sysinsight-tuning-demo
SYSINSIGHT_API_KEY='<你的接口密钥>' python3 replay_onestep.py --live-api
```

脚本默认使用 `gpt-5.6-sol`。密钥不会从文件读取，也不会写入结果文件。

## 当前边界

这不是完整的闭环数据库调优。LLAMBO 生成全新候选参数以及 DBTune 的“改配置→重启数据库→跑压测→比较实测结果”部分仍需要独立的 MySQL、sysbench/TPC-C/TPC-H、perf 和完整 Python 依赖环境。当前主机数据库是 PostgreSQL，不能直接套用仓库中以 MySQL/InnoDB 为核心的参数空间。

原始源码的相关子集放在 `vendor/`，输入快照放在 `inputs/`，因此后续可以继续在本目录扩展，不会影响监控目录和原始仓库快照。
