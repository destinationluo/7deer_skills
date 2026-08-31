"""Shared collector contracts for all radar platforms."""

from .base import Collector, CollectorResult, classify_source_health

__all__ = ["Collector", "CollectorResult", "classify_source_health"]
