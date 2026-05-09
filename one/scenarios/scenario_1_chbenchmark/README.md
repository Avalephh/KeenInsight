# 场景一: CH-Benchmark 负载

## 概述
运行 ClickHouse Benchmark (CH-Benchmark) 产生混合分析型查询负载，模拟 OLAP 场景下的性能问题。

## 使用方法

```bash
cd /root/KeenInsight/one/scenarios/scenario_1_chbenchmark

# 默认运行 (60秒, 30并发)
python3 run.py

# 自定义参数
python3 run.py --duration 120 --terminals 50

# 跳过 perf 采集 (加快运行)
python3 run.py --skip-perf

# 使用优化配置运行
python3 run.py --mode tuned
```

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| --duration | 60 | 运行时间(秒) |
| --terminals | 30 | 并发终端数 |
| --mode | default | 配置模式: default=保守配置, tuned=优化配置 |
| --skip-perf | False | 是否跳过 perf 数据采集 |

## 输出文件

- `../../performance/chbench_abnormal_functions.txt` - 异常状态函数热点
- `../../performance/chbench_normal_functions.txt` - 优化状态函数热点
- `../../performance/history_performance-chbenchmark.json` - 历史性能数据

## 依赖

- BenchBase (CH-Benchmark)
- PostgreSQL 12
- perf tools
