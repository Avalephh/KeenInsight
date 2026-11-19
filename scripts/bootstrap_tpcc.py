#!/usr/bin/env python3
"""Load a bounded, repeatable TPC-C-shaped dataset into PostgreSQL.

This is a data generator for the interactive demo, not a TPC-C benchmark
implementation and not a source of benchmark claims.  The default warehouse
count is selected from local CPU, memory, and free-disk capacity.  On the
current 4-vCPU/30-GiB/24-GiB-free host that recommendation is five warehouses,
which keeps the demo responsive while creating roughly 2.6 million
business-shaped rows.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


DEFAULT_DSN = "postgresql://keeninsight:keeninsight@localhost:5432/keeninsight"
TPCC_SCHEMA = "keeninsight_tpcc"
DISTRICTS_PER_WAREHOUSE = 10
CUSTOMERS_PER_DISTRICT = 3000
ORDERS_PER_DISTRICT = 3000
ITEMS = 100_000
DELIVERED_ORDERS_PER_DISTRICT = 2100
TABLES = (
    "warehouse",
    "district",
    "item",
    "stock",
    "customer",
    "history",
    "orders",
    "new_order",
    "order_line",
    "load_info",
)


def recommended_warehouses() -> int:
    """Choose a conservative interactive-demo scale from local resources."""
    cpu = os.cpu_count() or 1
    memory_gib = 0.0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                memory_gib = int(line.split()[1]) / (1024 * 1024)
                break
    except (OSError, ValueError):
        pass
    free_gib = shutil.disk_usage("/").free / (1024**3)
    if cpu >= 8 and memory_gib >= 16 and free_gib >= 50:
        return 10
    if cpu >= 4 and memory_gib >= 8 and free_gib >= 10:
        return 5
    return 2


def table_exists(connection: psycopg.Connection[Any], table_name: str) -> bool:
    row = connection.execute(
        "SELECT to_regclass(%s) IS NOT NULL AS present",
        (f"{TPCC_SCHEMA}.{table_name}",),
    ).fetchone()
    return bool(row and row["present"] if isinstance(row, dict) else row and row[0])


def existing_load(connection: psycopg.Connection[Any]) -> dict[str, Any] | None:
    if not table_exists(connection, "load_info"):
        return None
    row = connection.execute(
        """
        SELECT warehouses, generator_version, loaded_at, row_counts, total_bytes
        FROM keeninsight_tpcc.load_info
        WHERE id = 1
        """
    ).fetchone()
    return dict(row) if row else None


def ddl() -> list[str]:
    return [
        f"""
        CREATE TABLE {TPCC_SCHEMA}.warehouse (
            w_id INTEGER PRIMARY KEY,
            w_name TEXT NOT NULL,
            w_street_1 TEXT NOT NULL,
            w_street_2 TEXT NOT NULL,
            w_city TEXT NOT NULL,
            w_state CHAR(2) NOT NULL,
            w_zip CHAR(9) NOT NULL,
            w_tax NUMERIC(4, 4) NOT NULL,
            w_ytd NUMERIC(12, 2) NOT NULL
        )
        """,
        f"""
        CREATE TABLE {TPCC_SCHEMA}.district (
            d_w_id INTEGER NOT NULL REFERENCES {TPCC_SCHEMA}.warehouse(w_id),
            d_id INTEGER NOT NULL,
            d_name TEXT NOT NULL,
            d_street_1 TEXT NOT NULL,
            d_street_2 TEXT NOT NULL,
            d_city TEXT NOT NULL,
            d_state CHAR(2) NOT NULL,
            d_zip CHAR(9) NOT NULL,
            d_tax NUMERIC(4, 4) NOT NULL,
            d_ytd NUMERIC(12, 2) NOT NULL,
            d_next_o_id INTEGER NOT NULL,
            PRIMARY KEY (d_w_id, d_id)
        )
        """,
        f"""
        CREATE TABLE {TPCC_SCHEMA}.item (
            i_id INTEGER PRIMARY KEY,
            i_im_id INTEGER NOT NULL,
            i_name TEXT NOT NULL,
            i_price NUMERIC(5, 2) NOT NULL,
            i_data TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE {TPCC_SCHEMA}.stock (
            s_w_id INTEGER NOT NULL REFERENCES {TPCC_SCHEMA}.warehouse(w_id),
            s_i_id INTEGER NOT NULL REFERENCES {TPCC_SCHEMA}.item(i_id),
            s_quantity INTEGER NOT NULL,
            s_ytd INTEGER NOT NULL,
            s_order_cnt INTEGER NOT NULL,
            s_remote_cnt INTEGER NOT NULL,
            s_data TEXT NOT NULL,
            s_dist_01 CHAR(24) NOT NULL,
            s_dist_02 CHAR(24) NOT NULL,
            s_dist_03 CHAR(24) NOT NULL,
            s_dist_04 CHAR(24) NOT NULL,
            s_dist_05 CHAR(24) NOT NULL,
            s_dist_06 CHAR(24) NOT NULL,
            s_dist_07 CHAR(24) NOT NULL,
            s_dist_08 CHAR(24) NOT NULL,
            s_dist_09 CHAR(24) NOT NULL,
            s_dist_10 CHAR(24) NOT NULL,
            PRIMARY KEY (s_w_id, s_i_id)
        )
        """,
        f"""
        CREATE TABLE {TPCC_SCHEMA}.customer (
            c_w_id INTEGER NOT NULL,
            c_d_id INTEGER NOT NULL,
            c_id INTEGER NOT NULL,
            c_first TEXT NOT NULL,
            c_middle CHAR(2) NOT NULL,
            c_last TEXT NOT NULL,
            c_street_1 TEXT NOT NULL,
            c_street_2 TEXT NOT NULL,
            c_city TEXT NOT NULL,
            c_state CHAR(2) NOT NULL,
            c_zip CHAR(9) NOT NULL,
            c_phone TEXT NOT NULL,
            c_since TIMESTAMPTZ NOT NULL,
            c_credit CHAR(2) NOT NULL,
            c_credit_lim NUMERIC(12, 2) NOT NULL,
            c_discount NUMERIC(4, 4) NOT NULL,
            c_balance NUMERIC(12, 2) NOT NULL,
            c_ytd_payment NUMERIC(12, 2) NOT NULL,
            c_payment_cnt INTEGER NOT NULL,
            c_delivery_cnt INTEGER NOT NULL,
            c_data TEXT NOT NULL,
            PRIMARY KEY (c_w_id, c_d_id, c_id)
        )
        """,
        f"""
        CREATE TABLE {TPCC_SCHEMA}.history (
            h_id BIGSERIAL PRIMARY KEY,
            h_c_id INTEGER NOT NULL,
            h_c_d_id INTEGER NOT NULL,
            h_c_w_id INTEGER NOT NULL,
            h_d_id INTEGER NOT NULL,
            h_d_w_id INTEGER NOT NULL,
            h_date TIMESTAMPTZ NOT NULL,
            h_amount NUMERIC(6, 2) NOT NULL,
            h_data TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE {TPCC_SCHEMA}.orders (
            o_w_id INTEGER NOT NULL,
            o_d_id INTEGER NOT NULL,
            o_id INTEGER NOT NULL,
            o_c_id INTEGER NOT NULL,
            o_entry_d TIMESTAMPTZ NOT NULL,
            o_carrier_id INTEGER,
            o_ol_cnt INTEGER NOT NULL,
            o_all_local INTEGER NOT NULL,
            PRIMARY KEY (o_w_id, o_d_id, o_id)
        )
        """,
        f"""
        CREATE TABLE {TPCC_SCHEMA}.new_order (
            no_w_id INTEGER NOT NULL,
            no_d_id INTEGER NOT NULL,
            no_o_id INTEGER NOT NULL,
            PRIMARY KEY (no_w_id, no_d_id, no_o_id)
        )
        """,
        f"""
        CREATE TABLE {TPCC_SCHEMA}.order_line (
            ol_w_id INTEGER NOT NULL,
            ol_d_id INTEGER NOT NULL,
            ol_o_id INTEGER NOT NULL,
            ol_number INTEGER NOT NULL,
            ol_i_id INTEGER NOT NULL,
            ol_supply_w_id INTEGER NOT NULL,
            ol_delivery_d TIMESTAMPTZ,
            ol_quantity INTEGER NOT NULL,
            ol_amount NUMERIC(6, 2) NOT NULL,
            ol_dist_info CHAR(24) NOT NULL,
            PRIMARY KEY (ol_w_id, ol_d_id, ol_o_id, ol_number)
        )
        """,
        f"""
        CREATE TABLE {TPCC_SCHEMA}.load_info (
            id INTEGER PRIMARY KEY,
            warehouses INTEGER NOT NULL,
            generator_version TEXT NOT NULL,
            loaded_at TIMESTAMPTZ NOT NULL,
            row_counts JSONB NOT NULL,
            total_bytes BIGINT NOT NULL
        )
        """,
    ]


def load_data(connection: psycopg.Connection[Any], warehouses: int) -> None:
    """Populate the schema with deterministic TPC-C-shaped rows."""
    connection.execute("SET application_name = 'keeninsight-tpcc-loader'")
    connection.execute(f"CREATE SCHEMA IF NOT EXISTS {TPCC_SCHEMA}")
    for statement in ddl():
        connection.execute(statement)

    connection.execute(
        f"""
        INSERT INTO {TPCC_SCHEMA}.warehouse
            (w_id, w_name, w_street_1, w_street_2, w_city, w_state, w_zip, w_tax, w_ytd)
        SELECT g, 'Warehouse ' || g, 'Street 1 ' || g, 'Street 2 ' || g,
               'Demo City ' || g, 'ST', '10000' || lpad(g::text, 4, '0'),
               ((g * 17) % 2000)::numeric / 10000, 300000.00
        FROM generate_series(1, {warehouses}) AS series(g)
        """
    )
    connection.execute(
        f"""
        INSERT INTO {TPCC_SCHEMA}.district
            (d_w_id, d_id, d_name, d_street_1, d_street_2, d_city, d_state,
             d_zip, d_tax, d_ytd, d_next_o_id)
        SELECT w, d, 'District ' || d, 'Street 1 ' || d, 'Street 2 ' || d,
               'Demo City ' || w, 'ST', '20000' || lpad((w * 10 + d)::text, 4, '0'),
               ((w * 19 + d * 7) % 2000)::numeric / 10000, 30000.00, {ORDERS_PER_DISTRICT + 1}
        FROM generate_series(1, {warehouses}) AS ws(w)
        CROSS JOIN generate_series(1, {DISTRICTS_PER_WAREHOUSE}) AS ds(d)
        """
    )
    connection.execute(
        f"""
        INSERT INTO {TPCC_SCHEMA}.item (i_id, i_im_id, i_name, i_price, i_data)
        SELECT g, ((g * 17) % 10000) + 1, 'Item ' || lpad(g::text, 6, '0'),
               (((g * 97) % 10000)::numeric / 100) + 1.00,
               CASE WHEN g % 10 = 0 THEN 'ORIGINAL item data ' || md5(g::text)
                    ELSE 'item data ' || md5(g::text) END
        FROM generate_series(1, {ITEMS}) AS series(g)
        """
    )
    connection.execute(
        f"""
        INSERT INTO {TPCC_SCHEMA}.stock
            (s_w_id, s_i_id, s_quantity, s_ytd, s_order_cnt, s_remote_cnt, s_data,
             s_dist_01, s_dist_02, s_dist_03, s_dist_04, s_dist_05,
             s_dist_06, s_dist_07, s_dist_08, s_dist_09, s_dist_10)
        SELECT w, i, 10 + ((i * 13 + w * 7) % 91), 0, 0, 0,
               'stock data ' || md5((w::text || ':' || i::text)),
               lpad((w * 100000 + i)::text, 24, '0'),
               lpad((w * 100000 + i + 1)::text, 24, '0'),
               lpad((w * 100000 + i + 2)::text, 24, '0'),
               lpad((w * 100000 + i + 3)::text, 24, '0'),
               lpad((w * 100000 + i + 4)::text, 24, '0'),
               lpad((w * 100000 + i + 5)::text, 24, '0'),
               lpad((w * 100000 + i + 6)::text, 24, '0'),
               lpad((w * 100000 + i + 7)::text, 24, '0'),
               lpad((w * 100000 + i + 8)::text, 24, '0'),
               lpad((w * 100000 + i + 9)::text, 24, '0')
        FROM generate_series(1, {warehouses}) AS ws(w)
        CROSS JOIN generate_series(1, {ITEMS}) AS items(i)
        """
    )
    connection.execute(
        f"""
        INSERT INTO {TPCC_SCHEMA}.customer
            (c_w_id, c_d_id, c_id, c_first, c_middle, c_last, c_street_1,
             c_street_2, c_city, c_state, c_zip, c_phone, c_since, c_credit,
             c_credit_lim, c_discount, c_balance, c_ytd_payment, c_payment_cnt,
             c_delivery_cnt, c_data)
        SELECT w, d, c, 'Customer ' || c, 'OE',
               'Last ' || lpad(((c * 37 + d * 11 + w) % 1000)::text, 4, '0'),
               'Street 1 ' || c, 'Street 2 ' || c, 'Demo City ' || w, 'ST',
               '30000' || lpad((c % 10000)::text, 4, '0'),
               '1380000' || lpad((w * 100000 + d * 3000 + c)::text, 7, '0'),
               CURRENT_TIMESTAMP - (((w * 31 + d * 7 + c) % 1800) * INTERVAL '1 day'),
               CASE WHEN c % 10 = 0 THEN 'BC' ELSE 'GC' END,
               50000.00, ((c * 23 + d * 3) % 5000)::numeric / 10000,
               -10.00, 10.00, 1, 0,
               'customer data ' || md5((w::text || ':' || d::text || ':' || c::text))
        FROM generate_series(1, {warehouses}) AS ws(w)
        CROSS JOIN generate_series(1, {DISTRICTS_PER_WAREHOUSE}) AS ds(d)
        CROSS JOIN generate_series(1, {CUSTOMERS_PER_DISTRICT}) AS cs(c)
        """
    )
    connection.execute(
        f"""
        INSERT INTO {TPCC_SCHEMA}.history
            (h_c_id, h_c_d_id, h_c_w_id, h_d_id, h_d_w_id, h_date, h_amount, h_data)
        SELECT c, d, w, d, w, CURRENT_TIMESTAMP - ((c % 365) * INTERVAL '1 day'),
               10.00, 'history ' || md5((w::text || ':' || d::text || ':' || c::text))
        FROM generate_series(1, {warehouses}) AS ws(w)
        CROSS JOIN generate_series(1, {DISTRICTS_PER_WAREHOUSE}) AS ds(d)
        CROSS JOIN generate_series(1, {CUSTOMERS_PER_DISTRICT}) AS cs(c)
        """
    )
    connection.execute(
        f"""
        INSERT INTO {TPCC_SCHEMA}.orders
            (o_w_id, o_d_id, o_id, o_c_id, o_entry_d, o_carrier_id, o_ol_cnt, o_all_local)
        SELECT w, d, o, ((o * 17 + d * 13 + w) % {CUSTOMERS_PER_DISTRICT}) + 1,
               CURRENT_TIMESTAMP - ((o % 730) * INTERVAL '1 day'),
               CASE WHEN o <= {DELIVERED_ORDERS_PER_DISTRICT}
                    THEN ((o + d + w) % 10) + 1 ELSE NULL END,
               5 + ((o + d + w) % 11), 1
        FROM generate_series(1, {warehouses}) AS ws(w)
        CROSS JOIN generate_series(1, {DISTRICTS_PER_WAREHOUSE}) AS ds(d)
        CROSS JOIN generate_series(1, {ORDERS_PER_DISTRICT}) AS os(o)
        """
    )
    connection.execute(
        f"""
        INSERT INTO {TPCC_SCHEMA}.new_order (no_w_id, no_d_id, no_o_id)
        SELECT w, d, o
        FROM generate_series(1, {warehouses}) AS ws(w)
        CROSS JOIN generate_series(1, {DISTRICTS_PER_WAREHOUSE}) AS ds(d)
        CROSS JOIN generate_series({DELIVERED_ORDERS_PER_DISTRICT + 1}, {ORDERS_PER_DISTRICT}) AS os(o)
        """
    )
    connection.execute(
        f"""
        INSERT INTO {TPCC_SCHEMA}.order_line
            (ol_w_id, ol_d_id, ol_o_id, ol_number, ol_i_id, ol_supply_w_id,
             ol_delivery_d, ol_quantity, ol_amount, ol_dist_info)
        SELECT w, d, o, line_no,
               ((o * 97 + line_no * 13 + d * 7 + w * 3) % {ITEMS}) + 1,
               w,
               CASE WHEN o <= {DELIVERED_ORDERS_PER_DISTRICT}
                    THEN CURRENT_TIMESTAMP - ((o % 730) * INTERVAL '1 day')
                    ELSE NULL END,
               5 + ((o + line_no) % 6),
               (((o * 41 + line_no * 17 + d * 5 + w) % 500000)::numeric / 100) + 1.00,
               lpad((w * 100000 + d * 3000 + o + line_no)::text, 24, '0')
        FROM generate_series(1, {warehouses}) AS ws(w)
        CROSS JOIN generate_series(1, {DISTRICTS_PER_WAREHOUSE}) AS ds(d)
        CROSS JOIN generate_series(1, {ORDERS_PER_DISTRICT}) AS os(o)
        CROSS JOIN LATERAL generate_series(1, 5 + ((o + d + w) % 11)) AS lines(line_no)
        """
    )
    for index_sql in (
        f"CREATE INDEX customer_last_idx ON {TPCC_SCHEMA}.customer (c_w_id, c_d_id, c_last, c_first, c_id)",
        f"CREATE INDEX orders_customer_idx ON {TPCC_SCHEMA}.orders (o_w_id, o_d_id, o_c_id, o_id)",
        f"CREATE INDEX order_line_item_idx ON {TPCC_SCHEMA}.order_line (ol_w_id, ol_i_id)",
        f"CREATE INDEX stock_item_idx ON {TPCC_SCHEMA}.stock (s_w_id, s_i_id)",
    ):
        connection.execute(index_sql)
    for table_name in TABLES[:-1]:
        connection.execute(f"ANALYZE {TPCC_SCHEMA}.{table_name}")


def collect_summary(connection: psycopg.Connection[Any], warehouses: int) -> dict[str, Any]:
    row_counts: dict[str, int] = {}
    table_bytes: dict[str, int] = {}
    for table_name in TABLES[:-1]:
        row = connection.execute(
            f"SELECT (SELECT count(*)::bigint FROM {TPCC_SCHEMA}.{table_name}) AS rows, "
            "pg_total_relation_size(%s::regclass)::bigint AS bytes",
            (f"{TPCC_SCHEMA}.{table_name}",),
        ).fetchone()
        row_counts[table_name] = int(row["rows"])
        table_bytes[table_name] = int(row["bytes"])
    total_bytes = sum(table_bytes.values())
    connection.execute(
        f"""
        INSERT INTO {TPCC_SCHEMA}.load_info
            (id, warehouses, generator_version, loaded_at, row_counts, total_bytes)
        VALUES (1, %s, %s, CURRENT_TIMESTAMP, %s::jsonb, %s)
        ON CONFLICT (id) DO UPDATE SET
            warehouses = EXCLUDED.warehouses,
            generator_version = EXCLUDED.generator_version,
            loaded_at = EXCLUDED.loaded_at,
            row_counts = EXCLUDED.row_counts,
            total_bytes = EXCLUDED.total_bytes
        """,
        (warehouses, "tpcc-shaped-demo-v1", json.dumps(row_counts), total_bytes),
    )
    return {
        "schema": TPCC_SCHEMA,
        "warehouses": warehouses,
        "districts": warehouses * DISTRICTS_PER_WAREHOUSE,
        "row_counts": row_counts,
        "table_bytes": table_bytes,
        "total_bytes": total_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("KI_DATABASE_URL", DEFAULT_DSN))
    parser.add_argument("--warehouses", type=int, default=None)
    parser.add_argument("--replace", action="store_true", help="drop only keeninsight_tpcc before loading")
    args = parser.parse_args()
    warehouses = args.warehouses or recommended_warehouses()
    if not 1 <= warehouses <= 100:
        parser.error("--warehouses must be between 1 and 100")

    with psycopg.connect(args.database_url, row_factory=dict_row) as connection:
        connection.execute("SELECT 1")
        if table_exists(connection, "warehouse") and not args.replace:
            loaded = existing_load(connection)
            if loaded:
                print(json.dumps({"already_loaded": True, **loaded}, default=str, ensure_ascii=False))
                return 0
            raise RuntimeError(
                "keeninsight_tpcc already exists without loader metadata; "
                "inspect it manually or pass --replace if it is disposable demo data"
            )
        if args.replace:
            connection.execute(f"DROP SCHEMA IF EXISTS {TPCC_SCHEMA} CASCADE")
        with connection.transaction():
            load_data(connection, warehouses)
            summary = collect_summary(connection, warehouses)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
