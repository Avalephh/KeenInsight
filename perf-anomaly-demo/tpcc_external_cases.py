#!/usr/bin/env python3
"""Validate external-factor TPCC cases against the native PostgreSQL instance.

The external phase never changes PostgreSQL settings.  It only starts clients
that execute queries against the existing keeninsight_tpcc schema.  After the
Prometheus alert is observed, perf and the original SysInsight file-based
functions are run.  The tuned phase uses session-scoped SET statements only;
no persistent configuration or database object is changed by this runner.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parent
CASE_ROOT = ROOT / "tpcc_cases"
RESULT_ROOT = ROOT / "results" / "tpcc_external"
SOURCE_ROOT = Path(
    os.environ.get(
        "SYSINSIGHT_SOURCE_ROOT",
        str(ROOT.parent / "repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune"),
    )
)

# These are the repository sources used by the existing strict wrapper.
SOURCE_ANALYZER = SOURCE_ROOT / "DBTuner/utils/analyzeException.py"
MATCHER = SOURCE_ROOT / "DBTuner/utils/matchFunctions.py"
STATIC_LIBRARY = SOURCE_ROOT / "DBTuner/utils/paramater_association_library.json"

sys.path.insert(0, str(ROOT))
import run_demo as strict_demo  # noqa: E402


CASE_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "id": "c01_work_mem_sort",
        "title": "外部报表到达后高基数排序溢出",
        "event": "启动新的订单金额全量排序报表，正常 TPCC 点查询保持运行",
        "sql": "c01_sort.sql",
        "tuned_sql": "c01_sort_tuned.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {"work_mem": "256MB"},
        "plan_sql": (
            "SELECT sum(ol_amount) FROM (SELECT ol_w_id, ol_d_id, ol_o_id, "
            "ol_number, ol_amount FROM keeninsight_tpcc.order_line ORDER BY "
            "ol_amount DESC, ol_w_id, ol_d_id, ol_o_id, ol_number) AS q"
        ),
        "plan_sets": ["work_mem='256MB'"],
    },
    {
        "id": "c02_work_mem_distinct",
        "title": "外部去重分析到达后哈希/去重内存不足",
        "event": "启动新的订单明细去重分析，正常 TPCC 点查询保持运行",
        "sql": "c02_distinct.sql",
        "tuned_sql": "c02_distinct_tuned.sql",
        "mode": "pgbench",
        "clients": 4,
        "repair": {"work_mem": "256MB"},
        "plan_sql": (
            "SELECT count(*) FROM (SELECT DISTINCT ol_i_id, ol_amount, "
            "ol_quantity, ol_supply_w_id FROM keeninsight_tpcc.order_line) AS q"
        ),
        "plan_sets": ["work_mem='256MB'"],
    },
    {
        "id": "c03_parallel_gather",
        "title": "外部报表扩大后并行度不足",
        "event": "启动订单与订单明细的大表关联报表，正常 TPCC 点查询保持运行",
        "sql": "c03_parallel_join.sql",
        "tuned_sql": "c03_parallel_join_tuned.sql",
        "mode": "pgbench",
        "clients": 1,
        "repair": {"max_parallel_workers_per_gather": "4"},
        "plan_sql": (
            "SELECT o.o_w_id, o.o_d_id, count(*), sum(ol.ol_amount) FROM "
            "keeninsight_tpcc.orders o JOIN keeninsight_tpcc.order_line ol ON "
            "ol.ol_w_id=o.o_w_id AND ol.ol_d_id=o.o_d_id AND ol.ol_o_id=o.o_id "
            "GROUP BY o.o_w_id,o.o_d_id ORDER BY sum(ol.ol_amount) DESC"
        ),
        "plan_sets": ["max_parallel_workers_per_gather=4"],
    },
    {
        "id": "c04_parallel_cost",
        "title": "外部中等规模报表出现并行计划门槛",
        "event": "新报表反复扫描订单表，现有参数下规划器保持串行计划",
        "sql": "c04_parallel_cost.sql",
        "tuned_sql": "c04_parallel_cost_tuned.sql",
        "mode": "pgbench",
        "clients": 4,
        "repair": {"parallel_setup_cost": "0", "parallel_tuple_cost": "0.001"},
        "plan_sql": (
            "SELECT count(*), sum(o_id) FROM keeninsight_tpcc.orders "
            "WHERE o_entry_d >= TIMESTAMPTZ '2025-01-01'"
        ),
        "plan_sets": ["parallel_setup_cost=0", "parallel_tuple_cost=0.001"],
    },
    {
        "id": "c05_jit_threshold",
        "title": "外部复杂表达式查询触发 JIT 开销",
        "event": "新业务报表加入复杂数值表达式和全量订单明细关联",
        "sql": "c05_jit.sql",
        "tuned_sql": "c05_jit_tuned.sql",
        "mode": "pgbench",
        "clients": 3,
        "repair": {"jit": "off"},
        "plan_sql": (
            "SELECT sum((ol.ol_amount * (ol.ol_quantity + 1))::numeric) FROM "
            "keeninsight_tpcc.order_line ol JOIN keeninsight_tpcc.item i ON "
            "i.i_id=ol.ol_i_id WHERE i.i_price > 10"
        ),
        "plan_sets": ["jit=off"],
    },
    {
        "id": "c06_maintenance_memory",
        "title": "外部批处理触发大表索引维护",
        "event": "外部批处理要求对订单明细建立临时分析索引，正常 TPCC 查询保持运行",
        "sql": "c06_maintenance.sql",
        "tuned_sql": "c06_maintenance_tuned.sql",
        "mode": "psql",
        "clients": 4,
        "repair": {"maintenance_work_mem": "256MB"},
        "plan_sql": "",
        "plan_sets": [],
    },
    {
        "id": "c07_temp_buffers",
        "title": "外部临时分析表到达后临时缓冲不足",
        "event": "外部分析任务把 TPCC 订单明细装入临时表并反复扫描，正常 TPCC 点查询保持运行",
        "sql": "c07_temp_buffers.sql",
        "tuned_sql": "c07_temp_buffers_tuned.sql",
        "mode": "psql",
        "clients": 3,
        "repair": {"temp_buffers": "256MB"},
        "plan_sql": "",
        "plan_sets": [],
    },
    {
        "id": "c08_parallel_contention",
        "title": "外部并发报表突增后并行工作进程争用",
        "event": "外部同时提交多份 TPCC 订单报表，现有并行度导致工作进程争用",
        "sql": "c08_parallel_contention.sql",
        "tuned_sql": "c08_parallel_contention_tuned.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {"max_parallel_workers_per_gather": "1"},
        "plan_sql": (
            "SELECT o.o_w_id, o.o_d_id, count(*), sum(ol.ol_amount) FROM "
            "keeninsight_tpcc.orders o JOIN keeninsight_tpcc.order_line ol ON "
            "ol.ol_w_id=o.o_w_id AND ol.ol_d_id=o.o_d_id AND ol.ol_o_id=o.o_id "
            "GROUP BY o.o_w_id,o.o_d_id ORDER BY sum(ol.ol_amount) DESC"
        ),
        "plan_sets": ["max_parallel_workers_per_gather=1"],
    },
    {
        "id": "c09_window_work_mem",
        "title": "外部窗口报表到达后排序工作区不足",
        "event": "外部窗口报表对 TPCC 订单明细按仓库和订单顺序计算累计金额，正常 TPCC 点查询保持运行",
        "sql": "c09_window_work_mem.sql",
        "tuned_sql": "c09_window_work_mem_tuned.sql",
        "mode": "pgbench",
        "clients": 2,
        "repair": {"work_mem": "256MB"},
        "plan_sql": (
            "SELECT sum(running_amount) FROM (SELECT ol_w_id, ol_d_id, "
            "ol_o_id, ol_number, sum(ol_amount) OVER (PARTITION BY ol_w_id "
            "ORDER BY ol_d_id, ol_o_id, ol_number ROWS BETWEEN UNBOUNDED "
            "PRECEDING AND CURRENT ROW) AS running_amount FROM "
            "keeninsight_tpcc.order_line) AS running_orders"
        ),
        "plan_sets": ["work_mem='256MB'"],
    },
    {
        "id": "c10_window_spill",
        "title": "外部按商品窗口报表造成排序临时文件",
        "event": "外部分析按商品对 TPCC 订单明细计算累计金额，现有工作区导致窗口排序溢出",
        "sql": "c10_window_spill.sql",
        "tuned_sql": "c10_window_spill_tuned.sql",
        "mode": "pgbench",
        "clients": 2,
        "repair": {"work_mem": "256MB"},
        "plan_sql": (
            "SELECT sum(running_amount) FROM (SELECT ol_i_id, ol_amount, "
            "sum(ol_amount) OVER (PARTITION BY ol_i_id ORDER BY ol_amount "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_amount "
            "FROM keeninsight_tpcc.order_line) AS item_running_orders"
        ),
        "plan_sets": ["work_mem='256MB'"],
    },
    {
        "id": "c11_random_page_cost",
        "title": "外部高频切片查询触发随机访问代价误判",
        "event": "外部 TPCC 客户切片报表短时间内重复发起大量范围查找，现有随机访问代价让规划器选择较重的位图路径",
        "sql": "c11_random_page_cost.sql",
        "tuned_sql": "c11_random_page_cost_tuned.sql",
        "mode": "pgbench",
        "clients": 3,
        "repair": {"random_page_cost": "1.1"},
        "plan_sql": (
            "SELECT sum(q) FROM generate_series(1,5000) AS g CROSS JOIN LATERAL "
            "(SELECT sum(c_balance) AS q FROM keeninsight_tpcc.customer WHERE "
            "c_w_id=1 AND c_d_id=1 AND c_id BETWEEN 1 AND (300 + g*0)) AS customer_slice"
        ),
        "plan_sets": ["random_page_cost=1.1"],
    },
    {
        "id": "c12_hash_join_spill",
        "title": "外部关联报表造成哈希连接分批溢写",
        "event": "外部 TPCC 订单明细关联报表反复执行，当前工作区不足导致并行哈希连接分批写临时文件",
        "sql": "c12_hash_join_spill.sql",
        "tuned_sql": "c12_hash_join_spill_tuned.sql",
        "mode": "pgbench",
        "clients": 2,
        "repair": {"work_mem": "256MB"},
        "plan_sql": (
            "SELECT count(*), sum(a.ol_amount) FROM keeninsight_tpcc.order_line a "
            "JOIN keeninsight_tpcc.order_line b ON b.ol_w_id=a.ol_w_id AND "
            "b.ol_d_id=a.ol_d_id AND b.ol_o_id=a.ol_o_id AND b.ol_number=a.ol_number "
            "WHERE a.ol_amount > 0"
        ),
        "plan_sets": ["work_mem='256MB'"],
    },
    {
        "id": "c13_double_sort",
        "title": "外部双源报表造成两路排序溢出",
        "event": "外部 TPCC 对账报表同时整理两份订单明细源并按金额排序，当前工作区造成多路临时排序",
        "sql": "c13_double_sort.sql",
        "tuned_sql": "c13_double_sort_tuned.sql",
        "mode": "pgbench",
        "clients": 1,
        "repair": {"work_mem": "256MB"},
        "plan_sql": (
            "SELECT count(*), sum(a.ol_amount) FROM (SELECT ol_w_id, ol_d_id, "
            "ol_o_id, ol_number, ol_amount FROM keeninsight_tpcc.order_line "
            "ORDER BY ol_amount, ol_w_id, ol_d_id, ol_o_id, ol_number) a JOIN "
            "(SELECT ol_w_id, ol_d_id, ol_o_id, ol_number, ol_amount FROM "
            "keeninsight_tpcc.order_line ORDER BY ol_amount, ol_w_id, ol_d_id, "
            "ol_o_id, ol_number) b ON b.ol_w_id=a.ol_w_id AND b.ol_d_id=a.ol_d_id "
            "AND b.ol_o_id=a.ol_o_id AND b.ol_number=a.ol_number"
        ),
        "plan_sets": ["work_mem='256MB'"],
    },
    {
        "id": "c14_parallel_cost_heavy",
        "title": "外部重复报表扫描触发并行成本门槛",
        "event": "外部 TPCC 订单汇总短时间内连续提交大量扫描请求，现有并行成本门槛让每次请求保持串行",
        "sql": "c14_parallel_cost_heavy.sql",
        "tuned_sql": "c14_parallel_cost_heavy_tuned.sql",
        "mode": "pgbench",
        "clients": 2,
        "repair": {"parallel_setup_cost": "0", "parallel_tuple_cost": "0.001"},
        "plan_sql": (
            "SELECT count(*), sum(o_id) FROM keeninsight_tpcc.orders WHERE "
            "o_entry_d >= TIMESTAMPTZ '2025-01-01'"
        ),
        "plan_sets": ["parallel_setup_cost=0", "parallel_tuple_cost=0.001"],
    },
    {
        "id": "c15_maintenance_heavy",
        "title": "外部批处理连续构建多路临时索引",
        "event": "外部 TPCC 分析批处理在一个事务中连续构建多路临时索引后回滚，正常 TPCC 点查询保持运行",
        "sql": "c15_maintenance_heavy.sql",
        "tuned_sql": "c15_maintenance_heavy_tuned.sql",
        "mode": "psql",
        "clients": 2,
        "repair": {"maintenance_work_mem": "256MB"},
        "plan_sql": "",
        "plan_sets": [],
    },
    {
        "id": "c16_union_reconciliation",
        "title": "外部双来源对账触发集合去重溢写",
        "event": "外部对账任务把两份 TPCC 订单明细来源合并去重，当前工作区不足导致 UNION 排序落盘",
        "sql": "c16_union_reconciliation.sql",
        "tuned_sql": "c16_union_reconciliation_tuned.sql",
        "mode": "pgbench",
        "clients": 2,
        "repair": {"work_mem": "256MB"},
        "plan_sql": (
            "SELECT count(*) FROM (SELECT ol_i_id, ol_amount, ol_quantity, "
            "ol_supply_w_id, ol_w_id FROM keeninsight_tpcc.order_line "
            "UNION SELECT ol_i_id, ol_amount, ol_quantity, ol_supply_w_id, "
            "ol_w_id FROM keeninsight_tpcc.order_line) AS duplicate_feed"
        ),
        "plan_sets": ["work_mem='256MB'"],
    },
    {
        "id": "c17_intersect_reconciliation",
        "title": "外部双快照核对触发集合交集溢写",
        "event": "外部核对任务比较两份 TPCC 订单明细快照，当前工作区不足导致 INTERSECT 排序落盘",
        "sql": "c17_intersect_reconciliation.sql",
        "tuned_sql": "c17_intersect_reconciliation_tuned.sql",
        "mode": "pgbench",
        "clients": 2,
        "repair": {"work_mem": "256MB"},
        "plan_sql": (
            "SELECT count(*) FROM (SELECT ol_i_id, ol_amount, ol_quantity, "
            "ol_supply_w_id FROM keeninsight_tpcc.order_line WHERE ol_amount > 0 "
            "INTERSECT SELECT ol_i_id, ol_amount, ol_quantity, ol_supply_w_id "
            "FROM keeninsight_tpcc.order_line WHERE ol_amount > 0) AS common_feed"
        ),
        "plan_sets": ["work_mem='256MB'"],
    },
    {
        "id": "c18_except_reconciliation",
        "title": "外部仓库差异核对触发集合排除溢写",
        "event": "外部差异核对任务从 TPCC 全量订单明细中排除一个仓库来源，当前工作区不足导致 EXCEPT 排序落盘",
        "sql": "c18_except_reconciliation.sql",
        "tuned_sql": "c18_except_reconciliation_tuned.sql",
        "mode": "pgbench",
        "clients": 2,
        "repair": {"work_mem": "256MB"},
        "plan_sql": (
            "SELECT count(*) FROM (SELECT ol_i_id, ol_amount, ol_quantity, "
            "ol_supply_w_id, ol_w_id FROM keeninsight_tpcc.order_line "
            "EXCEPT SELECT ol_i_id, ol_amount, ol_quantity, ol_supply_w_id, "
            "ol_w_id FROM keeninsight_tpcc.order_line WHERE ol_w_id=1) "
            "AS remaining_feed"
        ),
        "plan_sets": ["work_mem='256MB'"],
    },
    {
        "id": "c19_cpu_tuple_parallel",
        "title": "外部库存范围报表触发每元组成本误判",
        "event": "外部库存范围报表扩大到整仓商品区间，当前每元组成本估计让规划器保持串行位图路径",
        "sql": "c19_cpu_tuple_parallel.sql",
        "tuned_sql": "c19_cpu_tuple_parallel_tuned.sql",
        "mode": "pgbench",
        "clients": 2,
        "repair": {"cpu_tuple_cost": "0.05"},
        "plan_sql": (
            "SELECT sum(s_quantity) FROM keeninsight_tpcc.stock WHERE s_w_id=1 "
            "AND s_i_id BETWEEN 1 AND 100000"
        ),
        "plan_sets": ["cpu_tuple_cost=0.05"],
    },
    {
        "id": "d01_work_mem_sort",
        "title": "外部订单金额报表造成排序工作区压力",
        "event": "外部订单金额报表开始全量排序，正常 TPCC 点查询保持运行",
        "sql": "d01_work_mem_sort.sql",
        "tuned_sql": "d01_work_mem_sort_tuned.sql",
        "mode": "pgbench",
        "clients": 4,
        "repair": {"work_mem": "256MB"},
        "plan_sql": (
            "SELECT sum(ol_amount) FROM (SELECT ol_w_id, ol_d_id, ol_o_id, "
            "ol_number, ol_amount FROM keeninsight_tpcc.order_line ORDER BY "
            "ol_amount DESC, ol_w_id, ol_d_id, ol_o_id, ol_number) AS q"
        ),
        "plan_sets": ["work_mem='256MB'"],
    },
    {
        "id": "d02_parallel_worker_contention",
        "title": "外部并行报表突发造成工作进程争用",
        "event": "外部同时提交多份订单聚合报表，正常 TPCC 点查询保持运行",
        "sql": "d02_parallel_worker_contention.sql",
        "tuned_sql": "d02_parallel_worker_contention_tuned.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {"max_parallel_workers_per_gather": "0"},
        "plan_sql": (
            "SELECT o.o_w_id, o.o_d_id, count(*), sum(ol.ol_amount) FROM "
            "keeninsight_tpcc.orders o JOIN keeninsight_tpcc.order_line ol ON "
            "ol.ol_w_id=o.o_w_id AND ol.ol_d_id=o.o_d_id AND ol.ol_o_id=o.o_id "
            "GROUP BY o.o_w_id,o.o_d_id ORDER BY sum(ol.ol_amount) DESC"
        ),
        "plan_sets": ["max_parallel_workers_per_gather=0"],
    },
    {
        "id": "d03_parallel_scan_threshold",
        "title": "外部全表聚合造成并行扫描争用",
        "event": "外部订单明细全表聚合报表到达，正常 TPCC 点查询保持运行",
        "sql": "d03_parallel_scan_threshold.sql",
        "tuned_sql": "d03_parallel_scan_threshold_tuned.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {"min_parallel_table_scan_size": "1GB"},
        "plan_sql": "SELECT sum(ol_amount) FROM keeninsight_tpcc.order_line",
        "plan_sets": ["min_parallel_table_scan_size='1GB'"],
    },
    {
        "id": "d04_nestloop_misestimation",
        "title": "外部关联报表造成嵌套循环误选",
        "event": "外部关联报表中的表达式谓词使规划器低估订单行数，正常 TPCC 点查询保持运行",
        "sql": "d04_nestloop_misestimation.sql",
        "tuned_sql": "d04_nestloop_misestimation_tuned.sql",
        "mode": "pgbench",
        "clients": 5,
        "repair": {"enable_nestloop": "off"},
        "plan_sql": (
            "SELECT sum(a.ol_amount+b.ol_amount) FROM keeninsight_tpcc.orders o JOIN "
            "keeninsight_tpcc.order_line a ON a.ol_w_id=o.o_w_id AND "
            "a.ol_d_id=o.o_d_id AND a.ol_o_id=o.o_id JOIN "
            "keeninsight_tpcc.order_line b ON b.ol_w_id=o.o_w_id AND "
            "b.ol_d_id=o.o_d_id AND b.ol_o_id=o.o_id WHERE "
            "o.o_id=(o.o_id+floor(random()*0))"
        ),
        "plan_sets": ["enable_nestloop=off"],
    },
    {
        "id": "d05_parallel_tuple_pressure",
        "title": "外部高基数聚合造成并行结果传输压力",
        "event": "外部商品汇总报表产生大量分组结果，正常 TPCC 点查询保持运行",
        "sql": "d05_parallel_tuple_pressure.sql",
        "tuned_sql": "d05_parallel_tuple_pressure_tuned.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {"parallel_tuple_cost": "10"},
        "plan_sql": (
            "SELECT count(*) FROM (SELECT ol_i_id, count(*), sum(ol_amount) "
            "FROM keeninsight_tpcc.order_line GROUP BY ol_i_id ORDER BY ol_i_id) q"
        ),
        "plan_sets": ["parallel_tuple_cost=10"],
    },
    {
        "id": "d06_parallel_setup_pressure",
        "title": "外部重复聚合报表造成并行启动压力",
        "event": "外部报表连续重复提交订单明细聚合，正常 TPCC 点查询保持运行",
        "sql": "d06_parallel_setup_pressure.sql",
        "tuned_sql": "d06_parallel_setup_pressure_tuned.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {"parallel_setup_cost": "100000"},
        "plan_sql": (
            "SELECT o.o_w_id, o.o_d_id, count(*), sum(ol.ol_amount) FROM "
            "keeninsight_tpcc.orders o JOIN keeninsight_tpcc.order_line ol ON "
            "ol.ol_w_id=o.o_w_id AND ol.ol_d_id=o.o_d_id AND ol.ol_o_id=o.o_id "
            "GROUP BY o.o_w_id,o.o_d_id ORDER BY sum(ol.ol_amount) DESC"
        ),
        "plan_sets": ["parallel_setup_cost=100000"],
    },
    {
        "id": "d07_temp_buffer_pressure",
        "title": "外部临时分析表造成临时缓冲压力",
        "event": "外部分析任务把订单明细装入临时表并反复扫描，正常 TPCC 点查询保持运行",
        "sql": "d07_temp_buffer_pressure.sql",
        "tuned_sql": "d07_temp_buffer_pressure_tuned.sql",
        "mode": "psql",
        "clients": 6,
        "repair": {"temp_buffers": "512MB"},
        "plan_sql": "",
        "plan_sets": [],
    },
    {
        "id": "d08_parallel_worker_cap",
        "title": "外部大表连接报表争用全局并行工作进程",
        "event": "外部商品价格报表集中扫描订单明细并连接商品表，正常 TPCC 点查询保持运行",
        "sql": "d08_parallel_worker_cap.sql",
        "tuned_sql": "d08_parallel_worker_cap_tuned.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {"max_parallel_workers": "0"},
        "plan_sql": (
            "SELECT sum((ol.ol_amount * (ol.ol_quantity + 1))::numeric) "
            "FROM keeninsight_tpcc.order_line ol JOIN keeninsight_tpcc.item i "
            "ON i.i_id=ol.ol_i_id WHERE i.i_price>10"
        ),
        "plan_sets": ["max_parallel_workers=0"],
    },
    {
        "id": "d09_jit_expression_pressure",
        "title": "外部表达式密集型客户分析造成 JIT 编译压力",
        "event": "外部客户画像任务对 TPCC 客户余额执行大量数学表达式计算，正常 TPCC 点查询保持运行",
        "sql": "d09_jit_expression_pressure.sql",
        "tuned_sql": "d09_jit_expression_pressure_tuned.sql",
        "mode": "pgbench",
        "clients": 4,
        "repair": {"jit": "off"},
        "plan_sql": (
            "SELECT sum((c.c_balance::double precision * g.n) + "
            "sqrt(abs(c.c_balance::double precision)) + "
            "sin(c.c_balance::double precision) + "
            "cos(c.c_balance::double precision) + "
            "ln(abs(c.c_balance::double precision) + 1) + "
            "exp((c.c_id::double precision) / 100000.0)) "
            "FROM keeninsight_tpcc.customer c CROSS JOIN "
            "generate_series(1,10000) g(n) WHERE g.n=1"
        ),
        "plan_sets": ["jit=off"],
    },
    {
        "id": "d10_random_access_pressure",
        "title": "外部高频客户切片造成随机访问压力",
        "event": "外部客户切片报表并发重复查找订单客户区间，正常 TPCC 点查询保持运行",
        "sql": "c11_random_page_cost.sql",
        "tuned_sql": "c11_random_page_cost_tuned.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {"random_page_cost": "1.1"},
        "plan_sql": (
            "SELECT sum(q) FROM generate_series(1,5000) AS g CROSS JOIN LATERAL "
            "(SELECT sum(c_balance) AS q FROM keeninsight_tpcc.customer WHERE "
            "c_w_id=1 AND c_d_id=1 AND c_id BETWEEN 1 AND (300 + g*0)) AS customer_slice"
        ),
        "plan_sets": ["random_page_cost=1.1"],
    },
    {
        "id": "d11_jit_high_concurrency",
        "title": "外部高并发表达式分析造成 JIT 编译压力",
        "event": "外部客户画像任务以更高并发执行表达式密集型 TPCC 分析，正常 TPCC 点查询保持运行",
        "sql": "d09_jit_expression_pressure.sql",
        "tuned_sql": "d09_jit_expression_pressure_tuned.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {"jit": "off"},
        "plan_sql": (
            "SELECT sum((c.c_balance::double precision * g.n) + "
            "sqrt(abs(c.c_balance::double precision)) + "
            "sin(c.c_balance::double precision) + "
            "cos(c.c_balance::double precision) + "
            "ln(abs(c.c_balance::double precision) + 1) + "
            "exp((c.c_id::double precision) / 100000.0)) "
            "FROM keeninsight_tpcc.customer c CROSS JOIN "
            "generate_series(1,10000) g(n) WHERE g.n=1"
        ),
        "plan_sets": ["jit=off"],
    },
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="keeninsight")
    parser.add_argument("--db-user", default="postgres")
    parser.add_argument("--run-as", default="postgres")
    parser.add_argument("--host", default="/var/run/postgresql")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--alert-name", default="SysInsightDemoAnomaly")
    parser.add_argument("--baseline-duration", type=int, default=20)
    parser.add_argument("--case-duration", type=int, default=25)
    parser.add_argument("--tuned-duration", type=int, default=15)
    parser.add_argument("--normal-clients", type=int, default=2)
    parser.add_argument("--perf-frequency", type=int, default=300)
    parser.add_argument("--normal-sql", default="normal.sql")
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated case IDs; empty means all defined TPCC cases",
    )
    parser.add_argument(
        "--tuned-override",
        default="",
        help="override the tuned SQL with one session SET, formatted parameter=value",
    )
    return parser.parse_args()


def process_args(args: argparse.Namespace) -> argparse.Namespace:
    # The existing perf helpers only need these fields.  Keeping this object
    # separate avoids changing the existing strict demo's command line API.
    return argparse.Namespace(
        perf_frequency=args.perf_frequency,
        stackcollapse=str(ROOT / "vendor" / "FlameGraph" / "stackcollapse-perf.pl"),
        prometheus_url=args.prometheus_url,
        alert_name=args.alert_name,
    )


def connection_options(config: Optional[Dict[str, Any]]) -> Optional[str]:
    if not config:
        return None
    options: List[str] = []
    for name, value in config.items():
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", str(name)):
            raise ValueError("unsafe connection GUC name: {}".format(name))
        text = str(value)
        if not re.fullmatch(r"[A-Za-z0-9_./:+%\- ]+", text):
            raise ValueError("unsafe connection GUC value for {}".format(name))
        options.append("-c {}={}".format(name, text))
    return " ".join(options)


def psql_command(
    args: argparse.Namespace,
    application_name: str,
    extra: Optional[List[str]] = None,
    connection_config: Optional[Dict[str, Any]] = None,
) -> List[str]:
    env = ["PGAPPNAME={}".format(application_name)]
    options = connection_options(connection_config)
    if options:
        env.append("PGOPTIONS={}".format(options))
    command = [
        "runuser",
        "-u",
        args.run_as,
        "--",
        "env",
        *env,
        "psql",
        "-X",
        "-A",
        "-t",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        args.host,
        "-p",
        str(getattr(args, "port", 5432)),
        "-U",
        args.db_user,
        "-d",
        args.db,
    ]
    if extra:
        command.extend(extra)
    return command


def psql_text(
    args: argparse.Namespace,
    sql: str,
    application_name: str,
    timeout: float = 30.0,
    connection_config: Optional[Dict[str, Any]] = None,
) -> str:
    completed = subprocess.run(
        psql_command(args, application_name, ["-c", sql], connection_config),
        cwd="/",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
    )
    return completed.stdout.strip()


def sql_literal(value: str) -> str:
    return "'{}'".format(value.replace("'", "''"))


def app_pids(args: argparse.Namespace, prefix: str) -> List[int]:
    sql = (
        "SELECT pid FROM pg_stat_activity WHERE datname=current_database() "
        "AND backend_type='client backend' AND application_name LIKE {} ORDER BY pid"
    ).format(sql_literal(prefix + "%"))
    raw = psql_text(args, sql, "perf-anomaly-demo-tpcc-meta-pids")
    pids: List[int] = []
    for line in raw.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            pass
    return pids


def wait_for_pids(args: argparse.Namespace, prefix: str, minimum: int, timeout: float = 8.0) -> List[int]:
    deadline = time.monotonic() + timeout
    latest: List[int] = []
    while time.monotonic() < deadline:
        try:
            latest = app_pids(args, prefix)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            latest = []
        if len(latest) >= minimum:
            return latest
        time.sleep(0.2)
    return latest


def metric_snapshot(args: argparse.Namespace, prefix: str, phase: str) -> Dict[str, Any]:
    sql = """
