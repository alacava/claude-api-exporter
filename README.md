# Anthropic API Usage & Cost Exporter

[![Docker Hub](https://img.shields.io/docker/v/antlac1/claude-api-exporter?label=Docker%20Hub&logo=docker)](https://hub.docker.com/r/antlac1/claude-api-exporter)

A Prometheus exporter for Anthropic Claude API spend. Polls the **Admin Usage API**
per API key + model (daily buckets), computes estimated USD from an editable
pricing table, and pulls the **Cost API** at workspace level for invoice
reconciliation. Exposes `/metrics` over HTTP for scraping.

## Why estimate cost per key?

The Cost API only groups by `workspace_id` / `description` — **not** by API key.
So actual billed dollars are available per workspace, while per-key cost is
derived from per-key token counts × your pricing table. Two ways to use it:

- **True per-key dollars:** put each key (or logical group) in its own workspace
  and rely on `anthropic_billed_cost_usd`.
- **Estimated per-key dollars:** use `anthropic_estimated_cost_usd` (token proxy),
  reconciled against `anthropic_billed_cost_usd` at workspace level.

## Setup

1. An **org admin** creates an Admin API key (`sk-ant-admin01-...`) in the Claude
   Console. This is distinct from a regular API key.
2. `cp .env.example .env` and set `ANTHROPIC_ADMIN_KEY`.
3. `docker compose up -d`
4. Check `curl localhost:9402/metrics`.

The image is published to Docker Hub at
[`antlac1/claude-api-exporter`](https://hub.docker.com/r/antlac1/claude-api-exporter)
and is pulled automatically by `docker-compose.yml`.
Pricing is fetched from the Anthropic docs at build time — the bundled
`pricing.json` is kept as a fallback and can be mounted read-only to override
rates without rebuilding.

To build locally instead of pulling from Docker Hub:

```bash
docker compose up -d --build   # overrides image: with a local build
# or
docker build -t antlac1/claude-api-exporter .
```

## GitHub Actions / Docker Hub

The image is built and pushed automatically on every push to `main`, producing
two tags: `:latest` and a date-stamped tag (e.g. `:2026.06.20`). To set this
up on your own fork:

1. Go to **Settings → Secrets and variables → Actions** in your GitHub repo.
2. Add two repository secrets:
   - `DOCKERHUB_USERNAME` — your Docker Hub username
   - `DOCKERHUB_TOKEN` — a Docker Hub [access token](https://hub.docker.com/settings/security)

## Metrics

| Metric | Labels | Meaning |
| --- | --- | --- |
| `anthropic_usage_tokens` | api_key_id, api_key_name, model, token_type, inference_geo, date | Tokens per day bucket (`token_type`: input/output/cache_write/cache_read) |
| `anthropic_estimated_cost_usd` | api_key_id, api_key_name, model, inference_geo, date | Estimated USD from token counts × pricing |
| `anthropic_billed_cost_usd` | workspace_id, description, date | Billed USD from the Cost API (reconciliation) |
| `anthropic_exporter_up` | — | 1 if last poll succeeded |
| `anthropic_exporter_last_success_timestamp_seconds` | — | Last successful poll time |
| `anthropic_exporter_poll_errors_total` | — | Failed poll counter |

Notes: Workbench usage has `api_key_id="none"`; default-workspace cost has
`workspace_id="default"`. Data lands within ~5 min; the API allows ~1 poll/min,
so the 300s default is comfortable.

## Prometheus scrape config

```yaml
scrape_configs:
  - job_name: anthropic
    scrape_interval: 300s
    static_configs:
      - targets: ["anthropic-exporter:9402"]   # or host:9402 if not on the same network
```

## Grafana query examples

Daily cost per key (stacked):
```promql
sum by (api_key_name) (anthropic_estimated_cost_usd)
```

Estimated vs. billed at workspace level (sanity check):
```promql
sum(anthropic_estimated_cost_usd)      # estimated total
sum(anthropic_billed_cost_usd)         # billed total
```

Cache hit ratio per key:
```promql
sum by (api_key_name) (anthropic_usage_tokens{token_type="cache_read"})
/
sum by (api_key_name) (anthropic_usage_tokens{token_type=~"input|cache_read"})
```

Alert on staleness:
```promql
time() - anthropic_exporter_last_success_timestamp_seconds > 1800
```

## Configuration (env)

| Var | Default | Notes |
| --- | --- | --- |
| `ANTHROPIC_ADMIN_KEY` | — | Required |
| `EXPORTER_PORT` | 9402 | HTTP /metrics port |
| `POLL_INTERVAL_SECONDS` | 300 | Keep ≥ 60 |
| `LOOKBACK_DAYS` | 2 | Re-polls recent days so late/revised data updates the right `date` |
| `COST_IN_CENTS` | true | Cost API returns lowest units (cents); divides by 100 |
| `PRICING_FILE` | /app/pricing.json | Mounted RO |

## Extending the token-field map

If logs show `Unmapped token field` (set `LOG_LEVEL=DEBUG`), add the field name
to `TOKEN_FIELD_MAP` in `exporter.py` mapping it to one of:
`input | output | cache_write | cache_read`.
