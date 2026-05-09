"""KeenInsight 完整链路主控文件

按以下 8 个步骤顺序执行：
  Step 1  数据库监控 —— 从历史性能文件读取监控数据（模拟真实采集）
  Step 2  窗口划分 + 阈值检测 —— 检查当前窗口性能是否触发异常
  Step 3  差分剖析 + SHAP 模型 —— 并行计算两路函数重要性
  Step 4  结果融合 + 根因定位 —— 合并两路结果，定位异常函数
  Step 5  静态分析 —— 将异常函数映射到数据库源码 knob
  Step 6  知识检索 —— 检索知识库（当前为空库，返回空列表）
  Step 7  推荐配置 —— 生成 knob 调优方案
  Step 8  应用配置 —— 将推荐配置写入数据库（需要 MySQL 连接）

用法：
    cd /root/KeenInsight/one
    python3 run_pipeline.py                        # 完整运行（Step 8 需要 MySQL）
    python3 run_pipeline.py --skip-apply           # 跳过 Step 8
    python3 run_pipeline.py --workload tpcc        # 指定 workload（sysbench/tpcc/tpch）
    python3 run_pipeline.py --perf-file <path>     # 指定异常 perf 文件
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

# ── 确保项目根目录在 sys.path ─────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import (
    BASELINE_BY_WORKLOAD,
    DB_CONFIG_FILE,
    HISTORY_PERF_SYSBENCH,
    HISTORY_PERF_PGBENCH,
    HISTORY_PERF_CHBENCH,
    MODEL_BY_WORKLOAD,
    NORMAL_SYSBENCH,
    PERF_OUTPUT_DIR,
    STATIC_LIB_FILE,
)


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _banner(step: int, title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  Step {step}: {title}")
    print(f"{'='*60}")


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _info(msg: str) -> None:
    print(f"  [INFO] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: 数据库监控
# ─────────────────────────────────────────────────────────────────────────────

def step1_monitor(workload: str, perf_file: str | None) -> dict[str, Any]:
    """从历史性能文件读取监控数据，模拟真实采集结果。

    真实场景下这里会调用 PerfCollector.collect() + convert_to_txt() +
    get_perf_function_range() 采集实时 perf 数据。
    """
    _banner(1, "数据库监控 (Database Monitoring)")

    # Select history file based on workload
    _HISTORY_MAP = {
        "pgbench": HISTORY_PERF_PGBENCH,
        "chbenchmark": HISTORY_PERF_CHBENCH,
    }
    history_file = _HISTORY_MAP.get(workload, HISTORY_PERF_SYSBENCH)

    _info(f"读取历史性能文件: {history_file}")

    with open(history_file) as f:
        history = json.load(f)

    records = history.get("data", [])
    if not records:
        raise RuntimeError(f"历史文件中没有数据记录: {history_file}")

    latest = records[-1]
    ext = latest.get("external_metrics", {})
    config = latest.get("configuration", {})

    _ok(f"读取到 {len(records)} 条历史记录，取最新一条")
    _info(f"TPS={ext.get('tps'):.2f}  LAT={ext.get('lat'):.2f}ms  QPS={ext.get('qps'):.2f}")
    _info(f"当前配置包含 {len(config)} 个 knob")

    # 解析 perf 文件路径
    raw_func_file: str = latest.get("function_file", "")
    if perf_file:
        resolved_perf = perf_file
    elif raw_func_file:
        resolved_perf = raw_func_file if os.path.isabs(raw_func_file) else os.path.join(_HERE, raw_func_file)
    else:
        resolved_perf = ""

    if resolved_perf and os.path.exists(resolved_perf):
        _ok(f"找到 perf 函数文件: {resolved_perf}")
    else:
        _warn(f"perf 函数文件不存在: {resolved_perf}  (差分剖析将跳过)")
        resolved_perf = ""

    baseline_file = BASELINE_BY_WORKLOAD.get(workload, NORMAL_SYSBENCH)
    if os.path.exists(baseline_file):
        _ok(f"基线文件: {baseline_file}")
    else:
        _warn(f"基线文件不存在: {baseline_file}")
        baseline_file = ""

    return {
        "workload": workload,
        "external_metrics": ext,
        "configuration": config,
        "perf_file": resolved_perf,
        "baseline_file": baseline_file,
        "history_json": history_file,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: 窗口划分 + 阈值检测
# ─────────────────────────────────────────────────────────────────────────────

def step2_threshold(monitor_data: dict[str, Any]) -> dict[str, Any]:
    """将监控数据划分窗口，检查是否触发性能异常。"""
    _banner(2, "窗口划分 + 阈值检测 (Windowed Threshold Check)")

    from keen_insight.anomaly_diagnosis.windowed_system_performance import WindowedSystemPerformance
    from keen_insight.anomaly_diagnosis.threshold_check import ThresholdCheck
    from keen_insight.models import MonitoringSnapshot

    ext = monitor_data["external_metrics"]
    workload = monitor_data["workload"]

    # 构造 MonitoringSnapshot（用外部指标模拟时序）
    snapshot = MonitoringSnapshot(
        system_metrics={
            "tps": [ext.get("tps", 0.0)],
            "lat": [ext.get("lat", 0.0)],
            "qps": [ext.get("qps", 0.0)],
        },
        db_metrics={
            "configuration": monitor_data["configuration"],
            "external_metrics": ext,
            "shap_input": {
                "json_path": monitor_data["history_json"],
                "top_k": 60,
            },
            **(
                {
                    "baseline_profile": {"function_file": monitor_data["baseline_file"]},
                    "abnormal_profile": {"function_file": monitor_data["perf_file"]},
                }
                if monitor_data["perf_file"] and monitor_data["baseline_file"]
                else {}
            ),
        },
    )

    analyzer = WindowedSystemPerformance(window_size=10)
    checker = ThresholdCheck(scene=workload)

    windows = analyzer.build_windows(snapshot)
    summaries = analyzer.summarize_window_metrics(windows)
    violations = checker.check_system_thresholds(summaries)

    _info(f"划分窗口数: {len(windows)}")
    _info(f"窗口摘要数: {len(summaries)}")

    if violations:
        _warn(f"检测到 {len(violations)} 个阈值违规:")
        for v in violations:
            _warn(f"  metric={v['metric']}  value={v['value']}  threshold={v['threshold']}  direction={v['direction']}")
    else:
        _ok("所有指标均在阈值范围内（无异常触发）")

    return {
        "snapshot": snapshot,
        "violations": violations,
        "anomaly_detected": len(violations) > 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: 差分剖析 + SHAP 模型（并行计算）
# ─────────────────────────────────────────────────────────────────────────────

def step3_profiling(monitor_data: dict[str, Any]) -> dict[str, Any]:
    """并行运行差分剖析和 SHAP 模型，得到两路函数重要性结果。"""
    _banner(3, "差分剖析 + SHAP 模型 (Differential Profiling & SHAP)")

    from DifferentialPofiling import DifferentialProfiling
    from ShapModel import SHAPModel

    workload = monitor_data["workload"]
    paths = MODEL_BY_WORKLOAD.get(workload, MODEL_BY_WORKLOAD["sysbench"])

    # ── 3a: 差分剖析 ──────────────────────────────────────────────────────────
    diff_functions: list[dict] = []
    if monitor_data["perf_file"] and monitor_data["baseline_file"]:
        _info("运行差分剖析 ...")
        t0 = time.time()
        profiler = DifferentialProfiling()
        diff = profiler.compare_profiles(
            {"function_file": monitor_data["baseline_file"]},
            {"function_file": monitor_data["perf_file"]},
        )
        diff_functions = profiler.rank_changed_features(diff)
        _ok(f"差分剖析完成，耗时 {time.time()-t0:.2f}s，发现 {len(diff_functions)} 个异常函数")
        for item in diff_functions[:5]:
            _info(f"  {item['function_name']}  diff={item['diff_from_mean']:.3f}  change={'↑' if item['change'] else '↓'}")
        if len(diff_functions) > 5:
            _info(f"  ... 共 {len(diff_functions)} 个")
    else:
        _warn("跳过差分剖析（缺少 perf 文件或基线文件）")

    # ── 3b: SHAP 模型 ─────────────────────────────────────────────────────────
    shap_functions: list[dict] = []
    _info("运行 SHAP 模型 ...")
    t0 = time.time()
    try:
        # SHAP 模型需要 txt_folder 中能找到 function_file 的 basename
        # 对于 chbenchmark，perf 文件在 PERFORMANCE_DIR 而非 PERF_OUTPUT_DIR
        perf_file = monitor_data.get("perf_file", "")
        shap_txt_folder = os.path.dirname(perf_file) if perf_file else PERF_OUTPUT_DIR
        if not shap_txt_folder or not os.path.isdir(shap_txt_folder):
            shap_txt_folder = PERF_OUTPUT_DIR
        os.makedirs(shap_txt_folder, exist_ok=True)

        shap_model = SHAPModel(
            model_path=paths["model"],
            mapping_json_path=paths["mapping"],
            static_lib_path=STATIC_LIB_FILE,
            txt_folder=shap_txt_folder,
        )
        shap_functions = shap_model.explain_system_features({
            "json_path": monitor_data["history_json"],
            "top_k": 60,
        })
        _ok(f"SHAP 模型完成，耗时 {time.time()-t0:.2f}s，返回 {len(shap_functions)} 个特征")
        for item in shap_functions[:5]:
            _info(f"  {item.get('function_name')}  shap={item.get('shap', 0):.5f}")
        if len(shap_functions) > 5:
            _info(f"  ... 共 {len(shap_functions)} 个")
    except ValueError as e:
        if "All-zero feature vector" in str(e) or "do not match" in str(e):
            _warn(f"SHAP 模型函数名不匹配（MySQL 模型 vs PostgreSQL 性能数据），切换到直接函数分析模式")
            # 使用差分剖析结果作为 SHAP 的替代，直接从 perf 文件分析函数重要性
            shap_functions = _analyze_functions_directly(perf_file, monitor_data.get("history_json", ""), 60)
            _ok(f"直接函数分析完成，返回 {len(shap_functions)} 个特征")
        else:
            _warn(f"SHAP 模型失败: {e}")
            shap_functions = []
    except Exception as e:
        _warn(f"SHAP 模型失败: {e}")
        shap_functions = []

    return {
        "diff_functions": diff_functions,
        "shap_functions": shap_functions,
    }


def _analyze_functions_directly(perf_file: str, history_json: str, top_k: int) -> list[dict]:
    """当 SHAP 模型不适用时，直接从 perf 数据分析函数重要性。"""
    if not perf_file or not os.path.exists(perf_file):
        return []

    import pandas as pd
    try:
        df = pd.read_csv(perf_file, sep='\t')
        if 'Sampling Rate (%)' in df.columns:
            df['Sampling Rate (%)'] = df['Sampling Rate (%)'].str.replace('%', '').astype(float)
        elif 'Sampling Rate' in df.columns:
            df['Sampling Rate (%)'] = df['Sampling Rate'].astype(float)

        # 按采样率排序，取 top_k
        df = df.sort_values('Sampling Rate (%)', ascending=False).head(top_k)

        results = []
        for _, row in df.iterrows():
            rate = row.get('Sampling Rate (%)', 0)
            func = row.get('Function', '')
            if func:
                results.append({
                    'function_name': func,
                    'shap': rate / 100.0,  # 归一化为类似 SHAP 的分数
                    'source': 'direct_analysis'
                })
        return results
    except Exception:
        return []

    return {
        "diff_functions": diff_functions,
        "shap_functions": shap_functions,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 4: 结果融合 + 根因定位
# ─────────────────────────────────────────────────────────────────────────────

def step4_root_cause(profiling_result: dict[str, Any]) -> dict[str, Any]:
    """融合差分剖析和 SHAP 两路结果，定位异常函数列表。"""
    _banner(4, "结果融合 + 根因定位 (Root Cause Localization)")

    diff_functions: list[dict] = profiling_result["diff_functions"]
    shap_functions: list[dict] = profiling_result["shap_functions"]

    # 融合策略：SHAP 函数按 shap 值排序，差分函数按 diff_from_mean 排序
    # 取两路结果的并集，优先保留 SHAP 排名靠前的函数
    seen: set[str] = set()
    merged: list[dict] = []

    for item in shap_functions:
        fn = item.get("function_name")
        if fn and fn not in seen:
            seen.add(fn)
            merged.append({
                "function_name": fn,
                "score": float(item.get("shap", 0.0)),
                "source": "shap",
            })

    for item in diff_functions:
        fn = item.get("function_name")
        if fn and fn not in seen:
            seen.add(fn)
            merged.append({
                "function_name": fn,
                "score": float(item.get("diff_from_mean", 0.0)),
                "source": "diff",
            })

    # 按分数降序
    merged.sort(key=lambda x: x["score"], reverse=True)

    _ok(f"融合后共 {len(merged)} 个候选异常函数")
    _info("Top 10 异常函数:")
    for i, item in enumerate(merged[:10]):
        _info(f"  [{i+1:2d}] {item['function_name']:<50s}  score={item['score']:.5f}  来源={item['source']}")

    return {"merged_functions": merged}


# ─────────────────────────────────────────────────────────────────────────────
# Step 5: 静态分析 —— 函数 → knob 映射
# ─────────────────────────────────────────────────────────────────────────────

def step5_static_analysis(root_cause_result: dict[str, Any]) -> dict[str, Any]:
    """通过静态分析库将异常函数映射到数据库 knob。"""
    _banner(5, "静态分析 (Static Analysis: Function → Knob)")

    from StaticAnalysis import StaticAnalysis

    merged_functions: list[dict] = root_cause_result["merged_functions"]
    function_names = [item["function_name"] for item in merged_functions]

    sa = StaticAnalysis(STATIC_LIB_FILE)
    result = sa.analyze_functions(function_names)

    func_to_knobs: dict = result.get("function_to_knobs", {})
    matched_knobs: list[dict] = result.get("matched_knobs", [])

    if func_to_knobs:
        _ok(f"找到 {len(func_to_knobs)} 个函数有对应 knob 映射:")
        for fn, knobs in func_to_knobs.items():
            _info(f"  {fn} => {knobs}")
    else:
        _warn("静态分析库中未找到匹配的函数（库中函数名为短 C 风格，与 SHAP 输出的 C++ 名存在差异）")

    if matched_knobs:
        _ok(f"共匹配到 {len(matched_knobs)} 个 knob:")
        for item in matched_knobs:
            _info(f"  knob={item['knob_name']}  "
                  f"data_flow={item['data_flow_functions']}  "
                  f"control_flow={item['control_flow_functions']}")
    else:
        _warn("未匹配到任何 knob（知识库为空或函数名不匹配）")

    return {
        "func_to_knobs": func_to_knobs,
        "matched_knobs": matched_knobs,
        "candidate_knob_names": list({
            item["knob_name"] for item in matched_knobs if item.get("knob_name")
        }),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 6: 知识检索
# ─────────────────────────────────────────────────────────────────────────────

def step6_knowledge_retrieval(static_result: dict[str, Any]) -> dict[str, Any]:
    """检索知识库，获取与候选 knob 相关的调优经验。"""
    _banner(6, "知识检索 (Knowledge Retrieval)")

    from keen_insight.anomaly_resolution.knowledge_retrieval import KnowledgeRetrieval
    from keen_insight.models import DiagnosisResult

    candidate_knobs = static_result["candidate_knob_names"]

    diagnosis = DiagnosisResult(
        category="system",
        summary=f"Anomalous functions mapped to knobs: {candidate_knobs}",
        root_causes=candidate_knobs,
        confidence=1.0 if candidate_knobs else 0.0,
        evidence={"matched_knobs": static_result["matched_knobs"]},
    )

    retriever = KnowledgeRetrieval()
    knowledge = retriever.retrieve(diagnosis, candidate_knobs)

    if knowledge:
        _ok(f"检索到 {len(knowledge)} 条知识条目:")
        for entry in knowledge[:3]:
            _info(f"  [{entry.source}] {entry.title}: {entry.content[:80]}")
    else:
        _info("知识库当前为空，返回空列表（不影响后续流程）")

    return {
        "diagnosis": diagnosis,
        "knowledge": knowledge,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 7: 推荐配置
# ─────────────────────────────────────────────────────────────────────────────

def step7_recommend(knowledge_result: dict[str, Any]) -> dict[str, Any]:
    """根据根因和知识库生成 knob 调优方案。"""
    _banner(7, "推荐配置 (Knob Recommendation)")

    from keen_insight.anomaly_resolution.target_knob_selection import TargetKnobSelection
    from keen_insight.anomaly_resolution.system_knob_tuning import SystemKnobTuning

    diagnosis = knowledge_result["diagnosis"]
    knowledge = knowledge_result["knowledge"]

    selector = TargetKnobSelection()
    tuner = SystemKnobTuning()

    candidates = selector.select_targets(diagnosis)
    ranked = selector.rank_targets(candidates, knowledge)
    plan = tuner.generate_plan(diagnosis, ranked, knowledge)

    _ok(f"生成调优方案: type={plan.plan_type}, target={plan.target}")
    _info(f"目标 knob 数量: {len(ranked)}")

    if plan.actions:
        _ok(f"调优动作 ({len(plan.actions)} 条):")
        for action in plan.actions:
            _info(f"  {action}")
    else:
        _warn("无具体调优动作（候选 knob 为空）")
        _info("提示：当静态分析库中的函数名与 SHAP/差分输出匹配时，此处将自动生成调优建议")

    _info(f"验证步骤: {plan.validation_steps}")
    _info(f"估算成本: {plan.estimated_cost}")

    # 构造可直接传给 ApplyKnob 的 knob 字典（使用 knob 默认值作为推荐值）
    recommended_knobs = _build_recommended_knobs(ranked)

    return {
        "plan": plan,
        "ranked_knobs": ranked,
        "recommended_knobs": recommended_knobs,
    }


def _build_recommended_knobs(knob_names: list[str]) -> dict[str, Any]:
    """从 mysql_knobs.json 读取每个 knob 的默认值，构造推荐字典（MySQL 模式）。"""
    from config import KNOB_CONFIG_FILE
    import json

    try:
        with open(KNOB_CONFIG_FILE) as f:
            raw = json.load(f)
    except Exception:
        return {}

    if isinstance(raw, dict):
        meta = raw
    elif isinstance(raw, list):
        meta = {item["name"]: item for item in raw if isinstance(item, dict) and "name" in item}
    else:
        return {}

    result: dict[str, Any] = {}
    for name in knob_names:
        info = meta.get(name)
        if not isinstance(info, dict):
            continue
        default = info.get("default")
        if default is not None:
            try:
                result[name] = int(default)
            except (ValueError, TypeError):
                result[name] = default
    return result


# MySQL knob 名 → PostgreSQL knob 名的映射表
# 仅保留有语义对应关系的条目；无对应的 MySQL 专属 knob 不在此表中
_MYSQL_TO_PG_KNOB: dict[str, str] = {
    "innodb_buffer_pool_size":        "shared_buffers",
    "innodb_buffer_pool_instances":   "shared_buffers",
    "innodb_log_buffer_size":         "wal_buffers",
    "innodb_log_file_size":           "max_wal_size",
    "innodb_flush_log_at_trx_commit": "synchronous_commit",
    "innodb_io_capacity":             "effective_io_concurrency",
    "innodb_io_capacity_max":         "effective_io_concurrency",
    "innodb_thread_concurrency":      "max_worker_processes",
    "innodb_lock_wait_timeout":       "lock_timeout",
    "innodb_deadlock_detect":         "deadlock_timeout",
    "lock_wait_timeout":              "lock_timeout",
    "wait_timeout":                   "idle_in_transaction_session_timeout",
    "max_connections":                "max_connections",
    "tmp_table_size":                 "temp_buffers",
    "max_heap_table_size":            "temp_buffers",
    "sort_buffer_size":               "work_mem",
    "join_buffer_size":               "work_mem",
    "read_rnd_buffer_size":           "work_mem",
    "read_buffer_size":               "work_mem",
    "bulk_insert_buffer_size":        "maintenance_work_mem",
    "myisam_sort_buffer_size":        "maintenance_work_mem",
    "open_files_limit":               "max_files_per_process",
    "table_open_cache":               "max_files_per_process",
}

# 每个 PG knob 的推荐调整幅度（相对当前值的倍数）
# 大于 1 表示建议调大，小于 1 表示建议调小
_PG_KNOB_TUNE_FACTOR: dict[str, float] = {
    "shared_buffers":                    8.0,    # 32MB → 256MB
    "wal_buffers":                       2.0,
    "max_wal_size":                      4.0,    # 256MB → 1GB
    "work_mem":                          16.0,   # 1MB → 16MB
    "maintenance_work_mem":              8.0,    # 16MB → 128MB
    "temp_buffers":                      2.0,
    "effective_io_concurrency":          200.0,  # 1 → 200 (absolute, handled below)
    "effective_cache_size":              4.0,    # 512MB → 2GB
    "max_worker_processes":              1.0,
    "max_files_per_process":             1.0,
    "lock_timeout":                      1.0,
    "deadlock_timeout":                  1.0,
    "idle_in_transaction_session_timeout": 1.0,
    "max_connections":                   1.0,
    "synchronous_commit":                1.0,
}


def _parse_pg_value(setting: str, vartype: str, unit: str = '') -> float | None:
    """解析 PostgreSQL 配置值，支持带单位的后缀（如 32MB, 256kB, 8kB）。

    PostgreSQL 在 pg_settings 中存储的是内部单位（如 8kB blocks）的数量。
    例如：shared_buffers = 4096 表示 4096 * 8kB = 32MB。
    """
    if not setting:
        return None
    s = str(setting).strip()

    # PostgreSQL 内部单位的乘数
    unit_multipliers = {
        'B': 1,
        'kB': 1024,
        'KB': 1024,
        'MB': 1024**2,
        'GB': 1024**3,
        'TB': 1024**4,
        '8kB': 8192,      # PostgreSQL 的默认块大小
        '8k': 8192,
    }

    # 如果 setting 本身包含单位
    for u, mult in unit_multipliers.items():
        if s.endswith(u):
            try:
                num_part = s[:-len(u)].strip()
                return float(num_part) * mult
            except ValueError:
                pass

    # 如果没有单位，使用 meta 中的 unit
    base_mult = 1
    if unit:
        u = unit.strip()
        base_mult = unit_multipliers.get(u, unit_multipliers.get(u.replace('B', 'B'), 1))

    try:
        return float(s) * base_mult
    except ValueError:
        pass

    return None


def _format_pg_value(value: float, vartype: str, meta: dict) -> str:
    """将数值格式化为 PostgreSQL 可接受的格式。

    PostgreSQL 的参数通常以内部单位存储（如 8kB blocks）。
    输出时需要转换成合适的格式。
    """
    unit = meta.get('unit', '')
    abs_val = abs(value)

    # PostgreSQL 特殊单位 - 需要根据实际值大小决定输出格式
    if unit in ('8kB', '8k'):
        # 8kB 是 PostgreSQL 默认块大小
        blocks = value / 8192
        if blocks >= 131072:  # >= 1GB (131072 * 8kB = 1GB)
            gb_val = blocks / 131072.0  # 块数/131072 = GB 数
            return f"{gb_val:.0f}GB"
        elif blocks >= 128:  # >= 1MB (128 * 8kB = 1MB)
            mb_val = blocks / 128.0  # 块数/128 = MB 数
            return f"{mb_val:.0f}MB"
        else:
            return f"{int(blocks)}"  # 返回块数 (小于 1MB)
    elif unit in ('kB', 'KB'):
        kb = value / 1024
        if kb >= 1024 * 1024:  # >= 1TB
            return f"{kb/1024:.0f}TB"
        elif kb >= 1024 * 64:  # >= 64GB
            return f"{kb/1024:.0f}GB"
        elif kb >= 1024:  # >= 1GB
            return f"{kb/1024:.0f}GB"
        elif kb >= 64:  # >= 64MB
            return f"{kb:.0f}MB"
        else:
            return f"{kb:.0f}kB"
    elif unit == 'MB':
        mb = value / (1024**2)
        if mb >= 1024:
            return f"{mb/1024:.0f}GB"
        elif mb >= 1:
            return f"{mb:.0f}MB"
        else:
            return f"{value:.0f}MB"
    elif unit == 'B':
        if abs_val >= 1024**3:
            return f"{value/(1024**3):.0f}GB"
        elif abs_val >= 1024**2:
            return f"{value/(1024**2):.0f}MB"
        elif abs_val >= 1024:
            return f"{value/1024:.0f}kB"
        return str(int(value))
    elif vartype == 'bool':
        return 'on' if value else 'off'
    elif vartype == 'real':
        return f"{value:.2f}"
    elif abs_val >= 1024**3:
        return f"{value/(1024**3):.0f}GB"
    elif abs_val >= 1024**2:
        return f"{value/(1024**2):.0f}MB"
    elif abs_val >= 1024:
        return f"{value/1024:.0f}kB"
    return str(int(value))


def _build_recommended_knobs_pg(knob_names: list[str]) -> dict[str, Any]:
    """从候选 knob 列表生成 PG 推荐值。

    knob_names 可以是 MySQL knob 名（自动翻译）或直接的 PG knob 名。
    推荐值 = 当前值 × 调整系数（见 _PG_KNOB_TUNE_FACTOR），取整后不超过 max_val。
    """
    from pg_connector import PGConnector

    # 收集需要调优的 PG knob 名（去重）
    pg_knob_names: list[str] = []
    seen: set[str] = set()
    for name in knob_names:
        # 如果本身就是 PG knob 名（在 _PG_KNOB_TUNE_FACTOR 中），直接使用
        if name in _PG_KNOB_TUNE_FACTOR:
            if name not in seen:
                seen.add(name)
                pg_knob_names.append(name)
        else:
            # 尝试 MySQL → PG 翻译
            pg_name = _MYSQL_TO_PG_KNOB.get(name)
            if pg_name and pg_name not in seen:
                seen.add(pg_name)
                pg_knob_names.append(pg_name)

    if not pg_knob_names:
        return {}

    pg = PGConnector()
    pg.connect()
    all_knobs = pg.collect_knobs()
    pg.close()

    result: dict[str, Any] = {}
    for pg_name in pg_knob_names:
        meta = all_knobs.get(pg_name)
        if not meta:
            continue
        vartype = meta.get("vartype", "")
        unit = meta.get("unit", "")
        if vartype not in ("integer", "real", "bool"):
            continue

        current = _parse_pg_value(str(meta["setting"]), vartype, unit)
        if current is None:
            continue

        factor = _PG_KNOB_TUNE_FACTOR.get(pg_name, 1.0)
        recommended = current * factor

        # 不超过 max_val
        max_val = _parse_pg_value(str(meta["max_val"]), vartype, unit) if meta.get("max_val") else None
        if max_val is not None and recommended > max_val:
            recommended = max_val

        # 不低于 min_val
        min_val = _parse_pg_value(str(meta["min_val"]), vartype, unit) if meta.get("min_val") else None
        if min_val is not None and recommended < min_val:
            recommended = min_val

        # 格式化为 PostgreSQL 格式
        result[pg_name] = _format_pg_value(recommended, vartype, meta)
        _info(f"  {pg_name}: {meta['setting']} ({unit}) → {result[pg_name]} (factor={factor:.1f})")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Step 8: 应用配置
# ─────────────────────────────────────────────────────────────────────────────

def step8_apply(
    recommend_result: dict[str, Any],
    skip: bool = False,
    db_type: str = "pg",
) -> None:
    """将推荐配置应用到数据库。

    db_type='pg'    → 连接本机 PostgreSQL，knob 名自动从 MySQL 名翻译为 PG 名
    db_type='mysql' → 连接 MySQL（需要 database/config_template.ini 配置正确）
    """
    _banner(8, f"应用配置 (Apply Knobs → {db_type.upper()})")

    if skip:
        _info("--skip-apply 已设置，跳过此步骤")
        return

    ranked_knobs: list[str] = recommend_result.get("ranked_knobs", [])

    if db_type == "pg":
        recommended_knobs = _build_recommended_knobs_pg(ranked_knobs)
        if not recommended_knobs:
            _warn("PG 模式下未找到可调优的 knob")
            _info(f"候选 knob: {ranked_knobs}")
            return
        _info(f"目标 PG knob: {list(recommended_knobs.keys())}")
        _info(f"推荐值: {recommended_knobs}")
        try:
            from ApplyKnob import ApplyKnob
            applier = ApplyKnob.from_pg_connector(online_mode=True, reinit=False)
            success = applier.apply(recommended_knobs)
            if success:
                _ok("配置已成功应用到 PostgreSQL 数据库")
            else:
                _fail("配置应用失败（请检查 PostgreSQL 连接和权限）")
        except Exception as e:
            _warn(f"无法连接 PostgreSQL，跳过应用步骤: {e}")
            _info(f"推荐配置（可手动应用）: {recommended_knobs}")

    else:  # mysql
        recommended_knobs = recommend_result.get("recommended_knobs", {})
        if not recommended_knobs:
            _warn("推荐配置为空，无需应用")
            _info("原因：静态分析未匹配到 knob，或 knob 无默认值可用")
            return
        _info(f"准备应用 {len(recommended_knobs)} 个 knob 到 MySQL: {list(recommended_knobs.keys())}")
        try:
            from ApplyKnob import ApplyKnob
            applier = ApplyKnob.from_config(
                config_path=DB_CONFIG_FILE,
                knob_num=-1,
                online_mode=True,
                reinit=False,
            )
            success = applier.apply(recommended_knobs)
            if success:
                _ok("配置已成功应用到 MySQL 数据库")
            else:
                _fail("配置应用失败（请检查 MySQL 连接和权限）")
        except Exception as e:
            _warn(f"无法连接 MySQL，跳过应用步骤: {e}")
            _info(f"推荐配置（可手动应用）: {recommended_knobs}")


# ─────────────────────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KeenInsight 完整链路主控",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--workload",
        choices=["sysbench", "tpcc", "tpch", "pgbench", "chbenchmark"],
        default="sysbench",
        help="工作负载类型，决定基线文件和模型选择（默认: sysbench）",
    )
    parser.add_argument(
        "--perf-file",
        default=None,
        metavar="PATH",
        help="指定异常 perf 函数文件路径（默认从历史 JSON 中读取）",
    )
    parser.add_argument(
        "--skip-apply",
        action="store_true",
        help="跳过 Step 8（不实际写入数据库）",
    )
    parser.add_argument(
        "--db-type",
        choices=["pg", "mysql"],
        default="pg",
        help="Step 8 目标数据库类型：pg（PostgreSQL，默认）或 mysql",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("\n" + "=" * 60)
    print("  KeenInsight 数据库性能异常诊断与调优系统")
    print(f"  workload={args.workload}  db_type={args.db_type}  skip_apply={args.skip_apply}")
    print("=" * 60)

    t_start = time.time()

    # Step 1: 监控
    monitor_data = step1_monitor(args.workload, args.perf_file)

    # Step 2: 窗口 + 阈值
    threshold_result = step2_threshold(monitor_data)

    # Step 3: 差分剖析 + SHAP（两路都跑，不因阈值未触发而跳过）
    profiling_result = step3_profiling(monitor_data)

    # Step 4: 融合 + 根因定位
    root_cause_result = step4_root_cause(profiling_result)

    # Step 5: 静态分析
    static_result = step5_static_analysis(root_cause_result)

    # Step 6: 知识检索
    knowledge_result = step6_knowledge_retrieval(static_result)

    # Step 7: 推荐配置
    recommend_result = step7_recommend(knowledge_result)

    # Step 8: 应用配置
    step8_apply(recommend_result, skip=args.skip_apply, db_type=args.db_type)

    elapsed = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"  全流程完成，总耗时 {elapsed:.2f}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
