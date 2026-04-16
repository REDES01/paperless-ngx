"""
Kafka producer for the paperless_ml app.

Publishes events to the data-stack Redpanda on the shared paperless_ml_net
docker network. Module-level singleton so we only open one connection per
webserver process.
"""

import json
import logging
import os
import threading

from kafka import KafkaProducer
from kafka.errors import KafkaError, NoBrokersAvailable

log = logging.getLogger("paperless_ml")

_producer: KafkaProducer | None = None
_producer_lock = threading.Lock()


def _broker() -> str:
    return os.environ.get("PAPERLESS_ML_KAFKA_BROKER", "redpanda:9092")


def get_producer() -> KafkaProducer | None:
    """
    Return a shared KafkaProducer instance, creating it on first use.
    Returns None if Redpanda is unreachable; callers MUST handle this.
    """
    global _producer
    if _producer is not None:
        return _producer
    with _producer_lock:
        if _producer is not None:
            return _producer
        try:
            _producer = KafkaProducer(
                bootstrap_servers=_broker(),
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                acks=0,                       # fire-and-forget; do not block uploads
                request_timeout_ms=2000,
                max_block_ms=2000,            # bound the connect attempt
                retries=0,
                linger_ms=0,
            )
            log.info("paperless_ml: Kafka producer connected to %s", _broker())
        except (NoBrokersAvailable, KafkaError) as exc:
            log.warning("paperless_ml: Kafka unavailable at %s (%s)", _broker(), exc)
            _producer = None
    return _producer


def publish(topic: str, event: dict) -> bool:
    """
    Best-effort publish. Returns True on success, False otherwise.
    Never raises — uploads must succeed even when Redpanda is down.
    """
    producer = get_producer()
    if producer is None:
        log.warning("paperless_ml: skipping publish to %s (no producer)", topic)
        return False
    try:
        future = producer.send(topic, event)
        # acks=0 means send returns immediately; flush briefly so the socket
        # actually flushes before the request thread returns.
        producer.flush(timeout=2)
        return True
    except (KafkaError, Exception) as exc:
        log.warning("paperless_ml: publish to %s failed: %s", topic, exc)
        # If the broker disappeared, drop the cached producer so the next
        # call retries the connect.
        global _producer
        _producer = None
        return False
