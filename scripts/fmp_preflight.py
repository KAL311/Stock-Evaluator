#!/usr/bin/env python3
"""FMP endpoint preflight.  Probes each candidate ownership endpoint against
AAPL on the live key and reports HTTP status, plan-restriction messages, and
which JSON fields carry the values the screener needs.

Usage:
  python scripts/fmp_preflight.py

The report exits 0 regardless of individual endpoint results — the caller
uses the printed summary to decide which endpoints the fetcher should call.
"""
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / "FMP_API.env"
FMP_BASE = "https://financialmodelingprep.com"
TIMEOUT = 15
PROBE_TICKER = "AAPL"


def load_api_key():
    key = os.environ.get("FMP_API_KEY")
    if key:
        return key.strip()
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            _, val = line.split("=", 1)
            val = val.strip().strip('"').strip("'")
            if val:
                return val
    return None


CANDIDATES = [
    # (label, path, query params, category)
    # Insider ownership candidates
    ("insider_ownership_acquisition_ratio", "/stable/insider-trading/statistics",
     {"symbol": PROBE_TICKER}, "insider_own"),
    ("insider_trading_latest", "/stable/insider-trading/latest",
     {"symbol": PROBE_TICKER, "page": 0, "limit": 5}, "insider_own"),
    ("shares_float_freefloat", "/stable/shares-float",
     {"symbol": PROBE_TICKER}, "insider_own_proxy"),

    # Institutional ownership candidates
    ("institutional_ownership_latest", "/stable/institutional-ownership/latest",
     {"symbol": PROBE_TICKER, "page": 0, "limit": 5}, "inst_own"),
    ("institutional_ownership_extract",
     "/stable/institutional-ownership/extract",
     {"symbol": PROBE_TICKER, "year": 2026, "quarter": 1}, "inst_own"),
    ("institutional_ownership_holder_performance_summary",
     "/stable/institutional-ownership/holder-performance-summary",
     {"symbol": PROBE_TICKER, "page": 0}, "inst_own"),
    ("institutional_ownership_symbol_positions_summary",
     "/stable/institutional-ownership/symbol-positions-summary",
     {"symbol": PROBE_TICKER, "year": 2026, "quarter": 1}, "inst_own"),
    ("holders_institutional", "/stable/holders/institutional",
     {"symbol": PROBE_TICKER}, "inst_own"),

    # Short interest candidates
    ("short_interest_latest", "/stable/short-interest/latest",
     {"symbol": PROBE_TICKER, "page": 0, "limit": 5}, "short_float"),
    ("short_interest", "/stable/short-interest",
     {"symbol": PROBE_TICKER}, "short_float"),
    ("stock_short_interest", "/stable/stock/short-interest",
     {"symbol": PROBE_TICKER}, "short_float"),
    ("shares_short_quote", "/stable/quote", {"symbol": PROBE_TICKER},
     "short_float_proxy"),

    # Company profile fallback
    ("profile", "/stable/profile", {"symbol": PROBE_TICKER}, "profile"),
]


def probe(path, params, key):
    p = {**params, "apikey": key}
    try:
        r = requests.get(FMP_BASE + path, params=p, timeout=TIMEOUT)
    except requests.RequestException as e:
        return {"status": -1, "error": str(e), "text": ""}
    text = r.text
    body = None
    try:
        body = r.json()
    except ValueError:
        body = None
    is_error_msg = False
    tier_restricted = False
    if isinstance(body, dict) and "Error Message" in body:
        is_error_msg = True
        msg = body["Error Message"]
        if "Legacy" in msg or "Restricted" in msg or "upgrade" in msg.lower():
            tier_restricted = True
    if r.status_code == 402:
        tier_restricted = True
    return {
        "status": r.status_code,
        "text": text[:400],
        "body": body,
        "restricted": tier_restricted,
        "is_error_msg": is_error_msg,
    }


def summarize_body(body):
    if body is None:
        return "no JSON"
    if isinstance(body, list):
        if not body:
            return "empty list"
        first = body[0] if isinstance(body[0], dict) else {}
        keys = sorted(first.keys()) if isinstance(first, dict) else []
        return f"list[{len(body)}], first-keys={keys[:12]}"
    if isinstance(body, dict):
        keys = sorted(body.keys())
        return f"dict keys={keys[:12]}"
    return f"scalar={body!r}"


def main():
    key = load_api_key()
    if not key:
        print("ERROR: FMP_API_KEY not set (env or FMP_API.env).", file=sys.stderr)
        sys.exit(1)

    print(f"FMP preflight against {PROBE_TICKER}, base={FMP_BASE}")
    print("=" * 80)

    passed = []
    restricted = []
    failed = []

    for label, path, params, category in CANDIDATES:
        res = probe(path, params, key)
        status = res["status"]
        if res["restricted"]:
            verdict = "RESTRICTED (tier gate)"
            restricted.append((category, label, path))
        elif status == 200 and isinstance(res["body"], list) and res["body"]:
            verdict = "PASS"
            passed.append((category, label, path, res["body"]))
        elif status == 200 and isinstance(res["body"], dict) and res["body"] \
                and not res["is_error_msg"]:
            verdict = "PASS (dict)"
            passed.append((category, label, path, res["body"]))
        elif status == 200:
            verdict = "EMPTY (200 but no data)"
            failed.append((category, label, path, "empty"))
        else:
            verdict = f"FAIL ({status})"
            failed.append((category, label, path, res["text"][:120]))

        body_summary = summarize_body(res["body"])
        print(f"[{verdict}] {label}")
        print(f"    path: {path}  params: {params}")
        print(f"    body: {body_summary}")
        if res["restricted"]:
            msg = res["body"].get("Error Message") if isinstance(res["body"], dict) else res["text"][:200]
            print(f"    note: {msg[:200]}")
        print()

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)

    def cat_passed(cat):
        return [p for p in passed if p[0] == cat]

    for cat in ("insider_own", "inst_own", "short_float"):
        ps = cat_passed(cat)
        if ps:
            print(f"  {cat}: {len(ps)} endpoint(s) usable")
            for _, label, path, body in ps:
                first = body[0] if isinstance(body, list) and body else body
                if isinstance(first, dict):
                    print(f"     - {label} ({path})")
                    print(f"       fields: {sorted(first.keys())[:15]}")
                    # print first record raw for inspection
                    print(f"       sample: {json.dumps(first, default=str)[:300]}")
        else:
            proxy = cat_passed(cat + "_proxy")
            if proxy:
                print(f"  {cat}: NO direct endpoint on this plan (proxy available)")
                for _, label, path, body in proxy:
                    print(f"     proxy - {label} ({path})")
                    first = body[0] if isinstance(body, list) and body else body
                    if isinstance(first, dict):
                        print(f"       fields: {sorted(first.keys())[:15]}")
                        print(f"       sample: {json.dumps(first, default=str)[:300]}")
            else:
                print(f"  {cat}: NO endpoint usable on this plan")

    if restricted:
        print()
        print("  Tier-restricted endpoints (paid plan required):")
        for cat, label, path in restricted:
            print(f"     - [{cat}] {label} {path}")


if __name__ == "__main__":
    main()
