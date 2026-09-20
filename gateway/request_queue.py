"""Request queue helpers for the Python gateway receive/worker loops."""

from __future__ import annotations

import asyncio
import time
from typing import Any


REQUEST_QUEUE_METRICS_KEY = "_gateway_queue_metrics"


def drain_request_queue(request_queue: asyncio.Queue) -> int:
    dropped = 0
    while True:
        try:
            request_queue.get_nowait()
            dropped += 1
        except asyncio.QueueEmpty:
            break
    return dropped


def enqueue_latest_request(request_queue: asyncio.Queue, data: dict[str, Any]) -> int:
    dropped = drain_request_queue(request_queue)
    try:
        request_queue.put_nowait(data)
    except asyncio.QueueFull:
        dropped += drain_request_queue(request_queue)
        request_queue.put_nowait(data)
    return dropped


def attach_request_queue_metrics(
    data: dict[str, Any],
    request_queue: asyncio.Queue,
    *,
    dropped: int,
) -> None:
    data[REQUEST_QUEUE_METRICS_KEY] = {
        "queue_size": request_queue.qsize(),
        "queue_maxsize": request_queue.maxsize,
        "dropped_pending": dropped,
        "enqueued_at": time.time(),
    }


def pop_request_queue_metrics(data: dict[str, Any], request_queue: asyncio.Queue) -> dict[str, Any]:
    metrics = data.pop(REQUEST_QUEUE_METRICS_KEY, {})
    if not isinstance(metrics, dict):
        metrics = {}
    enqueued_at = metrics.pop("enqueued_at", None)
    if isinstance(enqueued_at, (int, float)):
        metrics["queue_wait_ms"] = (time.time() - float(enqueued_at)) * 1000
    metrics["queue_size_after_dequeue"] = request_queue.qsize()
    return metrics


def build_request_dequeued_summary(
    data: dict[str, Any],
    *,
    round_id: str,
    playback_id: str,
    queue_metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "message_type": data.get("type"),
        "round_id": round_id,
        "playback_id": playback_id,
        **queue_metrics,
    }
