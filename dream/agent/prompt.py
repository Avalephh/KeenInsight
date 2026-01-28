"""
Prompt generation functions for database optimization system.
This module contains all prompt generation functions extracted from action_manager.py and planner.py.
"""


def fmt_cases(cases):
    lines = []
    for i, case in enumerate(cases, 1):
        lines.append(f"\n    Case {i}:\n    SQL: {case.get('query_info').get('query')}\n    Execution Plan: {case.get('query_info').get('plan_json')}\n    Origin Execution Runtime: {case.get('query_info').get('execution_time')}s\n    Root Causes: {case.get('root_causes')}\n    Fix_Actions: {case.get('fix_action')}\n    Improve: {case.get('approve_time')}")
    return "\n".join(lines)
    # try:
    #     return "\n".join([
    #         f"• SQL: {c.get('query_info').get('query')}\n Execution Plan: {c.get('query_info').get('plan_json')}\n Execution Runtime: {c.get('query_info').get('execution_time')}s\n RootCauses: {c.get('root_causes')}\n  Fix_Action: {c.get('fix_action')}\n  Improve: {c.get('approve_time')}s" for c in cases
    #     ])
    # except Exception:
    #     return str(cases)


def build_action_prompt(root_cause, base_info, action_space, mode, positives, negatives):
    """
    Format a prompt that feeds the LLM with all necessary context and enforces
    a consistent two-part output: Explanation + SQL Fix.
    """
    query_info = base_info.get("query_info")
    db_info = base_info.get("database_info", {})

    positives_text = fmt_cases(positives)
    negatives_text = fmt_cases(negatives)

    # Mode prompt
    if mode == "exploit":
        mode_instruction = """You are now in **EXPLOIT mode**. Refer to the following Positive Cases and Negative Cases, which are derived from historical fixes under the same root cause. 
    - Positive Cases represent effective fixes. Treat their Fix_Actions as **examples of optimization patterns**, You must **not copy them directly or merely reorder them**. Instead, propose **new fix_actions** that explore additional possibilities. 
    - Negative Cases represent ineffective fixes. Reflect on which actions led to no improvement or performance degradation, and avoid adopting these fix_actions in your solution.
        """
    else:
        mode_instruction = """You are now in **EXPLORE mode**. Refer to the following Positive Cases and Negative Cases, which are derived from historical fixes under different root causes.
    - Positive Cases represent effective fixes. Treat their Fix_Actions as **examples of optimization patterns**, You must **not copy them directly or merely reorder them**. Instead, propose **new fix_actions** that explore additional possibilities under the new root cause(s).
    - Negative Cases represent ineffective fixes. Reflect on which actions led to no improvement or performance degradation under other root causes, and avoid adopting these fix_actions in your solution.
        """

    return f"""As an SQL optimization expert, your task is to diagnose and fix the following slow SQL query.
    You have the context of slow SQL and the root cause of the performance degradation as below:

    ## Context
    1. Query Information:
    • SQL: {query_info.query}
    • Execution Plan: {query_info.plan_json}
    • Execution Runtime: {query_info.execution_time} s
    • Internal Metrics: Tuples Returned:{query_info.internal_metrics[0]}, Blocks Hit:{query_info.internal_metrics[1]}, Blocks Read:{query_info.internal_metrics[2]}, Tuples Fetched:{query_info.internal_metrics[3]}, Index Tuples Fetched:{query_info.internal_metrics[4]}, Sequential Tuples Read:{query_info.internal_metrics[5]}, Sequential Scans:{query_info.internal_metrics[6]}, Index Scans:{query_info.internal_metrics[7]}, Heap Blocks Hit:{query_info.internal_metrics[8]}, Heap Blocks Read:{query_info.internal_metrics[9]}, Index Blocks Hit:{query_info.internal_metrics[10]}, Index Blocks Read:{query_info.internal_metrics[11]}

    2. Database Environment Information:
    • Database Type: {db_info.get('db_type')}
    • Workload Type: {db_info.get('workload_type')}
    • Database Size: {db_info.get('size')}
    • Database Schema: TPC-H
    • CPU: {query_info.external_metrics[0]}
    • Read I/O: {query_info.external_metrics[1]}
    • Write I/O: {query_info.external_metrics[2]}
    • Virtual Memory: {query_info.external_metrics[3]}
    • Physical Memory: {query_info.external_metrics[4]}
    • Net Received: {query_info.external_metrics[5]}
    • Net Sent: {query_info.external_metrics[6]}

    3. **Identified Root Cause**: {root_cause}

    ## Mode Instruction
    {mode_instruction}
    • Positive Cases (effective references): {positives_text}
    • Negative Cases (ineffective references to avoid): {negatives_text}

    ## Historical Case Reflection
    Before proposing your optimization solution, analyze the historical cases to understand why certain approaches succeeded or failed, then adapt successful patterns to the current context while avoiding patterns that failed in similar situations.

    ## Action Space
    - If the root cause is "inappropriate query knobs", you MUST perform actions within Knob Space using SET knob = value.
    - If the root cause is "suboptimal plan optimizer", you MUST perform actions within Plan Optimizer Space using PostgreSQL comment-style hints ONLY. Output ONLY the hint block like: /*+ Set(knob_name value) Set(knob_name value) ... */. Do NOT include the SQL query.
    - If the root cause is "missing indexes", you MUST perform actions within Index Space using CREATE INDEX IF NOT EXISTS or DROP INDEX IF EXISTS.
    - If the root cause is "poorly written queries", you MUST perform actions within Query Rewrite Space using rewrite SQL.
    - **If multiple root causes are identified, you MUST consider all FIX_ACTION outputs jointly and determine the optimal solution that addresses the combined root causes, while strictly limiting actions to the relevant action spaces of the identified root causes. Do not perform any actions outside of these specified root causes.**
    - The Action Space defines the operations that allowed to perform, you MUST strictly limit your Fix_Action to these spaces. Do not adjust or introduce any actions beyond what is explicitly defined in the following Action Space.
    {action_space}

    ## Constraints
    - Fix action must be executable directly in {db_info.get('db_type')}.
    - Please ensure that the proposed fix_action strictly addresses the Identified Root Cause and does not deviate from the Action Space.
    - The fix action should be safe and should not drop or delete data unless explicitly required by the root cause.

    ## Output Format
    You must output ONLY in the following format, with explanation and fix action:

    Explanation:
    <concise technical reason why the root cause is causing the slowdown now>

    Fix_Action:
    <your fix action here, do not include any other text, if no fix action is needed, output an empty string>

    Query_Rewrite:
    <yes or no>

    ## Output Requirements
    - "Explanation" must be a short, precise, technical summary — no generic statements.
    - If the fix rewrites the SQL, "Query_Rewrite" must be "yes", else "no".
    - If rewriting, provide the optimized SQL under Fix_Action.
    - If adding indexes or changing knobs, provide the exact SQL or command.
    - Do not output any extra sections, comments, or steps."""


