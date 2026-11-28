#!/usr/bin/env python3
"""Small MySQL TPC-C-shaped workload used by the MySQL lab.

The PG lab uses pgbench.  MySQL does not speak the pgbench protocol, so this
module runs the same transaction mix with a bounded number of Python workers.
Every event is rolled back after exercising real rows, indexes, locks and
InnoDB undo/redo paths, leaving the imported TPCC data unchanged.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import pymysql
from pymysql.cursors import DictCursor


@dataclass
class WorkloadCounters:
    transactions: int = 0
    errors: int = 0
    latency_total_ms: float = 0.0


@dataclass
class WorkloadSpec:
    role: str
    scenario: str
    clients: int
    target_tps: float
    session_settings: Mapping[str, Any] = field(default_factory=dict)


class MySQLLabWorkload:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str = "keeninsight_tpcc",
        timeout: float = 30.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.database = database
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._counters: Dict[str, WorkloadCounters] = {}
        self._specs: Dict[str, WorkloadSpec] = {}
        self._started_at = 0.0
        self._last_sample_at = 0.0
        self._last_counts: Dict[str, int] = {}

    def _connect(self, role: str) -> pymysql.Connection:
        spec = self._specs.get(role)
        connection = pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=min(10.0, self.timeout),
            read_timeout=self.timeout,
            write_timeout=self.timeout,
        )
        if spec and spec.session_settings:
            with connection.cursor() as cursor:
                for name, value in spec.session_settings.items():
                    cursor.execute("SET SESSION `{}` = %s".format(name), (value,))
        return connection

    @staticmethod
    def _rand_wd() -> tuple[int, int]:
        return random.randint(1, 5), random.randint(1, 10)

    def _new_order(self, cursor: Any) -> None:
        w_id, d_id = self._rand_wd()
        c_id = random.randint(1, 3000)
        items = [random.randint(1, 100000) for _ in range(5)]
        cursor.execute(
            "SELECT d_next_o_id FROM district WHERE d_w_id=%s AND d_id=%s FOR UPDATE",
            (w_id, d_id),
        )
        row = cursor.fetchone()
        o_id = int(row["d_next_o_id"])
        cursor.execute(
            "UPDATE district SET d_next_o_id=d_next_o_id+1 WHERE d_w_id=%s AND d_id=%s",
            (w_id, d_id),
        )
        cursor.execute(
            "INSERT INTO orders(o_w_id,o_d_id,o_id,o_c_id,o_entry_d,o_carrier_id,o_ol_cnt,o_all_local) "
            "VALUES(%s,%s,%s,%s,NOW(6),NULL,5,1)",
            (w_id, d_id, o_id, c_id),
        )
        cursor.execute(
            "INSERT INTO new_order(no_w_id,no_d_id,no_o_id) VALUES(%s,%s,%s)",
            (w_id, d_id, o_id),
        )
        for number, item in enumerate(items, 1):
            cursor.execute(
                "UPDATE stock SET s_quantity=CASE WHEN s_quantity>=11 THEN s_quantity-1 ELSE s_quantity+89 END,"
                " s_ytd=s_ytd+1, s_order_cnt=s_order_cnt+1 WHERE s_w_id=%s AND s_i_id=%s",
                (w_id, item),
            )
            cursor.execute(
                "INSERT INTO order_line(ol_w_id,ol_d_id,ol_o_id,ol_number,ol_i_id,ol_supply_w_id,"
                "ol_delivery_d,ol_quantity,ol_amount,ol_dist_info) VALUES(%s,%s,%s,%s,%s,%s,NULL,1,10.00,%s)",
                (w_id, d_id, o_id, number, item, w_id, "DISTINFO000000000000000".ljust(24)[:24]),
            )

    def _payment(self, cursor: Any, hot: bool = False) -> None:
        if hot:
            w_id, d_id = 1, 1
        else:
            w_id, d_id = self._rand_wd()
        c_id = random.randint(1, 3000)
        cursor.execute("UPDATE warehouse SET w_ytd=w_ytd+1.00 WHERE w_id=%s", (w_id,))
        cursor.execute("UPDATE district SET d_ytd=d_ytd+1.00 WHERE d_w_id=%s AND d_id=%s", (w_id, d_id))
        cursor.execute(
            "UPDATE customer SET c_balance=c_balance-1.00, c_ytd_payment=c_ytd_payment+1.00,"
            " c_payment_cnt=c_payment_cnt+1 WHERE c_w_id=%s AND c_d_id=%s AND c_id=%s",
            (w_id, d_id, c_id),
        )
        cursor.execute(
            "INSERT INTO history(h_c_id,h_c_d_id,h_c_w_id,h_d_id,h_d_w_id,h_date,h_amount,h_data) "
            "VALUES(%s,%s,%s,%s,%s,NOW(6),1.00,'TPCC-HOT-DEMO')",
            (c_id, d_id, w_id, d_id, w_id),
        )

    def _stock_level(self, cursor: Any, hot: bool = False) -> None:
        if hot:
            w_id, d_id = 1, 1
        else:
            w_id, d_id = self._rand_wd()
        cursor.execute(
            "SELECT count(*) AS n FROM stock s WHERE s.s_w_id=%s AND s.s_quantity<15 AND s.s_i_id IN "
            "(SELECT DISTINCT ol.ol_i_id FROM order_line ol WHERE ol.ol_w_id=%s AND ol.ol_d_id=%s AND ol.ol_o_id>=2980)",
            (w_id, w_id, d_id),
        )
        cursor.fetchone()

    def _order_status(self, cursor: Any, hot: bool = False) -> None:
        if hot:
            w_id, d_id = 1, 1
        else:
            w_id, d_id = self._rand_wd()
        c_id = random.randint(1, 3000)
        cursor.execute(
            "SELECT o_id,o_c_id,o_entry_d,o_carrier_id FROM orders WHERE o_w_id=%s AND o_d_id=%s AND o_c_id=%s "
            "ORDER BY o_id DESC LIMIT 1",
            (w_id, d_id, c_id),
        )
        row = cursor.fetchone()
        if not row:
            return
        o_id = row["o_id"]
        cursor.execute(
            "SELECT ol_i_id,ol_quantity,ol_amount FROM order_line WHERE ol_w_id=%s AND ol_d_id=%s AND ol_o_id=%s "
            "ORDER BY ol_number",
            (w_id, d_id, o_id),
        )
        cursor.fetchall()

    def _delivery(self, cursor: Any) -> None:
        w_id, d_id = self._rand_wd()
        cursor.execute(
            "SELECT no_o_id FROM new_order WHERE no_w_id=%s AND no_d_id=%s ORDER BY no_o_id LIMIT 1 FOR UPDATE",
            (w_id, d_id),
        )
        row = cursor.fetchone()
        if not row:
            return
        o_id = row["no_o_id"]
        cursor.execute("DELETE FROM new_order WHERE no_w_id=%s AND no_d_id=%s AND no_o_id=%s", (w_id, d_id, o_id))
        cursor.execute(
            "UPDATE orders SET o_carrier_id=1 WHERE o_w_id=%s AND o_d_id=%s AND o_id=%s",
            (w_id, d_id, o_id),
        )

    def _normal_event(self, cursor: Any) -> None:
        bucket = random.randint(1, 100)
        if bucket <= 45:
            self._new_order(cursor)
        elif bucket <= 88:
            self._payment(cursor)
        elif bucket <= 92:
            self._order_status(cursor)
        elif bucket <= 96:
            self._delivery(cursor)
        else:
            self._stock_level(cursor)

    def _event(self, cursor: Any, role: str) -> None:
        spec = self._specs[role]
        scenario = spec.scenario
        if scenario == "payment_hot":
            self._payment(cursor, hot=True)
        elif scenario == "stock_level_hot":
            self._stock_level(cursor, hot=True)
        elif scenario == "stock_level_burst":
            self._stock_level(cursor, hot=False)
        elif scenario == "order_status_hot":
            self._order_status(cursor, hot=True)
        elif scenario in {"full_mix", "wal_checkpoint", "normal", "baseline"}:
            self._normal_event(cursor)
        else:
            raise ValueError("unknown MySQL workload scenario: {}".format(scenario))

    def _worker(self, role: str) -> None:
        spec = self._specs[role]
        interval = (1.0 / (spec.target_tps / max(1, spec.clients))) if spec.target_tps > 0 else 0.0
        next_at = time.monotonic()
        connection = None
        try:
            connection = self._connect(role)
            cursor = connection.cursor()
            while not self._stop.is_set():
                started = time.perf_counter()
                try:
                    connection.begin()
                    self._event(cursor, role)
                    connection.rollback()
                    elapsed = (time.perf_counter() - started) * 1000.0
                    with self._lock:
                        counter = self._counters.setdefault(role, WorkloadCounters())
                        counter.transactions += 1
                        counter.latency_total_ms += elapsed
                except Exception:
                    try:
                        connection.rollback()
                    except Exception:
                        pass
                    with self._lock:
                        self._counters.setdefault(role, WorkloadCounters()).errors += 1
                if interval:
                    next_at += interval
                    delay = next_at - time.monotonic()
                    if delay > 0:
                        self._stop.wait(delay)
                    else:
                        next_at = time.monotonic()
        finally:
            if connection is not None:
                connection.close()

    def start(self, specs: Sequence[WorkloadSpec]) -> None:
        with self._lock:
            self.stop()
            self._stop = threading.Event()
            self._counters = {}
            self._specs = {spec.role: spec for spec in specs}
            self._last_counts = {spec.role: 0 for spec in specs}
            self._threads = []
            for spec in specs:
                for _ in range(max(1, int(spec.clients))):
                    thread = threading.Thread(target=self._worker, args=(spec.role,), daemon=True)
                    thread.start()
                    self._threads.append(thread)
            self._started_at = time.monotonic()
            self._last_sample_at = self._started_at
            self._last_counts = {}

    def stop(self) -> None:
        self._stop.set()
        for thread in list(self._threads):
            thread.join(timeout=3.0)
        self._threads = []

    def running(self) -> bool:
        return bool(self._threads) and any(thread.is_alive() for thread in self._threads)

    def counters(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                role: {
                    "transactions": counter.transactions,
                    "errors": counter.errors,
                    "latency_ms": (counter.latency_total_ms / counter.transactions) if counter.transactions else None,
                }
                for role, counter in self._counters.items()
            }

    def sample(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            elapsed = max(0.001, now - self._last_sample_at)
            result = {"elapsed_seconds": round(elapsed, 3), "business": {}, "pressure": {}}
            for role in ("control", "external"):
                counter = self._counters.get(role, WorkloadCounters())
                previous = self._last_counts.get(role, counter.transactions)
                delta = max(0, counter.transactions - previous)
                self._last_counts[role] = counter.transactions
                key = "business" if role == "control" else "pressure"
                result[key] = {
                    "tps_live": round(delta / elapsed, 2),
                    "tps": round(counter.transactions / max(0.001, now - self._started_at), 2),
                    "latency_live_ms": round(counter.latency_total_ms / counter.transactions, 3) if counter.transactions else None,
                    "transactions": counter.transactions,
                    "errors": counter.errors,
                }
            self._last_sample_at = now
            return result
