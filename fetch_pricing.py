#!/usr/bin/env python3
"""
Fetch current Anthropic model pricing from the docs page and update pricing.json.
Run at container build time and daily via GitHub Actions. Falls back gracefully
on any error so a build/run never breaks due to a transient network issue or a
docs page redesign.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

PRICING_URL = os.environ.get(
    "ANTHROPIC_PRICING_URL",
    "https://platform.claude.com/docs/en/about-claude/pricing.md",
)
PRICING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")
TIMEOUT = 20

# The "## Model pricing" table lists models by display name, not API model ID,
# so a name -> ID mapping has to be maintained by hand. Verified against
# https://platform.claude.com/docs/en/about-claude/models/overview and each
# model's own /docs/en/models/<slug>/overview page. Pre-4.6-generation models
# get both their dateless alias and dated snapshot ID, since usage/cost
# reports may reference either depending on which one a caller used.
NAME_TO_MODEL_IDS = {
    "Claude Fable 5.1": ["claude-fable-5-1"],
    "Claude Mythos 5.1": ["claude-mythos-5-1"],
    "Claude Fable 5": ["claude-fable-5"],
    "Claude Mythos 5": ["claude-mythos-5"],
    "Claude Opus 5": ["claude-opus-5"],
    "Claude Opus 4.8": ["claude-opus-4-8"],
    "Claude Opus 4.7": ["claude-opus-4-7"],
    "Claude Opus 4.6": ["claude-opus-4-6"],
    "Claude Opus 4.5": ["claude-opus-4-5", "claude-opus-4-5-20251101"],
    "Claude Opus 4.1": ["claude-opus-4-1", "claude-opus-4-1-20250805"],
    "Claude Opus 4": ["claude-opus-4-0", "claude-opus-4-20250514"],
    "Claude Sonnet 5": ["claude-sonnet-5"],
    "Claude Sonnet 4.6": ["claude-sonnet-4-6"],
    "Claude Sonnet 4.5": ["claude-sonnet-4-5", "claude-sonnet-4-5-20250929"],
    "Claude Sonnet 4": ["claude-sonnet-4-0", "claude-sonnet-4-20250514"],
    "Claude Haiku 4.5": ["claude-haiku-4-5", "claude-haiku-4-5-20251001"],
    "Claude Haiku 3.5": ["claude-3-5-haiku-20241022"],
}


def fetch_page(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "anthropic-prom-exporter/fetch-pricing (build-time)"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def parse_models(text: str) -> dict:
    """
    Parse the "## Model pricing" table:

      | Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |

    Returns {model_id: {input, output, cache_write, cache_read}}, where
    cache_write is the 5-minute-TTL write price and cache_read is the actual
    cache-hit price from the table (not a derived multiplier, since it varies
    by model, e.g. 0.025x on Fable/Mythos 5.1 vs the standard 0.1x).
    """
    section_match = re.search(r"## Model pricing\n(.*?)\n## ", text, re.S)
    if not section_match:
        return {}
    section = section_match.group(1)

    models = {}
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        if cells[0].lower() == "model" or set(cells[0]) <= set("- "):
            continue

        name = re.sub(r"\s*\(.*$", "", cells[0]).strip()

        prices = []
        for cell in cells[1:6]:
            m = re.search(r"\$([\d.]+)", cell)
            if not m:
                prices = None
                break
            prices.append(float(m.group(1)))
        if prices is None:
            continue
        input_price, cache_write, _cache_write_1h, cache_read, output_price = prices

        model_ids = NAME_TO_MODEL_IDS.get(name)
        if not model_ids:
            print(
                f"fetch_pricing: WARNING - unmapped model {name!r} in pricing table; "
                "add it to NAME_TO_MODEL_IDS in fetch_pricing.py",
                file=sys.stderr,
            )
            continue

        for model_id in model_ids:
            models[model_id] = {
                "input": input_price,
                "output": output_price,
                "cache_write": cache_write,
                "cache_read": cache_read,
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
        print(f"fetch_pricing: WARNING - could not fetch pricing page: {exc}", file=sys.stderr)
        print("fetch_pricing: keeping existing pricing.json unchanged.", file=sys.stderr)
        return

    models = parse_models(text)
    if not models:
        print(
            "fetch_pricing: WARNING - no model prices parsed from page; "
            "keeping existing pricing.json unchanged.",
            file=sys.stderr,
        )
        return

    existing = load_existing()
    merged = existing.get("models", {})
    merged.update(models)   # docs-fetched values win; hand-edited keys not in docs are kept
    existing["models"] = merged
    existing["_comment"] = (
        f"USD per MILLION tokens. Auto-fetched from {PRICING_URL}. "
        "cache_write = 5-min TTL write price; cache_read = cache-hit price, both taken "
        "directly from the docs table. us_multiplier applies when inference_geo == 'us' "
        "on newer models."
    )

    with open(PRICING_FILE, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(
        f"fetch_pricing: updated pricing.json - "
        f"{len(models)} model ID(s) from docs: {', '.join(sorted(models))}"
    )


if __name__ == "__main__":
    main()
