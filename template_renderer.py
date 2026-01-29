#!/usr/bin/env python3
"""
Template renderer for DREAM HTML pages.
Reads data from results JSON files and fills HTML templates in font/.
"""

import html
import json
from pathlib import Path
from typing import Dict, List, Optional

ROOT_CAUSE_TYPES = [
    ("missing indexes", "索引选择"),
    ("inappropriate query knobs", "参数调优"),
    ("suboptimal plan optimizer", "查询计划调优"),
    ("poorly written queries", "查询改写"),
]

ROOT_CAUSE_MAP = {
    "missing indexes": "索引选择",
    "inappropriate query knobs": "参数调优",
    "suboptimal plan optimizer": "查询计划调优",
    "poorly written queries": "查询改写",
    "index_selection": "索引选择",
    "parameter_tuning": "参数调优",
    "query_plan_tuning": "查询计划调优",
    "query_rewrite": "查询改写",
}


def load_json(path: Path) -> Dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def format_query_preview(query: str, max_length: int = 100) -> str:
    query_clean = query.replace("\n", " ").strip()
    if len(query_clean) > max_length:
        return query_clean[:max_length] + "..."
    return query_clean


def format_execution_time(time_value: float) -> str:
    if time_value < 1:
        return f"{time_value:.3f} 秒"
    if time_value < 60:
        return f"{time_value:.2f} 秒"
    minutes = int(time_value // 60)
    seconds = time_value % 60
    return f"{minutes} 分 {seconds:.2f} 秒"


def format_plan_json(plan_json) -> str:
    try:
        if isinstance(plan_json, str):
            plan_data = json.loads(plan_json)
        else:
            plan_data = plan_json
        return json.dumps(plan_data, indent=2, ensure_ascii=False)
    except Exception:
        return str(plan_json)


def generate_metrics_charts(external_metrics: List[List[float]]) -> str:
    if not external_metrics or len(external_metrics) < 7:
        return ""

    metrics = [
        ("CPU 使用率", 0, "%", 100),
        ("读 I/O", 2, "MB/s", 2000),
        ("写 I/O", 3, "MB/s", 500),
        ("虚拟内存", 4, "GB", 16),
        ("物理内存", 5, "MB", 100),
        ("网络接收", 6, "MB", 100),
    ]

    charts_html = ""
    for metric_name, idx, unit, max_val in metrics:
        if idx < len(external_metrics) and len(external_metrics[idx]) > 0:
            values = external_metrics[idx]
            if metric_name == "物理内存" and unit == "MB":
                if max(values) < 1:
                    values = [v * 1024 for v in values]

            avg_val = sum(values) / len(values) if values else 0
            if avg_val > max_val * 0.8:
                status = "danger"
                status_text = "偏高"
            elif avg_val > max_val * 0.6:
                status = "warning"
                status_text = "偏高"
            else:
                status = "normal"
                status_text = "正常"

            values_str = ",".join(map(str, values))
            charts_html += f"""
      <div class="metric-card">
        <div class="metric-header">
          <h3 class="metric-name">{metric_name}</h3>
          <span class="metric-status"><span class="tag {status}">{status_text}</span></span>
        </div>
        <div class="metric-chart" data-values="{values_str}" data-status="{status}" data-max="{max_val}" data-unit="{unit}"></div>
      </div>
"""

    return charts_html


def generate_internal_metrics_table(internal_metrics: List[float]) -> str:
    metric_mapping = [
        ("返回元组数", "查询返回的元组总数"),
        ("块命中数", "从缓存中读取的块数"),
        ("块读取数", "从磁盘读取的块数"),
        ("获取元组数", "实际获取的元组数量"),
        ("索引元组获取数", "通过索引获取的元组数"),
        ("顺序元组读取数", "顺序扫描读取的元组数"),
        ("顺序扫描次数", "执行顺序扫描的次数"),
        ("索引扫描次数", "执行索引扫描的次数"),
        ("堆块命中数", "从缓存命中的堆块数"),
        ("堆块读取数", "从磁盘读取的堆块数"),
        ("索引块命中数", "从缓存命中的索引块数"),
        ("索引块读取数", "从磁盘读取的索引块数"),
    ]

    rows = ""
    for i, (name, desc) in enumerate(metric_mapping):
        if i < len(internal_metrics):
            value = int(internal_metrics[i]) if i < len(internal_metrics) else 0
            category = (
                "元组统计" if "元组" in name else
                "块统计" if "块" in name and "索引块" not in name and "堆块" not in name else
                "扫描统计" if "扫描" in name else
                "索引统计" if "索引" in name and "块" not in name else
                "堆块统计" if "堆块" in name else
                "索引块统计" if "索引块" in name else "其他"
            )

            rows += f"""
      <tr>
        <td>{category}</td>
        <td>{name}</td>
        <td>{value}</td>
        <td>{desc}</td>
      </tr>
"""

    return rows


def build_diagnosis_rows(confidence: Dict) -> str:
    rows = '<tr><th>异常类型</th><th>置信度</th></tr>'
    for root_cause, root_cause_zh in ROOT_CAUSE_TYPES:
        rc_key = root_cause.lower().replace(" ", "_")
        conf_value = confidence.get(root_cause) or confidence.get(rc_key) or 0.0
        if conf_value >= 0.8:
            tag_class = "danger"
        elif conf_value >= 0.6:
            tag_class = "warning"
        else:
            tag_class = "normal"
        rows += f"""
          <tr>
            <td>{root_cause_zh}</td>
            <td><span class="tag {tag_class}">{conf_value:.2f}</span></td>
          </tr>
"""
    return rows


def build_sql_item_html(
    query_id: str,
    query_info: Dict,
    diagnosis_result: Optional[Dict],
) -> str:
    query = query_info.get("query", "")
    execution_time = query_info.get("execution_time", 0.0)
    plan_json = query_info.get("plan_json", "")
    external_metrics = query_info.get("external_metrics", [])
    internal_metrics = query_info.get("internal_metrics", [])

    query_preview = html.escape(format_query_preview(query))
    time_str = format_execution_time(execution_time)
    plan_formatted = html.escape(format_plan_json(plan_json))
    metrics_charts = generate_metrics_charts(external_metrics)
    internal_metrics_rows = generate_internal_metrics_table(internal_metrics)
    query_escaped = html.escape(query)

    if diagnosis_result:
        confidence = diagnosis_result.get("confidence", {})
        explanation = diagnosis_result.get("explanation") or "暂无解释说明"
        diagnosis_rows = build_diagnosis_rows(confidence)
        explanation_escaped = html.escape(str(explanation))
        diagnosis_section_style = ""
        button_section_style = 'style="display: none;"'
    else:
        diagnosis_rows = build_diagnosis_rows({})
        explanation_escaped = "暂无解释说明"
        diagnosis_section_style = 'style="display: none;"'
        button_section_style = ""

    return f"""
    <!-- SQL {query_id} -->
    <div class="sql-item" data-query-id="{query_id}">
      <div class="sql-header" onclick="toggleSqlDetails('sql{query_id}')">
        <div class="sql-summary">
          <div class="sql-number">SQL #{query_id}</div>
          <div class="sql-text-preview">{query_preview}</div>
          <div class="sql-time">执行时间: <strong>{time_str}</strong></div>
        </div>
        <div class="expand-icon" id="icon-sql{query_id}">▼</div>
      </div>

      <div class="sql-details" id="details-sql{query_id}">
        <!-- 数据库信息和查询信息 -->
        <div class="info-row">
          <!-- 数据库信息 -->
          <div class="detail-section">
            <h3>数据库信息</h3>
            <div class="info-cards">
              <div class="info-card">
                <div class="info-card-label">数据库类型</div>
                <div class="info-card-value">PostgreSQL</div>
              </div>
              <div class="info-card">
                <div class="info-card-label">工作负载类型</div>
                <div class="info-card-value">OLTP</div>
              </div>
              <div class="info-card">
                <div class="info-card-label">数据库大小</div>
                <div class="info-card-value">50 GB</div>
              </div>
              <div class="info-card">
                <div class="info-card-label">负载信息</div>
                <div class="info-card-value">TPC-H</div>
              </div>
            </div>
          </div>

          <!-- 异常查询信息 -->
          <div class="detail-section query-info-section">
            <h3>异常查询信息</h3>
            <div class="info-cards">
              <div class="info-card full-width">
                <div class="info-card-label">SQL 语句</div>
                <div class="query-info">{query_escaped}</div>
              </div>
              <div class="info-card full-width">
                <div class="info-card-label">执行计划</div>
                <div class="plan-info">{plan_formatted}</div>
              </div>
            </div>
          </div>
        </div>

        <!-- 系统外部信息 -->
        <div class="detail-section system-metrics">
          <h3>系统外部信息</h3>
          <div class="metrics-panel">
{metrics_charts}
          </div>
        </div>

        <!-- 系统内部信息 -->
        <div class="detail-section system-metrics">
          <h3>系统内部信息</h3>
          <table>
            <tr>
              <th>指标类别</th>
              <th>指标名称</th>
              <th>当前值</th>
              <th>说明</th>
            </tr>
{internal_metrics_rows}
          </table>
        </div>

        <!-- 诊断结果 -->
        <div class="detail-section" id="diagnosis-section-sql{query_id}" {diagnosis_section_style}>
          <h3>异常诊断结果</h3>
          <div class="diagnosis-container">
            <!-- 左侧：异常类型和置信度 -->
            <div class="diagnosis-left">
              <table id="diagnosis-table-sql{query_id}">
{diagnosis_rows}
              </table>
            </div>

            <!-- 右侧：统一解释说明 -->
            <div class="diagnosis-right">
              <div class="diagnosis-explanation">
                <div class="explanation-title">解释说明</div>
                <div id="explanation-text-sql{query_id}" class="explanation-content">{explanation_escaped}</div>
              </div>
            </div>
          </div>

          <div style="margin-top: 16px; text-align: center;">
            <button onclick="generateTuningAdvice('sql{query_id}')" style="padding: 8px 20px; font-size: 14px;">生成调优建议</button>
          </div>
        </div>

        <!-- 诊断按钮区域 -->
        <div class="detail-section" id="diagnosis-button-section-sql{query_id}" {button_section_style}>
          <div style="text-align: center; padding: 20px;">
            <button onclick="diagnoseSingleQuery('{query_id}')" style="padding: 10px 24px; font-size: 14px; background: #1e3c72; color: #fff; border: none; border-radius: 6px; cursor: pointer;">
              诊断此SQL
            </button>
          </div>
        </div>
      </div>
    </div>
"""


def render_diagnosis_page(template: str, slow_query_data: Dict, diagnosis_results: Dict) -> str:
    sql_items_html = ""
    for query_id, query_data in slow_query_data.items():
        query_info = query_data.get("query_info", {})
        diagnosis_result = diagnosis_results.get(query_id)
        sql_items_html += build_sql_item_html(query_id, query_info, diagnosis_result)
    return template.replace("{{ sql_items_html }}", sql_items_html)


def build_tuning_rows(tuning_suggestions: List[Dict]) -> str:
    if not tuning_suggestions:
        return '<tr><td colspan="2" style="text-align: center; color: #999;">暂无调优建议</td></tr>'
    rows = ""
    for suggestion in tuning_suggestions:
        action_text = suggestion.get("action", "")
        action_code = html.escape(action_text)
        rows += f"""
      <tr>
        <td>{suggestion.get('type', '')}</td>
        <td>
          <div class="tuning-action-code">{action_code}</div>
        </td>
      </tr>
"""
    return rows


def render_handling_page(
    template: str,
    query_id: str,
    slow_query_data: Dict,
    diagnosis_results: Dict,
    tuning_results: Dict,
    tuning_suggestions: List[Dict],
) -> str:
    query_data = slow_query_data.get(query_id, {}).get("query_info", {})
    diagnosis_result = diagnosis_results.get(query_id, {})
    tuning_result = tuning_results.get(query_id, {})

    query = query_data.get("query", "")
    plan_json = query_data.get("plan_json", "")
    execution_time = query_data.get("execution_time", 0.0)

    confidence = diagnosis_result.get("confidence", {})
    explanation = diagnosis_result.get("explanation") or "暂无解释说明"
    diagnosis_table_html = build_diagnosis_rows(confidence)
    tuning_rows_html = build_tuning_rows(tuning_suggestions) if tuning_suggestions else ""

    plan_formatted = format_plan_json(plan_json)
    expected_old_time = format_execution_time(execution_time)

    template = template.replace("{{ query }}", html.escape(query))
    template = template.replace("{{ plan_json }}", html.escape(plan_formatted))
    template = template.replace("{{ diagnosis_table_html }}", diagnosis_table_html)
    template = template.replace("{{ explanation }}", html.escape(str(explanation)))
    template = template.replace("{{ tuning_rows_html }}", tuning_rows_html)
    template = template.replace("{{ expected_old_time }}", expected_old_time)
    template = template.replace("{{ query_id }}", html.escape(str(query_id)))
    template = template.replace("{{ fix_action }}", html.escape(tuning_result.get("fix_action", "")))
    template = template.replace("{{ rewrite_sql }}", html.escape(tuning_result.get("rewrite_sql", "")))
    return template