SELECT json_build_object(
  'ts', extract(epoch FROM clock_timestamp()),
  'phase', current_setting('application_name'),
  'active_case_backends', (SELECT count(*)::int FROM pg_stat_activity
      WHERE application_name LIKE {prefix}),
  'active_total', (SELECT count(*)::int FROM pg_stat_activity WHERE state='active'),
  'xact_commit', d.xact_commit,
  'xact_rollback', d.xact_rollback,
  'tup_returned', d.tup_returned,
  'tup_fetched', d.tup_fetched,
  'blks_read', d.blks_read,
  'blks_hit', d.blks_hit,
  'temp_files', d.temp_files,
  'temp_bytes', d.temp_bytes,
  'deadlocks', d.deadlocks,
  'database_size', pg_database_size(current_database()),
  'checkpoints_req', (SELECT checkpoints_req FROM pg_stat_bgwriter),
  'checkpoints_timed', (SELECT checkpoints_timed FROM pg_stat_bgwriter),
  'checkpoint_write_time', (SELECT checkpoint_write_time FROM pg_stat_bgwriter),
  'checkpoint_sync_time', (SELECT checkpoint_sync_time FROM pg_stat_bgwriter),
  'buffers_backend', (SELECT buffers_backend FROM pg_stat_bgwriter),
  'buffers_backend_fsync', (SELECT buffers_backend_fsync FROM pg_stat_bgwriter),
  'buffers_checkpoint', (SELECT buffers_checkpoint FROM pg_stat_bgwriter),
  'buffers_clean', (SELECT buffers_clean FROM pg_stat_bgwriter),
  'maxwritten_clean', (SELECT maxwritten_clean FROM pg_stat_bgwriter),
  'tpcc_n_dead_tup', COALESCE((SELECT sum(n_dead_tup)::bigint FROM pg_stat_user_tables
      WHERE schemaname='keeninsight_tpcc'), 0),
  'tpcc_n_tup_ins', COALESCE((SELECT sum(n_tup_ins)::bigint FROM pg_stat_user_tables
      WHERE schemaname='keeninsight_tpcc'), 0),
  'tpcc_n_tup_upd', COALESCE((SELECT sum(n_tup_upd)::bigint FROM pg_stat_user_tables
      WHERE schemaname='keeninsight_tpcc'), 0),
  'tpcc_n_tup_del', COALESCE((SELECT sum(n_tup_del)::bigint FROM pg_stat_user_tables
      WHERE schemaname='keeninsight_tpcc'), 0),
  'tpcc_autovacuum_count', COALESCE((SELECT sum(autovacuum_count)::bigint FROM pg_stat_user_tables
      WHERE schemaname='keeninsight_tpcc'), 0),
  'tpcc_demo_n_dead_tup', COALESCE((SELECT n_dead_tup::bigint FROM pg_stat_user_tables
      WHERE schemaname='keeninsight_tpcc' AND relname='tpcc_demo_autovacuum'), 0),
  'tpcc_demo_n_tup_upd', COALESCE((SELECT n_tup_upd::bigint FROM pg_stat_user_tables
      WHERE schemaname='keeninsight_tpcc' AND relname='tpcc_demo_autovacuum'), 0),
  'tpcc_demo_autovacuum_count', COALESCE((SELECT autovacuum_count::bigint FROM pg_stat_user_tables
      WHERE schemaname='keeninsight_tpcc' AND relname='tpcc_demo_autovacuum'), 0),
  'autovacuum_active', (SELECT count(*)::int FROM pg_stat_activity
      WHERE datname=current_database() AND backend_type='autovacuum'),
  'lock_waits', (SELECT count(*)::int FROM pg_stat_activity
      WHERE datname=current_database() AND wait_event_type='Lock')
)::text
FROM pg_stat_database d
WHERE d.datname=current_database();
""".format(prefix=sql_literal(prefix + "%"))
    raw = psql_text(args, sql, "perf-anomaly-demo-tpcc-meta-{}".format(phase))
    data = json.loads(raw)
    data["phase"] = phase
    data["observed_at"] = utc_now()
    return data


def settings_snapshot(args: argparse.Namespace, names: List[str], label: str) -> Dict[str, str]:
    quoted = ", ".join(sql_literal(name) for name in names)
    sql = "SELECT name, setting FROM pg_settings WHERE name IN ({}) ORDER BY name;".format(quoted)
    raw = psql_text(args, sql, "perf-anomaly-demo-tpcc-meta-settings-{}".format(label))
    result: Dict[str, str] = {}
    for line in raw.splitlines():
        if "|" in line:
            name, value = line.split("|", 1)
            result[name] = value
    return result


def stage_sql(source: Path, stage_dir: Path) -> Path:
    destination = stage_dir / source.name
    shutil.copyfile(str(source), str(destination))
    destination.chmod(0o644)
    return destination


ALLOWED_SESSION_PARAMETERS = {
    "work_mem",
    "maintenance_work_mem",
    "temp_buffers",
    "max_parallel_workers_per_gather",
    "parallel_setup_cost",
    "parallel_tuple_cost",
    "random_page_cost",
    "cpu_tuple_cost",
    "jit",
    "enable_nestloop",
    "min_parallel_table_scan_size",
    "max_parallel_workers",
}


def parse_tuned_override(raw: str) -> Optional[Dict[str, str]]:
    if not raw:
        return None
    if "=" not in raw:
        raise ValueError("--tuned-override must be parameter=value")
    parameter, value = raw.split("=", 1)
    parameter = parameter.strip()
    value = value.strip().strip("'").strip('"')
    if parameter not in ALLOWED_SESSION_PARAMETERS:
        raise ValueError("unsupported session parameter: {}".format(parameter))
    if not value or not re.fullmatch(r"[A-Za-z0-9_.+-]+", value):
        raise ValueError("unsafe or empty session parameter value")
    return {"parameter": parameter, "value": value}


def stage_tuned_sql(
    case: Dict[str, Any], stage_dir: Path, tuned_override: Optional[Dict[str, str]]
) -> Path:
    if tuned_override is None:
        return stage_sql(CASE_ROOT / case["tuned_sql"], stage_dir)
    destination = stage_dir / "gpt_tuned_{}".format(case["sql"])
    source_text = (CASE_ROOT / case["sql"]).read_text(encoding="utf-8")
    statement = "SET {} = '{}';\n".format(
        tuned_override["parameter"], tuned_override["value"]
    )
    destination.write_text(statement + source_text, encoding="utf-8")
    destination.chmod(0o644)
    return destination


def start_pgbench(
    args: argparse.Namespace,
    sql_path: Path,
    phase_dir: Path,
    app_name: str,
    clients: int,
    duration: int,
    role: str = "control",
    connection_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    log_path = phase_dir / (app_name.rsplit("-", 1)[-1] + ".log")
    log_handle = log_path.open("w", encoding="utf-8")
    command = [
        "runuser",
        "-u",
        args.run_as,
        "--",
        "env",
        "PGAPPNAME={}".format(app_name),
        *(["PGOPTIONS={}".format(options)] if (options := connection_options(connection_config)) else []),
        "pgbench",
        "-h",
        args.host,
        "-p",
        str(getattr(args, "port", 5432)),
        "-U",
        args.db_user,
        "-n",
        "-M",
        "simple",
        "-c",
        str(clients),
        "-T",
        str(max(1, int(duration))),
        "-f",
        str(sql_path),
        "-P",
        "5",
        args.db,
    ]
    process = subprocess.Popen(
        command,
        cwd="/",
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return {
        "process": process,
        "log_handle": log_handle,
        "log_path": log_path,
        "app_name": app_name,
        "kind": "pgbench",
        "role": role,
        "started_at": time.monotonic(),
        "command": command,
    }


def start_psql_batch(
    args: argparse.Namespace,
    sql_path: Path,
    phase_dir: Path,
    prefix: str,
    workers: int,
    connection_config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    processes: List[Dict[str, Any]] = []
    for index in range(workers):
        app_name = "{}external{}".format(prefix, index)
        log_path = phase_dir / "external_{}.log".format(index)
        log_handle = log_path.open("w", encoding="utf-8")
        command = psql_command(args, app_name, ["-f", str(sql_path)], connection_config)
        process = subprocess.Popen(
            command,
            cwd="/",
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(
            {
                "process": process,
                "log_handle": log_handle,
                "log_path": log_path,
                "app_name": app_name,
                "kind": "psql",
                "role": "external",
                "started_at": time.monotonic(),
                "command": command,
            }
        )
    return processes


def terminate_process(item: Dict[str, Any]) -> None:
    process = item.get("process")
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def wait_processes(processes: List[Dict[str, Any]], terminate: bool = False) -> None:
    if terminate:
        for item in processes:
            terminate_process(item)
    for item in processes:
        process = item.get("process")
        if process is not None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            item["returncode"] = process.returncode
            item["finished_at"] = time.monotonic()
        handle = item.pop("log_handle", None)
        if handle is not None:
            handle.close()


def read_alert(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    return strict_demo.read_prometheus_alert(process_args(args))


def wait_alert_clear(args: argparse.Namespace, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_alert(args) is None:
            return True
        time.sleep(1.0)
    return read_alert(args) is None


def sample_phase(
    args: argparse.Namespace,
    prefix: str,
    phase_dir: Path,
    phase: str,
    duration: int,
    perf_phase: bool,
) -> Dict[str, Any]:
    sample_path = phase_dir / "{}_samples.jsonl".format(phase)
    samples: List[Dict[str, Any]] = []
    trigger: Optional[Dict[str, Any]] = None
    perf_info: Optional[Dict[str, Any]] = None
    started = time.monotonic()
    deadline = started + duration

    while time.monotonic() < deadline:
        try:
            pids = app_pids(args, prefix)
            snapshot = metric_snapshot(args, prefix, phase)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
            pids = []
            snapshot = {"phase": phase, "observed_at": utc_now(), "error": str(exc)}
        snapshot["pids"] = pids
        snapshot["elapsed_seconds"] = round(time.monotonic() - started, 3)
        samples.append(snapshot)
        with sample_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

        if perf_phase and trigger is None:
            alert = read_alert(args)
            if alert is not None:
                trigger = {
                    "triggered": True,
                    "at": utc_now(),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "source": "prometheus",
                    "alert_name": alert.get("labels", {}).get("alertname"),
                    "alert": alert,
                    "pids": pids,
                }
                if pids:
                    perf_info = strict_demo.start_perf(
                        process_args(args),
                        phase_dir,
                        "anomaly",
                        pids,
                        duration + 5,
                    )
                    trigger["perf_status_at_start"] = perf_info.get("status")
                else:
                    trigger["perf_status_at_start"] = "not_started_no_pids"

        remaining = max(0.0, min(1.0, deadline - time.monotonic()))
        if remaining:
            time.sleep(remaining)

    if perf_info is not None:
        strict_demo.stop_perf(perf_info)

    return {
        "phase": phase,
        "samples_path": str(sample_path.relative_to(ROOT)),
        "sample_count": len(samples),
        "first": samples[0] if samples else None,
        "last": samples[-1] if samples else None,
        "trigger": trigger,
        "perf": perf_info,
    }


def baseline_run(
    args: argparse.Namespace,
    run_dir: Path,
    stage_dir: Path,
    normal_sql_name: str = "normal.sql",
) -> Dict[str, Any]:
    baseline_dir = run_dir / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    normal_sql = stage_sql(CASE_ROOT / normal_sql_name, stage_dir)
    prefix = "perf-anomaly-demo-tpcc-baseline-"
    workers = [
        start_pgbench(
            args,
            normal_sql,
            baseline_dir,
            prefix + "control",
            args.normal_clients,
            args.baseline_duration,
            role="control",
        )
    ]
    time.sleep(0.5)
    pids = wait_for_pids(args, prefix, 1)
    perf_info: Optional[Dict[str, Any]] = None
    if pids:
        perf_info = strict_demo.start_perf(
            process_args(args), baseline_dir, "baseline", pids, args.baseline_duration + 5
        )
    samples = sample_phase(args, prefix, baseline_dir, "baseline", args.baseline_duration, False)
    wait_processes(workers)
    if perf_info is not None:
        strict_demo.stop_perf(perf_info)
    result: Dict[str, Any] = {
        "workers": serializable_processes(workers),
        "samples": samples,
        "perf": perf_info,
    }
    result["metrics"] = parse_pgbench_log(Path(workers[0]["log_path"]))
    if perf_info is not None:
        result["perf_postprocess"] = strict_demo.postprocess_perf(
            process_args(args), baseline_dir, perf_info
        )
        if result["perf_postprocess"].get("status") == "sysinsight_counts_generated":
            counts_path = ROOT / result["perf_postprocess"]["counts_path"]
            profile_path = baseline_dir / "normal_profile_postgresql_demo.csv"
            count = strict_demo.write_normal_profile(counts_path, profile_path)
            result["normal_profile"] = {
                "path": str(profile_path.relative_to(ROOT)),
                "function_count": count,
            }
    return result


def serializable_processes(processes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for item in processes:
        result.append(
            {
                key: value
                for key, value in item.items()
                if key not in {"process", "log_handle"}
            }
        )
    return result


def delta(first: Optional[Dict[str, Any]], last: Optional[Dict[str, Any]], key: str) -> Optional[float]:
    if not first or not last:
        return None
    a = first.get(key)
    b = last.get(key)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(b) - float(a)
    return None


def parse_pgbench_log(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    tps_values = re.findall(r"tps = ([0-9.]+)", text)
    latency_values = re.findall(r"latency average = ([0-9.]+) ms", text)
    result: Dict[str, Any] = {}
    if tps_values:
        result["tps"] = float(tps_values[-1])
    if latency_values:
        result["latency_ms"] = float(latency_values[-1])
    return result


def parse_psql_log(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    values = [float(item) for item in re.findall(r"Time: ([0-9.]+) ms", text)]
    if not values:
        return {}
    return {
        "timing_count": len(values),
        "timing_total_ms": sum(values),
        "timing_max_ms": max(values),
    }


def phase_external_metrics(processes: List[Dict[str, Any]]) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    for item in processes:
        if item.get("kind") == "pgbench":
            metrics = parse_pgbench_log(Path(item["log_path"]))
        elif item.get("kind") == "psql":
            metrics = parse_psql_log(Path(item["log_path"]))
        else:
            metrics = {}
        elapsed = None
        if isinstance(item.get("started_at"), (int, float)) and isinstance(item.get("finished_at"), (int, float)):
            elapsed = round(float(item["finished_at"]) - float(item["started_at"]), 3)
        results.append(
            {
                "app_name": item.get("app_name"),
                "kind": item.get("kind"),
                "returncode": item.get("returncode"),
                "log_path": str(item.get("log_path")),
                "elapsed_seconds": elapsed,
                "metrics": metrics,
            }
        )
    tps = [item["metrics"]["tps"] for item in results if "tps" in item["metrics"]]
    latency = [item["metrics"]["latency_ms"] for item in results if "latency_ms" in item["metrics"]]
    aggregate: Dict[str, Any] = {"workers": results}
    if tps:
        aggregate["tps_sum"] = sum(tps)
    if latency:
        aggregate["latency_avg_ms"] = sum(latency) / len(latency)
    timings = [
        item["metrics"]["timing_total_ms"]
        for item in results
        if "timing_total_ms" in item["metrics"]
    ]
    if timings:
        aggregate["timing_total_ms"] = sum(timings)
    return aggregate


def external_only_metrics(processes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return metrics for the injected TPCC workload, excluding the control load."""
    return phase_external_metrics(
        [item for item in processes if item.get("role") == "external"]
    )


