#!/usr/bin/env python3
"""
Generate diagnosis.html from slow_query_list.json using DREAM.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List

from dream.agent.db_agent import DBAgent
from dream.utils.types import QueryInfo

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent


def format_query_preview(query: str, max_length: int = 100) -> str:
    """Format query for preview"""
    query_clean = query.replace('\n', ' ').strip()
    if len(query_clean) > max_length:
        return query_clean[:max_length] + '...'
    return query_clean


def format_execution_time(time: float) -> str:
    """Format execution time"""
    if time < 1:
        return f"{time:.3f} 秒"
    elif time < 60:
        return f"{time:.2f} 秒"
    else:
        minutes = int(time // 60)
        seconds = time % 60
        return f"{minutes} 分 {seconds:.2f} 秒"


def format_plan_json(plan_json: str) -> str:
    """Format plan JSON for display"""
    try:
        if isinstance(plan_json, str):
            plan_data = json.loads(plan_json)
        else:
            plan_data = plan_json
        return json.dumps(plan_data, indent=2, ensure_ascii=False)
    except:
        return str(plan_json)


def generate_metrics_charts(external_metrics: List[List[float]]) -> str:
    """Generate metrics chart HTML"""
    if not external_metrics or len(external_metrics) < 7:
        return ""
    
    # Metric names and their indices in external_metrics
    # external_metrics structure: [cpu, io_read, io_write, vm, pm, net_recv, net_send, ...]
    metrics = [
        ("CPU 使用率", 0, "%", 100),
        ("读 I/O", 2, "MB/s", 2000),
        ("写 I/O", 3, "MB/s", 500),
        ("虚拟内存", 4, "GB", 16),
        ("物理内存", 5, "MB", 100),  # 改为MB单位，因为数值很小（约0.3-2MB）
        ("网络接收", 6, "MB", 100),
        # 注意：external_metrics只有7个元素（索引0-6），没有索引7，所以网络发送暂时不显示
        # ("网络发送", 7, "KB", 50),
    ]
    
    charts_html = ""
    for metric_name, idx, unit, max_val in metrics:
        if idx < len(external_metrics) and len(external_metrics[idx]) > 0:
            values = external_metrics[idx]
            # 对于物理内存，需要转换为MB（如果原来是GB单位）
            if metric_name == "物理内存" and unit == "MB":
                # 如果值小于1，可能是GB单位，需要转换为MB
                if max(values) < 1:
                    values = [v * 1024 for v in values]  # GB转MB
            
            # Determine status
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
            
            # 不再添加物理内存的注释
            note = ""
            
            charts_html += f"""
      <div class="metric-card">
        <div class="metric-header">
          <h3 class="metric-name">{metric_name}</h3>
          <span class="metric-status"><span class="tag {status}">{status_text}</span></span>
        </div>
        <div class="metric-chart" data-values="{values_str}" data-status="{status}" data-max="{max_val}" data-unit="{unit}"></div>
        {note}
      </div>
"""
    
    return charts_html


def generate_internal_metrics_table(internal_metrics: List[float]) -> str:
    """Generate internal metrics table HTML"""
    # Map internal metrics to descriptions
    # Based on PostgreSQL metrics: [tuples_returned, blocks_hit, blocks_read, tuples_fetched, 
    #                                index_tuples_fetched, seq_tuples_read, seq_scan, idx_scan,
    #                                heap_blocks_hit, heap_blocks_read, idx_blocks_hit, idx_blocks_read, execution_time]
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
            category = "元组统计" if "元组" in name else \
                      "块统计" if "块" in name and "索引块" not in name and "堆块" not in name else \
                      "扫描统计" if "扫描" in name else \
                      "索引统计" if "索引" in name and "块" not in name else \
                      "堆块统计" if "堆块" in name else \
                      "索引块统计" if "索引块" in name else "其他"
            
            rows += f"""
      <tr>
        <td>{category}</td>
        <td>{name}</td>
        <td>{value}</td>
        <td>{desc}</td>
      </tr>