def build_fix_prompt(fix_action, err):
    """Build prompt for fixing non-executable SQL statements."""
    return f"""You have been given a non-executable SQL statement and the exact database error:
    • Non-executable SQL: {fix_action}
    • Database Error Message: {err}
    Your sole objective is to produce a corrected, fully-formed SQL statement (or an ordered sequence of SQL statements) that will run without error.
    Strict rules — follow exactly:
    1) OUTPUT NOTHING BUT THE SQL STATEMENT(S). No explanations, no comments, no markdown, no surrounding quotes.
    2) If a single statement suffices, output exactly that statement, terminated by a single semicolon and a newline.
    3) If multiple statements are required, output them in execution order, each terminated by a semicolon and a newline.
    -- Output (strict) --
    <paste only the corrected fix action statement(s) here, exactly as SQL, nothing else>"""


def build_rewrite_prompt(origin_sql, rewrite_sql):
    """Build prompt for rewriting SQL to match reference output."""
    return f"""You are given two SQL statements:
    • Reference SQL (correct output): {origin_sql}
    • Divergent SQL (incorrect output): {rewrite_sql}
    Your task is to rewrite the Divergent SQL so that its result set matches the Reference SQL exactly.

    Strict output rules:
    1. Output only the corrected SQL statement.
    2. Do not include explanations, comments, markdown, or any extra text.
    3. The SQL must be fully executable and self-contained.
    4. Preserve the original intent and structure of the Divergent SQL as much as possible while ensuring the output matches exactly.
    5. If you cannot rewrite the SQL, output the original SQL.

    — Output Format —
    <paste only the revised SQL here, exactly as SQL, nothing else>"""


