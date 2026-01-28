#!/usr/bin/env python3
"""
Generate HTML pages from slow query data using DREAM diagnosis.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Dict, List

from dream.agent.db_agent import DBAgent
from dream.utils.types import QueryInfo

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_template(template_name: str) -> str:
    """Load HTML template"""
    template_path = os.path.join(os.path.dirname(__file__), 'font', template_name)
    with open(template_path, 'r', encoding='utf-8') as f:
        return f.read()


def save_html(content: str, filename: str):
    """Save HTML content to file"""
    output_path = os.path.join(os.path.dirname(__file__), 'font', filename)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)
    logger.info(f"Saved {filename}")


def format_query_preview(query: str, max_length: int = 100) -> str:
    """Format query for preview"""
    if len(query) > max_length:
        return query[:max_length] + '...'
    return query


def format_execution_time(time: float) -> str:
    """Format execution time"""
    if time < 1:
        return f"{time:.2f} 秒"
    elif time < 60:
        return f"{time:.2f} 秒"
    else:
        minutes = int(time // 60)
        seconds = time % 60
        return f"{minutes} 分 {seconds:.2f} 秒"


def format_plan_json(plan_json: str) -> str:
    """Format plan JSON for display"""
    try:
        plan_data = json.loads(plan_json)
        return json.dumps(plan_data, indent=2, ensure_ascii=False)
    except:
        return plan_json


def generate_metrics_charts(external_metrics: List[List[float]]) -> str:
    """Generate metrics chart HTML"""
    if not external_metrics or len(external_metrics) < 7:
        return ""
    
    # Metric names and their indices
    metrics = [
        ("CPU 使用率", 0, "%", 100),
        ("读 I/O", 2, "MB/s", 2000),
        ("写 I/O", 3, "MB/s", 500),
        ("虚拟内存", 4, "GB", 16),
        ("物理内存", 5, "GB", 12),
        ("网络接收", 6, "MB", 100),
        ("网络发送", 7, "MB", 50),
    ]
    
    charts_html = ""
    for metric_name, idx, unit, max_val in metrics:
        if idx < len(external_metrics):
            values = external_metrics[idx]
            if len(values) > 0:
                # Determine status
                avg_val = sum(values) / len(values)
                if avg_val > max_val * 0.8:
                    status = "danger"
                elif avg_val > max_val * 0.6:
                    status = "warning"
                else:
                    status = "normal"
                
                status_text = "偏高" if status != "normal" else "正常"
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
    """Generate internal metrics table HTML"""
    # Map internal metrics to descriptions
    # Based on PostgreSQL metrics structure
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
                      "块统计" if "块" in name else \
                      "扫描统计" if "扫描" in name else \
                      "索引统计" if "索引" in name else \
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


def generate_diagnosis_results(root_causes: List[str], confidence: Dict, explanation: str) -> str:
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
        rc_type = root_cause_types.get(root_cause.lower().replace(" ", "_"), root_cause)
        conf_value = confidence.get(root_cause, 0.5)
        
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


async def generate_diagnosis_html(agent: DBAgent, slow_query_data: Dict, config_path: str = None):
    """Generate diagnosis.html from slow query data"""
    logger.info("Generating diagnosis.html...")
    
    # Load template
    template = load_template('diagnosis.html')
    
    # Find the section where SQL items are defined (after <h2>慢SQL列表</h2>)
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
            
            explanation = await agent.planner.llm_predict(
                query_info, root_causes, state_confidence
            )
        except Exception as e:
            logger.error(f"Error diagnosing query {query_id}: {e}")
            root_causes = ["未知"]
            state_confidence = {}
            explanation = "诊断过程中出现错误，请稍后重试。"
        
        # Generate HTML for this SQL item
        query_preview = format_query_preview(query)
        time_str = format_execution_time(execution_time)
        plan_formatted = format_plan_json(plan_json)
        metrics_charts = generate_metrics_charts(external_metrics)
        internal_metrics_rows = generate_internal_metrics_table(internal_metrics)
        diagnosis_rows, explanation_text = generate_diagnosis_results(root_causes, state_confidence, explanation)
        
        sql_item_html = f"""
    <!-- SQL {query_id} -->
    <div class="sql-item">
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
            <table>
              <tr>
                <th>信息名称</th>
                <th>当前值</th>
              </tr>
              <tr>
                <td>数据库类型</td>
                <td>PostgreSQL</td>
              </tr>
              <tr>
                <td>工作负载类型</td>
                <td>OLTP</td>
              </tr>
              <tr>
                <td>数据库大小</td>
                <td>50 GB</td>
              </tr>
              <tr>
                <td>负载信息</td>
                <td>TPC-H</td>
              </tr>
            </table>
          </div>

          <!-- 异常查询信息 -->
          <div class="detail-section">
            <h3>异常查询信息</h3>
            <table>
              <tr>
                <th>信息项</th>
                <th>内容</th>
              </tr>
              <tr>
                <td>SQL 语句</td>
                <td>
                  <div class="query-info">{query}</div>
                </td>
              </tr>
              <tr>
                <td>执行时间</td>
                <td>{time_str}</td>
              </tr>
              <tr>
                <td>执行计划</td>
                <td>
                  <div class="plan-info">{plan_formatted}</div>
                </td>
              </tr>
            </table>
          </div>
        </div>

        <!-- 系统外部信息 -->
        <div class="detail-section">
          <h3>系统外部信息</h3>
          <div class="metrics-panel">
{metrics_charts}
          </div>
        </div>

        <!-- 系统内部信息 -->
        <div class="detail-section">
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
                {explanation_text}
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
    
    # Replace the SQL items section in template
    # Find the section after <h2>慢SQL列表</h2> and before </div> that closes the section
    import re
    # Match from <h2>慢SQL列表</h2> to the closing </div> of the section (before </main>)
    pattern = r'(<h2>慢SQL列表</h2>\s*)(.*?)(\s*</div>\s*</main>)'
    
    # Find all SQL items in the original template to determine the replacement point
    # We'll replace everything between <h2>慢SQL列表</h2> and the last </div> before </main>
    # But we need to be careful not to replace the closing tags of nested divs
    
    # Better approach: find the pattern that matches from <h2>慢SQL列表</h2> to </main>
    # and replace the content between them
    pattern = r'(<h2>慢SQL列表</h2>\s*)(.*?)(\s*</div>\s*</main>)'
    
    # Count opening and closing divs to find the right section
    # Actually, let's use a simpler approach: find the section div and replace its content
    pattern = r'(<div class="section">\s*<h2>慢SQL列表</h2>\s*)(.*?)(\s*</div>\s*</main>)'
    replacement = r'\1' + sql_items_html + r'\3'
    updated_template = re.sub(pattern, replacement, template, flags=re.DOTALL)
    
    save_html(updated_template, 'diagnosis.html')
    logger.info("diagnosis.html generated successfully")


