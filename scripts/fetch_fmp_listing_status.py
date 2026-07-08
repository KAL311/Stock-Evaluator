#!/usr/bin/env python3
"""Fetch FMP listing-status oracle used by the live screener's liveness gate.

Produces ``data/fmp/listing_status.json`` with:
  - ``active_symbols``   — FMP /stable/stock-list (all listed symbols).
                           Superset: includes some names that are
                           actually delisted (frozen quote). Not
                           authoritative alone.
  - ``delisted_symbols`` — FMP /stable/delisted-companies (page 0 only
                           on the Starter tier, ~100 most-recent
                           delistings). Authoritative when present.
  - ``quote_probes``     — per-ticker map of ``ticker -> {ts, price,
                           checked_at}`` from /stable/quote. The quote
                           timestamp age is the reliable ACTIVE/DELISTED
                           discriminator: a frozen quote > 180 days old
                           marks the ticker as post-delisting; a recent
                           quote (even a laggy one from a datavendor) marks
                           the ticker as trading.

Usage:
  python scripts/fetch_fmp_listing_status.py                       # refresh + probe
  python scripts/fetch_fmp_listing_status.py --skip-bulk           # only per-ticker
  python scripts/fetch_fmp_listing_status.py --skip-quotes         # only bulk lists
  python scripts/fetch_fmp_listing_status.py --tickers-file X.txt  # probe only these
"""
import argparse
import json
import os
import random
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "stock_cache.db"
CACHE_DIR = ROOT / "data" / "fmp"
CACHE_PATH = CACHE_DIR / "listing_status.json"
ENV_PATH = ROOT / "FMP_API.env"
FMP_BASE = "https://financialmodelingprep.com"
TIMEOUT = 15
MAX_RETRIES = 4
RATE_CAP_PER_MIN = 250
RATE_WINDOW_SEC = 60.0
DEFAULT_QUOTE_TTL_DAYS = 7


class RateLimiter:
    def __init__(self, cap: int, window_sec: float):
        self.cap = cap
        self.window = window_sec
        self._timestamps: deque = deque()

    def acquire(self):
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] > self.window:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.cap:
            sleep_for = self.window - (now - self._timestamps[0]) + 0.01
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] > self.window:
                self._timestamps.popleft()
        self._timestamps.append(time.monotonic())


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


def fmp_get(path, params, key, limiter: RateLimiter):
    p = {**params, "apikey": key}
    for attempt in range(MAX_RETRIES):
        limiter.acquire()
        try:
            r = requests.get(FMP_BASE + path, params=p, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                continue
            if r.status_code >= 500:
                time.sleep((1.5 ** attempt) + random.uniform(0, 0.5))
                continue
            if r.status_code == 402:
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt < MAX_RETRIES - 1:
                time.sleep((1.5 ** attempt) + random.uniform(0, 0.5))
                continue
            return None
    return None


def load_cache():
    if not CACHE_PATH.exists():
        return {
            "fetched_at": None,
            "active_symbols": [],
            "delisted_symbols": [],
            "quote_probes": {},
        }
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception as e:
        print(f"WARNING: failed to parse existing cache {CACHE_PATH}: {e}", file=sys.stderr)
        return {
            "fetched_at": None,
            "active_symbols": [],
            "delisted_symbols": [],
            "quote_probes": {},
        }


def save_cache(cache):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2))
    tmp.replace(CACHE_PATH)


def _normalize(sym: str) -> str:
    if not sym:
        return sym
    return sym.strip().upper()


def refresh_bulk_lists(key, limiter):
    """Refresh active + delisted bulk lists. Returns (active_set, delisted_set)."""
    print("  Fetching /stable/stock-list ...", end=" ", flush=True)
    j = fmp_get("/stable/stock-list", {}, key, limiter)
    active = set()
    if isinstance(j, list):
        for row in j:
            s = _normalize(row.get("symbol"))
            if s:
                active.add(s)
    print(f"{len(active)} symbols")

    print("  Fetching /stable/delisted-companies?page=0 ...", end=" ", flush=True)
    j = fmp_get("/stable/delisted-companies", {"page": 0}, key, limiter)
    delisted = set()
    if isinstance(j, list):
        for row in j:
            s = _normalize(row.get("symbol"))
            if s:
                delisted.add(s)
    print(f"{len(delisted)} symbols (Starter tier: page 0 only)")

    return active, delisted


def get_universe_tickers(db_path):
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[0] for r in conn.execute("SELECT ticker FROM stocks").fetchall()]
    finally:
        conn.close()


