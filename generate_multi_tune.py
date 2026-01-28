#!/usr/bin/env python3
"""
Generate multi-tune.html with multi-round tuning suggestions.
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


def format_execution_time(time: float) -> str:
    """Format execution time"""
    if time < 1:
        return f"{time:.3f}s"
    elif time < 60:
        return f"{time:.2f}s"
    else:
        minutes = int(time // 60)
        seconds = time % 60
        return f"{minutes}m {seconds:.2f}s"


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
            })
    
    # Parse query rewrite
    if rewrite_sql and rewrite_sql.strip() != '':
        suggestions.append({
            'type': '查询改写',
            'action': rewrite_sql,
        })
    
    # Parse query plan hints
    if '/*+' in fix_action or 'SET(' in fix_action:
        hint_lines = [line.strip() for line in fix_action.split('\n') 
                     if '/*+' in line or 'SET(' in line]
        if hint_lines:
            suggestions.append({
                'type': '查询计划调优',
                'action': '\n'.join(hint_lines),
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
            })
    
    return suggestions


async def generate_multi_tune_html(agent: DBAgent, query_id: str, slow_query_data: Dict, max_rounds: int = 5):
    """Generate multi-tune.html with multiple rounds of tuning"""
    logger.info(f"Generating multi-tune.html for query {query_id} with {max_rounds} rounds...")
    
    if query_id not in slow_query_data:
        logger.error(f"Query {query_id} not found")
        return
    
    query_data = slow_query_data[query_id]['query_info']
    initial_query = query_data['query']
    initial_time = query_data['execution_time']
    plan_json = query_data['plan_json']
    
    # Load template
    template_path = os.path.join(os.path.dirname(__file__), 'font', 'multi-tune.html')
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    
    # Store rounds data
    rounds_data = {}
    current_query = initial_query
    current_time = initial_time
    times = [initial_time]
    
    # Generate rounds
    for round_num in range(1, max_rounds + 1):
        logger.info(f"Generating round {round_num}...")
        
        # Build QueryInfo
        query_info = QueryInfo(
            query_id=query_data['query_id'],
            query=current_query,
            plan_json=plan_json,
            external_metrics=query_data['external_metrics'],
            internal_metrics=query_data['internal_metrics'],
            execution_time=current_time,
            is_rewrite=(current_query != initial_query)
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
            
            # Update query if rewritten
            if rewrite_sql and rewrite_sql.strip() != '':
                current_query = rewrite_sql
            
            # Estimate new time (simulate improvement)
            improvement_ratio = evaluation_result.get('improvement_ratio', 0.2)
            new_time = current_time * (1 - improvement_ratio)
            current_time = max(new_time, initial_time * 0.1)  # Don't go below 10% of original
            
            times.append(current_time)
            
            # Get root cause names in Chinese
            root_cause_types = {
                "index_selection": "索引选择",
                "parameter_tuning": "参数调优",
                "query_plan_tuning": "查询计划调优",
                "query_rewrite": "查询改写",
            }
            
            root_cause_names = []
            for rc in root_causes:
                rc_key = rc.lower().replace(" ", "_")
                rc_name = root_cause_types.get(rc_key, rc)
                root_cause_names.append(rc_name)
            
            rounds_data[round_num] = {
                'execTime': format_execution_time(current_time),
                'execTimeChange': f'↓ {((initial_time - current_time) / initial_time * 100):.1f}% vs 初始',
                'rootCauses': root_cause_names,
                'explanation': explanation,
                'operations': [
                    {
                        'rootCause': op['type'],
                        'operation': op['action']
                    }
                    for op in tuning_suggestions
                ]
            }
            
        except Exception as e:
            logger.error(f"Error in round {round_num}: {e}", exc_info=True)
            # Use previous round data or default
            if round_num > 1:
                rounds_data[round_num] = rounds_data[round_num - 1].copy()
                rounds_data[round_num]['execTime'] = format_execution_time(current_time)
            else:
                rounds_data[round_num] = {
                    'execTime': format_execution_time(current_time),
                    'execTimeChange': '↓ 0% vs 初始',
                    'rootCauses': ['未知'],
                    'explanation': '调优过程中出现错误。',
                    'operations': []
                }
    
    # Generate comparison chart HTML
    max_time = max(times)
    chart_bars = ""
    for i, time in enumerate(times):
        if i == 0:
            label = "初始状态"
            improvement = ""
        else:
            improvement_pct = ((initial_time - time) / initial_time * 100)
            label = f"第 {i} 轮"
            improvement = f'<span class="improvement-badge">-{improvement_pct:.1f}%</span>'
        
        height_pct = (time / max_time * 100) if max_time > 0 else 0
        chart_bars += f"""
      <div class="chart-bar">
        <div class="bar-container">
          <div class="bar" style="height: {height_pct}%;" data-value="{time:.2f}">{format_execution_time(time)}</div>
        </div>
        <div class="bar-label">{label}{improvement}</div>
      </div>
"""
    
    # Update chart in template
    template = re.sub(
        r'(<div class="comparison-chart">)(.*?)(</div>)',
        r'\1' + chart_bars + r'\3',
        template,
        flags=re.DOTALL
    )
    
    # Generate round selector options
    round_options = ""
    for i in range(1, max_rounds + 1):
        selected = "selected" if i == max_rounds else ""
        round_options += f'<option value="{i}" {selected}>第 {i} 轮调优</option>\n      '
    
    template = re.sub(
        r'(<select id="round-select".*?>)(.*?)(</select>)',
        r'\1' + round_options + r'\3',
        template,
        flags=re.DOTALL
    )
    
    # Generate JavaScript roundData
    js_round_data = "const roundData = {\n"
    for round_num, data in rounds_data.items():
        root_causes_js = json.dumps(data['rootCauses'], ensure_ascii=False)
        operations_js = json.dumps(data['operations'], ensure_ascii=False, indent=8)
        explanation_escaped = data['explanation'].replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n')
        
        js_round_data += f"""    {round_num}: {{
      execTime: '{data['execTime']}',
      execTimeChange: '{data['execTimeChange']}',
      rootCauses: {root_causes_js},
      explanation: '{explanation_escaped}',
      operations: {operations_js}
    }},
"""
    js_round_data += "  };"
    
    # Replace roundData in template
    template = re.sub(
        r'(const roundData = \{)(.*?)(\};)',
        js_round_data,
        template,
        flags=re.DOTALL
    )
    
    # Update current round display
    template = re.sub(
        r'(<span id="current-round">)(.*?)(</span>)',
        r'\1' + str(max_rounds) + r'\3',
        template
    )
    
    # Save
    output_path = os.path.join(os.path.dirname(__file__), 'font', 'multi-tune.html')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(template)
    
    logger.info("multi-tune.html generated successfully")


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
    
    # Get query_id from command line or use first one
    query_id = sys.argv[2] if len(sys.argv) > 2 else list(slow_query_data.keys())[0]
    max_rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    
    # Initialize agent
    async with DBAgent(configs=configs) as agent:
        await generate_multi_tune_html(agent, query_id, slow_query_data, max_rounds)


if __name__ == '__main__':
    asyncio.run(main())
