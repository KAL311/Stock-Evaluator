"""Fetch annual FMP fundamentals (income / balance / cash-flow) for a ticker sample.

Purpose
-------
Feeds the SimFin↔FMP parity study (scripts/parity_study_a.py). Emits raw JSON
per (ticker, statement) plus three long-format CSVs (one row per (ticker, FY))
that src/fmp_mapping.py consumes to produce DataFrames with SimFin column names.

Constraints (per Study A brief)
--------------------------------
- Starter plan: 300 calls/min hard limit → we cap at 250/min for safety margin.
- 20GB/30d bandwidth cap → 1000 tickers × 3 statements × 5y JSON is well under.
- 429/5xx: retry with exponential backoff.
- Raw JSON cached under data/fmp/raw/ so re-runs are free.

Endpoints (current /stable/ paths, 2026):
- /stable/income-statement          ?symbol=X&period=annual&limit=N
- /stable/balance-sheet-statement   ?symbol=X&period=annual&limit=N
- /stable/cash-flow-statement       ?symbol=X&period=annual&limit=N

Ticker normalization
--------------------
SimFin uses dashes for share classes (BRK-A, BRK-B). FMP accepts dashes too on
/stable/. If a ticker fails on FMP with the dash form, we try the dot form as
a fallback (BRK.A). Both failures → logged.

CLI
---
    py -3 scripts/fetch_fundamentals_fmp.py --top 1000 --years 5
    py -3 scripts/fetch_fundamentals_fmp.py --tickers-file mysample.txt
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / "FMP_API.env"
CACHE_DB = REPO_ROOT / "data" / "stock_cache.db"
RAW_DIR = REPO_ROOT / "data" / "fmp" / "raw"
OUT_DIR = REPO_ROOT / "data" / "fmp"
FAIL_LOG = REPO_ROOT / "data" / "fmp" / "fetch_failures.log"

FMP_BASE = "https://financialmodelingprep.com"
TIMEOUT = 30
MAX_RETRIES = 5

STATEMENTS = [
    ("income", "/stable/income-statement"),
    ("balance", "/stable/balance-sheet-statement"),
    ("cashflow", "/stable/cash-flow-statement"),
]


def load_api_key() -> str | None:
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


class RateLimiter:
    """Token-bucket. 250 calls / 60s window."""

    def __init__(self, max_per_min: int = 250):
        self.max = max_per_min
        self.window = []  # timestamps of recent calls

    def acquire(self):
        now = time.monotonic()
        self.window = [t for t in self.window if now - t < 60]
        if len(self.window) >= self.max:
            sleep_for = 60 - (now - self.window[0]) + 0.05
            if sleep_for > 0:
                time.sleep(sleep_for)
        self.window.append(time.monotonic())


def fmp_get(path: str, params: dict, key: str, limiter: RateLimiter) -> tuple[int, object]:
    """GET with rate-limit + retry. Returns (status, json_or_None).
    Status -1 means transport error after retries."""
    p = dict(params)
    p["apikey"] = key
    url = FMP_BASE + path + "?" + urllib.parse.urlencode(p)
    for attempt in range(MAX_RETRIES):
        limiter.acquire()
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
                data = json.load(r)
                return r.status, data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                continue
            if 500 <= e.code < 600:
                time.sleep((1.5 ** attempt) + random.uniform(0, 0.5))
                continue
            return e.code, None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep((1.5 ** attempt) + random.uniform(0, 0.5))
            continue
    return -1, None


def normalize_ticker(t: str) -> str:
    return t.strip().upper()


def alt_ticker(t: str) -> str | None:
    """SimFin↔FMP: try dot↔dash swap for share classes."""
    if "-" in t and len(t) <= 6:
        return t.replace("-", ".")
    if "." in t and len(t) <= 6:
        return t.replace(".", "-")
    return None


def fetch_statement_cached(
    ticker: str, stmt_name: str, path: str, years: int, key: str, limiter: RateLimiter
) -> tuple[list | None, int, bool]:
    """Return (data, bytes_added, from_cache)."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = RAW_DIR / f"{ticker}_{stmt_name}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text()), 0, True
        except json.JSONDecodeError:
            cache_file.unlink()

    st, data = fmp_get(path, {"symbol": ticker, "period": "annual", "limit": years}, key, limiter)
    if st != 200 or not isinstance(data, list) or not data:
        # Try fallback ticker form once
        alt = alt_ticker(ticker)
        if alt:
            st2, data2 = fmp_get(
                path, {"symbol": alt, "period": "annual", "limit": years}, key, limiter
            )
            if st2 == 200 and isinstance(data2, list) and data2:
                data = data2
                st = 200
        if st != 200 or not isinstance(data, list) or not data:
            return None, 0, False

    payload = json.dumps(data).encode("utf-8")
    cache_file.write_bytes(payload)
    return data, len(payload), False


