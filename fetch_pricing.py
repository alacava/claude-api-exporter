#!/usr/bin/env python3
"""
Fetch current Anthropic model pricing from the docs page and update pricing.json.
Intended to run at container build time. Falls back gracefully on any error so
the build never breaks due to a transient network issue.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

PRICING_URL = os.environ.get(
    "ANTHROPIC_PRICING_URL",
    "https://platform.claude.com/docs/en/pricing.md",
)
PRICING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
TIMEOUT = 20

# Standard Anthropic cache-pricing multipliers (applied to input $/1M)
CACHE_WRITE_MULT = 1.25   # 5-min TTL write
CACHE_READ_MULT = 0.10    # cache read hit


def fetch_page(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "anthropic-prom-exporter/fetch-pricing (build-time)"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def parse_models(text: str) -> dict:
    """
    Scan markdown for table rows that contain a backtick-quoted ``claude-*`` model ID
    and at least two ``$N.NN`` dollar amounts (input, output).

    Expected column layout (indices are flexible):
      Model Name | Model ID | Context | Input $/1M | Output $/1M
    """
    models = {}
    for line in text.splitlines():
        m_id = re.search(r'`(claude-[a-z0-9.\-]+)`', line)
        if not m_id:
            continue
        model_id = m_id.group(1)

        prices = re.findall(r'\$(\d+(?:\.\d+)?)', line)
        if len(prices) < 2:
            continue

        try:
            # Last two dollar amounts in the row are input / output
            input_price = float(prices[-2])
            output_price = float(prices[-1])
        except (ValueError, IndexError):
            continue

        models[model_id] = {
            "input": input_price,
            "output": output_price,
            "cache_write": round(input_price * CACHE_WRITE_MULT, 6),
            "cache_read": round(input_price * CACHE_READ_MULT, 6),
        }

    return models


def load_existing() -> dict:
    try:
        with open(PRICING_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"models": {}, "us_multiplier": 1.1}


def main() -> None:
    print(f"fetch_pricing: fetching {PRICING_URL}", flush=True)

    try:
        text = fetch_page(PRICING_URL)
    except (urllib.error.URLError, OSError, Exception) as exc:
        print(f"fetch_pricing: WARNING – could not fetch pricing page: {exc}", file=sys.stderr)
        print("fetch_pricing: keeping existing pricing.json unchanged.", file=sys.stderr)
        return

    models = parse_models(text)
    if not models:
        print(
            "fetch_pricing: WARNING – no model prices parsed from page; "
            "keeping existing pricing.json unchanged.",
            file=sys.stderr,
        )
        return

    existing = load_existing()
    merged = existing.get("models", {})
    merged.update(models)   # docs-fetched values win; hand-edited keys not in docs are kept
    existing["models"] = merged
    existing["_comment"] = (
        f"USD per MILLION tokens. Auto-fetched from {PRICING_URL} at build time. "
        "cache_write = 1.25x input (5-min TTL); cache_read = 0.1x input. "
        "us_multiplier applies when inference_geo == 'us' on newer models."
    )

    with open(PRICING_FILE, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2)
        fh.write("\n")

    print(
        f"fetch_pricing: updated pricing.json – "
        f"{len(models)} model(s) from docs: {', '.join(sorted(models))}"
    )


if __name__ == "__main__":
    main()