def build_root_cause_diagnosis_prompt(query_info, root_causes, state_confidence, tuning_history=None, timeout=300):
    """Build prompt for LLM-based root cause diagnosis with per-label confidence map.

    tuning_history: Optional list summarizing per-root-cause success/failure counts, e.g.,
      [
        {"label": "missing indexes", "success_count": 2, "fail_count": 3},
        {"label": "inappropriate query knobs", "success_count": 1, "fail_count": 0}
      ]
    """

    # Handle timeout case
    execution_time_text = f"{query_info.execution_time} s"
    if query_info.execution_time == timeout:
        execution_time_text = f"{query_info.execution_time} (timeout)"

    # Format tuning history (success/failure counts)
    if tuning_history:
        lines = []
        for item in tuning_history:
            label = item.get("label")
            s_cnt = item.get("success_count", 0)
            f_cnt = item.get("fail_count", 0)
            lines.append(f"{label}: success_count={s_cnt}, fail_count={f_cnt};")
        history_text = " ".join(lines)
        history_text = "{" + history_text + "}"
    else:
        history_text = "No previous tuning attempts recorded."

    return f"""As a database performance diagnosis expert, your task is to identify the **most likely root cause(s) and diagnosis confidence** of the following slow SQL query.

    ## Context
    1. Query Information:
    • SQL: {query_info.query}
    • Execution Plan: {query_info.plan_json}
    • Execution Runtime: {execution_time_text}
    • Internal Metrics: Tuples Returned:{query_info.internal_metrics[0]}, Blocks Hit:{query_info.internal_metrics[1]}, Blocks Read:{query_info.internal_metrics[2]}, Tuples Fetched:{query_info.internal_metrics[3]}, Index Tuples Fetched:{query_info.internal_metrics[4]}, Sequential Tuples Read:{query_info.internal_metrics[5]}, Sequential Scans:{query_info.internal_metrics[6]}, Index Scans:{query_info.internal_metrics[7]}, Heap Blocks Hit:{query_info.internal_metrics[8]}, Heap Blocks Read:{query_info.internal_metrics[9]}, Index Blocks Hit:{query_info.internal_metrics[10]}, Index Blocks Read:{query_info.internal_metrics[11]}
    • CPU: {query_info.external_metrics[0]}
    • Read I/O: {query_info.external_metrics[1]}
    • Write I/O: {query_info.external_metrics[2]}
    • Virtual Memory: {query_info.external_metrics[3]}
    • Physical Memory: {query_info.external_metrics[4]}
    • Net Received: {query_info.external_metrics[5]}
    • Net Sent: {query_info.external_metrics[6]}

    2. Plan-Aware Exploration:
    - Analyze execution plan and runtime metrics to guide confidence adjustments:
     * missing indexes: If the execution plan shows large sequential scans on big tables with selective filters (e.g., WHERE clauses on non-indexed columns), and CPU is high while I/O is moderate, increase confidence for 'missing indexes'.
     * suboptimal plan optimizer: If the execution plan shows complex joins, nested loops, or inefficient aggregation strategies (e.g., hashed vs. sorted aggregation), especially when the estimated rows differ significantly from actual rows, increase confidence for 'suboptimal plan optimizer'.
     * inappropriate query knobs: High CPU with low I/O, or repeated parallel workers not utilized efficiently, increase confidence for 'inappropriate query knobs'.
     * poorly written queries: Large aggregations, complex expressions, unnecessary computations in SELECT/GROUP BY clauses, increase confidence for 'poorly written queries'.

    3. Initial Suggestion:
    - Initial model suggested root causes: {root_causes}
    - Initial model confidence (0-1): {state_confidence}
    - Historical Tuning Summary (per root cause): {history_text}

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
    - You MUST provide a detailed explanation of the diagnosis results in **Chinese (简体中文)**, explaining why each root cause has its assigned confidence level based on the query information, execution plan, and metrics.
    - The explanation MUST be written in Chinese (简体中文) and should be clear, technical, and comprehensive. **DO NOT use English or any other language. Only Chinese is allowed for the explanation field.**
    - You MUST output strict JSON only in the format below. No extra text, no explanation outside the JSON.

    ## Output JSON Example
    {{
      "predicted_root_causes": ["missing indexes"],
      "root_causes": [
        {{"label": "missing indexes", "confidence": 0.72}},
        {{"label": "inappropriate query knobs", "confidence": 0.18}},
        {{"label": "suboptimal plan optimizer", "confidence": 0.10}},
        {{"label": "poorly written queries", "confidence": 0.05}}
      ],
      "explanation": "根据执行计划分析，该查询在lineitem表上进行了大规模顺序扫描，过滤条件为l_shipdate。执行计划显示扫描了24,705,682行，但仅获取了1,363个元组，这表明缺少索引。高CPU使用率（91.9-99.9%）结合中等I/O表明查询是CPU密集型，很可能是由于全表扫描导致的。'missing indexes'的置信度为0.72，反映了执行计划中的明确证据。其他根因的置信度较低，因为当前指标对它们的支持较少。"
    }}"""


