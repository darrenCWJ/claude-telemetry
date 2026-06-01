#!/usr/bin/env python3
"""
Claude Code OTEL hook — portable edition.

Drop this file into any project at:  .claude/hooks/claude_hook.py
Then add to your project's          .claude/settings.json  (see bottom of file)

Configuration (env vars, all optional):
  OTEL_ENDPOINT              OTLP HTTP collector URL  (default: http://localhost:4318)
  CLAUDE_OTEL_MAX_CONTENT_LEN  Max chars for tool input/output in spans (default: 2000)

Hook types handled:
  python .claude/hooks/claude_hook.py pre     PreToolUse
  python .claude/hooks/claude_hook.py post    PostToolUse
  python .claude/hooks/claude_hook.py stop    Stop
  python .claude/hooks/claude_hook.py prompt  UserPromptSubmit
"""

import json
import os
import sys
import time
from pathlib import Path

OTEL_ENDPOINT = os.environ.get("OTEL_ENDPOINT", "http://localhost:4318")
TEMP_DIR = Path(os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp"))) / "claude_otel"
MAX_CONTENT_LEN = int(os.environ.get("CLAUDE_OTEL_MAX_CONTENT_LEN", "2000"))

# USD per million tokens. Keyed by exact model id; _pricing_for() falls back to
# family (opus/sonnet/haiku) substring match so older/newer ids still price right.
#   cache_write_5m = 1.25× input (5-minute cache)
#   cache_write_1h = 2.0×  input (1-hour cache — Claude Code's default ephemeral tier)
#   cache_read     = 0.1×  input
_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-8":           {"input": 15.0, "cache_write_5m": 18.75, "cache_write_1h": 30.0, "cache_read": 1.50, "output": 75.0},
    "claude-opus-4-7":           {"input": 15.0, "cache_write_5m": 18.75, "cache_write_1h": 30.0, "cache_read": 1.50, "output": 75.0},
    "claude-sonnet-4-6":         {"input": 3.0,  "cache_write_5m": 3.75,  "cache_write_1h": 6.0,  "cache_read": 0.30, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.8,  "cache_write_5m": 1.0,   "cache_write_1h": 1.6,  "cache_read": 0.08, "output": 4.0},
}
_FAMILY_PRICING: dict[str, dict[str, float]] = {
    "opus":   {"input": 15.0, "cache_write_5m": 18.75, "cache_write_1h": 30.0, "cache_read": 1.50, "output": 75.0},
    "sonnet": {"input": 3.0,  "cache_write_5m": 3.75,  "cache_write_1h": 6.0,  "cache_read": 0.30, "output": 15.0},
    "haiku":  {"input": 0.8,  "cache_write_5m": 1.0,   "cache_write_1h": 1.6,  "cache_read": 0.08, "output": 4.0},
}
_DEFAULT_PRICING = _FAMILY_PRICING["sonnet"]

_CWD = os.getcwd()
_PROJECT_NAME = Path(_CWD).name


def _pricing_for(model: str) -> dict[str, float]:
    """Exact id first, then family substring (opus/sonnet/haiku), else sonnet default."""
    if model in _PRICING:
        return _PRICING[model]
    m = (model or "").lower()
    for family, price in _FAMILY_PRICING.items():
        if family in m:
            return price
    return _DEFAULT_PRICING


def _cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_5m_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
) -> float:
    p = _pricing_for(model)
    return (
        input_tokens * p["input"]
        + cache_creation_5m_tokens * p["cache_write_5m"]
        + cache_creation_1h_tokens * p["cache_write_1h"]
        + cache_read_tokens * p["cache_read"]
        + output_tokens * p["output"]
    ) / 1_000_000


# --------------------------------------------------------------------------- #
# Transcript parsing — token usage lives in the JSONL at transcript_path, NOT
# in the Stop event itself. Each assistant message is written multiple times as
# it streams (identical final usage each time), so we dedup by message.id.
# --------------------------------------------------------------------------- #