def explain(
    args: argparse.Namespace,
    sql: str,
    sets: List[str],
    label: str,
    connection_config: Optional[Dict[str, Any]] = None,
) -> str:
    if not sql:
        return ""
    prefix = "SET statement_timeout='30s';"
    settings = "".join(" SET {};".format(item) for item in sets)
    return psql_text(
        args,
        prefix + settings + " EXPLAIN (COSTS ON) " + sql + ";",
        "perf-anomaly-demo-tpcc-meta-explain-{}".format(label),
        timeout=40.0,
        connection_config=connection_config,
    )


def source_detection(case_dir: Path, normal_profile: Path) -> Dict[str, Any]:
    if not normal_profile.exists():
        return {"status": "normal_profile_missing"}
    copied_profile = case_dir / "normal_profile_postgresql_demo.csv"
    shutil.copyfile(str(normal_profile), str(copied_profile))
    command = [
        sys.executable,
        str(ROOT / "sysinsight_detection.py"),
        "--run-dir",
        str(case_dir),
    ]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    (case_dir / "sysinsight_detection.log").write_text(completed.stdout, encoding="utf-8")
    result_path = case_dir / "sysinsight_source_detection_result.json"
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["runner_returncode"] = completed.returncode
            return result
        except json.JSONDecodeError:
            pass
    return {"status": "source_detection_failed", "returncode": completed.returncode}