"""
    
    return rows


def generate_diagnosis_results(root_causes: List[str], confidence: Dict, explanation: str) -> tuple:
    """Generate diagnosis results HTML"""
    # Root cause type mapping
    root_cause_types = {
        "index_selection": "索引选择",
        "parameter_tuning": "参数调优",
        "query_plan_tuning": "查询计划调优",
        "query_rewrite": "查询改写",
    }
    
    # Generate table rows
    rows = ""
    for root_cause in root_causes:
        rc_key = root_cause.lower().replace(" ", "_")
        rc_type = root_cause_types.get(rc_key, root_cause)
        conf_value = confidence.get(root_cause, confidence.get(rc_key, 0.5))
        
        if conf_value >= 0.8:
            tag_class = "danger"
        elif conf_value >= 0.6:
            tag_class = "warning"
        else:
            tag_class = "normal"
        
        rows += f"""
          <tr>
            <td>{rc_type}</td>
            <td><span class="tag {tag_class}">{conf_value:.2f}</span></td>
          </tr>
"""
    
    return rows, explanation


async def generate_diagnosis_html(agent: DBAgent, slow_query_data: Dict):
    """Generate diagnosis.html from slow query data"""
    logger.info("Generating diagnosis.html...")
    
    # Load template
    template_path = BASE_DIR / "font" / "diagnosis.html"
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    
    # Generate SQL items
    sql_items_html = ""
    
    for query_id, query_data in slow_query_data.items():
        query_info_data = query_data['query_info']
        query = query_info_data['query']
        execution_time = query_info_data['execution_time']
        plan_json = query_info_data['plan_json']
        external_metrics = query_info_data['external_metrics']
        internal_metrics = query_info_data['internal_metrics']
        
        # Build QueryInfo
        query_info = QueryInfo(
            query_id=query_info_data['query_id'],
            query=query,
            plan_json=plan_json,
            external_metrics=external_metrics,
            internal_metrics=internal_metrics,
            execution_time=execution_time,
            is_rewrite=query_info_data.get('is_rewrite', False)
        )
        
        # Run diagnosis
        state = {
            "root_tried": set(),
            "current_root": None,
            "mode": "exploit",
            "confidence": {},
            "attempts": {},
            "successes": {},
            "component_attempts": {},
        }
        
        try:
            predicted_root, updated_state = await agent.planner.predict(
                query_info, state, agent.memory_manager
            )
            
            root_causes = predicted_root if isinstance(predicted_root, list) else [predicted_root]
            state_confidence = updated_state.get('confidence', {})
            
            # Get explanation
            explanation = await agent.planner.llm_predict(
                query_info, root_causes, state_confidence
            )
            # Ensure explanation is a string
            if isinstance(explanation, dict):
                # If it's a dict, try to extract text or convert to string
                explanation = explanation.get('explanation', explanation.get('text', str(explanation)))
            elif not isinstance(explanation, str):
                explanation = str(explanation)
        except Exception as e:
            logger.error(f"Error diagnosing query {query_id}: {e}", exc_info=True)
            root_causes = ["未知"]
            state_confidence = {}
            explanation = "诊断过程中出现错误，请稍后重试。"
        
        # Generate HTML components
        query_preview = format_query_preview(query)
        time_str = format_execution_time(execution_time)
        plan_formatted = format_plan_json(plan_json)
        metrics_charts = generate_metrics_charts(external_metrics)
        internal_metrics_rows = generate_internal_metrics_table(internal_metrics)
        diagnosis_rows, explanation_text = generate_diagnosis_results(root_causes, state_confidence, explanation)
        
        # Ensure explanation_text is a string
        if not isinstance(explanation_text, str):
            if isinstance(explanation_text, dict):
                explanation_text = explanation_text.get('explanation', explanation_text.get('text', str(explanation_text)))
            else:
                explanation_text = str(explanation_text)
        
        # Escape HTML special characters in query and explanation
        query_escaped = query.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        explanation_escaped = explanation_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        sql_item_html = f"""
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
        <div class="detail-section">
          <h3>异常诊断结果</h3>
          <div class="diagnosis-container">
            <!-- 左侧：异常类型和置信度 -->
            <div class="diagnosis-left">
              <table>
                <tr>
                  <th>异常类型</th>
                  <th>置信度</th>
                </tr>
{diagnosis_rows}
              </table>
            </div>

            <!-- 右侧：统一解释说明 -->
            <div class="diagnosis-right">
              <div class="diagnosis-explanation">
                <div class="explanation-title">解释说明</div>
                {explanation_escaped}
              </div>
            </div>
          </div>
          
          <div style="margin-top: 16px; text-align: center;">
            <button onclick="generateTuningAdvice('sql{query_id}')" style="padding: 8px 20px; font-size: 14px;">生成调优建议</button>
          </div>
        </div>
      </div>
    </div>