def parse_transcript(path: str) -> dict[str, dict]:
    """
    Read a Claude Code transcript JSONL file.
    Returns {message_id: {model, ts, input, output, cache_read, cache_creation}},
    deduped by the assistant message id (first occurrence wins — duplicates carry
    identical usage). message_id is the unit of de-duplication and idempotency.
    """
    records: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("type") != "assistant":
                    continue
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id")
                usage = msg.get("usage") or {}
                if not mid or mid in records or not isinstance(usage, dict) or not usage:
                    continue
                cc_total = int(usage.get("cache_creation_input_tokens", 0) or 0)
                # Split cache writes into 5-minute vs 1-hour tiers (priced differently).
                cc = usage.get("cache_creation")
                if isinstance(cc, dict):
                    cc_1h = int(cc.get("ephemeral_1h_input_tokens", 0) or 0)
                    # 5m absorbs any remainder so 5m + 1h always reconciles to the total
                    cc_5m = max(0, cc_total - cc_1h)
                else:
                    cc_5m, cc_1h = cc_total, 0  # fall back to 5m if the breakdown is absent
                records[mid] = {
                    "model": msg.get("model") or "unknown",
                    "ts": o.get("timestamp") or "",
                    "input": int(usage.get("input_tokens", 0) or 0),
                    "output": int(usage.get("output_tokens", 0) or 0),
                    "cache_read": int(usage.get("cache_read_input_tokens", 0) or 0),
                    "cache_creation": cc_total,
                    "cache_creation_5m": cc_5m,
                    "cache_creation_1h": cc_1h,
                }
    except (FileNotFoundError, OSError):
        pass
    return records


def transcript_span_seconds(path: str) -> float:
    """
    Wall-clock seconds from the first to the last timestamped line in a transcript
    (includes idle gaps — the natural reading of "how long the session ran").
    Scans every line, not just assistant turns, so it captures the true span.
    """
    lo = hi = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = o.get("timestamp")
                if not ts:
                    continue
                if lo is None or ts < lo:
                    lo = ts
                if hi is None or ts > hi:
                    hi = ts
    except (FileNotFoundError, OSError):
        return 0.0
    if not lo or not hi:
        return 0.0
    try:
        from datetime import datetime
        a = datetime.fromisoformat(lo.replace("Z", "+00:00"))
        b = datetime.fromisoformat(hi.replace("Z", "+00:00"))
        return max(0.0, (b - a).total_seconds())
    except Exception:
        return 0.0


def aggregate_by_model(records: dict[str, dict], ids) -> dict[str, dict]:
    """Sum token usage across the given message ids, grouped by model."""
    by_model: dict[str, dict] = {}
    for mid in ids:
        r = records.get(mid)
        if not r:
            continue
        agg = by_model.setdefault(r["model"], {"input": 0, "output": 0, "cache_read": 0,
                                               "cache_creation": 0, "cache_creation_5m": 0, "cache_creation_1h": 0})
        for k in ("input", "output", "cache_read", "cache_creation", "cache_creation_5m", "cache_creation_1h"):
            agg[k] += r[k]
    return by_model


def _token_state_path(session_id: str) -> Path:
    return TEMP_DIR / f"tokens_state_{session_id}.json"


def _load_state(session_id: str) -> dict:
    """State = {emitted_ids: set, last_span_seconds: float}."""
    p = _token_state_path(session_id)
    if p.exists():
        try:
            raw = json.loads(p.read_text())
            return {
                "emitted_ids": set(raw.get("emitted_ids", [])),
                "last_span_seconds": float(raw.get("last_span_seconds", 0.0)),
            }
        except Exception:
            pass
    return {"emitted_ids": set(), "last_span_seconds": 0.0}


def _save_state(session_id: str, ids: set, last_span_seconds: float) -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    _token_state_path(session_id).write_text(json.dumps({
        "emitted_ids": sorted(ids),
        "last_span_seconds": round(last_span_seconds, 3),
    }))


try:
    from opentelemetry import trace, metrics
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    import logging
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False


def _build_providers(session_id: str):
    resource = Resource.create({
        SERVICE_NAME: "claude-code",
        "session.id": session_id,
        "host.name": os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
    })

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces", timeout=5))
    )
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{OTEL_ENDPOINT}/v1/metrics", timeout=5),
            export_interval_millis=3_600_000,  # 1h — hooks exit before this fires; force_flush is the only export path
        )],
    )
    metrics.set_meter_provider(meter_provider)

    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(
        SimpleLogRecordProcessor(OTLPLogExporter(endpoint=f"{OTEL_ENDPOINT}/v1/logs", timeout=5))
    )
    set_logger_provider(log_provider)

    handler = LoggingHandler(level=logging.DEBUG, logger_provider=log_provider)
    app_logger = logging.getLogger("claude-code")
    app_logger.addHandler(handler)
    app_logger.propagate = False
    app_logger.setLevel(logging.DEBUG)
    logging.getLogger("opentelemetry").setLevel(logging.WARNING)

    return tracer_provider, meter_provider, log_provider


def _flush(tp, mp, lp) -> None:
    tp.force_flush(timeout_millis=5000)
    # shutdown() does exactly one final export then kills the background thread,
    # avoiding the double-export that force_flush() causes (it both notifies the
    # background thread AND does a direct export, yielding two rows per metric).
    mp.shutdown()
    lp.force_flush(timeout_millis=5000)


