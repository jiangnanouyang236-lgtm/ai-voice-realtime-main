"""LLM metrics parsing helpers used by Gateway trace recording."""

from __future__ import annotations

import json
from typing import Any


def parse_llm_metrics_json(raw_metrics: str | None) -> dict[str, Any]:
    if not raw_metrics:
        return {}
    try:
        metrics = json.loads(raw_metrics)
    except (TypeError, json.JSONDecodeError):
        return {}
    return metrics if isinstance(metrics, dict) else {}