def probe_quote(ticker, key, limiter):
    """Return {ts, price, source} or None on empty/error."""
    j = fmp_get(f"/stable/quote", {"symbol": ticker}, key, limiter)
    if not isinstance(j, list) or not j:
        return None
    d = j[0]
    ts = d.get("timestamp")
    price = d.get("price")
    if ts is None:
        return None
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    return {"ts": ts, "price": price, "source": "/stable/quote"}


def probe_batch(tickers, key, limiter, ttl_days, existing_probes):
    """Probe quotes for tickers, skipping those with a fresh cached probe.
    Returns a dict {ticker: {ts, price, checked_at}}.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    ttl_seconds = ttl_days * 86400.0
    fresh = 0
    probed = 0
    misses = 0
    updated = dict(existing_probes)
    for i, t in enumerate(tickers, start=1):
        key_t = _normalize(t)
        cached = existing_probes.get(key_t) or existing_probes.get(t)
        if cached:
            cached_at = cached.get("checked_at")
            try:
                cached_dt = datetime.fromisoformat(cached_at) if cached_at else None
            except ValueError:
                cached_dt = None
            if cached_dt is not None:
                if cached_dt.tzinfo is None:
                    cached_dt = cached_dt.replace(tzinfo=timezone.utc)
                if (now - cached_dt).total_seconds() < ttl_seconds:
                    fresh += 1
                    continue
        rec = probe_quote(t, key, limiter)
        probed += 1
        if rec is not None:
            rec["checked_at"] = now_iso
            updated[key_t] = rec
        else:
            misses += 1
        if i % 50 == 0:
            print(f"    probed {i}/{len(tickers)}  (fresh={fresh}, new={probed-misses}, empty={misses})")
    return updated, fresh, probed, misses


def main():
    ap = argparse.ArgumentParser(
        description="Fetch FMP listing-status oracle (bulk lists + per-ticker quote timestamps)."
    )
    ap.add_argument("--db", default=str(DB_PATH), help=f"Path to stock_cache.db (default: {DB_PATH})")
    ap.add_argument("--skip-bulk", action="store_true", help="Do not refresh bulk lists (use existing cache).")
    ap.add_argument("--skip-quotes", action="store_true", help="Do not probe per-ticker quotes.")
    ap.add_argument("--force-quote-refresh", action="store_true", help="Ignore quote TTL and re-probe every ticker.")
    ap.add_argument("--tickers-file", default=None, help="Probe only the tickers in this newline-delimited file.")
    ap.add_argument("--quote-ttl-days", type=int, default=DEFAULT_QUOTE_TTL_DAYS, help=f"Skip probe if cached within this many days (default: {DEFAULT_QUOTE_TTL_DAYS}).")
    args = ap.parse_args()

    key = load_api_key()
    if not key:
        print("ERROR: FMP_API_KEY not set (env or FMP_API.env). Cannot fetch oracle.", file=sys.stderr)
        sys.exit(1)

    limiter = RateLimiter(RATE_CAP_PER_MIN, RATE_WINDOW_SEC)
    cache = load_cache()

    if not args.skip_bulk:
        print("Refreshing bulk lists ...")
        active, delisted = refresh_bulk_lists(key, limiter)
        cache["active_symbols"] = sorted(active)
        cache["delisted_symbols"] = sorted(delisted)
        cache["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_cache(cache)
        print(f"  Wrote {CACHE_PATH.name}: {len(active)} active, {len(delisted)} delisted.")
    else:
        print("Skipping bulk refresh (using cached lists).")

    if args.skip_quotes:
        print("Skipping per-ticker quote probes.")
        print("Done.")
        return

    # Decide which tickers to probe.
    if args.tickers_file:
        tf = Path(args.tickers_file)
        if not tf.exists():
            print(f"ERROR: --tickers-file not found: {tf}", file=sys.stderr)
            sys.exit(1)
        tickers = [
            line.strip()
            for line in tf.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        print(f"Probing {len(tickers)} tickers from {tf.name} ...")
    else:
        tickers = get_universe_tickers(args.db)
        print(f"Probing {len(tickers)} tickers from stocks table ...")

    if not tickers:
        print("No tickers to probe. Done.")
        return

    existing = cache.get("quote_probes", {})
    if args.force_quote_refresh:
        existing = {}
    updated, fresh, probed, misses = probe_batch(
        tickers, key, limiter, args.quote_ttl_days, existing
    )
    cache["quote_probes"] = updated
    save_cache(cache)

    active_bulk = len(cache.get("active_symbols", []))
    delisted_bulk = len(cache.get("delisted_symbols", []))
    print()
    print(f"Done. active={active_bulk}, delisted={delisted_bulk}, "
          f"quote_probes cached={len(updated)}.")
    print(f"  This run: skipped-fresh={fresh}, probed={probed}, empty-responses={misses}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
