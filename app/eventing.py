"""Kafka event transport and an auditable local event record.

The paper's monitoring path emits probe messages to Kafka.  This module uses
the real kafka-python producer when a bootstrap server is configured and never
reports a successful Kafka delivery when the broker is unavailable.  A
database outbox/local event record keeps the demo inspectable in environments
without a broker; its status is explicitly ``database-fallback``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import Database

LOG = logging.getLogger(__name__)


class KafkaEventBus:
    """Publish probe events to Kafka and persist the delivery evidence."""

    def __init__(
        self,
        db: Database,
        *,
        bootstrap_servers: str = "",
        topic: str = "keeninsight.events",
    ) -> None:
        self.db = db
        self.bootstrap_servers = bootstrap_servers.strip()
        self.topic = topic
        self._producer: Any = None
        self._lock = threading.Lock()
        self.transport = "unavailable"
        self.last_error: str | None = None
        self.last_publish_at: datetime | None = None
        self.sent_count = 0
        self.failed_count = 0

    def start(self) -> None:
        if not self.bootstrap_servers:
            self.transport = "database-fallback"
            self.last_error = "KI_KAFKA_BOOTSTRAP_SERVERS is not configured"
            return
        try:
            from kafka import KafkaProducer

            producer = KafkaProducer(
                bootstrap_servers=[item.strip() for item in self.bootstrap_servers.split(",") if item.strip()],
                value_serializer=lambda value: json.dumps(
                    value, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8"),
                key_serializer=lambda value: str(value).encode("utf-8") if value is not None else None,
                acks="all",
                retries=3,
                request_timeout_ms=3000,
                api_version_auto_timeout_ms=3000,
            )
            # Force a metadata request so status distinguishes a configured
            # but unreachable broker from a successful Kafka path.
            producer.partitions_for(self.topic)
            self._producer = producer
            self.transport = "kafka"
            self.last_error = None
        except Exception as exc:  # broker is an optional deployment service
            self.transport = "database-fallback"
            self.last_error = str(exc)
            LOG.warning("Kafka transport unavailable; using explicit DB fallback: %s", exc)

    def stop(self) -> None:
        producer = self._producer
        self._producer = None
        if producer is not None:
            try:
                producer.flush(timeout=3)
                producer.close(timeout=3)
            except Exception:
                LOG.exception("Kafka producer close failed")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "configured": bool(self.bootstrap_servers),
                "bootstrap_servers": self.bootstrap_servers,
                "topic": self.topic,
                "transport": self.transport,
                "available": self.transport == "kafka",
                "sent_count": self.sent_count,
                "failed_count": self.failed_count,
                "last_error": self.last_error,
                "last_publish_at": self.last_publish_at.isoformat()
                if self.last_publish_at
                else None,
            }

    def publish(self, event: dict[str, Any], *, event_key: str | None = None) -> dict[str, Any]:
        payload = dict(event)
        payload.setdefault("schema", "keeninsight-ebpf-event-v1")
        payload.setdefault("event_uuid", str(uuid.uuid4()))
        payload.setdefault("observed_at", datetime.now(timezone.utc).isoformat())
        outbox_id = self.db.fetch_one(
            """
            INSERT INTO keeninsight.event_outbox (topic, event_key, payload)
            VALUES (%s, %s, %s::jsonb)
            RETURNING id
            """,
            (self.topic, event_key, json.dumps(payload, ensure_ascii=False)),
        )
        if not outbox_id:
            raise RuntimeError("could not create Kafka outbox record")
        payload["outbox_id"] = int(outbox_id["id"])
        producer = self._producer
        delivery = {
            "outbox_id": int(outbox_id["id"]),
            "event_uuid": payload["event_uuid"],
            "transport": self.transport,
        }
        if producer is not None and self.transport == "kafka":
            try:
                metadata = producer.send(
                    self.topic, key=event_key, value=payload
                ).get(timeout=5)
                self.db.execute(
                    """
                    UPDATE keeninsight.event_outbox
                    SET status = 'published', attempts = attempts + 1,
                        published_at = now(), last_error = NULL,
                        payload = %s::jsonb
                    WHERE id = %s
                    """,
                    (json.dumps(payload, ensure_ascii=False), outbox_id["id"]),
                )
                self._persist_event(payload, transport="kafka-producer")
                with self._lock:
                    self.sent_count += 1
                    self.last_publish_at = datetime.now(timezone.utc)
                    self.last_error = None
                delivery.update(
                    {
                        "status": "published",
                        "partition": metadata.partition,
                        "offset": metadata.offset,
                    }
                )
                return delivery
            except Exception as exc:
                self.db.execute(
                    """
                    UPDATE keeninsight.event_outbox
                    SET status = 'failed', attempts = attempts + 1, last_error = %s
                    WHERE id = %s
                    """,
                    (str(exc), outbox_id["id"]),
                )
                with self._lock:
                    self.failed_count += 1
                    self.last_error = str(exc)
                delivery.update({"status": "failed", "error": str(exc)})
                return delivery

        self.db.execute(
            """
            UPDATE keeninsight.event_outbox
            SET status = 'failed', attempts = attempts + 1,
                last_error = %s
            WHERE id = %s
            """,
            (self.last_error or "Kafka producer is unavailable", outbox_id["id"]),
        )
        self._persist_event(payload, transport="database-fallback")
        with self._lock:
            self.failed_count += 1
            self.last_publish_at = datetime.now(timezone.utc)
        delivery.update(
            {
                "status": "database-fallback",
                "error": self.last_error or "Kafka producer is unavailable",
            }
        )
        return delivery

    def _persist_event(self, event: dict[str, Any], *, transport: str) -> None:
        observed_at = event.get("observed_at")
        if isinstance(observed_at, str):
            try:
                observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            except ValueError:
                observed_at = datetime.now(timezone.utc)
        payload = dict(event)
        payload["event_uuid"] = event.get("event_uuid")
        values = (
            event.get("session_id"),
            observed_at,
            event.get("event_type") or "probe_event",
            event.get("probe_kind") or "unknown",
            event.get("function_name") or "unknown",
            event.get("binary_path"),
            event.get("pid"),
            event.get("tid"),
            event.get("cpu"),
            event.get("cgroup_id"),
            event.get("cid"),
            event.get("query_id"),
            event.get("duration_ns"),
            event.get("bytes"),
            bool(event.get("is_anomaly")),
            transport,
            json.dumps(payload, ensure_ascii=False, default=str),
        )
        # Producer and consumer can observe the same Kafka record nearly
        # simultaneously.  Serialize on the event UUID and check the
        # materialized table inside one transaction so the consumer cannot
        # create a second audit row in the producer's commit window.
        event_uuid = str(event.get("event_uuid") or "")
        with self.db.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    if event_uuid:
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (event_uuid,),
                        )
                        cur.execute(
                            "SELECT 1 FROM keeninsight.ebpf_events "
                            "WHERE payload->>'event_uuid' = %s LIMIT 1",
                            (event_uuid,),
                        )
                        if cur.fetchone():
                            return
                    cur.execute(
                        """
                        INSERT INTO keeninsight.ebpf_events
                            (session_id, observed_at, event_type, probe_kind, function_name,
                             binary_path, pid, tid, cpu, cgroup_id, cid, query_id,
                             duration_ns, bytes, is_anomaly, transport, payload)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT DO NOTHING
                        """,
                        values,
                    )


class KafkaEventConsumer:
    """Optional Kafka consumer that materializes messages not already audited."""

    def __init__(
        self,
        db: Database,
        *,
        bootstrap_servers: str,
        topic: str,
        group_id: str,
    ) -> None:
        self.db = db
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.group_id = group_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.consumed_count = 0

    def start(self) -> None:
        if not self.bootstrap_servers or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="keeninsight-kafka-consumer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)
        self._thread = None

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "topic": self.topic,
            "group_id": self.group_id,
            "consumed_count": self.consumed_count,
            "last_error": self.last_error,
        }

    def _run(self) -> None:
        consumer = None
        try:
            from kafka import KafkaConsumer

            consumer = KafkaConsumer(
                self.topic,
                bootstrap_servers=[item.strip() for item in self.bootstrap_servers.split(",") if item.strip()],
                group_id=self.group_id,
                auto_offset_reset="latest",
                enable_auto_commit=True,
                value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
                consumer_timeout_ms=1000,
            )
            while not self._stop.is_set():
                try:
                    for message in consumer:
                        if self._stop.is_set():
                            break
                        payload = message.value
                        if isinstance(payload, dict):
                            exists = self.db.fetch_one(
                                "SELECT 1 FROM keeninsight.ebpf_events WHERE payload->>'event_uuid' = %s LIMIT 1",
                                (payload.get("event_uuid"),),
                            )
                            if not exists:
                                # Reuse a small bus-shaped insert without
                                # creating another producer or outbox row.
                                KafkaEventBus(self.db)._persist_event(
                                    payload, transport="kafka-consumer"
                                )
                            self.consumed_count += 1
                except Exception as exc:
                    self.last_error = str(exc)
                    time.sleep(1)
        except Exception as exc:
            self.last_error = str(exc)
            LOG.warning("Kafka consumer unavailable: %s", exc)
        finally:
            if consumer is not None:
                try:
                    consumer.close()
                except Exception:
                    LOG.exception("Kafka consumer close failed")