def build_action_space_prompt(root_causes, db, knob_config, current_knob_values, query_info=None):
    """Build action space prompt based on root causes."""
    # Root causes
    root_causes = root_causes if isinstance(root_causes, list) else [root_causes]

    # Build action space prompt based on root causes
    action_space_prompt = ""

    # Index related
    if "missing indexes" in root_causes:
        current_indexes = db.get_indexes() if db else []
        action_space_prompt += f"""
    • Index Space: 
    - The naming convention for newly created indexes must follow the pattern: (table_name)_(col1)_(col2)_idx.
    - Index creation should be strictly guided by the slow_sql queries.
    - Consider the query execution plan to identify missing indexes that could improve performance.
    - Avoid generating duplicate or subsumed indexes, and do not drop primary keys, unique indexes, or any indexes that enforce constraints.
    - Do not conflict with the existing indexes in the database: {current_indexes}
    """

    # Knob related
    if "inappropriate query knobs" in root_causes:
        system_knobs_specs = knob_config.get("system_knobs")
        current_values = current_knob_values.get("system_knobs")

        knob_lines = []
        for knob_name, spec in system_knobs_specs.items():
            current_val = current_values.get(knob_name)
            knob_lines.append(f"{knob_name}: type: {spec.get('type')}, min: {spec.get('min')}, max: {spec.get('max')}, default: {spec.get('default')}, current: {current_val}")

        action_space_prompt += f"""
    • Knob Space:  
    - Knob Space is strictly limited to the following knobs, you must only use these knobs in your Fix_Action, and any knob not explicitly listed here must not be used: 
    {knob_lines}
    """

    # Execution plan/optimizer related
    if "suboptimal plan optimizer" in root_causes:
        query_knobs_specs = knob_config.get("query_knobs")
        current_values = current_knob_values.get("query_knobs")

        knob_lines = []
        for knob_name, spec in query_knobs_specs.items():
            current_val = current_values.get(knob_name)
            knob_lines.append(f"{knob_name}: type: {spec.get('type')}, min: {spec.get('min')}, max: {spec.get('max')}, default: {spec.get('default')}, current: {current_val}")

        action_space_prompt += f"""
    • Plan Optimizer Space: 
    - Plan Optimizer Space is strictly limited to the following knobs, you must only use these knobs in your Fix_Action, and any knob not explicitly listed here must not be used: 
    {knob_lines}
    - **IMPORTANT**: When using Plan Optimizer Space, output ONLY the hint block. Do NOT include the original SQL. Example: /*+ Set(enable_hashjoin on) Set(enable_sort on) */"""

    # SQL readability/rewrite related
    if "poorly written queries" in root_causes:
        from .action.diagnose_tools import SQLRuleMatcher

        matcher = SQLRuleMatcher()
        rule_descriptions = matcher.get_rule_descriptions(query_info.query)

        action_space_prompt += f"""
    • Query Rewrite Space: 
    - Query Rewrite need to generate a semantically equivalent SELECT statement that produces exactly the same result set as the original query while improving performance.
    - **Analysis-Based Optimization**: The following rewrite rules have been identified as applicable based on the query structure analysis:
    {rule_descriptions}
    - **Implementation Guidelines**:
      * Apply only the rules that match the specific query structure and are expected to improve performance
      * Ensure the rewritten query maintains identical result set and semantic equivalence
      * Prioritize rules that reduce computational complexity, eliminate redundant operations, or optimize data access patterns
      * Test the rewritten query to verify it produces the same results as the original
    - **Quality Assurance**: The final output must be a complete, executable SQL statement that preserves all original query semantics while demonstrating measurable performance improvements.
    """

    return action_space_prompt


def get_fix_agent_instructions():
    """Get instructions for the fix agent."""
    return """You are a SQL Precision Repair Agent with two core responsibilities:
    1. Executable Repair: When provided with a raw, non-executable SQL statement and its exact database error message,
    produce a corrected SQL statement (or the minimal sequence of statements) that will execute successfully in the target SQL dialect.
    2. Output Alignment: When given two SQL statements whose result sets do not match,
    rewrite the SQL so that its output exactly matches the output of the original SQL."""


def get_diagnostic_agent_instructions():
    """Get instructions for the diagnostic agent."""
    return """You are an expert in database performance diagnosis and SQL optimization.
    Your role is to:
    1. Understand detailed database and query execution context.
    2. Identify how the root cause leads to performance degradation.
    3. Output exactly three labeled sections: Explanation, Fix_Action and Query_Rewrite.
    4. Always make the Fix_Action executable directly in the target database.
    5. Never include extra text, comments, or sections beyond the required format.
    6. Always apply the given MCP APIs when producing fixes, if applicable.
    7. Assume the provided execution plan, metrics, and environment info are accurate.
    8. Keep the Explanation concise and technical, avoiding generic descriptions."""


