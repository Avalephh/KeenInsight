# 场景模板

此文件夹用于存放新场景的模板代码。

## 创建新场景

1. 在 `scenarios/` 下创建新文件夹，如 `scenario_X_name/`
2. 包含以下文件:
   - `run.py` - 场景运行脚本
   - `README.md` - 场景说明

## 场景命名规范

- 文件夹名: `scenario_N_description`
- 例如: `scenario_4_sysbench_oltp`

## 场景应包含的功能

- 运行负载 (benchmark)
- 采集数据库指标
- 可选采集 perf 数据
- 保存结果到 history JSON
