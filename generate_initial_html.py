#!/usr/bin/env python3
"""
Generate initial diagnosis.html from slow_query_list.json without diagnosis results.
This HTML will be populated dynamically via API calls.
"""

import json
import logging
import os
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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


def generate_metrics_charts(external_metrics: list) -> str:
    """Generate metrics chart HTML"""
    if not external_metrics or len(external_metrics) < 7:
        return ""
    
    metrics = [
        ("CPU 使用率", 0, "%", 100),
        ("读 I/O", 2, "MB/s", 2000),
        ("写 I/O", 3, "MB/s", 500),
        ("虚拟内存", 4, "GB", 16),
        ("物理内存", 5, "MB", 100),  # 改为MB单位，因为数值很小
        ("网络接收", 6, "MB", 100),
        # 注意：external_metrics只有7个元素（索引0-6），没有索引7，所以网络发送暂时不显示
        # ("网络发送", 7, "MB", 50),
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


def generate_internal_metrics_table(internal_metrics: list) -> str:
    """Generate internal metrics table HTML"""
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


def generate_initial_diagnosis_html(slow_query_data: dict):
    """Generate initial diagnosis.html without diagnosis results"""
    logger.info("Generating initial diagnosis.html...")
    
    # Load template
    template_path = os.path.join(os.path.dirname(__file__), 'font', 'diagnosis.html')
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    
    # Generate SQL items (without diagnosis results)
    sql_items_html = ""
    
    for query_id, query_data in slow_query_data.items():
        query_info_data = query_data['query_info']
        query = query_info_data['query']
        execution_time = query_info_data['execution_time']
        plan_json = query_info_data['plan_json']
        external_metrics = query_info_data['external_metrics']
        internal_metrics = query_info_data['internal_metrics']
        
        # Generate HTML components
        query_preview = format_query_preview(query)
        time_str = format_execution_time(execution_time)
        plan_formatted = format_plan_json(plan_json)
        metrics_charts = generate_metrics_charts(external_metrics)
        internal_metrics_rows = generate_internal_metrics_table(internal_metrics)
        
        # Escape HTML special characters in query
        query_escaped = query.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        # Generate SQL item without diagnosis results (will be populated via API)
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

        <!-- 诊断结果 (初始为空，通过API填充) -->
        <div class="detail-section" id="diagnosis-section-sql{query_id}" style="display: none;">
          <h3>异常诊断结果</h3>
          <div class="diagnosis-container">
            <!-- 左侧：异常类型和置信度 -->
            <div class="diagnosis-left">
              <table id="diagnosis-table-sql{query_id}">
                <tr>
                  <th>异常类型</th>
                  <th>置信度</th>
                </tr>
              </table>
            </div>

            <!-- 右侧：统一解释说明 -->
            <div class="diagnosis-right">
              <div class="diagnosis-explanation">
                <div class="explanation-title">解释说明</div>
                <div id="explanation-text-sql{query_id}"></div>
              </div>
            </div>
          </div>
          
          <div style="margin-top: 16px; text-align: center;">
            <button onclick="generateTuningAdvice('sql{query_id}')" style="padding: 8px 20px; font-size: 14px;">生成调优建议</button>
          </div>
        </div>

        <!-- 诊断按钮区域 -->
        <div class="detail-section" id="diagnosis-button-section-sql{query_id}">
          <div style="text-align: center; padding: 20px;">
            <button onclick="diagnoseSingleQuery('{query_id}')" style="padding: 10px 24px; font-size: 14px; background: #1e3c72; color: #fff; border: none; border-radius: 6px; cursor: pointer;">
              诊断此SQL
            </button>
          </div>
        </div>
      </div>
    </div>
"""
        sql_items_html += sql_item_html
    
    # Replace the SQL items section
    pattern = r'(<div class="section">\s*<h2>慢SQL列表</h2>\s*)(.*?)(\s*</div>\s*</main>)'
    
    # Add one-click diagnosis button before SQL list
    header_with_button = r'\1<div style="margin-bottom: 20px; text-align: right;"><button id="diagnose-all-btn" onclick="diagnoseAllQueries()" style="padding: 10px 24px; font-size: 14px; background: #1e3c72; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-weight: 600;">一键诊断</button></div>' + sql_items_html + r'\3'
    
    updated_template = re.sub(pattern, header_with_button, template, flags=re.DOTALL)
    
    # Save the updated template
    output_path = os.path.join(os.path.dirname(__file__), 'font', 'diagnosis.html')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(updated_template)
    
    logger.info("Initial diagnosis.html generated successfully")


def main():
    """Main function"""
    # Load slow query data
    json_path = os.path.join(os.path.dirname(__file__), 'slow_query_list.json')
    with open(json_path, 'r', encoding='utf-8') as f:
        slow_query_data = json.load(f)
    
    # Generate initial HTML
    generate_initial_diagnosis_html(slow_query_data)


if __name__ == '__main__':
    main()