def handle_pre(event: dict) -> None:
    """Store tool-call start timestamp so PostToolUse can compute duration."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    session_id = event.get("session_id", "unknown")
    tool_name = event.get("tool_name", "unknown")
    state_path = TEMP_DIR / f"{session_id}__{tool_name}__latest.json"
    state_path.write_text(json.dumps({"start_ns": time.time_ns()}))


def handle_post(event: dict) -> None:
    """Emit a trace span, metrics, and a log record for the completed tool call."""
    if not OTEL_AVAILABLE:
        return

    session_id = event.get("session_id", "unknown")
    tool_name = event.get("tool_name", "unknown")
    tool_input = event.get("tool_input") or {}
    tool_response = event.get("tool_response") or {}
    end_ns = time.time_ns()

    state_path = TEMP_DIR / f"{session_id}__{tool_name}__latest.json"
    start_ns = end_ns - 100_000_000
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            start_ns = state.get("start_ns", start_ns)
            state_path.unlink(missing_ok=True)
        except Exception:
            pass

    duration_ms = (end_ns - start_ns) / 1_000_000
    is_error = isinstance(tool_response, dict) and "error" in tool_response

    tp, mp, lp = _build_providers(session_id)
    tracer = trace.get_tracer("claude-code.hooks")
    meter = metrics.get_meter("claude-code.hooks")

    labels = {"tool_name": tool_name, "error": str(is_error).lower()}
    meter.create_counter("claude_tool_calls_total", description="Total Claude Code tool calls").add(1, labels)
    meter.create_histogram("claude_tool_duration_ms", unit="ms", description="Tool call duration in ms").record(duration_ms, labels)

    def _truncate(value: object) -> str:
        raw = json.dumps(value, ensure_ascii=False, default=str)
        if len(raw) > MAX_CONTENT_LEN:
            return raw[:MAX_CONTENT_LEN] + f"… [truncated {len(raw) - MAX_CONTENT_LEN} chars]"
        return raw

    input_str = _truncate(tool_input)
    output_str = _truncate(tool_response)

    span_attrs = {
        "tool.name": tool_name,
        "session.id": session_id,
        "duration_ms": round(duration_ms, 2),
        "tool.input": input_str,
        "tool.output": output_str,
        "project.name": _PROJECT_NAME,
        "project.cwd": _CWD,
    }

    with tracer.start_as_current_span(f"claude.tool/{tool_name}", start_time=start_ns, attributes=span_attrs) as span:
        if is_error:
            span.set_status(trace.StatusCode.ERROR)
            span.set_attribute("error.message", str(tool_response.get("error", "")))

    import logging
    level = logging.ERROR if is_error else logging.INFO
    logging.getLogger("claude-code.tool").log(
        level,
        "tool/%s completed in %.1f ms  session=%s",
        tool_name, duration_ms, session_id,
        extra={
            "tool_name": tool_name,
            "session_id": session_id,
            "duration_ms": round(duration_ms, 2),
            "error": str(is_error).lower(),
            "tool_input": input_str,
            "tool_output": output_str,
            "project_name": _PROJECT_NAME,
        },
    )

    _flush(tp, mp, lp)


def handle_prompt(event: dict) -> None:
    """Log the user's prompt text so we know what Claude is being asked to do."""
    if not OTEL_AVAILABLE:
        return

    session_id = event.get("session_id", "unknown")
    prompt = event.get("prompt", "")
    if not prompt:
        return

    tp, mp, lp = _build_providers(session_id)

    import logging
    logging.getLogger("claude-code.prompt").info(
        "user prompt  session=%s", session_id,
        extra={
            "session_id": session_id,
            "prompt": prompt[:4000],
            "project_name": _PROJECT_NAME,
            "project_cwd": _CWD,
        },
    )

    _flush(tp, mp, lp)


