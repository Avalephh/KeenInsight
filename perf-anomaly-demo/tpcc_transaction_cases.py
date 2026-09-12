#!/usr/bin/env python3
"""Real TPCC transaction workload definitions for the validation runner.

The workload SQL is intentionally separate from the older AP/report cases.
The normal profile is a rollback-protected mix of the five TPC-C transaction
families.  The autovacuum pressure uses only a disposable table in the same
database/schema and is removed after the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import tpcc_external_cases as runner


CASE_DEFINITIONS = [
    {
        "id": "tp_wal_checkpoint",
        "title": "TPCC New Order/Payment 写入突增造成 WAL 与 checkpoint 压力",
        "event": "增加真实 TPCC 写事务客户端，外部压力不修改 PostgreSQL 参数",
        "sql": "tp_normal.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_normal.sql",
        "mode": "pgbench",
        "clients": 10,
        "repair": {},
        "tpcc_transactions": ["New Order", "Payment", "Order Status", "Delivery", "Stock Level"],
        "pressure_evidence": ["checkpoints_req", "checkpoint_write_time", "checkpoint_sync_time", "buffers_backend"],
    },
    {
        "id": "tp_payment_commit",
        "title": "TPCC Payment 并发提交压力",
        "event": "增加真实 Payment 事务客户端，观察高并发提交和 WAL flush 竞争",
        "sql": "tp_payment_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_payment_burst.sql",
        "mode": "pgbench",
        "clients": 12,
        "repair": {},
        "tpcc_transactions": ["Payment"],
        "pressure_evidence": ["xact_commit", "xact_rollback", "checkpoint_write_time", "buffers_backend_fsync"],
    },
    {
        "id": "tp_autovacuum_churn",
        "title": "TPCC 更新 churn 触发 autovacuum 竞争",
        "event": "增加外部已提交更新事务，使同库表出现 dead tuple 和 autovacuum 工作",
        "sql": "tp_autovacuum_churn.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_autovacuum_churn.sql",
        "mode": "pgbench",
        "clients": 8,
        "repair": {},
        "tpcc_transactions": ["Payment-like update churn"],
        "pressure_evidence": ["n_dead_tup", "n_tup_upd", "autovacuum_count", "autovacuum_active"],
    },
    {
        "id": "tp_new_order_burst",
        "title": "TPCC New Order 外部突发并发",
        "event": "外部站点突然增加 New Order 事务客户端，造成订单、库存和 WAL 写入竞争",
        "sql": "tp_new_order_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_new_order_burst.sql",
        "mode": "pgbench",
        "clients": 4,
        "repair": {},
        "tpcc_transactions": ["New Order"],
        "pressure_evidence": ["xact_commit", "buffers_backend", "checkpoints_req", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_delivery_burst",
        "title": "TPCC Delivery 外部突发并发",
        "event": "外部履约站点突然增加 Delivery 事务客户端，造成待配送订单索引和订单行更新竞争",
        "sql": "tp_delivery_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_delivery_burst.sql",
        "mode": "pgbench",
        "clients": 4,
        "repair": {},
        "tpcc_transactions": ["Delivery"],
        "pressure_evidence": ["lock_waits", "buffers_backend", "tpcc_n_tup_upd", "tpcc_n_tup_del"],
    },
    {
        "id": "tp_order_status_burst",
        "title": "TPCC Order Status 外部读并发",
        "event": "外部客服站点突然增加 Order Status 事务客户端，造成订单和订单行索引读取竞争",
        "sql": "tp_order_status_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_order_status_burst.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {},
        "tpcc_transactions": ["Order Status"],
        "pressure_evidence": ["blks_read", "blks_hit", "buffers_backend", "active_total"],
    },
    {
        "id": "tp_stock_level_burst",
        "title": "TPCC Stock Level 外部读并发",
        "event": "外部库存站点突然增加 Stock Level 事务客户端，造成最近订单行和库存范围检查竞争",
        "sql": "tp_stock_level_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_stock_level_burst.sql",
        "mode": "pgbench",
        "clients": 5,
        "repair": {},
        "tpcc_transactions": ["Stock Level"],
        "pressure_evidence": ["blks_read", "blks_hit", "buffers_backend", "active_total"],
    },
    {
        "id": "tp_remote_payment",
        "title": "TPCC Remote Payment 外部并发",
        "event": "外部跨仓支付流量突增，Payment 同时访问本地仓库和远程客户仓库",
        "sql": "tp_remote_payment.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_remote_payment.sql",
        "mode": "pgbench",
        "clients": 4,
        "repair": {},
        "tpcc_transactions": ["Payment (remote warehouse branch)"],
        "pressure_evidence": ["xact_commit", "buffers_backend", "checkpoint_write_time", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_remote_new_order",
        "title": "TPCC Remote New Order 外部并发",
        "event": "外部跨仓订单流量突增，New Order 含远程供货库存访问",
        "sql": "tp_remote_new_order.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_remote_new_order.sql",
        "mode": "pgbench",
        "clients": 4,
        "repair": {},
        "tpcc_transactions": ["New Order (remote supply branch)"],
        "pressure_evidence": ["xact_commit", "buffers_backend", "checkpoints_req", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_new_order_hot",
        "title": "TPCC 热点仓库 New Order 外部并发",
        "event": "外部大客户集中访问同一仓库和地区，形成真实 New Order 热点",
        "sql": "tp_new_order_hot.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_new_order_hot.sql",
        "mode": "pgbench",
        "clients": 3,
        "repair": {},
        "tpcc_transactions": ["New Order (hot warehouse/district)"],
        "pressure_evidence": ["lock_waits", "xact_commit", "buffers_backend", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_payment_hot",
        "title": "TPCC 热点仓库 Payment 外部并发",
        "event": "外部大客户集中访问同一仓库和地区，形成真实 Payment 热点",
        "sql": "tp_payment_hot.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_payment_hot.sql",
        "mode": "pgbench",
        "clients": 3,
        "repair": {},
        "tpcc_transactions": ["Payment (hot warehouse/district)"],
        "pressure_evidence": ["lock_waits", "xact_commit", "buffers_backend_fsync", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_order_status_hot",
        "title": "TPCC 热点仓库 Order Status 外部并发",
        "event": "外部客服/订单查询集中访问同一仓库和地区，形成热点 Order Status 事务流量",
        "sql": "tp_order_status_hot.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_order_status_hot.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {},
        "tpcc_transactions": ["Order Status (hot warehouse/district)"],
        "pressure_evidence": ["blks_read", "blks_hit", "buffers_backend", "active_total"],
    },
    {
        "id": "tp_stock_level_hot",
        "title": "TPCC 热点仓库 Stock Level 外部并发",
        "event": "外部库存站点集中访问同一仓库和地区，形成热点 Stock Level 事务流量",
        "sql": "tp_stock_level_hot.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_stock_level_hot.sql",
        "mode": "pgbench",
        "clients": 5,
        "repair": {},
        "tpcc_transactions": ["Stock Level (hot warehouse/district)"],
        "pressure_evidence": ["blks_read", "blks_hit", "buffers_backend", "active_total"],
    },
    {
        "id": "tp_read_mix_burst",
        "title": "TPCC Order Status/Stock Level 混合读突发",
        "event": "外部运营站点同时增加 Order Status 与 Stock Level 两类 TP 事务客户端",
        "sql": "tp_read_mix_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_read_mix_burst.sql",
        "mode": "pgbench",
        "clients": 6,
        "repair": {},
        "tpcc_transactions": ["Order Status", "Stock Level"],
        "pressure_evidence": ["blks_read", "blks_hit", "buffers_backend", "active_total"],
    },
    {
        "id": "tp_full_mix_surge",
        "title": "TPCC 全交易混合流量突增",
        "event": "外部业务站点突然增加完整 TPCC 事务混合流量，模拟正常 OLTP 客户端数量增长",
        "sql": "tp_normal.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_normal.sql",
        "mode": "pgbench",
        "clients": 3,
        "repair": {},
        "tpcc_transactions": ["New Order", "Payment", "Order Status", "Delivery", "Stock Level"],
        "pressure_evidence": ["xact_commit", "blks_read", "buffers_backend", "checkpoints_req"],
    },
    {
        "id": "tp_new_order_moderate",
        "title": "TPCC New Order 中等突发",
        "event": "外部订单入口增加中等规模 New Order 客户端，形成可恢复的写入压力",
        "sql": "tp_new_order_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_new_order_burst.sql",
        "mode": "pgbench",
        "clients": 3,
        "repair": {},
        "tpcc_transactions": ["New Order"],
        "pressure_evidence": ["xact_commit", "buffers_backend", "tpcc_n_tup_upd", "checkpoints_req"],
    },
    {
        "id": "tp_payment_moderate",
        "title": "TPCC Payment 中等突发",
        "event": "外部支付入口增加中等规模 Payment 客户端，形成可恢复的提交压力",
        "sql": "tp_payment_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_payment_burst.sql",
        "mode": "pgbench",
        "clients": 3,
        "repair": {},
        "tpcc_transactions": ["Payment"],
        "pressure_evidence": ["xact_commit", "buffers_backend_fsync", "checkpoint_write_time", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_order_status_io_burst",
        "title": "TPCC Order Status 随机读 I/O 突发",
        "event": "外部客服站点增加随机 Order Status 请求，形成随机索引读取压力",
        "sql": "tp_order_status_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_order_status_burst.sql",
        "mode": "pgbench",
        "clients": 8,
        "repair": {},
        "tpcc_transactions": ["Order Status"],
        "pressure_evidence": ["blks_read", "blks_hit", "buffers_backend", "active_total"],
    },
    {
        "id": "tp_new_order_surge",
        "title": "TPCC New Order 高强度写入突发",
        "event": "外部订单入口在短时间内增加大量 New Order 客户端，形成订单与库存写入竞争",
        "sql": "tp_new_order_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_new_order_burst.sql",
        "mode": "pgbench",
        "clients": 8,
        "repair": {},
        "tpcc_transactions": ["New Order"],
        "pressure_evidence": ["xact_commit", "buffers_backend", "checkpoints_req", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_payment_surge",
        "title": "TPCC Payment 高强度提交突发",
        "event": "外部支付入口在短时间内增加大量 Payment 客户端，形成提交与 WAL 写入竞争",
        "sql": "tp_payment_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_payment_burst.sql",
        "mode": "pgbench",
        "clients": 8,
        "repair": {},
        "tpcc_transactions": ["Payment"],
        "pressure_evidence": ["xact_commit", "buffers_backend_fsync", "checkpoint_write_time", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_delivery_surge",
        "title": "TPCC Delivery 高强度履约突发",
        "event": "外部履约站点在短时间内增加大量 Delivery 客户端，形成待配送订单和订单行更新竞争",
        "sql": "tp_delivery_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_delivery_burst.sql",
        "mode": "pgbench",
        "clients": 8,
        "repair": {},
        "tpcc_transactions": ["Delivery"],
        "pressure_evidence": ["lock_waits", "buffers_backend", "tpcc_n_tup_upd", "tpcc_n_tup_del"],
    },
    {
        "id": "tp_remote_payment_surge",
        "title": "TPCC Remote Payment 高强度突发",
        "event": "外部跨仓支付入口突然增加大量远程 Payment 客户端，形成跨仓客户更新竞争",
        "sql": "tp_remote_payment.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_remote_payment.sql",
        "mode": "pgbench",
        "clients": 8,
        "repair": {},
        "tpcc_transactions": ["Payment (remote warehouse branch)"],
        "pressure_evidence": ["xact_commit", "buffers_backend", "checkpoint_write_time", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_remote_new_order_surge",
        "title": "TPCC Remote New Order 高强度突发",
        "event": "外部跨仓订单入口突然增加大量含远程供货的 New Order 客户端",
        "sql": "tp_remote_new_order.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_remote_new_order.sql",
        "mode": "pgbench",
        "clients": 8,
        "repair": {},
        "tpcc_transactions": ["New Order (remote supply branch)"],
        "pressure_evidence": ["xact_commit", "buffers_backend", "checkpoints_req", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_new_order_hot_surge",
        "title": "TPCC 热点仓库 New Order 高强度突发",
        "event": "外部大客户将大量 New Order 集中到同一仓库和地区，形成真实 TPCC 热点锁竞争",
        "sql": "tp_new_order_hot.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_new_order_hot.sql",
        "mode": "pgbench",
        "clients": 8,
        "repair": {},
        "tpcc_transactions": ["New Order (hot warehouse/district)"],
        "pressure_evidence": ["lock_waits", "xact_commit", "buffers_backend", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_payment_hot_surge",
        "title": "TPCC 热点仓库 Payment 高强度突发",
        "event": "外部大客户将大量 Payment 集中到同一仓库和地区，形成真实 TPCC 热点更新竞争",
        "sql": "tp_payment_hot.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_payment_hot.sql",
        "mode": "pgbench",
        "clients": 8,
        "repair": {},
        "tpcc_transactions": ["Payment (hot warehouse/district)"],
        "pressure_evidence": ["lock_waits", "xact_commit", "buffers_backend_fsync", "tpcc_n_tup_upd"],
    },
    {
        "id": "tp_read_mix_surge",
        "title": "TPCC Order Status/Stock Level 高强度读突发",
        "event": "外部运营站点同时增加大量 Order Status 与 Stock Level TP 事务客户端",
        "sql": "tp_read_mix_burst.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_read_mix_burst.sql",
        "mode": "pgbench",
        "clients": 10,
        "repair": {},
        "tpcc_transactions": ["Order Status", "Stock Level"],
        "pressure_evidence": ["blks_read", "blks_hit", "buffers_backend", "active_total"],
    },
    {
        "id": "tp_full_mix_surge_high",
        "title": "TPCC 五类交易高强度混合突发",
        "event": "外部业务站点同时增加完整五类 TPCC 事务客户端，模拟 OLTP 流量阶跃增长",
        "sql": "tp_normal.sql",
        "normal_sql": "tp_normal.sql",
        "tuned_sql": "tp_normal.sql",
        "mode": "pgbench",
        "clients": 8,
        "repair": {},
        "tpcc_transactions": ["New Order", "Payment", "Order Status", "Delivery", "Stock Level"],
        "pressure_evidence": ["xact_commit", "blks_read", "buffers_backend", "checkpoints_req"],
    },
]


def prepare_case(args: Any, case: Dict[str, Any], case_dir: Path) -> Dict[str, Any]:
    if case["id"] != "tp_autovacuum_churn":
        return {"status": "not_required"}
    setup_sql = """
