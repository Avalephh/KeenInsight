import os
import sys
from pathlib import Path

from agents import Agent, Runner
from agents._config import set_default_openai_api

# 添加项目根目录到Python路径
project_root = str(Path(__file__).parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)


# 设置大模型API
os.environ["OPENAI_API_KEY"] = ""
os.environ["OPENAI_BASE_URL"] = ""
set_default_openai_api("chat_completions")


def delete_all_loggers():
    """清理和重置所有日志记录器"""
    import logging

    loggers = [logging.getLogger(name) for name in logging.root.manager.loggerDict]
    for logger in loggers:
        handlers = logger.handlers[:]
        for handler in handlers:
            logger.removeHandler(handler)
        logger.propagate = True
        logger.setLevel(logging.CRITICAL)


def read_prompt() -> str:
    prompt = """As a database performance diagnosis expert, your task is to identify the **most likely root cause(s) and diagnosis confidence** of the following slow SQL query.

    ## Context
    1. Query Information:
    • SQL: select l_returnflag, l_linestatus, sum(l_quantity) as sum_qty, sum(l_extendedprice) as sum_base_price, sum(l_extendedprice * (1 - l_discount)) as sum_disc_price, sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) as sum_charge, avg(l_quantity) as avg_qty, avg(l_extendedprice) as avg_price, avg(l_discount) as avg_disc, count(*) as count_order from lineitem where l_shipdate <= date '1998-12-01' - interval '80' day group by l_returnflag, l_linestatus order by l_returnflag, l_linestatus;
    • Execution Plan: [[{"Plan": {"Node Type": "Aggregate", "Strategy": "Sorted", "Partial Mode": "Finalize", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 2303161.54, "Total Cost": 2303163.5, "Plan Rows": 6, "Plan Width": 236, "Group Key": ["l_returnflag", "l_linestatus"], "Plans": [{"Node Type": "Gather Merge", "Parent Relationship": "Outer", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 2303161.54, "Total Cost": 2303162.94, "Plan Rows": 12, "Plan Width": 236, "Workers Planned": 2, "Plans": [{"Node Type": "Sort", "Parent Relationship": "Outer", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 2302161.52, "Total Cost": 2302161.53, "Plan Rows": 6, "Plan Width": 236, "Sort Key": ["l_returnflag", "l_linestatus"], "Plans": [{"Node Type": "Aggregate", "Strategy": "Hashed", "Partial Mode": "Partial", "Parent Relationship": "Outer", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 2302161.3, "Total Cost": 2302161.44, "Plan Rows": 6, "Plan Width": 236, "Group Key": ["l_returnflag", "l_linestatus"], "Planned Partitions": 0, "Plans": [{"Node Type": "Seq Scan", "Parent Relationship": "Outer", "Parallel Aware": true, "Async Capable": false, "Relation Name": "lineitem", "Alias": "lineitem", "Startup Cost": 0.0, "Total Cost": 1436969.35, "Plan Rows": 24719770, "Plan Width": 25, "Filter": "(l_shipdate <= '1998-09-12 00:00:00'::timestamp without time zone)"}]}]}]}]}, "JIT": {"Functions": 9, "Options": {"Inlining": true, "Optimization": true, "Expressions": true, "Deforming": true}}}]]
    • Execution Runtime: 20 (timeout)
    • Internal Metrics: Tuples Returned:48894860, Blocks Hit:502271, Blocks Read:415066, Tuples Fetched:495, Index Tuples Fetched:0, Sequential Tuples Read:48892571, Sequential Scans:3, Index Scans:0, Heap Blocks Hit:501515, Heap Blocks Read:415062, Index Blocks Hit:0, Index Blocks Read:0
    • CPU: [99.9, 99.9, 99.4, 99.425, 100.0, 99.9, 99.9, 99.9, 49.9]
    • Read I/O: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    • Write I/O: [0.0078125, 7.048828125, 11.462890625, 8.6397705078125, 25.1171875, 0.6132812500000004, 0.17871093750000003, 0.07910156249999982, 0.28125]
    • Virtual Memory: [4.314579010009766, 4.314579010009766, 4.314579010009766, 4.314579010009766, 4.314579010009766, 4.314579010009766, 4.314579010009766, 4.314579010009766, 4.314399719238281]
    • Physical Memory: [4.172580718994141, 4.172580718994141, 4.172580718994141, 4.172580718994141, 4.173130035400391, 4.17364501953125, 4.173954010009766, 4.173954010009766, 4.173954010009766]
    • Net Received: [0.01804065704345703, 0.012827396392822266, 0.0019021034240722656, 0.0026131868362426758, 0.0016670227050781235, 0.003884553909301759, 0.002189159393310546, 0.0016053915023803724, 0.012109756469726562]
    • Net Sent: [0.01871967315673828, 0.008646607398986816, 0.001465797424316406, 0.0026131868362426758, 0.0016670227050781235, 0.003884553909301759, 0.002189159393310546, 0.0016053915023803724, 0.012109756469726562]

    2. Plan-Aware Exploration:
    - Analyze execution plan and runtime metrics to guide confidence adjustments:
     * missing indexes: If the execution plan shows large sequential scans on big tables with selective filters (e.g., WHERE clauses on non-indexed columns), and CPU is high while I/O is moderate, increase confidence for 'missing indexes'.
     * suboptimal plan optimizer: If the execution plan shows complex joins, nested loops, or inefficient aggregation strategies (e.g., hashed vs. sorted aggregation), especially when the estimated rows differ significantly from actual rows, increase confidence for 'suboptimal plan optimizer'.
     * inappropriate query knobs: High CPU with low I/O, or repeated parallel workers not utilized efficiently, increase confidence for 'inappropriate query knobs'.
     * poorly written queries: Large aggregations, complex expressions, unnecessary computations in SELECT/GROUP BY clauses, increase confidence for 'poorly written queries'.

    3. Initial Suggestion:
    - Initial model suggested root causes: ["inappropriate query knobs"]
    - Initial model confidence (0-1): {'missing indexes': 0.34, 'inappropriate query knobs': 0.61, 'suboptimal plan optimizer': 0.21, 'poorly written queries': 0.14}
    - Historical Tuning Summary (per root cause): {['missing indexes', 'suboptimal plan optimizer']: success_count=0, fail_count=1; ["suboptimal plan optimizer"]: success_count=0, fail_count=1; ['missing indexes']: success_count=0, fail_count=1; ['inappropriate query knobs']: success_count=1, fail_count=2;}

    4. Root Cause Diagnosis Guidance:
    Use the **Query Information** and the **Initial Suggestion** to infer the root causes and their confidence, as follows:
    - If a label has success_count = 0 and fail_count > 0, it is likely incorrect. Gradually reduce its confidence below 0.5, while gradually increasing the confidence of other root causes supported by Query Information until they exceed 0.5. If the label remains low after repeated failures, fully shift confidence to other unexplored candidates root causes.
    - If a label has success_count > 0 and fail_count = 0, it indicates consistent potential. CONTINUE exploring this root cause. Gradually increase its confidence as evidence accumulates.  
    - If a label has success_count > 0 and fail_count > 0, it indicates partial benefit but decreasing returns. CONTINUE using this root cause as a reference, and increase the other root causes confidence for exploraiton. Adjust confidence downward if further attempts do not yield improvements.  
    
    5. Confidence rules Guidance:
    - Confidence adjustment of each root cause **MUST** be restricted to the range of [0.1,0.3].  
    - If a root cause shows no improvement after multiple rounds, reduce its confidence below 0.5 to retire it from active diagnosis. In such cases, select a new candidate root cause for exploration, initializing its confidence slightly above 0.5. 
    - Confidence values should reflect both historical results and current judgment based on Query Information, Ensure the model makes independent judgments about which root causes to explore next, rather than blindly following previous confidence values.  

    ## Constraints (STRICT)
    - You MUST output ALL allowed labels WITH a confidence in [0,1]: missing indexes, inappropriate query knobs, suboptimal plan optimizer, poorly written queries
    - You MUST also output a separate array `predicted_root_causes` listing the final predicted root cause labels (derived from confidence and considering historical priors above).
    - `predicted_root_causes` MUST be exactly the set of labels with confidence > 0.5. Labels with confidence ≤ 0.5 MUST NOT appear in `predicted_root_causes`.
    - You MUST output strict JSON only in the format below. No extra text, no explanation.

    ## Output JSON Example
    {
      "predicted_root_causes": ["missing indexes"],
      "root_causes": [
        {"label": "missing indexes", "confidence": 0.72},
        {"label": "inappropriate query knobs", "confidence": 0.18},
        {"label": "suboptimal plan optimizer", "confidence": 0.10},
        {"label": "poorly written queries", "confidence": 0.05}
      ]
    }
    """
    return prompt


def get_root_cause_llm_instructions():
    """Get instructions for the root cause diagnosis LLM."""
    return """You are a database slow SQL root cause diagnosis expert.
    Only output the allowed root cause label(s) and their confidence in json format, nothing else."""


def run_llm(prompt_text) -> str:
    # Lazy import after path setup

    agent = Agent(
        name="RootCauseLLM",
        model="gpt-4.1",
        instructions=get_root_cause_llm_instructions(),
    )
    result = Runner.run_sync(starting_agent=agent, input=prompt_text)
    return (result.final_output or "").strip()


def main():

    prompt_text = read_prompt()
    output = run_llm(prompt_text)

    print(output)


if __name__ == "__main__":
    delete_all_loggers()
    main()