def run_case(
    args: argparse.Namespace,
    case: Dict[str, Any],
    run_dir: Path,
    stage_dir: Path,
    normal_profile: Path,
    baseline_settings: Dict[str, str],
    baseline_metrics: Optional[Dict[str, Any]] = None,
    tuned_override: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    case_dir = run_dir / case["id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    prefix = "perf-anomaly-demo-tpcc-{}-".format(case["id"])
    normal_sql = stage_sql(CASE_ROOT / case.get("normal_sql", "normal.sql"), stage_dir)
    anomaly_sql = stage_sql(CASE_ROOT / case["sql"], stage_dir)
    tuned_sql = stage_tuned_sql(case, stage_dir, tuned_override)

    wait_alert_clear(args)
    before_settings = settings_snapshot(args, list(case["repair"].keys()), case["id"] + "-before")
    anomaly_dir = case_dir / "anomaly"
    anomaly_dir.mkdir(parents=True, exist_ok=True)
    control = start_pgbench(
        args,
        normal_sql,
        anomaly_dir,
        prefix + "control",
        args.normal_clients,
        args.case_duration,
        role="control",
    )
    processes: List[Dict[str, Any]] = [control]
    time.sleep(0.5)
    if case["mode"] == "pgbench":
        external = start_pgbench(
            args,
            anomaly_sql,
            anomaly_dir,
            prefix + "external",
            case["clients"],
            args.case_duration,
            role="external",
        )
        processes.append(external)
    else:
        processes.extend(
            start_psql_batch(args, anomaly_sql, anomaly_dir, prefix, case["clients"])
        )

    anomaly_samples = sample_phase(
        args, prefix, anomaly_dir, "anomaly", args.case_duration, True
    )
    wait_processes(processes)
    anomaly_perf = anomaly_samples.get("perf")
    anomaly_postprocess: Dict[str, Any] = {}
    if anomaly_perf is not None:
        anomaly_postprocess = strict_demo.postprocess_perf(
            process_args(args), anomaly_dir, anomaly_perf
        )
    # The original wrapper searches for anomaly_counts_*.txt directly in its
    # run directory; the case keeps those files under anomaly/.
    detection = source_detection(anomaly_dir, normal_profile)

    tuned_dir = case_dir / "tuned"
    tuned_dir.mkdir(parents=True, exist_ok=True)
    tuned_prefix = prefix + "tuned-"
    tuned_control = start_pgbench(
        args,
        normal_sql,
        tuned_dir,
        tuned_prefix + "control",
        args.normal_clients,
        args.tuned_duration,
        role="control",
    )
    tuned_processes: List[Dict[str, Any]] = [tuned_control]
    time.sleep(0.5)
    if case["mode"] == "pgbench":
        tuned_processes.append(
            start_pgbench(
                args,
                tuned_sql,
                tuned_dir,
                tuned_prefix + "external",
                case["clients"],
                args.tuned_duration,
                role="external",
            )
        )
    else:
        tuned_processes.extend(
            start_psql_batch(args, tuned_sql, tuned_dir, tuned_prefix, case["clients"])
        )
    tuned_samples = sample_phase(
        args, tuned_prefix, tuned_dir, "tuned", args.tuned_duration, False
    )
    wait_processes(tuned_processes)
    wait_alert_clear(args)

    tuned_settings = settings_snapshot(args, list(case["repair"].keys()), case["id"] + "-after")
    anomaly_all_metrics = phase_external_metrics(processes)
    tuned_all_metrics = phase_external_metrics(tuned_processes)
    anomaly_metrics = external_only_metrics(processes)
    tuned_metrics = external_only_metrics(tuned_processes)
    anomaly_first = anomaly_samples.get("first")
    anomaly_last = anomaly_samples.get("last")
    tuned_first = tuned_samples.get("first")
    tuned_last = tuned_samples.get("last")
    metric_deltas = {
        "anomaly_temp_bytes": delta(anomaly_first, anomaly_last, "temp_bytes"),
        "tuned_temp_bytes": delta(tuned_first, tuned_last, "temp_bytes"),
        "anomaly_temp_files": delta(anomaly_first, anomaly_last, "temp_files"),
        "tuned_temp_files": delta(tuned_first, tuned_last, "temp_files"),
        "anomaly_checkpoints_req": delta(anomaly_first, anomaly_last, "checkpoints_req"),
        "tuned_checkpoints_req": delta(tuned_first, tuned_last, "checkpoints_req"),
    }

    plans: Dict[str, str] = {}
    if case.get("plan_sql"):
        plans["base"] = explain(args, case["plan_sql"], [], case["id"] + "-base")
        tuned_plan_sets = case.get("plan_sets", [])
        if tuned_override is not None:
            tuned_plan_sets = [
                "{}='{}'".format(
                    tuned_override["parameter"], tuned_override["value"]
                )
            ]
        plans["tuned"] = explain(
            args, case["plan_sql"], tuned_plan_sets, case["id"] + "-tuned"
        )
        (case_dir / "plans.json").write_text(
            json.dumps(plans, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    global_after = settings_snapshot(args, list(baseline_settings.keys()), case["id"] + "-global-after")
    global_unchanged = all(global_after.get(k) == v for k, v in baseline_settings.items())
    anomaly_tps = anomaly_metrics.get("tps_sum")
    tuned_tps = tuned_metrics.get("tps_sum")
    parallel_plan_changed = bool(
        plans.get("base") != plans.get("tuned")
        and "Gather" in plans.get("tuned", "")
    )
    improvement: Dict[str, Any] = {
        "tps_ratio": (tuned_tps / anomaly_tps) if anomaly_tps and tuned_tps else None,
        "latency_ratio": (
            tuned_metrics.get("latency_avg_ms") / anomaly_metrics.get("latency_avg_ms")
            if tuned_metrics.get("latency_avg_ms") and anomaly_metrics.get("latency_avg_ms")
            else None
        ),
        "temp_bytes_reduced": (
            metric_deltas["anomaly_temp_bytes"] is not None
            and metric_deltas["tuned_temp_bytes"] is not None
            and metric_deltas["tuned_temp_bytes"] < metric_deltas["anomaly_temp_bytes"]
        ),
        "parallel_plan_changed": parallel_plan_changed,
        "global_settings_unchanged": global_unchanged,
    }
    if anomaly_metrics.get("timing_total_ms") and tuned_metrics.get("timing_total_ms"):
        improvement["timing_ratio"] = (
            tuned_metrics["timing_total_ms"] / anomaly_metrics["timing_total_ms"]
        )
    tps_improved = bool(improvement["tps_ratio"] and improvement["tps_ratio"] > 1.05)
    latency_improved = bool(improvement["latency_ratio"] and improvement["latency_ratio"] < 0.95)
    repair_effective = bool(
        tps_improved
        or latency_improved
        or improvement["temp_bytes_reduced"]
    )
    if case["mode"] == "psql":
        if improvement.get("timing_ratio") is not None:
            repair_effective = improvement["timing_ratio"] < 0.95

    result = {
        "id": case["id"],
        "title": case["title"],
        "external_event": case["event"],
        "repair_candidate": case["repair"],
        "repair_candidate_source": "case_definition_preset_control_experiment_only",
        "gpt_generated_configuration": False,
        "tuned_override": tuned_override,
        "mode": case["mode"],
        "baseline_metrics": baseline_metrics or {},
        "before_settings": before_settings,
        "anomaly": {
            "samples": anomaly_samples,
            "workers": serializable_processes(processes),
            "metrics": anomaly_metrics,
            "all_process_metrics": anomaly_all_metrics,
            "perf_postprocess": anomaly_postprocess,
        },
        "sysinsight_source_detection": detection,
        "tuned": {
            "samples": tuned_samples,
            "workers": serializable_processes(tuned_processes),
            "metrics": tuned_metrics,
            "all_process_metrics": tuned_all_metrics,
            "settings_after": tuned_settings,
        },
        "metric_deltas": metric_deltas,
        "plans": plans,
        "improvement": improvement,
        "repair_effective_in_controlled_session": repair_effective,
        "safety": {
            "persistent_settings_changed": not global_unchanged,
            "uses_session_set_in_tuned_workers": True,
            "database_objects_persisted": False if case["mode"] == "psql" else None,
        },
    }
    (case_dir / "case_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return result


def main() -> int:
    args = parse_args()
    if args.normal_clients < 1 or args.case_duration < 5 or args.tuned_duration < 5:
        raise SystemExit("clients must be positive and durations must be at least 5 seconds")
    selected = [item.strip() for item in args.only.split(",") if item.strip()]
    cases = [case for case in CASE_DEFINITIONS if not selected or case["id"] in selected]
    if not cases:
        raise SystemExit("no matching cases")
    tuned_override = parse_tuned_override(args.tuned_override)

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RESULT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    stage_dir = Path(tempfile.mkdtemp(prefix="tpcc-external-sql-"))
    # pgbench/psql run as the postgres OS user and must traverse this
    # short-lived staging directory.  The SQL files themselves remain 0644.
    stage_dir.chmod(0o755)
    tuned_names: List[str] = []
    for case in cases:
        tuned_names.extend(case["repair"].keys())
    baseline_settings = settings_snapshot(args, sorted(set(tuned_names)), "initial")
    summary: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": utc_now(),
        "database": args.db,
        "schema": "keeninsight_tpcc",
        "cases_requested": [case["id"] for case in cases],
        "baseline_settings": baseline_settings,
        "tuned_override": tuned_override,
        "scope": {
            "external_phase_changes_parameters": False,
            "tuned_phase_uses_persistent_alter_system": False,
            "repair_values_are_gpt_generated": False,
            "repair_values_source": "CASE_DEFINITIONS.repair; do not count as SysInsight API evidence",
            "sysinsight_function_range": "original DBEnv.get_perf_function_range",
            "sysinsight_exception_compare": "original analyzeException.compare_file_sample_rate",
            "sysinsight_function_matching": "original matchFunctions.find_top_and_matched_functions",
        },
    }
    (run_dir / "preflight.json").write_text(
        json.dumps(
            {
                "started_at": utc_now(),
                "db": args.db,
                "db_user": args.db_user,
                "host": args.host,
                "settings": baseline_settings,
                "psql": shutil.which("psql"),
                "pgbench": shutil.which("pgbench"),
                "perf": shutil.which("perf"),
                "perf_event_paranoid": strict_demo.read_int_file("/proc/sys/kernel/perf_event_paranoid"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    try:
        print("[1/{}] 采集 TPCC 正常负载基线...".format(len(cases) + 2), flush=True)
        baseline = baseline_run(args, run_dir, stage_dir, normal_sql_name=args.normal_sql)
        summary["baseline"] = baseline
        profile_rel = baseline.get("normal_profile", {}).get("path")
        if not profile_rel:
            raise RuntimeError("baseline perf did not produce a normal profile")
        normal_profile = ROOT / profile_rel

        for index, case in enumerate(cases, start=2):
            print(
                "[{}/{}] 验证 {}：{}".format(index, len(cases) + 2, case["id"], case["title"]),
                flush=True,
            )
            result = run_case(
                args,
                case,
                run_dir,
                stage_dir,
                normal_profile,
                baseline_settings,
                baseline_metrics=baseline.get("metrics", {}),
                tuned_override=tuned_override,
            )
            summary.setdefault("cases", []).append(
                {
                    "id": result["id"],
                    "repair_effective_in_controlled_session": result["repair_effective_in_controlled_session"],
                    "prometheus_triggered": bool(result["anomaly"]["samples"].get("trigger")),
                    "perf_status": result["anomaly"]["perf_postprocess"].get("status"),
                    "source_anomaly_functions": result["sysinsight_source_detection"].get(
                        "source_compare", {}
                    ).get("key_function_count"),
                    "source_matched_knobs": len(
                        result["sysinsight_source_detection"].get("source_match", {}).get(
                            "matched_knob", []
                        )
                    ),
                }
            )

        summary["completed_at"] = utc_now()
        summary["effective_case_count"] = sum(
            1
            for item in summary.get("cases", [])
            if item.get("repair_effective_in_controlled_session")
        )
        (run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        print("[{}] 测试完成，结果目录：{}".format(len(cases) + 2, run_dir), flush=True)
        return 0
    except KeyboardInterrupt:
        (run_dir / "error.txt").write_text("interrupted\n", encoding="utf-8")
        print("收到中断，已停止本轮测试负载。", file=sys.stderr)
        return 130
    except Exception as exc:
        (run_dir / "error.txt").write_text(
            "{}: {}\n".format(type(exc).__name__, exc), encoding="utf-8"
        )
        print("测试失败：{}".format(exc), file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(str(stage_dir), ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