CREATE TABLE IF NOT EXISTS keeninsight_tpcc.tpcc_demo_autovacuum (
    id integer PRIMARY KEY,
    value integer NOT NULL,
    touched_at timestamptz NOT NULL
);
TRUNCATE keeninsight_tpcc.tpcc_demo_autovacuum;
INSERT INTO keeninsight_tpcc.tpcc_demo_autovacuum (id, value, touched_at)
SELECT g, 0, clock_timestamp() FROM generate_series(1,100) AS g;
ANALYZE keeninsight_tpcc.tpcc_demo_autovacuum;
"""
    runner.psql_text(args, setup_sql, "perf-anomaly-demo-tpcc-prepare-autovacuum", timeout=60.0)
    return {
        "status": "prepared",
        "table": "keeninsight_tpcc.tpcc_demo_autovacuum",
        "rows": 100,
        "initial_state": "created_truncated_loaded_analyzed",
    }


def reset_case(args: Any, case: Dict[str, Any], case_dir: Path) -> Dict[str, Any]:
    if case["id"] != "tp_autovacuum_churn":
        return {"status": "not_required"}
    reset_sql = """
TRUNCATE keeninsight_tpcc.tpcc_demo_autovacuum;
INSERT INTO keeninsight_tpcc.tpcc_demo_autovacuum (id, value, touched_at)
SELECT g, 0, clock_timestamp() FROM generate_series(1,100) AS g;
ANALYZE keeninsight_tpcc.tpcc_demo_autovacuum;
"""
    runner.psql_text(args, reset_sql, "perf-anomaly-demo-tpcc-reset-autovacuum", timeout=60.0)
    return {"status": "reset"}


def cleanup_case(args: Any, case: Dict[str, Any], case_dir: Path) -> Dict[str, Any]:
    if case["id"] != "tp_autovacuum_churn":
        return {"status": "not_required"}
    cleanup_sql = "DROP TABLE IF EXISTS keeninsight_tpcc.tpcc_demo_autovacuum;"
    runner.psql_text(args, cleanup_sql, "perf-anomaly-demo-tpcc-cleanup-autovacuum", timeout=60.0)
    return {"status": "dropped"}


def describe_case(case: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "id": case["id"],
            "tpcc_transactions": case["tpcc_transactions"],
            "external_event": case["event"],
            "pressure_evidence": case["pressure_evidence"],
        },
        ensure_ascii=False,
    )
