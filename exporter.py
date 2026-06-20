#!/usr/bin/env python3
"""
Anthropic API usage & cost Prometheus exporter.

Polls the Admin Usage API (per API key + model, daily buckets), computes an
estimated USD cost from a configurable pricing table, and also pulls the Cost
API at workspace granularity for reconciliation against the actual invoice.

Exposes Prometheus metrics on an HTTP /metrics endpoint for scraping.

Docs: https://platform.claude.com/docs/en/manage-claude/usage-cost-api
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from prometheus_client import Counter, Gauge, start_http_server

# --------------------------------------------------------------------------- #
# Configuration (env-driven)
# --------------------------------------------------------------------------- #
ADMIN_KEY = os.environ.get("ANTHROPIC_ADMIN_KEY", "").strip()
API_BASE = os.environ.get("ANTHROPIC_API_BASE", "https://api.anthropic.com").rstrip("/")
API_VERSION = os.environ.get("ANTHROPIC_API_VERSION", "2023-06-01")
PORT = int(os.environ.get("EXPORTER_PORT", "9402"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))
PRICING_FILE = os.environ.get("PRICING_FILE", "/app/pricing.json")
# Cost API returns decimal strings in lowest units (cents) per Anthropic docs.
COST_IN_CENTS = os.environ.get("COST_IN_CENTS", "true").lower() in ("1", "true", "yes")
USER_AGENT = os.environ.get(
    "EXPORTER_USER_AGENT", "anthropic-prom-exporter/1.0.0 (+https://github.com/)"
)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("anthropic-exporter")

# --------------------------------------------------------------------------- #
# Token field -> pricing category normalization
#
# The usage endpoint reports several token categories; field names can vary and
# cache-creation may arrive nested. We map everything onto four billing buckets:
#   input | output | cache_write | cache_read
# Unrecognized *token* fields are logged so you can extend the map.
# --------------------------------------------------------------------------- #
TOKEN_FIELD_MAP = {
    "uncached_input_tokens": "input",
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache_read",
    "cached_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_write",
    "cache_creation_tokens": "cache_write",
    # nested cache_creation breakdown
    "ephemeral_5m_input_tokens": "cache_write",
    "ephemeral_1h_input_tokens": "cache_write",
}
CATEGORIES = ("input", "output", "cache_write", "cache_read")

# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
TOKENS = Gauge(
    "anthropic_usage_tokens",
    "Tokens consumed in the day bucket, by key/model/type/geo.",
    ["api_key_id", "api_key_name", "model", "token_type", "inference_geo", "date"],
)
EST_COST = Gauge(
    "anthropic_estimated_cost_usd",
    "Estimated USD cost computed from token counts and the pricing table.",
    ["api_key_id", "api_key_name", "model", "inference_geo", "date"],
)
BILLED_COST = Gauge(
    "anthropic_billed_cost_usd",
    "Billed USD cost from the Cost API (workspace granularity, for reconciliation).",
    ["workspace_id", "description", "date"],
)
LAST_POLL = Gauge(
    "anthropic_exporter_last_success_timestamp_seconds",
    "Unix timestamp of the last fully successful poll cycle.",
)
UP = Gauge("anthropic_exporter_up", "1 if the last poll cycle succeeded, else 0.")
ERRORS = Counter(
    "anthropic_exporter_poll_errors_total", "Total number of failed poll cycles."
)
MONTHLY_EST_COST = Gauge(
    "anthropic_monthly_estimated_cost_usd",
    "Estimated USD cost for the current calendar month, by API key.",
    ["api_key_id", "api_key_name"],
)
MONTHLY_BILLED_COST = Gauge(
    "anthropic_monthly_billed_cost_usd",
    "Billed USD cost for the current calendar month (workspace level).",
    ["workspace_id"],
)


# --------------------------------------------------------------------------- #
# Anthropic Admin API client
# --------------------------------------------------------------------------- #
class AdminClient:
    def __init__(self, key: str):
        if not key:
            raise SystemExit("ANTHROPIC_ADMIN_KEY is required (sk-ant-admin01-...).")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-api-key": key,
                "anthropic-version": API_VERSION,
                "User-Agent": USER_AGENT,
            }
        )

    def get_paginated(self, path: str, params: list[tuple[str, str]]) -> list[dict]:
        """GET an admin endpoint, following has_more/next_page pagination."""
        out: list[dict] = []
        page: str | None = None
        while True:
            q = list(params)
            if page:
                q.append(("page", page))
            resp = self.session.get(f"{API_BASE}{path}", params=q, timeout=60)
            resp.raise_for_status()
            body = resp.json()
            out.extend(body.get("data", []))
            if body.get("has_more") and body.get("next_page"):
                page = body["next_page"]
            else:
                break
        return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_pricing() -> dict:
    try:
        with open(PRICING_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        log.info("Loaded pricing for %d model(s) from %s",
                 len(data.get("models", {})), PRICING_FILE)
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not load pricing file %s (%s); cost estimates disabled.",
                  PRICING_FILE, exc)
        return {"models": {}, "us_multiplier": 1.0}


def window():
    """Return (starting_at, ending_at) ISO8601 Z covering LOOKBACK_DAYS + today."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=LOOKBACK_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def monthly_window():
    """Return (starting_at, ending_at) covering the 1st of the current month to now."""
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def to_date(iso_ts: str) -> str:
    """Extract YYYY-MM-DD from an ISO timestamp; fall back to the raw string."""
    try:
        return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return (iso_ts or "unknown")[:10]