def load_tickers_from_cache(top_n: int) -> list[str]:
    if not CACHE_DB.exists():
        raise FileNotFoundError(f"Cache DB missing: {CACHE_DB}")
    conn = sqlite3.connect(str(CACHE_DB))
    scored = conn.execute(
        "SELECT ticker, potential_score FROM stocks "
        "WHERE potential_score IS NOT NULL "
        "ORDER BY potential_score DESC"
    ).fetchall()
    conn.close()
    tickers = [normalize_ticker(t) for t, _ in scored]
    if top_n and top_n < len(tickers):
        top = tickers[:top_n]
        rest = tickers[top_n:]
        # Study A design: top 500 + random 500. Approximate here: if user asks
        # for top N, return N. Sampling logic lives in parity_study_a.py.
        return top
    return tickers


def load_tickers_from_file(path: Path) -> list[str]:
    tickers = []
    for line in path.read_text().splitlines():
        s = line.strip().split(",")[0].strip()
        if s and not s.startswith("#"):
            tickers.append(normalize_ticker(s))
    # de-dup preserving order
    seen = set()
    return [t for t in tickers if not (t in seen or seen.add(t))]


def emit_csv_from_json(records: dict[str, list[dict]], statement: str, out_path: Path) -> int:
    """records: {ticker: [row0_recent, row1, ...]}. Write long-format CSV.
    Preserves ALL FMP fields so the mapper picks what it needs."""
    import csv

    all_keys = set()
    for rows in records.values():
        for r in rows:
            all_keys.update(r.keys())
    all_keys.discard("symbol")  # replaced by 'ticker'
    ordered = ["ticker"] + sorted(all_keys)
    n = 0
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ordered, extrasaction="ignore")
        w.writeheader()
        for ticker, rows in records.items():
            for r in rows:
                r2 = dict(r)
                r2["ticker"] = ticker
                w.writerow(r2)
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="Fetch FMP annual fundamentals.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--tickers-file", type=Path, help="Newline-delimited tickers file.")
    src.add_argument("--top", type=int, help="Top N by cached potential_score.")
    ap.add_argument("--years", type=int, default=5, help="Annual periods to fetch.")
    ap.add_argument(
        "--rate-per-min",
        type=int,
        default=250,
        help="Cap calls per minute (Starter hard limit 300).",
    )
    args = ap.parse_args()

    key = load_api_key()
    if not key:
        print(
            "ERROR: FMP_API_KEY not set (env or FMP_API.env). Exiting cleanly.",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.tickers_file:
        tickers = load_tickers_from_file(args.tickers_file)
    else:
        tickers = load_tickers_from_cache(args.top)

    print(f"Fetching {len(tickers)} tickers × 3 statements × {args.years}y annual (rate cap {args.rate_per_min}/min)")
    print(f"  Raw cache dir: {RAW_DIR}")
    print(f"  Output dir:    {OUT_DIR}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(args.rate_per_min)

    records = {name: {} for name, _ in STATEMENTS}
    failures: list[tuple[str, str]] = []
    bytes_fetched = 0
    cache_hits = 0
    api_calls = 0

    t0 = time.time()
    for i, tk in enumerate(tickers, 1):
        for stmt, path in STATEMENTS:
            data, nbytes, from_cache = fetch_statement_cached(
                tk, stmt, path, args.years, key, limiter
            )
            if data is None:
                failures.append((tk, stmt))
                continue
            records[stmt][tk] = data
            bytes_fetched += nbytes
            if from_cache:
                cache_hits += 1
            else:
                api_calls += 1
        if i % 25 == 0 or i == len(tickers):
            el = time.time() - t0
            print(
                f"  [{i:>4}/{len(tickers)}] {tk:<8} api_calls={api_calls} "
                f"cache_hits={cache_hits} bytes={bytes_fetched/1e6:.1f}MB "
                f"elapsed={el:.1f}s fails={len(failures)}"
            )

    # Emit long-format CSVs
    print("\nEmitting long-format CSVs...")
    row_counts = {}
    for stmt, _ in STATEMENTS:
        out = OUT_DIR / f"fundamentals_{stmt}.csv"
        n = emit_csv_from_json(records[stmt], stmt, out)
        row_counts[stmt] = n
        print(f"  {out.name}: {n} rows  ({len(records[stmt])} tickers)")

    # Failure log
    if failures:
        FAIL_LOG.write_text("\n".join(f"{t}\t{s}" for t, s in failures))
        print(f"\n{len(failures)} (ticker,stmt) failures logged: {FAIL_LOG}")

    # Bandwidth report
    print(f"\nDone. api_calls={api_calls} cache_hits={cache_hits} "
          f"total_bytes_from_api={bytes_fetched/1e6:.1f}MB "
          f"({bytes_fetched/1e9:.3f}GB of 20GB monthly cap)")


if __name__ == "__main__":
    main()