"""
        sql_items_html += sql_item_html
    
    # Replace the SQL items section
    # Find the pattern: <h2>慢SQL列表</h2> followed by content, then </div> before </main>
    pattern = r'(<h2>慢SQL列表</h2>\s*)(.*?)(\s*</div>\s*</main>)'
    
    # Use a more specific pattern that matches the section div
    pattern = r'(<div class="section">\s*<h2>慢SQL列表</h2>\s*)(.*?)(\s*</div>\s*</main>)'
    
    replacement = r'\1' + sql_items_html + r'\3'
    updated_template = re.sub(pattern, replacement, template, flags=re.DOTALL)
    
    # Save the updated template
    output_path = BASE_DIR / "results" / "diagnosis.html"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(updated_template)
    
    logger.info("diagnosis.html generated successfully")


async def generate_handling_html(agent: DBAgent, query_id: str, slow_query_data: Dict, fix_action: str = "", rewrite_sql: str = "", root_causes: List[str] = None, state_confidence: Dict = None):
    """Generate handling.html for a specific query by reading from diagnosis.html"""
    logger.info(f"Generating handling.html for query {query_id}...")
    
    if query_id not in slow_query_data:
        logger.error(f"Query {query_id} not found")
        return
    
    query_data = slow_query_data[query_id]['query_info']
    
    # Try to get execution_time from diagnosis result first (most accurate)
    execution_time = None
    diagnosis_storage_file = BASE_DIR / "results" / "diagnosis_results.json"
    if os.path.exists(diagnosis_storage_file):
        try:
            with open(diagnosis_storage_file, 'r', encoding='utf-8') as f:
                all_results = json.load(f)
                diagnosis_result = all_results.get(query_id)
                if diagnosis_result and 'query_info' in diagnosis_result:
                    execution_time = diagnosis_result['query_info'].get('execution_time')
                    logger.info(f"Got execution_time {execution_time} from diagnosis result for query {query_id}")
        except Exception as e:
            logger.warning(f"Error reading diagnosis result for execution_time: {e}")
    
    # Fallback to query_data if not found in diagnosis result
    if execution_time is None:
        execution_time = query_data.get('execution_time')
        if execution_time is not None:
            logger.info(f"Got execution_time {execution_time} from query_data for query {query_id}")
    
    # Final fallback
    if execution_time is None:
        logger.error(f"execution_time not found for query {query_id}, using default 0.0")
        execution_time = 0.0
    
    # Read diagnosis.html to extract content (generate if missing)
    diagnosis_path = BASE_DIR / "results" / "diagnosis.html"
    if not diagnosis_path.exists():
        try:
            from template_renderer import load_json, render_diagnosis_page
            template_path = BASE_DIR / "font" / "diagnosis.html"
            template = template_path.read_text(encoding="utf-8")
            diagnosis_results = load_json(diagnosis_storage_file)
            rendered = render_diagnosis_page(template, slow_query_data, diagnosis_results)
            diagnosis_path.write_text(rendered, encoding="utf-8")
            logger.info(f"Generated diagnosis.html at {diagnosis_path}")
        except Exception as e:
            logger.error(f"diagnosis.html not found at {diagnosis_path} and failed to generate: {e}")
            return
    
    with open(diagnosis_path, 'r', encoding='utf-8') as f:
        diagnosis_html = f.read()
    
    # Extract SQL query from diagnosis.html for this query_id
    # Find the section with query-info for this query_id
    query_pattern = rf'<div class="sql-item" data-query-id="{query_id}">.*?<div class="query-info">(.*?)</div>'
    query_match = re.search(query_pattern, diagnosis_html, re.DOTALL)
    query_html = query_match.group(1) if query_match else ""
    
    # Extract plan from diagnosis.html
    plan_pattern = rf'<div class="sql-item" data-query-id="{query_id}">.*?<div class="plan-info">(.*?)</div>'
    plan_match = re.search(plan_pattern, diagnosis_html, re.DOTALL)
    plan_html = plan_match.group(1) if plan_match else ""
    
    # Extract diagnosis results (confidence table and explanation) from diagnosis.html
    # The table content includes the header row and data rows
    diagnosis_table_pattern = rf'<table id="diagnosis-table-sql{query_id}">(.*?)</table>'
    diagnosis_table_match = re.search(diagnosis_table_pattern, diagnosis_html, re.DOTALL)
    diagnosis_table_html = None
    if diagnosis_table_match:
        table_content = diagnosis_table_match.group(1)
        # Check if table has data rows (more than just the header)
        tr_count = len(re.findall(r'<tr>', table_content))
        if tr_count > 1:
            # Has diagnosis data, use it
            diagnosis_table_html = table_content
        else:
            # Only has header, will generate from root_causes and state_confidence
            diagnosis_table_html = None
    else:
        diagnosis_table_html = None
    
    # Extract explanation text
    explanation_pattern = rf'<div id="explanation-text-sql{query_id}"[^>]*class="explanation-content"[^>]*>(.*?)</div>'
    explanation_match = re.search(explanation_pattern, diagnosis_html, re.DOTALL)
    if not explanation_match:
        # Try alternative pattern without class
        explanation_pattern = rf'<div id="explanation-text-sql{query_id}"[^>]*>(.*?)</div>'
        explanation_match = re.search(explanation_pattern, diagnosis_html, re.DOTALL)
    explanation_html = explanation_match.group(1).strip() if explanation_match and explanation_match.group(1).strip() else None
    
    # If diagnosis results are not available in diagnosis.html, generate from root_causes and state_confidence
    # Check if we need to generate diagnosis results (either table is None or has no data rows, or explanation is None)
    need_generate_diagnosis = False
    if diagnosis_table_html is None:
        need_generate_diagnosis = True
    else:
        # Check if table has data rows (more than just header)
        # Count <tr> tags - should have at least 5 (1 header + 4 data rows for all root causes)
        tr_count = len(re.findall(r'<tr>', diagnosis_table_html))
        if tr_count < 5:  # Less than 1 header + 4 data rows means incomplete
            need_generate_diagnosis = True
    
    # Generate diagnosis table to ensure all 4 root cause types are shown
    # This ensures consistency even if diagnosis.html doesn't have complete data
    if need_generate_diagnosis or explanation_html is None:
        # Generate diagnosis table from root_causes and state_confidence
        root_cause_types = {
            "missing indexes": "索引选择",
            "inappropriate query knobs": "参数调优",
            "suboptimal plan optimizer": "查询计划调优",
            "poorly written queries": "查询改写",
            "index_selection": "索引选择",
            "parameter_tuning": "参数调优",
            "query_plan_tuning": "查询计划调优",
            "query_rewrite": "查询改写",
        }
        
        all_root_cause_types = [
            "missing indexes",
            "inappropriate query knobs",
            "suboptimal plan optimizer",
            "poorly written queries"
        ]
        
        diagnosis_rows = '<tr><th>异常类型</th><th>置信度</th></tr>'
        for root_cause in all_root_cause_types:
            rc_key = root_cause.lower().replace(" ", "_")
            rc_type = root_cause_types.get(root_cause) or root_cause_types.get(rc_key) or root_cause
            conf_value = state_confidence.get(root_cause) or state_confidence.get(rc_key) or 0.0
            
            # Set color based on confidence
            if conf_value > 0.9:
                tag_class = 'danger'
            elif conf_value > 0.5:
                tag_class = 'warning'
            else:
                tag_class = 'normal'
            
            diagnosis_rows += f"""
                <tr>
                  <td>{rc_type}</td>
                  <td><span class="tag {tag_class}">{conf_value:.2f}</span></td>
                </tr>
