"""Shared collector contracts for all radar platforms."""

from .base import (
    Collector,
    CollectorResult,
    PendingRawPayload,
    classify_source_health,
)

__all__ = [
    "Collector",
    "CollectorResult",
    "PendingRawPayload",
    "classify_source_health",
]
