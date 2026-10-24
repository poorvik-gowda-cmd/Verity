"""Usage, token, latency, and cost tracker for Gemini API calls.

Provides centralized metrics tracking required by AGENTS.md §6.8 (usage report).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class UsageMetrics:
    """Snapshot of aggregated usage metrics."""

    provider: str
    model: str
    total_calls: int = 0
    cached_calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def average_latency_ms(self) -> float:
        executed_calls = self.total_calls - self.cached_calls
        if executed_calls <= 0:
            return 0.0
        return self.total_latency_ms / executed_calls


class UsageTracker:
    """Thread-safe tracker for Gemini API execution and token metrics."""

    # Default official pricing per million tokens (Gemini 2.5 Flash / Flash standard tier)
    DEFAULT_INPUT_COST_PER_MILLION: float = 0.075
    DEFAULT_OUTPUT_COST_PER_MILLION: float = 0.30

    def __init__(
        self,
        provider: str = "google",
        model: str = "gemini-2.5-flash",
        input_cost_per_million: Optional[float] = None,
        output_cost_per_million: Optional[float] = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.input_cost_per_million = (
            input_cost_per_million
            if input_cost_per_million is not None
            else self.DEFAULT_INPUT_COST_PER_MILLION
        )
        self.output_cost_per_million = (
            output_cost_per_million
            if output_cost_per_million is not None
            else self.DEFAULT_OUTPUT_COST_PER_MILLION
        )

        self._lock = threading.Lock()
        self._total_calls = 0
        self._cached_calls = 0
        self._failed_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_latency_ms = 0.0

    def record_call(
        self,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        cached: bool = False,
        failed: bool = False,
    ) -> None:
        """Record an API invocation or cache hit."""
        with self._lock:
            self._total_calls += 1
            if cached:
                self._cached_calls += 1
                return

            if failed:
                self._failed_calls += 1

            self._input_tokens += max(0, input_tokens)
            self._output_tokens += max(0, output_tokens)
            self._total_latency_ms += max(0.0, latency_ms)

    def calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Calculate estimated cost in USD for given token amounts."""
        in_cost = (input_tokens / 1_000_000.0) * self.input_cost_per_million
        out_cost = (output_tokens / 1_000_000.0) * self.output_cost_per_million
        return in_cost + out_cost

    def get_metrics(self) -> UsageMetrics:
        """Return a snapshot of current aggregated usage metrics."""
        with self._lock:
            cost = self.calculate_cost(self._input_tokens, self._output_tokens)
            return UsageMetrics(
                provider=self.provider,
                model=self.model,
                total_calls=self._total_calls,
                cached_calls=self._cached_calls,
                failed_calls=self._failed_calls,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                total_latency_ms=self._total_latency_ms,
                estimated_cost_usd=round(cost, 6),
            )

    def get_summary(self) -> Dict[str, Any]:
        """Return a dictionary summary formatted for reporting."""
        metrics = self.get_metrics()
        return {
            "provider": metrics.provider,
            "model": metrics.model,
            "total_calls": metrics.total_calls,
            "api_calls_executed": metrics.total_calls - metrics.cached_calls,
            "cached_calls": metrics.cached_calls,
            "failed_calls": metrics.failed_calls,
            "input_tokens": metrics.input_tokens,
            "output_tokens": metrics.output_tokens,
            "total_tokens": metrics.total_tokens,
            "average_latency_ms": round(metrics.average_latency_ms, 2),
            "estimated_cost_usd": f"${metrics.estimated_cost_usd:.6f}",
        }

    def reset(self) -> None:
        """Reset all tracked counters."""
        with self._lock:
            self._total_calls = 0
            self._cached_calls = 0
            self._failed_calls = 0
            self._input_tokens = 0
            self._output_tokens = 0
            self._total_latency_ms = 0.0
