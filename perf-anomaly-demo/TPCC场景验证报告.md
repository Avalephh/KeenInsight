# TPCC 外部压力与 SysInsight API 闭环验证

更新时间：2026-09-12

本轮使用本机 `keeninsight` PostgreSQL 12 的真实 TPCC 数据。外部阶段只运行 TPCC 的 New Order、Payment、Delivery、Order Status、Stock Level 路径；正常控制负载为 `tp_normal.sql`。每组均执行：

`Prometheus 告警 → perf 采样 → SysInsight 原始检测函数 → 用户提供的 GPT5.6-SOL API → 原始选择器选出的配置 → 临时应用 → 同一 TPCC 控制负载复测 → 配置还原`。

## 判定标准

- 压力阶段控制 TPS ≤ 无压力基线的 80%。
- 修复阶段控制 TPS ≥ 压力阶段的 120%。
- 修复阶段控制 TPS ≥ 无压力基线的 80%。
- Prometheus 告警、perf、API 配置应用和配置还原都成功。

## 达标的 6 组场景

| 场景 | 正常基线 TPS | 外部压力 TPS | 修复后 TPS | 压力下降 | 恢复到基线 | 较压力提升 |
|---|---:|---:|---:|---:|---:|---:|
| Order Status 突发 | 1568.82 | 681.23 | 1382.74 | 56.6% | 88.1% | 103.0% |
| Stock Level 突发 | 1567.95 | 813.32 | 1478.75 | 48.1% | 94.3% | 81.8% |
| Payment 中等突发 | 1582.75 | 1134.94 | 1584.37 | 28.3% | 100.1% | 39.6% |
| Delivery 突发 | 1488.06 | 944.52 | 1272.71 | 36.5% | 85.5% | 34.7% |
| 热点 Stock Level | 1585.63 | 813.58 | 1523.32 | 48.7% | 96.1% | 87.2% |
| 热点 Payment | 1571.95 | 1150.37 | 1521.64 | 26.8% | 96.8% | 32.3% |

上表的配置不是预设修复值，均来自 API 原始响应并由原始选择器选中。主要实际变更如下；每组还应用了 API 返回的其它字段：

| 场景 | API 返回中影响较大的实际变更 |
|---|---|
| Order Status 突发 | `fsync on→off`、`synchronous_commit on→off`、`jit on→off`、`shared_buffers 16384→1966173`、并行 worker/gather 调整 |
| Stock Level 突发 | `fsync on→off`、`synchronous_commit on→off`、`jit on→off`、`shared_buffers 16384→1048319`、`work_mem 4096→12277` |
| Payment 中等突发 | `port 5432→5543`、`shared_buffers 16384→2007913`、`max_wal_size 1024→6143`、`max_connections 100→23`、`effective_cache_size` 调整 |
| Delivery 突发 | `jit on→off`、`shared_buffers 16384→1572839`、`work_mem 4096→8189`、并行成本/阈值调整 |
| 热点 Stock Level | `fsync on→off`、`synchronous_commit on→off`、`jit on→off`、`shared_buffers 16384→1009253`、`work_mem 4096→12281` |
| 热点 Payment | `synchronous_commit on→off`、`shared_buffers 16384→1638397`、`work_mem 4096→12287`、`max_wal_size 1024→6143`、锁超时参数调整 |

数值是 PostgreSQL `pg_settings` 的原始单位（例如 `shared_buffers` 的单位为 8kB）。这些配置只在测试上下文中临时应用，6 组的 `api_configuration_restored` 均为真，测试结束后服务回到 `5432`。`fsync=off` 等值不适合生产环境，本报告只记录真实 Demo 行为，不作生产建议。

## 未达标结果

本轮场景库有 27 个定义，25 个唯一场景完成了新标准闭环验证；包含重复复测在内共生成 31 个严格结果文件，其中 6 组达标，19 个唯一场景未达标。未达标不是被隐藏：例如：

| 场景 | 压力下降 | 修复后恢复到基线 | 结论 |
|---|---:|---:|---|
| New Order 突发 | 34.3% | 72.8% | API 配置提升不足 |
| Payment 高强度突发 | 51.9% | 71.4% | API 配置提升不足 |
| 远程 New Order 高强度 | 51.5% | 60.1% | API 配置提升不足 |
| Order Status/Stock Level 高强度混合读 | 61.8% | 42.5% | API 配置提升不足 |
| 五类 TPCC 高强度混合 | 59.0% | 48.5% | API 配置提升不足 |
| 热点 New Order 高强度 | 23.4% | 76.9% | 未达到 80% 恢复线 |

所有失败组仍保留完整的压力样本、perf 数据、原始 API `result.json`、应用/还原状态和 TPS 日志，不能把“检测到了”误报成“修复成功”。

## 结果证据

达标组的完整结果：

- `results/tpcc_api_validation/20260912_002346/tp_order_status_burst/case_result.json`
- `results/tpcc_api_validation/20260912_003154/tp_stock_level_burst/case_result.json`
- `results/tpcc_api_validation/20260912_023000_tp_payment_moderate_retry/tp_payment_moderate/case_result.json`
- `results/tpcc_api_validation/20260912_030000_tp_delivery_burst_retry/tp_delivery_burst/case_result.json`
- `results/tpcc_api_validation/20260912_052000_tp_stock_level_hot/tp_stock_level_hot/case_result.json`
- `results/tpcc_api_validation/20260912_054000_tp_payment_hot/tp_payment_hot/case_result.json`

上述 `case_result.json` 同一场景目录中的 `selected_api_configuration.json` 和
`anomaly/sysinsight_source_detection_result.json` 也作为结构化凭证提交；对应的真实 API 原始结果为：

- `results/tpcc_api_validation/20260912_002346/tp_order_status_burst/sysinsight_api/result.json`
- `results/tpcc_api_validation/20260912_003154/tp_stock_level_burst/sysinsight_api/result.json`
- `results/tpcc_api_validation/20260912_021208/tp_payment_moderate/sysinsight_api/result.json`
- `results/tpcc_api_validation/20260912_030000_tp_delivery_burst_retry/tp_delivery_burst/sysinsight_api/result.json`
- `results/tpcc_api_validation/20260912_052000_tp_stock_level_hot/tp_stock_level_hot/sysinsight_api/result.json`
- `results/tpcc_api_validation/20260912_054000_tp_payment_hot/tp_payment_hot/sysinsight_api/result.json`

Git 只保留以上结构化凭证；原始 `perf.data`、折叠栈、采样时间序列和日志仍留在产生它们的实验机上，不作为数据库数据文件上传。

复现命令示例：

```bash
cd /root/new/perf-anomaly-demo
SYSINSIGHT_GPT_API_KEY='<your-api-key>' python3 tpcc_api_recommendation_validation.py \
  --only tp_payment_hot \
  --workload-module tpcc_transaction_cases \
  --normal-sql tp_normal.sql \
  --baseline-duration 20 --case-duration 25 --tuned-duration 60 \
  --normal-clients 2 --perf-frequency 300 \
  --n-candidates 1 --n-templates 1 --selector-n-gens 1 \
  --run-source-selector
```
