#!/usr/bin/env python3
"""
One-time backfill of Claude Code token/cost metrics from existing transcripts.

The Stop hook only emits going forward; every session that ended before the hook
was fixed has token usage sitting in its transcript JSONL but nothing in ClickHouse.
This script parses every transcript, sums usage (deduped by message.id), and writes
the result straight into otel.otel_metrics_sum at each session's real timestamp.

Why a direct INSERT and not the OTEL SDK: the SDK stamps every metric point at
export time, so it cannot backdate TimeUnix — pushing history through it would pile
all past cost onto "now" and wreck every time-series panel. We insert rows shaped
exactly like the collector's own, with the historical TimeUnix.

It also writes a per-session state file (the same one the live hook reads) marking
every message id as already-emitted, so the currently-active session does not double
count on its next Stop.

Usage:
    python backfill.py            # backfill all projects' transcripts
    python backfill.py --dry-run  # print what would be inserted, write nothing
"""

import base64
import glob
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / ".claude" / "hooks"))
from claude_hook import parse_transcript, transcript_span_seconds, _cost_usd  # noqa: E402

CH_URL = os.environ.get("CLICKHOUSE_HTTP", "http://localhost:8123")
CH_USER = os.environ.get("CLICKHOUSE_USER", "otel")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "otel_secret")
PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
TEMP_DIR = Path(os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp"))) / "claude_otel"

# token_type label -> usage key
_TYPES = {
    "input": "input",
    "output": "output",
    "cacheRead": "cache_read",
    "cacheCreation": "cache_creation",
}


def _ch_execute(sql: str, body: str = "") -> str:
    auth = base64.b64encode(f"{CH_USER}:{CH_PASSWORD}".encode()).decode()
    url = f"{CH_URL}/?query={urllib.parse.quote(sql)}"
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _project_from_transcript(path: str, session_dir: str) -> str:
    """Project label = Path(cwd).name, matching the live hook. cwd is on transcript lines."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = o.get("cwd")
                if cwd:
                    return Path(cwd).name
    except OSError:
        pass
    # Fallback: decode the dir name (….-Desktop-Claude-Tele -> last segment-ish)
    return session_dir.split("-")[-1] or "unknown"


def _fmt_ts(iso: str) -> str:
    """'2026-05-31T18:16:18.645Z' -> ClickHouse DateTime64 'YYYY-MM-DD HH:MM:SS.ffffff' (UTC)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def build_rows() -> tuple[list[dict], dict[str, set], dict[str, float]]:
    """Return (metric rows, {sid: set(message_ids)}, {sid: span_seconds}) across transcripts."""
    rows: list[dict] = []
    state: dict[str, set] = {}
    spans: dict[str, float] = {}

    for path in glob.glob(str(PROJECTS_DIR / "*" / "*.jsonl")):
        session_id = Path(path).stem
        session_dir = Path(path).parent.name
        records = parse_transcript(path)
        if not records:
            continue
        state.setdefault(session_id, set()).update(records.keys())
        spans[session_id] = transcript_span_seconds(path)
        project = _project_from_transcript(path, session_dir)

        # group by model
        by_model: dict[str, dict] = {}
        for r in records.values():
            agg = by_model.setdefault(r["model"], {"input": 0, "output": 0, "cache_read": 0,
                                                    "cache_creation": 0, "cache_creation_5m": 0,
                                                    "cache_creation_1h": 0, "max_ts": ""})
            for k in ("input", "output", "cache_read", "cache_creation", "cache_creation_5m", "cache_creation_1h"):
                agg[k] += r[k]
            if r["ts"] > agg["max_ts"]:
                agg["max_ts"] = r["ts"]

        for model, agg in by_model.items():
            ts = _fmt_ts(agg["max_ts"])
            attrs_base = {"model": model, "session_id": session_id, "project": project}
            for label, key in _TYPES.items():
                if agg[key] <= 0:
                    continue
                rows.append(_metric_row("claude_code.token.usage", "",
                                        {**attrs_base, "token_type": label}, agg[key], ts, session_id))
            cost = _cost_usd(model, agg["input"], agg["output"], agg["cache_read"],
                             agg["cache_creation_5m"], agg["cache_creation_1h"])
            if cost > 0:
                rows.append(_metric_row("claude_code.cost.usage", "USD", attrs_base, cost, ts, session_id))

        # One duration row per session (value = total wall-clock seconds), at session end.
        span = spans.get(session_id, 0.0)
        if span > 0:
            end_ts = _fmt_ts(max((r["max_ts"] for r in by_model.values()), default=""))
            rows.append(_metric_row("claude_code.session.duration", "s",
                                    {"session_id": session_id, "project": project}, span, end_ts, session_id))
    return rows, state, spans


def _metric_row(name: str, unit: str, attrs: dict, value: float, ts: str, session_id: str) -> dict:
    return {
        "ResourceAttributes": {"service.name": "claude-code", "session.id": session_id},
        "ServiceName": "claude-code",
        "ScopeName": "claude-code.backfill",
        "MetricName": name,
        "MetricUnit": unit,
        "Attributes": attrs,
        "StartTimeUnix": ts,
        "TimeUnix": ts,
        "Value": value,
        "Flags": 0,
        "AggregationTemporality": 2,
        "IsMonotonic": True,
    }


def main() -> None:
    dry = "--dry-run" in sys.argv
    rows, state, spans = build_rows()
    sessions = len(state)
    cost_rows = [r for r in rows if r["MetricName"] == "claude_code.cost.usage"]
    total_cost = sum(r["Value"] for r in cost_rows)
    print(f"Parsed {sessions} sessions -> {len(rows)} metric rows, "
          f"{len(cost_rows)} cost rows, total ${total_cost:.4f}")

    if dry:
        for r in cost_rows[:10]:
            print(f"  {r['TimeUnix']}  {r['Attributes']['project']:20.20}  "
                  f"{r['Attributes']['model']:22.22}  ${r['Value']:.5f}")
        print("dry-run: nothing written.")
        return

    # Idempotency guard: backfill is a full rebuild from the transcripts (the source
    # of truth), so clear the metrics it owns before inserting. Without this, re-running
    # silently doubles every historical row. --append skips the clear if you really mean to.
    if "--append" not in sys.argv:
        print("Clearing existing claude_code.* metric rows (full rebuild)...")
        _ch_execute("ALTER TABLE otel.otel_metrics_sum DELETE WHERE MetricName LIKE 'claude_code.%' SETTINGS mutations_sync=1")

    body = "\n".join(json.dumps(r) for r in rows)
    _ch_execute("INSERT INTO otel.otel_metrics_sum FORMAT JSONEachRow", body)
    print(f"Inserted {len(rows)} rows into otel.otel_metrics_sum.")

    # Mark every backfilled message as emitted (and record the span) so the live
    # hook won't re-emit tokens or duration it has already accounted for.
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    for sid, ids in state.items():
        (TEMP_DIR / f"tokens_state_{sid}.json").write_text(json.dumps({
            "emitted_ids": sorted(ids),
            "last_span_seconds": round(spans.get(sid, 0.0), 3),
        }))
    print(f"Wrote {len(state)} session state files to {TEMP_DIR}.")


if __name__ == "__main__":
    main()