async def generate_handling_html(agent: DBAgent, query_id: str, slow_query_data: Dict):
    """Generate handling.html for a specific query"""
    logger.info(f"Generating handling.html for query {query_id}...")
    
    if query_id not in slow_query_data:
        logger.error(f"Query {query_id} not found")
        return
    
    query_data = slow_query_data[query_id]['query_info']
    query = query_data['query']
    execution_time = query_data['execution_time']
    plan_json = query_data['plan_json']
    
    # Build QueryInfo
    query_info = QueryInfo(
        query_id=query_data['query_id'],
        query=query,
        plan_json=plan_json,
        external_metrics=query_data['external_metrics'],
        internal_metrics=query_data['internal_metrics'],
        execution_time=execution_time,
        is_rewrite=query_data.get('is_rewrite', False)
    )
    
    # Get root causes
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
        
        explanation = await agent.planner.llm_predict(
            query_info, root_causes, state_confidence
        )
        
        # Generate tuning actions
        evaluation_result = await agent.action_manager.step(
            query_info, root_causes, mode="exploit"
        )
        
        fix_action = evaluation_result.get('fix_action', '')
        rewrite_sql = evaluation_result.get('rewrite_sql', '')
        
        # Parse tuning suggestions
        tuning_suggestions = parse_tuning_actions(fix_action, rewrite_sql, root_causes)
        
    except Exception as e:
        logger.error(f"Error generating tuning for query {query_id}: {e}")
        tuning_suggestions = []
        explanation = "生成调优建议时出现错误。"
        root_causes = []
        state_confidence = {}
    
    # Load template
    template = load_template('handling.html')
    
    # Generate tuning suggestions table
    suggestions_rows = ""
    for suggestion in tuning_suggestions:
        action_code = suggestion['action'].replace('\n', '<br>')
        suggestions_rows += f"""
      <tr>
        <td>{suggestion['type']}</td>
        <td>
          <div style="font-family: 'Courier New', monospace; font-size: 12px; background: #f8fafc; padding: 8px; border-radius: 4px;">
            {action_code}
          </div>
        </td>
        <td><span class="tag warning">待确认</span></td>
      </tr>
"""
    
    # Generate diagnosis results
    diagnosis_rows = ""
    root_cause_types = {
        "index_selection": "索引选择",
        "parameter_tuning": "参数调优",
        "query_plan_tuning": "查询计划调优",
        "query_rewrite": "查询改写",
    }
    
    for root_cause in root_causes:
        rc_type = root_cause_types.get(root_cause.lower().replace(" ", "_"), root_cause)
        conf_value = state_confidence.get(root_cause, 0.5)
        
        if conf_value >= 0.8:
            tag_class = "danger"
        elif conf_value >= 0.6:
            tag_class = "warning"
        else:
            tag_class = "normal"
        
        diagnosis_rows += f"""
          <tr>
            <td>{rc_type}</td>
            <td><span class="tag {tag_class}">{conf_value:.2f}</span></td>
          </tr>
"""
    
    # Update template
    import re
    
    # Update query
    template = re.sub(
        r'(<div class="query-box">)(.*?)(</div>)',
        r'\1' + query + r'\3',
        template,
        flags=re.DOTALL
    )
    
    # Update plan
    plan_formatted = format_plan_json(plan_json)
    template = re.sub(
        r'(<div class="plan-box">)(.*?)(</div>)',
        r'\1' + plan_formatted + r'\3',
        template,
        flags=re.DOTALL
    )
    
    # Update diagnosis results
    template = re.sub(
        r'(<div class="diagnosis-left">.*?<table>.*?<tr>.*?<th>异常类型</th>.*?<th>置信度</th>.*?</tr>)(.*?)(</table>.*?</div>)',
        r'\1' + diagnosis_rows + r'\3',
        template,
        flags=re.DOTALL
    )
    
    # Update explanation
    template = re.sub(
        r'(<div class="explanation-title">解释说明</div>)(.*?)(</div>)',
        r'\1\n' + explanation + r'\3',
        template,
        flags=re.DOTALL
    )
    
    # Update tuning suggestions
    if suggestions_rows:
        template = re.sub(
            r'(<table>.*?<tr>.*?<th>根因类型</th>.*?<th>调优建议</th>.*?<th>状态</th>.*?</tr>)(.*?)(</table>)',
            r'\1' + suggestions_rows + r'\3',
            template,
            flags=re.DOTALL
        )
    else:
        # Remove the table if no suggestions
        template = re.sub(
            r'(<h2>调优建议与预期效果</h2>.*?<table>.*?</table>)',
            r'<h2>调优建议与预期效果</h2>\n<p>暂无调优建议</p>',
            template,
            flags=re.DOTALL
        )
    
    # Update execution time
    time_str = format_execution_time(execution_time)
    template = re.sub(
        r'(<div class="time-label">调优前执行时间</div>.*?<div class="time-value">)(.*?)(</div>)',
        r'\1' + time_str + r'\3',
        template
    )
    
    save_html(template, 'handling.html')
    logger.info("handling.html generated successfully")