"""
        
        diagnosis_table_html = diagnosis_rows
        
        # Generate explanation if not available
        if explanation_html is None:
            # Try to get explanation from LLM if available
            try:
                llm_result = await agent.planner.llm_predict(
                    QueryInfo(
                        query_id=query_data['query_id'],
                        query=query_data['query'],
                        plan_json=query_data['plan_json'],
                        external_metrics=query_data['external_metrics'],
                        internal_metrics=query_data['internal_metrics'],
                        execution_time=query_data['execution_time'],
                        is_rewrite=query_data.get('is_rewrite', False)
                    ),
                    root_causes if root_causes else [],
                    state_confidence
                )
                if isinstance(llm_result, dict):
                    explanation_html = llm_result.get('explanation', '暂无解释说明')
                elif isinstance(llm_result, str):
                    explanation_html = llm_result
                else:
                    explanation_html = '暂无解释说明'
            except Exception as e:
                logger.warning(f"Failed to get explanation from LLM: {e}")
                explanation_html = '暂无解释说明'
    
    # Ensure explanation_html is a string
    if explanation_html is None:
        explanation_html = '暂无解释说明'
    elif not isinstance(explanation_html, str):
        explanation_html = str(explanation_html) if explanation_html else '暂无解释说明'
    
    # Parse tuning suggestions from fix_action
    if root_causes is None:
        root_causes = []
    if state_confidence is None:
        state_confidence = {}
    
    tuning_suggestions = parse_tuning_actions(fix_action, rewrite_sql, root_causes)
    
    # Load handling.html template
    template_path = BASE_DIR / "font" / "handling.html"
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    
    # Generate tuning suggestions table (without status column)
    suggestions_rows = ""
    for suggestion in tuning_suggestions:
        # Process action text: convert <br> to newlines, then escape HTML
        action_text = suggestion['action']
        # Convert <br> and <br/> tags to newlines (case insensitive)
        import re as re_module
        action_text = re_module.sub(r'<br\s*/?>', '\n', action_text, flags=re_module.IGNORECASE)
        # Escape HTML special characters (preserve newlines for white-space: pre-wrap)
        action_code = action_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        suggestions_rows += f"""
      <tr>
        <td>{suggestion['type']}</td>
        <td>
          <div class="tuning-action-code">{action_code}</div>
        </td>
      </tr>