def get_root_cause_llm_instructions():
    """Get instructions for the root cause diagnosis LLM."""
    return """You are a database slow SQL root cause diagnosis expert.
    Only output the allowed root cause label(s) and their confidence in json format, nothing else."""


def build_simple_diagnosis_prompt(query_info, base_info):
    """Build prompt for simple LLM-based diagnosis without root cause prediction."""
    db_info = base_info.get("database_info", {})

    return f"""As an SQL optimization expert, your task is to diagnose and fix the following slow SQL query.
    You need to analyze the query and provide a direct fix without root cause prediction.

    ## Context
    1. Query Information:
    • SQL: {query_info.query}
    • Execution Plan: {query_info.plan_json}
    • Execution Runtime: {query_info.execution_time} s
    • Internal Metrics: Tuples Returned:{query_info.internal_metrics[0]}, Blocks Hit:{query_info.internal_metrics[1]}, Blocks Read:{query_info.internal_metrics[2]}, Tuples Fetched:{query_info.internal_metrics[3]}, Index Tuples Fetched:{query_info.internal_metrics[4]}, Sequential Tuples Read:{query_info.internal_metrics[5]}, Sequential Scans:{query_info.internal_metrics[6]}, Index Scans:{query_info.internal_metrics[7]}, Heap Blocks Hit:{query_info.internal_metrics[8]}, Heap Blocks Read:{query_info.internal_metrics[9]}, Index Blocks Hit:{query_info.internal_metrics[10]}, Index Blocks Read:{query_info.internal_metrics[11]}

    2. Database Environment Information:
    • Database Type: {db_info.get('db_type')}
    • Workload Type: {db_info.get('workload_type')}
    • Database Size: {db_info.get('size')}
    • Database Schema: TPC-H
    • CPU: {query_info.external_metrics[0]}
    • Read I/O: {query_info.external_metrics[1]}
    • Write I/O: {query_info.external_metrics[2]}
    • Virtual Memory: {query_info.external_metrics[3]}
    • Physical Memory: {query_info.external_metrics[4]}
    • Net Received: {query_info.external_metrics[5]}
    • Net Sent: {query_info.external_metrics[6]}

    ## Available Action Spaces
    You can perform the following types of optimizations:
    
    • Index Space: Create indexes to improve query performance
    - The naming convention for newly created indexes must follow the pattern: (table_name)_(col1)_(col2)_idx
    - You MUST use the CREATE INDEX IF NOT EXISTS or DROP INDEX IF EXISTS to create or drop indexes.
    - Index creation should be strictly guided by the slow_sql queries
    - Consider the query execution plan to identify missing indexes
    - Avoid generating duplicate or subsumed indexes
    - Do not drop primary keys, unique indexes, or constraint indexes
    
    • Knob Space: Adjust database configuration parameters
    - You MUST use the SET knob = value to modify database knobs.
    - Focus on parameters that can improve query performance
    - Be conservative with changes to avoid system instability
    
    • Plan Optimizer Space: Use PostgreSQL hints to guide query execution
    - Output ONLY the hint block like: /*+ Set(knob_name value) Set(knob_name value) ... */
    - Do NOT include the SQL query when using hints
    - Use hints to force specific join methods, scan types, etc.
    
    • Query Rewrite Space: Rewrite the SQL for better performance
    - Generate a semantically equivalent SELECT statement
    - Ensure the rewritten query produces exactly the same result set
    - Focus on reducing computational complexity and optimizing data access patterns
    - Maintain identical result set and semantic equivalence

    ## Constraints
    - Fix action must be executable directly in {db_info.get('db_type')}
    - The fix action should be safe and should not drop or delete data unless necessary
    - Ensure all proposed actions are syntactically correct and executable

    ## Output Format
    You must output ONLY in the following format:

    Explanation:
    <concise technical reason for the performance issue and proposed solution>

    Fix_Action:
    <your fix SQL or optimization action here>

    Query_Rewrite:
    <yes or no>

    ## Output Requirements
    - "Explanation" must be a short, precise, technical summary
    - If the fix rewrites the SQL, "Query_Rewrite" must be "yes", else "no"
    - If rewriting, provide the optimized SQL under Fix_Action
    - If adding indexes or changing knobs, provide the exact SQL or command
    - Do not output any extra sections, comments, or steps
    - Focus on the most impactful optimization for this specific query"""