def parse_tuning_actions(fix_action: str, rewrite_sql: str, root_causes: List[str]) -> List[Dict]:
    """Parse tuning actions into structured format"""
    suggestions = []
    
    # Parse index creation
    if 'CREATE INDEX' in fix_action.upper() or 'CREATE UNIQUE INDEX' in fix_action.upper():
        index_lines = [line.strip() for line in fix_action.split('\n') 
                      if 'CREATE' in line.upper() and 'INDEX' in line.upper()]
        if index_lines:
            suggestions.append({
                'type': '索引选择',
                'action': '\n'.join(index_lines),
                'status': '待确认'
            })
    
    # Parse query rewrite
    if rewrite_sql and rewrite_sql.strip() != '':
        suggestions.append({
            'type': '查询改写',
            'action': rewrite_sql,
            'status': '待确认'
        })
    
    # Parse query plan hints
    if '/*+' in fix_action or 'SET(' in fix_action:
        hint_lines = [line.strip() for line in fix_action.split('\n') 
                     if '/*+' in line or 'SET(' in line]
        if hint_lines:
            suggestions.append({
                'type': '查询计划调优',
                'action': '\n'.join(hint_lines),
                'status': '待确认'
            })
    
    # Parse parameter tuning
    if 'SET ' in fix_action.upper():
        param_lines = [line.strip() for line in fix_action.split('\n') 
                       if 'SET ' in line.upper() and any(param in line.upper() 
                       for param in ['work_mem', 'shared_buffers', 'max_parallel', 'enable_'])]
        if param_lines:
            suggestions.append({
                'type': '参数调优',
                'action': '\n'.join(param_lines),
                'status': '待确认'
            })
    
    return suggestions


async def main():
    """Main function"""
    import sys
    
    # Load config
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'tpch_config.json')
        if not os.path.exists(config_path):
            config_path = os.path.join(os.path.dirname(__file__), 'config', 'tpch_config.json.example')
    
    with open(config_path, 'r', encoding='utf-8') as f:
        configs = json.load(f)
    
    # Load slow query data
    json_path = os.path.join(os.path.dirname(__file__), 'slow_query_list.json')
    with open(json_path, 'r', encoding='utf-8') as f:
        slow_query_data = json.load(f)
    
    # Initialize agent
    async with DBAgent(configs=configs) as agent:
        # Generate diagnosis.html
        await generate_diagnosis_html(agent, slow_query_data, config_path)
        
        # Generate handling.html for first query (or can be called per query)
        if slow_query_data:
            first_query_id = list(slow_query_data.keys())[0]
            await generate_handling_html(agent, first_query_id, slow_query_data)


if __name__ == '__main__':
    asyncio.run(main())
