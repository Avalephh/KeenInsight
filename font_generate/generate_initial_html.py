#!/usr/bin/env python3
"""
Generate initial diagnosis.html from slow_query_list.json without diagnosis results.
This HTML will be populated dynamically via API calls.
"""

import json
import logging
from pathlib import Path

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
    """Generate initial diagnosis.html from template and results data."""
    logger.info("Generating initial diagnosis.html from template...")
    template_path = BASE_DIR / "font" / "diagnosis.html"
    output_path = BASE_DIR / "results" / "diagnosis.html"
    template = template_path.read_text(encoding="utf-8")
    from template_renderer import render_diagnosis_page
    content = render_diagnosis_page(template, slow_query_data, {})
    output_path.write_text(content, encoding="utf-8")
    logger.info("Initial diagnosis.html generated successfully")


def main():
    """Main function"""
    # Load slow query data
    json_path = BASE_DIR / "results" / "slow_query_list.json"
    with open(json_path, 'r', encoding='utf-8') as f:
        slow_query_data = json.load(f)
    
    # Generate initial HTML
    generate_initial_diagnosis_html(slow_query_data)


if __name__ == '__main__':
    main()
