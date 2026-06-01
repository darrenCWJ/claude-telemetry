# claude-telemetry

Self-hosted telemetry for [Claude Code](https://claude.com/claude-code): capture
token usage and cost per session, ship it through an OpenTelemetry collector into
ClickHouse, and explore it in a Grafana dashboard.

```
Claude Code hooks ──OTLP/HTTP──▶ OTEL Collector ──▶ ClickHouse ──▶ Grafana
   (.claude/hooks)                 (:4318)            (:9000)        (:3000)
```

## What's inside

| Path | Purpose |
|------|---------|
| `.claude/hooks/claude_hook.py` | Claude Code hook — emits token/cost metrics and tool spans over OTLP. Drop into any project. |
| `.claude/settings.json` | Wires the hook into the `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop` events. |
| `docker-compose.yml` | ClickHouse + OTEL Collector + Grafana stack. |
| `otel-collector-config.yaml` | Collector pipeline → ClickHouse exporter (30-day TTL). |
| `grafana/` | Provisioned ClickHouse datasource + the Claude analytics dashboard. |
| `backfill.py` | One-time import of historical token/cost from existing transcripts. |
| `sdk/` | OTEL instrumentation wrapper for direct Anthropic SDK apps. |
| `hooks/claude_hook.py` | Reference copy of the hook (same file you place under `.claude/hooks/`). |

## Prerequisites

- Docker + Docker Compose
- Python 3.10+ (for `backfill.py` and the SDK wrapper)
- Claude Code (for the hooks to fire)

## Setup

1. **Create your `.env`** from the template and set real passwords:

   ```bash
   cp .env.example .env
   # edit .env — set CLICKHOUSE_PASSWORD and GRAFANA_ADMIN_PASSWORD
   ```

   | Var | Used by | Default |
   |-----|---------|---------|
   | `CLICKHOUSE_PASSWORD` | ClickHouse, collector, Grafana datasource | `otel_secret` |
   | `OTEL_ENDPOINT` | hooks + SDK | `http://localhost:4318` |
   | `GRAFANA_ADMIN_PASSWORD` | Grafana admin login | `admin` |

2. **Start the stack:**

   ```bash
   docker compose up -d
   ```

   | Service | URL |
   |---------|-----|
   | Grafana | http://localhost:3000 (login `admin` / your `GRAFANA_ADMIN_PASSWORD`) |
   | ClickHouse HTTP | http://localhost:8123 |
   | OTEL Collector (OTLP HTTP) | http://localhost:4318 |

   The collector auto-creates the ClickHouse schema on first run.

3. **Install the hook into a project** you want to track — copy `.claude/hooks/claude_hook.py`
   and the hook block from `.claude/settings.json` into that project's `.claude/`.
   New Claude Code sessions then emit metrics automatically.

## Backfill historical sessions

The Stop hook only records sessions going forward. To import token/cost from
transcripts that predate the hook:

```bash
pip install -r requirements.txt
python backfill.py --dry-run   # preview what would be inserted
python backfill.py             # write to ClickHouse at each session's real timestamp
```

It reads `CLICKHOUSE_HTTP`, `CLICKHOUSE_USER`, and `CLICKHOUSE_PASSWORD` from the
environment (defaults: `http://localhost:8123`, `otel` / `otel_secret`).

## Dashboard

Grafana ships with the **Claude analytics** dashboard pre-provisioned
(`grafana/dashboards/claude-analytics.json`): cost over time, tokens and cost by
model, usage by hour, tool-call breakdown, and per-session totals.

## Configuration notes

- The hook is configured entirely via env vars — `OTEL_ENDPOINT` and
  `CLAUDE_OTEL_MAX_CONTENT_LEN` (max chars of tool input/output captured in spans,
  default `2000`).
- ClickHouse retention is 30 days (`ttl: 720h` in `otel-collector-config.yaml`).
- Never commit `.env` — it holds real credentials and is gitignored.
