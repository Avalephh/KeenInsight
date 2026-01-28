import tiktoken


def count_tokens(text: str, model: str = "gpt-4"):
    """
    计算文本在指定模型下的 token 数量
    :param text: 输入的字符串
    :param model: 模型名称 (如 "gpt-3.5-turbo", "gpt-4", "gpt-4o")
    :return: token 数量
    """
    # 获取对应模型的编码器
    encoding = tiktoken.encoding_for_model(model)
    tokens = encoding.encode(text)
    return len(tokens)


if __name__ == "__main__":
    # 在这里粘贴你要统计的文本
    sample_text = """As an SQL optimization expert, your task is to diagnose and fix the following slow SQL query.
    You have the context of slow SQL and the root cause of the performance degradation as below:

    ## Context
    1. Query Information:
    • SQL: select l_returnflag, l_linestatus, sum(l_quantity) as sum_qty, sum(l_extendedprice) as sum_base_price, sum(l_extendedprice * (1 - l_discount)) as sum_disc_price, sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) as sum_charge, avg(l_quantity) as avg_qty, avg(l_extendedprice) as avg_price, avg(l_discount) as avg_disc, count(*) as count_order from lineitem where l_shipdate <= date '1998-12-01' - interval '80' day group by l_returnflag, l_linestatus order by l_returnflag, l_linestatus;
    • Execution Plan: [[{"Plan": {"Node Type": "Aggregate", "Strategy": "Sorted", "Partial Mode": "Finalize", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 2303247.43, "Total Cost": 2303249.39, "Plan Rows": 6, "Plan Width": 236, "Group Key": ["l_returnflag", "l_linestatus"], "Plans": [{"Node Type": "Gather Merge", "Parent Relationship": "Outer", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 2303247.43, "Total Cost": 2303248.83, "Plan Rows": 12, "Plan Width": 236, "Workers Planned": 2, "Plans": [{"Node Type": "Sort", "Parent Relationship": "Outer", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 2302247.41, "Total Cost": 2302247.42, "Plan Rows": 6, "Plan Width": 236, "Sort Key": ["l_returnflag", "l_linestatus"], "Plans": [{"Node Type": "Aggregate", "Strategy": "Hashed", "Partial Mode": "Partial", "Parent Relationship": "Outer", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 2302247.19, "Total Cost": 2302247.33, "Plan Rows": 6, "Plan Width": 236, "Group Key": ["l_returnflag", "l_linestatus"], "Planned Partitions": 0, "Plans": [{"Node Type": "Seq Scan", "Parent Relationship": "Outer", "Parallel Aware": true, "Async Capable": false, "Relation Name": "lineitem", "Alias": "lineitem", "Startup Cost": 0.0, "Total Cost": 1436969.35, "Plan Rows": 24722224, "Plan Width": 25, "Filter": "(l_shipdate <= '1998-09-12 00:00:00'::timestamp without time zone)"}]}]}]}]}, "JIT": {"Functions": 9, "Options": {"Inlining": true, "Optimization": true, "Expressions": true, "Deforming": true}}}]]
    • Execution Runtime: 24.024606227874756 s
    • Internal Metrics: Tuples Returned:59988790, Blocks Hit:525856, Blocks Read:600682, Tuples Fetched:1363, Index Tuples Fetched:0, Sequential Tuples Read:59986052, Sequential Scans:3, Index Scans:0, Heap Blocks Hit:523895, Heap Blocks Read:600647, Index Blocks Hit:0, Index Blocks Read:1

    2. Database Environment Information:
    • Database Type: postgresql
    • Workload Type: OLAP
    • Database Size: ['15 GB']
    • Database Schema: TPC-H
    • CPU: [99.9, 99.9, 99.9, 99.4, 98.9, 99.225, 99.45, 99.15, 35.0]
    • Read I/O: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    • Write I/O: [11.7587890625, 0.18359375000000006, 0.5380859375, 0.0830078124999998, 0.9140625, 0.043945312499999944, 0.037109374999999965, 6.251953125000007, 15.53515625]
    • Virtual Memory: [4.311798095703125, 4.311798095703125, 4.311798095703125, 4.311798095703125, 4.311798095703125, 4.311798095703125, 4.311798095703125, 4.311798095703125, 4.311798095703125]
    • Physical Memory: [0.1578216552734375, 0.1578216552734375, 0.1578216552734375, 0.21632385253906244, 0.5198211669921875, 0.8200225830078124, 1.1268157958984375, 1.4333343505859373, 1.5044708251953125]
    • Net Received: [0.019826889038085938, 0.007236003875732422, 0.0023005008697509766, 0.001473784446716309, 0.0015850067138671875, 0.0019696950912475586, 0.0020103454589843763, 0.001145124435424804, 0.06734561920166016]
    • Net Sent: [0.019594192504882812, 0.004851102828979492, 0.0023005008697509766, 0.001473784446716309, 0.0015850067138671875, 0.0019696950912475586, 0.0020103454589843763, 0.001145124435424804, 0.06734561920166016]

    3. **Identified Root Cause**: ['missing indexes', 'suboptimal plan optimizer']

    ## Mode Instruction
    You are now in **EXPLOIT mode**. Refer to the following Positive Cases and Negative Cases, which are derived from historical fixes under the same root cause. 
    - Positive Cases represent effective fixes. Use the provided fix_actions as a foundation and attempt to derive even better fix_actions.  
    - Negative Cases represent ineffective fixes. Reflect on which actions led to no improvement or performance degradation, and avoid adopting these fix_actions in your solution.
        
    • Positive Cases (effective references): 
    • Negative Cases (ineffective references to avoid): 
    Case 1:
    SQL: /*+ Set(enable_parallel_hash on) Set(enable_hashjoin on) Set(enable_indexscan on) Set(enable_seqscan off) Set(enable_bitmapscan on) Set(enable_gathermerge on) Set(enable_material on) Set(enable_nestloop off) Set(max_parallel_workers_per_gather 4) Set(hash_mem_multiplier 4.0) Set(work_mem 262144) */ select sum(l_extendedprice* (1 - l_discount)) as revenue from lineitem, part where ( p_partkey = l_partkey and p_brand = 'Brand#14' and p_container in ('SM CASE', 'SM BOX', 'SM PACK', 'SM PKG') and l_quantity >= 9 and l_quantity <= 9 + 10 and p_size between 1 and 5 and l_shipmode in ('AIR', 'AIR REG') and l_shipinstruct = 'DELIVER IN PERSON' ) or ( p_partkey = l_partkey and p_brand = 'Brand#11' and p_container in ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK') and l_quantity >= 18 and l_quantity <= 18 + 10 and p_size between 1 and 10 and l_shipmode in ('AIR', 'AIR REG') and l_shipinstruct = 'DELIVER IN PERSON' ) or ( p_partkey = l_partkey and p_brand = 'Brand#11' and p_container in ('LG CASE', 'LG BOX', 'LG PACK', 'LG PKG') and l_quantity >= 23 and l_quantity <= 23 + 10 and p_size between 1 and 15 and l_shipmode in ('AIR', 'AIR REG') and l_shipinstruct = 'DELIVER IN PERSON' );
    Execution Plan: [[{"Plan": {"Node Type": "Aggregate", "Strategy": "Plain", "Partial Mode": "Finalize", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 1445288.75, "Total Cost": 1445288.76, "Plan Rows": 1, "Plan Width": 32, "Plans": [{"Node Type": "Gather", "Parent Relationship": "Outer", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 1445288.01, "Total Cost": 1445288.72, "Plan Rows": 7, "Plan Width": 32, "Workers Planned": 7, "Single Copy": false, "Plans": [{"Node Type": "Aggregate", "Strategy": "Plain", "Partial Mode": "Partial", "Parent Relationship": "Outer", "Parallel Aware": false, "Async Capable": false, "Startup Cost": 1444288.01, "Total Cost": 1444288.02, "Plan Rows": 1, "Plan Width": 32, "Plans": [{"Node Type": "Hash Join", "Parent Relationship": "Outer", "Parallel Aware": true, "Async Capable": false, "Join Type": "Inner", "Startup Cost": 62227.28, "Total Cost": 1444286.73, "Plan Rows": 170, "Plan Width": 12, "Inner Unique": true, "Hash Cond": "(lineitem.l_partkey = part.p_partkey)", "Join Filter": "(((part.p_brand = 'Brand#14'::bpchar) AND (part.p_container = ANY ('{\"SM CASE\",\"SM BOX\",\"SM PACK\",\"SM PKG\"}'::bpchar[])) AND (lineitem.l_quantity >= '9'::numeric) AND (lineitem.l_quantity <= '19'::numeric) AND (part.p_size <= 5)) OR ((part.p_brand = 'Brand#11'::bpchar) AND (part.p_container = ANY ('{\"MED BAG\",\"MED BOX\",\"MED PKG\",\"MED PACK\"}'::bpchar[])) AND (lineitem.l_quantity >= '18'::numeric) AND (lineitem.l_quantity <= '28'::numeric) AND (part.p_size <= 10)) OR ((part.p_brand = 'Brand#11'::bpchar) AND (part.p_container = ANY ('{\"LG CASE\",\"LG BOX\",\"LG PACK\",\"LG PKG\"}'::bpchar[])) AND (lineitem.l_quantity >= '23'::numeric) AND (lineitem.l_quantity <= '33'::numeric) AND (part.p_size <= 15)))", "Plans": [{"Node Type": "Seq Scan", "Parent Relationship": "Outer", "Parallel Aware": true, "Async Capable": false, "Relation Name": "lineitem", "Alias": "lineitem", "Startup Cost": 0.0, "Total Cost": 1381625.08, "Plan Rows": 165476, "Plan Width": 21, "Filter": "((l_shipmode = ANY ('{AIR,\"AIR REG\"}'::bpchar[])) AND (l_shipinstruct = 'DELIVER IN PERSON'::bpchar) AND (((l_quantity >= '9'::numeric) AND (l_quantity <= '19'::numeric)) OR ((l_quantity >= '18'::numeric) AND (l_quantity <= '28'::numeric)) OR ((l_quantity >= '23'::numeric) AND (l_quantity <= '33'::numeric))))"}, {"Node Type": "Hash", "Parent Relationship": "Inner", "Parallel Aware": true, "Async Capable": false, "Startup Cost": 62212.0, "Total Cost": 62212.0, "Plan Rows": 1222, "Plan Width": 30, "Plans": [{"Node Type": "Seq Scan", "Parent Relationship": "Outer", "Parallel Aware": true, "Async Capable": false, "Relation Name": "part", "Alias": "part", "Startup Cost": 0.0, "Total Cost": 62212.0, "Plan Rows": 1222, "Plan Width": 30, "Filter": "((p_size >= 1) AND (((p_brand = 'Brand#14'::bpchar) AND (p_container = ANY ('{\"SM CASE\",\"SM BOX\",\"SM PACK\",\"SM PKG\"}'::bpchar[])) AND (p_size <= 5)) OR ((p_brand = 'Brand#11'::bpchar) AND (p_container = ANY ('{\"MED BAG\",\"MED BOX\",\"MED PKG\",\"MED PACK\"}'::bpchar[])) AND (p_size <= 10)) OR ((p_brand = 'Brand#11'::bpchar) AND (p_container = ANY ('{\"LG CASE\",\"LG BOX\",\"LG PACK\",\"LG PKG\"}'::bpchar[])) AND (p_size <= 15))))"}]}]}]}]}]}, "JIT": {"Functions": 21, "Options": {"Inlining": true, "Optimization": true, "Expressions": true, "Deforming": true}}}]]
    Origin Execution Runtime: 2.0602874755859375s
    Root Causes: ['missing indexes', 'suboptimal plan optimizer']
    Fix_Actions: CREATE INDEX IF NOT EXISTS lineitem_l_partkey_l_quantity_l_shipmode_l_shipinstruct_idx ON lineitem(l_partkey, l_quantity, l_shipmode, l_shipinstruct);
CREATE INDEX IF NOT EXISTS part_p_partkey_p_brand_p_container_p_size_idx ON part(p_partkey, p_brand, p_container, p_size);SET max_parallel_workers_per_gather = 4;
SET work_mem = '262144kB';
SET hash_mem_multiplier = 4.0;
    Improve: -8.642799615859985

    ## Action Space
    - If the root cause is "inappropriate query knobs", you MUST perform actions within Knob Space using SET knob = value.
    - If the root cause is "suboptimal plan optimizer", you MUST perform actions within Plan Optimizer Space using PostgreSQL comment-style hints ONLY. Output ONLY the hint block like: /*+ Set(knob_name value) Set(knob_name value) ... */. Do NOT include the SQL query.
    - If the root cause is "missing indexes", you MUST perform actions within Index Space using CREATE INDEX IF NOT EXISTS or DROP INDEX IF EXISTS.
    - If the root cause is "poorly written queries", you MUST perform actions within Query Rewrite Space using rewrite SQL.
    - **If multiple root causes are identified, you MUST consider all FIX_ACTION outputs jointly and determine the optimal solution that addresses the combined root causes, while strictly limiting actions to the relevant action spaces of the identified root causes. Do not perform any actions outside of these specified root causes.**
    - The Action Space defines the operations that allowed to perform, you MUST strictly limit your Fix_Action to these spaces. Do not adjust or introduce any actions beyond what is explicitly defined in the following Action Space.
    
    • Index Space: 
    - The naming convention for newly created indexes must follow the pattern: (table_name)_(col1)_(col2)_idx.
    - Index creation should be strictly guided by the slow_sql queries.
    - Consider the query execution plan to identify missing indexes that could improve performance.
    - Avoid generating duplicate or subsumed indexes, and do not drop primary keys, unique indexes, or any indexes that enforce constraints.
    - Do not conflict with the existing indexes in the database: ['nation_pkey', 'supplier_pkey', 'customer_pkey', 'partsupp_pkey', 'part_pkey', 'orders_pkey', 'lineitem_pkey', 'region_pkey']
    
    • Plan Optimizer Space: 
    - Plan Optimizer Space is strictly limited to the following knobs, you must only use these knobs in your Fix_Action, and any knob not explicitly listed here must not be used: 
    enable_async_append: type: boolean, min: 0, max: 1, default: 1
    enable_bitmapscan: type: boolean, min: 0, max: 1, default: 1
    enable_gathermerge: type: boolean, min: 0, max: 1, default: 1
    enable_hashagg: type: boolean, min: 0, max: 1, default: 1
    enable_hashjoin: type: boolean, min: 0, max: 1, default: 1
    enable_incremental_sort: type: boolean, min: 0, max: 1, default: 1
    enable_indexonlyscan: type: boolean, min: 0, max: 1, default: 1
    enable_indexscan: type: boolean, min: 0, max: 1, default: 1
    enable_material: type: boolean, min: 0, max: 1, default: 1
    enable_memoize: type: boolean, min: 0, max: 1, default: 1
    enable_mergejoin: type: boolean, min: 0, max: 1, default: 1
    enable_nestloop: type: boolean, min: 0, max: 1, default: 1
    enable_parallel_append: type: boolean, min: 0, max: 1, default: 1
    enable_parallel_hash: type: boolean, min: 0, max: 1, default: 1
    enable_partition_pruning: type: boolean, min: 0, max: 1, default: 1
    enable_partitionwise_aggregate: type: boolean, min: 0, max: 1, default: 0
    enable_partitionwise_join: type: boolean, min: 0, max: 1, default: 0
    enable_seqscan: type: boolean, min: 0, max: 1, default: 1
    enable_sort: type: boolean, min: 0, max: 1, default: 1
    - **IMPORTANT**: When using Plan Optimizer Space, output ONLY the hint block. Do NOT include the original SQL. Example: /*+ Set(enable_hashjoin on) Set(enable_sort on) */

    ## Constraints
    - Fix action must be executable directly in postgresql.
    - Please ensure that the proposed fix_action strictly addresses the Identified Root Cause and does not deviate from the Action Space.
    - The fix action should be safe and should not drop or delete data unless explicitly required by the root cause.

    ## Output Format
    You must output ONLY in the following format, with explanation and fix action:

    Explanation:
    <concise technical reason why the root cause is causing the slowdown now>

    Fix_Action:
    <your fix SQL or optimization action here>

    Query_Rewrite:
    <yes or no>

    ## Output Requirements
    - "Explanation" must be a short, precise, technical summary — no generic statements.
    - If the fix rewrites the SQL, "Query_Rewrite" must be "yes", else "no".
    - If rewriting, provide the optimized SQL under Fix_Action.
    - If adding indexes or changing knobs, provide the exact SQL or command.
    - Do not output any extra sections, comments, or steps.
"""

    model_name = "gpt-4o-mini"  # 也可以改成 "gpt-3.5-turbo" 或 "gpt-4o"
    token_count = count_tokens(sample_text, model_name)

    print(f"模型 {model_name} 下的 token 数: {token_count}")
