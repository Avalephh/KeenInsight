# KeenInsight pgbench 异常检测与自动调优演示

本演示展示了 KeenInsight 如何检测 PostgreSQL 数据库的异常场景，并通过自动调优参数来修复性能问题。

## 场景概述

**异常场景**: pgbench 高并发负载 + 低内存配置
- **工作负载**: pgbench TPC-B 风格的事务处理
- **异常表现**:
  - `work_mem=512kB` (过小，导致排序溢出到磁盘)
  - `shared_buffers=32MB` (过小，缓存命中率低)
  - `maintenance_work_mem=16MB` (过小)

**检测到的异常函数**:
1. `ExecSort` - 排序操作 CPU 占用过高 (35%)
2. `ExecHashJoin` - 哈希连接 CPU 占用过高 (18%)
3. `ExecSeqScan` - 顺序扫描占用过高 (15%)

**推荐的调优方案**:
1. `work_mem`: 512kB → 16MB (增加 32 倍)
2. `shared_buffers`: 32MB → 256MB (增加 8 倍)
3. `maintenance_work_mem`: 16MB → 128MB (增加 8 倍)

## 文件结构

```
keen_insight/
├── demo_pgbench.py              # 主演示脚本
├── demo_scenario.py             # 备选演示脚本
├── pgbench_runner.sh            # pgbench 压力测试工具
├── collect_pgbench_metrics.py   # 性能指标收集器
├── performance/
│   ├── history_performance-pgbench.json        # 当前性能历史
│   ├── history_performance-pgbench-normal.json # 正常基线
│   ├── history_performance-pgbench-abnormal.json # 异常数据
│   ├── pgbench_normal_functions.txt   # 正常函数剖析
│   └── pgbench_abnormal_functions.txt # 异常函数剖析
└── database/
    └── paramater_association_library.json # 包含 PostgreSQL 函数映射
```

## 快速开始

### 方式一: 使用演示脚本 (推荐)

```bash
cd /root/KeenInsight/one

# 1. 设置异常场景
python3 demo_pgbench.py setup

# 2. 运行异常检测管道
python3 run_pipeline.py --workload pgbench --db-type pg --skip-apply

# 3. 运行完整演示 (包含自动调优)
python3 demo_pgbench.py run
```

### 方式二: 手动运行

```bash
cd /root/KeenInsight/one

# 初始化 pgbench 数据库
su - postgres -c "pgbench -i -s 10 sbtest"

# 应用异常配置
su - postgres -c "psql -d sbtest -c \"ALTER SYSTEM SET work_mem = '512kB';\""
su - postgres -c "psql -d sbtest -c \"ALTER SYSTEM SET shared_buffers = '32MB';\""
pg_ctlcluster 12 main reload

# 运行 KeenInsight 管道
python3 run_pipeline.py --workload pgbench --db-type pg

# 应用调优配置
su - postgres -c "psql -d sbtest -c \"ALTER SYSTEM SET work_mem = '16MB';\""
su - postgres -c "psql -d sbtest -c \"ALTER SYSTEM SET shared_buffers = '256MB';\""
pg_ctlcluster 12 main reload
```

## 管道流程

| 步骤 | 功能 | 输入 | 输出 |
|------|------|------|------|
| 1 | 数据库监控 | 性能历史 JSON | 监控数据 |
| 2 | 窗口划分 + 阈值检测 | 监控数据 | 异常标志 |
| 3 | 差分剖析 + SHAP | 函数剖析文件 | 异常函数列表 |
| 4 | 根因定位 | 异常函数 | 融合排序 |
| 5 | 静态分析 | 异常函数 | 函数→Knob 映射 |
| 6 | 知识检索 | 映射结果 | 调优建议 |
| 7 | 推荐配置 | 建议 | 调优方案 |
| 8 | 应用配置 | 方案 | 参数修改 |

## 关键参数说明

### PostgreSQL 参数

| 参数 | 描述 | 默认值 | 异常值 | 调优值 |
|------|------|--------|--------|--------|
| `work_mem` | 单个查询工作内存 | 5MB | 512kB | 16MB |
| `shared_buffers` | 共享缓冲区 | 128MB | 32MB | 256MB |
| `maintenance_work_mem` | 维护操作内存 | 64MB | 16MB | 128MB |
| `effective_cache_size` | 规划器缓存估计 | 4GB | 1GB | 6GB |
| `max_connections` | 最大连接数 | 100 | 20 | 100 |

### 阈值配置

编辑 `keen_insight/anomaly_diagnosis/threshold_check.py`:

```python
"pgbench": {
    "cpu_mean": 95.0,
    "mem_mean": 95.0,
    "io_mean": 90.0,
    "tps_min": 7000.0,   # 低于此值触发异常
    "lat_max": 5.0,      # 高于此值触发异常
},
```

## 扩展演示

### 添加更多函数映射

编辑 `database/paramater_association_library.json`:

```json
{
    "knob_name": "work_mem",
    "data_flow_functions": ["ExecSort", "ExecHashJoin", "ExecAgg"],
    "control_flow_functions": ["qsort_arg", "tuplesort_begin_heap"]
}
```

### 修改 pgbench 工作负载

创建自定义 pgbench 脚本:

```bash
# 创建自定义脚本
cat > /root/KeenInsight/one/custom_workload.sql << 'EOF'
\set aid random(1, 1000000)
BEGIN;
UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE aid = :aid;
SELECT * FROM pgbench_accounts WHERE aid = :aid ORDER BY abalance;
COMMIT;
EOF

# 运行自定义负载
su - postgres -c "pgbench -d sbtest -f custom_workload.sql -c 50 -j 8 -T 60"
```

## 故障排除

### 问题: SHAP 模型失败
```
[WARN] SHAP 模型失败: .../test_perf_output/...
```
**原因**: 函数剖析文件格式不正确或模型文件缺失
**解决**: 确保 `pgbench_abnormal_functions.txt` 存在且格式正确

### 问题: 差分剖析发现 0 个函数
```
[OK] 差分剖析完成，耗时 0.01s，发现 0 个异常函数
```
**原因**: 函数名在基线和异常文件中不匹配
**解决**: 检查 `pgbench_normal_functions.txt` 和 `pgbench_abnormal_functions.txt` 中的函数名

### 问题: 静态分析无匹配
```
[WARN] 静态分析库中未找到匹配的函数
```
**原因**: 函数名不在 `paramater_association_library.json` 中
**解决**: 添加函数到库文件中

## 参考资料

- [PostgreSQL 性能调优指南](https://www.postgresql.org/docs/current/runtime-config-resource.html)
- [pgbench 文档](https://www.postgresql.org/docs/current/pgbench.html)
- [KeenInsight 架构](./docs/architecture.md)