def handle_stop(event: dict) -> None:
    """
    Emit token-usage and cost metrics for whatever turns have completed since the
    previous Stop.

    The Stop hook fires at the END OF EVERY TURN, not just at session end (one real
    session produces many Stop events). Token usage is NOT in the event itself — it
    lives in the JSONL file at event['transcript_path']. So each Stop:
      1. parses the full transcript (deduped by message.id),
      2. loads the set of message ids already emitted for this session,
      3. emits a DELTA — only the tokens/cost of messages not yet emitted,
      4. records the now-complete id set.
    Because each Stop emits only its delta, the monotonic counter sums to the true
    session total and the time-bucketed dashboard panels stay correct. If a Stop is
    missed (async timeout), the next Stop catches the gap. If the state file is lost
    mid-session, that session re-emits once (a tolerated, rare overcount).
    """
    if not OTEL_AVAILABLE:
        return

    session_id = event.get("session_id", "unknown")
    cwd = event.get("cwd") or _CWD
    project = Path(cwd).name if cwd else _PROJECT_NAME
    transcript_path = event.get("transcript_path")

    records = parse_transcript(transcript_path) if transcript_path else {}
    state = _load_state(session_id)
    emitted = state["emitted_ids"]
    new_ids = [mid for mid in records if mid not in emitted]
    by_model = aggregate_by_model(records, new_ids)

    # Session duration grows monotonically; emit the increment since the last Stop
    # so the counter's sum() equals the final wall-clock span.
    span_now = transcript_span_seconds(transcript_path) if transcript_path else 0.0
    span_delta = max(0.0, span_now - state["last_span_seconds"])

    # Debug dump — totals for this delta (helps diagnose future issues)
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        (TEMP_DIR / f"stop_debug_{session_id[:8]}.json").write_text(json.dumps({
            "event_keys": list(event.keys()),
            "transcript_path": transcript_path,
            "messages_total": len(records),
            "messages_already_emitted": len(emitted),
            "messages_new_this_stop": len(new_ids),
            "delta_by_model": by_model,
            "project": project,
        }, indent=2))
    except Exception:
        pass

    if not new_ids:
        return  # nothing new this turn — no metrics to emit

    tp, mp, lp = _build_providers(session_id)
    meter = metrics.get_meter("claude-code.hooks")

    # Count the session exactly once (on the first Stop that emits anything for it)
    if not emitted:
        meter.create_counter(
            "claude_sessions_total", description="Total Claude Code sessions completed"
        ).add(1, {"session_id": session_id})

    tok = meter.create_counter("claude_code.token.usage", description="Tokens used by Claude Code sessions")
    cost_ctr = meter.create_counter("claude_code.cost.usage", unit="USD", description="Estimated cost of Claude Code session")

    # Duration delta (seconds) — sum() over the session yields total wall-clock span.
    if span_delta > 0:
        meter.create_counter(
            "claude_code.session.duration", unit="s", description="Claude Code session wall-clock seconds"
        ).add(span_delta, {"session_id": session_id, "project": project})

    total_in = total_out = 0
    total_cost = 0.0
    for model, agg in by_model.items():
        base = {"model": model, "session_id": session_id, "project": project}
        if agg["input"]:
            tok.add(agg["input"], {**base, "token_type": "input"})
        if agg["output"]:
            tok.add(agg["output"], {**base, "token_type": "output"})
        if agg["cache_read"]:
            tok.add(agg["cache_read"], {**base, "token_type": "cacheRead"})
        if agg["cache_creation"]:
            tok.add(agg["cache_creation"], {**base, "token_type": "cacheCreation"})
        c = _cost_usd(model, agg["input"], agg["output"], agg["cache_read"],
                      agg["cache_creation_5m"], agg["cache_creation_1h"])
        if c > 0:
            cost_ctr.add(c, base)
        total_in += agg["input"]
        total_out += agg["output"]
        total_cost += c

    # Persist the now-complete id set + span so the next Stop emits only new turns
    _save_state(session_id, emitted | set(records.keys()), span_now)

    import logging
    logging.getLogger("claude-code.session").info(
        "Stop delta  session=%s  project=%s  new_msgs=%d  in=%d  out=%d  cost=$%.6f",
        session_id, project, len(new_ids), total_in, total_out, total_cost,
    )

    _flush(tp, mp, lp)


def main() -> None:
    if len(sys.argv) < 2:
        return

    hook_type = sys.argv[1].lower()
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        event: dict = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}

    try:
        if hook_type == "pre":
            handle_pre(event)
        elif hook_type == "post":
            handle_post(event)
        elif hook_type == "stop":
            handle_stop(event)
        elif hook_type == "prompt":
            handle_prompt(event)
    except Exception as exc:
        print(f"[claude-otel] {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()


# =============================================================================
# To enable tracking for this project, create .claude/settings.json containing:
#
# {
#   "hooks": {
#     "UserPromptSubmit": [
#       { "matcher": "*", "hooks": [{ "type": "command", "command": "python \".claude/hooks/claude_hook.py\" prompt", "timeout": 8, "async": true }] }
#     ],
#     "PreToolUse": [
#       { "matcher": "*", "hooks": [{ "type": "command", "command": "python \".claude/hooks/claude_hook.py\" pre", "timeout": 8, "async": true }] }
#     ],
#     "PostToolUse": [
#       { "matcher": "*", "hooks": [{ "type": "command", "command": "python \".claude/hooks/claude_hook.py\" post", "timeout": 10, "async": true }] }
#     ],
#     "Stop": [
#       { "matcher": "*", "hooks": [{ "type": "command", "command": "python \".claude/hooks/claude_hook.py\" stop", "timeout": 10, "async": true }] }
#     ]
#   }
# }
#
# Set OTEL_ENDPOINT env var to point to your collector (default: http://localhost:4318)
# =============================================================================
