"""
OpenTelemetry instrumentation wrapper for the Anthropic Claude API.

Usage:
    import anthropic
    from sdk.setup_otel import setup_otel
    from sdk.claude_otel import instrument_client

    setup_otel("my-app")
    client = instrument_client(anthropic.Anthropic())

    # All messages.create calls now emit traces, metrics, and logs.
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": "Hello"}],
    )
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any

from opentelemetry import trace, metrics
from opentelemetry.trace import StatusCode

logger = logging.getLogger("claude-api")

# USD per million tokens — update when Anthropic changes pricing
_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7":          {"input": 15.0,  "output": 75.0},
    "claude-sonnet-4-6":        {"input": 3.0,   "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.8,  "output": 4.0},
}
_DEFAULT_PRICING = {"input": 3.0, "output": 15.0}


def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p = _PRICING.get(model, _DEFAULT_PRICING)
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


def instrument_client(client: Any) -> Any:
    """
    Wrap an anthropic.Anthropic (or AsyncAnthropic) client so that every
    messages.create call is captured as an OTEL span with token/cost metrics.

    Returns the same client object with the method monkey-patched in place.
    """
    original = client.messages.create

    @functools.wraps(original)
    def _instrumented(**kwargs: Any) -> Any:
        tracer = trace.get_tracer("claude-api")
        meter = metrics.get_meter("claude-api")

        model: str = kwargs.get("model", "unknown")
        max_tokens: int = kwargs.get("max_tokens", 0)
        message_count: int = len(kwargs.get("messages", []))

        token_counter = meter.create_counter(
            "claude_api_tokens_total",
            description="Tokens consumed by Claude API calls",
        )
        cost_counter = meter.create_counter(
            "claude_api_cost_usd",
            unit="USD",
            description="Estimated cost of Claude API calls in USD",
        )
        duration_hist = meter.create_histogram(
            "claude_api_request_duration_ms",
            unit="ms",
            description="Claude API request duration in ms",
        )
        request_counter = meter.create_counter(
            "claude_api_requests_total",
            description="Total Claude API requests",
        )

        start_ns = time.time_ns()

        with tracer.start_as_current_span(
            f"claude.messages.create/{model}",
            attributes={
                "gen_ai.system": "anthropic",
                "gen_ai.request.model": model,
                "gen_ai.request.max_tokens": max_tokens,
                "gen_ai.request.message_count": message_count,
            },
        ) as span:
            try:
                response = original(**kwargs)

                duration_ms = (time.time_ns() - start_ns) / 1_000_000
                attrs = {"model": model, "error": "false"}

                if hasattr(response, "usage") and response.usage:
                    in_tok = getattr(response.usage, "input_tokens", 0)
                    out_tok = getattr(response.usage, "output_tokens", 0)
                    cost = _cost_usd(model, in_tok, out_tok)

                    span.set_attribute("gen_ai.usage.input_tokens", in_tok)
                    span.set_attribute("gen_ai.usage.output_tokens", out_tok)
                    span.set_attribute("gen_ai.usage.cost_usd", cost)

                    token_counter.add(in_tok, {**attrs, "token_type": "input"})
                    token_counter.add(out_tok, {**attrs, "token_type": "output"})
                    cost_counter.add(cost, attrs)

                    logger.info(
                        "claude api  model=%s  in=%d  out=%d  cost=$%.6f  duration=%.0f ms",
                        model, in_tok, out_tok, cost, duration_ms,
                    )

                request_counter.add(1, attrs)
                duration_hist.record(duration_ms, attrs)

                return response

            except Exception as exc:
                duration_ms = (time.time_ns() - start_ns) / 1_000_000
                err_attrs = {"model": model, "error": "true"}
                request_counter.add(1, err_attrs)
                duration_hist.record(duration_ms, err_attrs)
                span.set_status(StatusCode.ERROR, str(exc))
                span.set_attribute("error.type", type(exc).__name__)
                logger.error("claude api error  model=%s  error=%s", model, exc)
                raise

    client.messages.create = _instrumented
    return client
