# 场景目录

此目录包含不同的负载场景，用于触发 KeenInsight 的诊断和调优功能。

## 当前场景

| 场景 | 名称 | 状态 |
|------|------|------|
| 1 | CH-Benchmark (OLAP) | ✅ 已完成 |
| 2 | pgbench (OLTP) | 📋 待开发 |
| 3 | 自定义 SQL | 📋 待开发 |

## 使用方式

1. 运行场景触发负载:
   ```bash
   cd scenarios/scenario_1_chbenchmark
   python3 run.py
   ```

2. 运行诊断分析:
   ```bash
   cd /root/KeenInsight/one
   python3 run_pipeline.py --workload chbenchmark
   ```

## 添加新场景

参见 `template/README.md`