"""
    
    # Update query (use the same style as diagnosis.html)
    if query_html:
        template = re.sub(
            r'(<div class="query-info">)(.*?)(</div>)',
            r'\1' + query_html + r'\3',
            template,
            flags=re.DOTALL
        )
    
    # Update plan (use the same style as diagnosis.html)
    if plan_html:
        template = re.sub(
            r'(<div class="plan-info">)(.*?)(</div>)',
            r'\1' + plan_html + r'\3',
            template,
            flags=re.DOTALL
        )
    
    # Update diagnosis results (use the same content from diagnosis.html or generated content)
    if diagnosis_table_html:
        # diagnosis_table_html already includes the header row and all data rows
        # Replace the entire table content inside diagnosis-left
        template = re.sub(
            r'(<div class="diagnosis-left">.*?<table>)(.*?)(</table>.*?</div>)',
            r'\1' + diagnosis_table_html + r'\3',
            template,
            flags=re.DOTALL
        )
    elif need_generate_diagnosis:
        # If no diagnosis data in diagnosis.html, generate from root_causes and state_confidence
        # This should include all 4 root cause types
        pass  # Already generated above
    
    # Update explanation (use the same content from diagnosis.html or generated content)
    if explanation_html:
        # Escape HTML in explanation
        explanation_escaped = explanation_html.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        template = re.sub(
            r'(<div id="explanation-text-handling"[^>]*class="explanation-content"[^>]*>)(.*?)(</div>)',
            r'\1' + explanation_escaped + r'\3',
            template,
            flags=re.DOTALL
        )
    
    # Update title from "调优建议与预期效果" to "调优建议"
    template = re.sub(
        r'(<h2[^>]*>)(调优建议与预期效果)(</h2>)',
        r'\1调优建议\3',
        template
    )
    
    # Update tuning suggestions (remove status column)
    if suggestions_rows:
        template = re.sub(
            r'(<table class="tuning-table">.*?<tr>.*?<th>根因类型</th>.*?<th>调优建议</th>.*?</tr>)(.*?)(</table>)',
            r'\1' + suggestions_rows + r'\3',
            template,
            flags=re.DOTALL
        )
    else:
        # Remove the table if no suggestions
        template = re.sub(
            r'(<h2[^>]*>调优建议</h2>.*?<table class="tuning-table">.*?</table>)',
            r'<h2>调优建议</h2>\n<p>暂无调优建议</p>',
            template,
            flags=re.DOTALL
        )
    
    # Update execution time in "预期调优效果" section (only 调优前执行时间)
    # This is the baseline time before tuning, we don't modify the expected result (10.10秒)
    time_str = format_execution_time(execution_time)
    logger.info(f"Setting baseline execution time to {time_str} for query {query_id} in expected effect section")

    # Prefer placeholder replacement when using templates
    if "{{ expected_old_time }}" in template:
        template = template.replace("{{ expected_old_time }}", time_str)
        logger.info("Successfully updated expected-old-time using template placeholder")
    else:
        # Fallback to DOM pattern replacement for older templates
        pattern = r'(<div\s+class="time-value"\s+id="expected-old-time">)([^<]*?)(</div>)'
        new_template = re.sub(pattern, r'\1' + time_str + r'\3', template, flags=re.DOTALL)
        if new_template != template:
            template = new_template
            logger.info("Successfully updated expected-old-time using id selector")
        else:
            logger.error(f"Failed to update expected-old-time. time_str={time_str}")
    
    # Don't modify 调优后执行时间 - it should stay as 10.10 秒 in the template
    # The template already has the correct value, so we don't need to modify it
    
    # Save
    output_path = BASE_DIR / "results" / "handling.html"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(template)
    
    logger.info("handling.html generated successfully")


def parse_tuning_actions(fix_action: str, rewrite_sql: str, root_causes: List[str]) -> List[Dict]:
    """Parse tuning actions into structured format based on root causes"""
    suggestions = []
    
    # Map root causes to Chinese names
    root_cause_map = {
        "missing indexes": "索引选择",
        "inappropriate query knobs": "参数调优",
        "suboptimal plan optimizer": "查询计划调优",
        "poorly written queries": "查询改写",
        "index_selection": "索引选择",
        "parameter_tuning": "参数调优",
        "query_plan_tuning": "查询计划调优",
        "query_rewrite": "查询改写",
    }
    
    # Normalize root_causes
    normalized_root_causes = []
    for rc in root_causes:
        rc_lower = rc.lower().replace(" ", "_")
        normalized_root_causes.append(rc_lower)
    
    # Parse index creation (for "missing indexes" or "index_selection")
    if any(rc in ["missing indexes", "index_selection"] for rc in normalized_root_causes):
        index_lines = [line.strip() for line in fix_action.split('\n') 
                      if 'CREATE' in line.upper() and 'INDEX' in line.upper()]
        if index_lines:
            suggestions.append({
                'type': '索引选择',
                'action': '\n'.join(index_lines)
            })
    
    # Parse query rewrite (for "poorly written queries" or "query_rewrite")
    if any(rc in ["poorly written queries", "query_rewrite"] for rc in normalized_root_causes):
        if rewrite_sql and rewrite_sql.strip() != '':
            suggestions.append({
                'type': '查询改写',
                'action': rewrite_sql
            })
    
    # Parse query plan hints (for "suboptimal plan optimizer" or "query_plan_tuning")
    if any(rc in ["suboptimal plan optimizer", "query_plan_tuning"] for rc in normalized_root_causes):
        hint_lines = [line.strip() for line in fix_action.split('\n') 
                     if '/*+' in line or 'SET(' in line]
        if hint_lines:
            suggestions.append({
                'type': '查询计划调优',
                'action': '\n'.join(hint_lines)
            })
    
    # Parse parameter tuning (for "inappropriate query knobs" or "parameter_tuning")
    if any(rc in ["inappropriate query knobs", "parameter_tuning"] for rc in normalized_root_causes):
        # Extract all SET statements that are not index-related
        param_lines = []
        for line in fix_action.split('\n'):
            line_stripped = line.strip()
            if 'SET ' in line_stripped.upper() and 'INDEX' not in line_stripped.upper():
                # Check if it's a parameter setting (not a query hint)
                if not ('/*+' in line_stripped or 'SET(' in line_stripped):
                    param_lines.append(line_stripped)
        
        if param_lines:
            suggestions.append({
                'type': '参数调优',
                'action': '\n'.join(param_lines)
            })
    
    # If no root causes match, try to infer from fix_action content
    if not suggestions:
        # Try index creation
        if 'CREATE INDEX' in fix_action.upper() or 'CREATE UNIQUE INDEX' in fix_action.upper():
            index_lines = [line.strip() for line in fix_action.split('\n') 
                          if 'CREATE' in line.upper() and 'INDEX' in line.upper()]
            if index_lines:
                suggestions.append({
                    'type': '索引选择',
                    'action': '\n'.join(index_lines)
                })
        
        # Try query rewrite
        if rewrite_sql and rewrite_sql.strip() != '':
            suggestions.append({
                'type': '查询改写',
                'action': rewrite_sql
            })
        
        # Try query plan hints
        if '/*+' in fix_action or 'SET(' in fix_action:
            hint_lines = [line.strip() for line in fix_action.split('\n') 
                         if '/*+' in line or 'SET(' in line]
            if hint_lines:
                suggestions.append({
                    'type': '查询计划调优',
                    'action': '\n'.join(hint_lines)
                })
        
        # Try parameter tuning
        if 'SET ' in fix_action.upper():
            param_lines = [line.strip() for line in fix_action.split('\n') 
                           if 'SET ' in line.upper() and 'INDEX' not in line.upper() 
                           and '/*+' not in line and 'SET(' not in line]
            if param_lines:
                suggestions.append({
                    'type': '参数调优',
                    'action': '\n'.join(param_lines)
                })
    
    return suggestions


async def main():
    """Main function"""
    import sys
    
    # Load config
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    if config_path is None:
        config_path = BASE_DIR / "config" / "tpch_config.json"
        if not config_path.exists():
            config_path = BASE_DIR / "config" / "tpch_config.json.example"
    
    with open(config_path, 'r', encoding='utf-8') as f:
        configs = json.load(f)
    
    # Load slow query data
    json_path = BASE_DIR / "results" / "slow_query_list.json"
    with open(json_path, 'r', encoding='utf-8') as f:
        slow_query_data = json.load(f)
    
    # Initialize agent
    async with DBAgent(configs=configs) as agent:
        # Generate diagnosis.html
        await generate_diagnosis_html(agent, slow_query_data)
        
        # Generate handling.html for first query
        if slow_query_data:
            first_query_id = list(slow_query_data.keys())[0]
            await generate_handling_html(agent, first_query_id, slow_query_data)


if __name__ == '__main__':
    asyncio.run(main())