def extract_tokens(result: dict) -> dict[str, float]:
    """Walk a result entry, summing token fields into the four categories."""
    totals = {c: 0.0 for c in CATEGORIES}

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                elif "token" in k.lower() and isinstance(v, (int, float)):
                    cat = TOKEN_FIELD_MAP.get(k)
                    if cat:
                        totals[cat] += v
                    else:
                        log.debug("Unmapped token field %r=%s", k, v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(result)
    return totals


def estimate_cost(model: str, tokens: dict[str, float], geo: str, pricing: dict) -> float:
    models = pricing.get("models", {})
    rates = models.get(model) or models.get("default")
    if not rates:
        log.warning("No pricing for model %r and no default; cost=0.", model)
        return 0.0
    cost = sum((tokens.get(cat, 0.0) / 1_000_000.0) * float(rates.get(cat, 0.0))
               for cat in CATEGORIES)
    if geo == "us":
        cost *= float(pricing.get("us_multiplier", 1.1))
    return cost


# --------------------------------------------------------------------------- #
# Poll cycle
# --------------------------------------------------------------------------- #
def fetch_key_names(client: AdminClient) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        for key in client.get_paginated("/v1/organizations/api_keys", [("limit", "100")]):
            if key.get("id"):
                names[key["id"]] = key.get("name") or key["id"]
    except requests.RequestException as exc:
        log.warning("Could not list API keys (names will fall back to IDs): %s", exc)
    return names


def poll_once(client: AdminClient, pricing: dict) -> None:
    start, end = window()
    key_names = fetch_key_names(client)

    # ---- Usage: per api_key_id + model + inference_geo, daily buckets ----
    usage_params = [
        ("starting_at", start),
        ("ending_at", end),
        ("bucket_width", "1d"),
        ("group_by[]", "api_key_id"),
        ("group_by[]", "model"),
        ("group_by[]", "inference_geo"),
        ("limit", "31"),
    ]
    buckets = client.get_paginated(
        "/v1/organizations/usage_report/messages", usage_params
    )

    TOKENS.clear()
    EST_COST.clear()
    for bucket in buckets:
        date = to_date(bucket.get("starting_at", ""))
        for res in bucket.get("results", []):
            key_id = res.get("api_key_id") or "none"
            model = res.get("model") or "unknown"
            geo = res.get("inference_geo") or "not_available"
            name = key_names.get(key_id, key_id)
            tokens = extract_tokens(res)

            for cat in CATEGORIES:
                if tokens[cat]:
                    TOKENS.labels(key_id, name, model, cat, geo, date).set(tokens[cat])

            cost = estimate_cost(model, tokens, geo, pricing)
            if cost:
                EST_COST.labels(key_id, name, model, geo, date).set(cost)

    # ---- Cost: workspace + description, daily (reconciliation) ----
    cost_params = [
        ("starting_at", start),
        ("ending_at", end),
        ("group_by[]", "workspace_id"),
        ("group_by[]", "description"),
    ]
    BILLED_COST.clear()
    try:
        cost_buckets = client.get_paginated("/v1/organizations/cost_report", cost_params)
        for bucket in cost_buckets:
            date = to_date(bucket.get("starting_at", ""))
            for res in bucket.get("results", []):
                ws = res.get("workspace_id") or "default"
                desc = res.get("description") or "unknown"
                raw = res.get("amount", res.get("cost", 0))
                try:
                    usd = float(raw) / (100.0 if COST_IN_CENTS else 1.0)
                except (TypeError, ValueError):
                    usd = 0.0
                if usd:
                    BILLED_COST.labels(ws, desc, date).set(usd)
    except requests.RequestException as exc:
        # Cost endpoint may be unavailable (e.g. Claude Platform on AWS); don't
        # fail the whole cycle over reconciliation data.
        log.warning("Cost report fetch failed (continuing): %s", exc)

    # ---- Monthly: current-month totals per API key and workspace ----
    m_start, m_end = monthly_window()
    monthly_usage = client.get_paginated(
        "/v1/organizations/usage_report/messages",
        [
            ("starting_at", m_start),
            ("ending_at", m_end),
            ("bucket_width", "1d"),
            ("group_by[]", "api_key_id"),
            ("group_by[]", "model"),
            ("group_by[]", "inference_geo"),
            ("limit", "31"),
        ],
    )
    monthly_costs: dict[str, float] = {}
    for bucket in monthly_usage:
        for res in bucket.get("results", []):
            key_id = res.get("api_key_id") or "none"
            model  = res.get("model") or "unknown"
            geo    = res.get("inference_geo") or "not_available"
            monthly_costs[key_id] = (
                monthly_costs.get(key_id, 0.0)
                + estimate_cost(model, extract_tokens(res), geo, pricing)
            )
    MONTHLY_EST_COST.clear()
    for key_id, total in monthly_costs.items():
        MONTHLY_EST_COST.labels(key_id, key_names.get(key_id, key_id)).set(total)

    try:
        monthly_billed: dict[str, float] = {}
        for bucket in client.get_paginated(
            "/v1/organizations/cost_report",
            [("starting_at", m_start), ("ending_at", m_end), ("group_by[]", "workspace_id")],
        ):
            for res in bucket.get("results", []):
                ws  = res.get("workspace_id") or "default"
                raw = res.get("amount", res.get("cost", 0))
                try:
                    usd = float(raw) / (100.0 if COST_IN_CENTS else 1.0)
                except (TypeError, ValueError):
                    usd = 0.0
                monthly_billed[ws] = monthly_billed.get(ws, 0.0) + usd
        MONTHLY_BILLED_COST.clear()
        for ws, total in monthly_billed.items():
            MONTHLY_BILLED_COST.labels(ws).set(total)
    except requests.RequestException as exc:
        log.warning("Monthly cost report fetch failed (continuing): %s", exc)

    LAST_POLL.set(time.time())
    log.info("Poll OK: %d usage bucket(s) across %d key(s).",
             len(buckets), len(key_names) or 0)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
_stop = threading.Event()


def _handle_signal(signum, _frame):
    log.info("Received signal %s, shutting down.", signum)
    _stop.set()


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    client = AdminClient(ADMIN_KEY)
    pricing = load_pricing()

    start_http_server(PORT)
    log.info("Exporter listening on :%d/metrics (poll every %ds, lookback %dd).",
             PORT, POLL_INTERVAL, LOOKBACK_DAYS)

    while not _stop.is_set():
        try:
            poll_once(client, pricing)
            UP.set(1)
        except requests.HTTPError as exc:
            UP.set(0)
            ERRORS.inc()
            body = exc.response.text[:300] if exc.response is not None else ""
            log.error("HTTP error during poll: %s %s", exc, body)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            UP.set(0)
            ERRORS.inc()
            log.exception("Poll cycle failed: %s", exc)
        _stop.wait(POLL_INTERVAL)

    log.info("Stopped.")


if __name__ == "__main__":
    main()
