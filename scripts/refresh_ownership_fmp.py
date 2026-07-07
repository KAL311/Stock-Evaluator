#!/usr/bin/env python3
"""Fetch ownership data from Financial Modeling Prep (FMP) Starter tier and
write to ownership_live table.  Frozen scoring pipeline and finviz_cache
untouched.  Consumed by the screener only when USE_FMP_OWNERSHIP=1 is set.

Preflight (scripts/fmp_preflight.py, run 2026-07-07 against the live key)
determined which endpoints Starter actually grants:

  PASS:
    /stable/shares-float
        -> freeFloat percentage of outstanding shares.  Insider ownership
           (closely-held share fraction) is 1 - freeFloat/100.  This matches
           Finviz "Insider Own" (both compute closely-held / outstanding
           from SEC filings).
    /stable/insider-trading/statistics
        -> Quarterly Form 4 acquired/disposed transaction counts.  Useful
           for a *flow* signal, not a *level* (%-of-shares-owned) signal.
           Not consumed here.
  RESTRICTED (HTTP 402 - plan gate):
    /stable/institutional-ownership/*      -> inst_own stays NULL
  UNAVAILABLE (endpoint not on Starter):
    /stable/short-interest*, sharesShort on quote  -> short_float stays NULL

Usage:
  python scripts/refresh_ownership_fmp.py              # top 200
  python scripts/refresh_ownership_fmp.py --top 500
  python scripts/refresh_ownership_fmp.py --top 0      # all tickers
"""
import argparse
import os
import random
import sqlite3
import sys
import time
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "stock_cache.db"
ENV_PATH = ROOT / "FMP_API.env"
FMP_BASE = "https://financialmodelingprep.com"
TIMEOUT = 15
MAX_RETRIES = 4

# Starter plan cap is 300 calls/min.  Budget 250/min for safety margin.
RATE_CAP_PER_MIN = 250
RATE_WINDOW_SEC = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS ownership_live (
    ticker TEXT NOT NULL,
    insider_own REAL,
    inst_own REAL,
    short_float REAL,
    filing_date TEXT NOT NULL,
    fetched_at TEXT,
    PRIMARY KEY (ticker, filing_date)
);
CREATE INDEX IF NOT EXISTS idx_ownership_live_ticker ON ownership_live(ticker);
CREATE INDEX IF NOT EXISTS idx_ownership_live_filing_date ON ownership_live(filing_date);
"""


class RateLimiter:
    """Sliding-window rate limiter.  Blocks until sending would keep us at
    or under ``cap`` calls in the trailing ``window_sec`` window.
    """

    def __init__(self, cap: int, window_sec: float):
        self.cap = cap
        self.window = window_sec
        self._timestamps: deque[float] = deque()

    def acquire(self):
        now = time.monotonic()
        # Drop timestamps outside the trailing window.
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


def get_tickers(conn, top_n=None):
    if top_n:
        q = (
            "SELECT ticker FROM stocks WHERE potential_score IS NOT NULL "
            f"ORDER BY potential_score DESC LIMIT {int(top_n)}"
        )
    else:
        q = "SELECT ticker FROM stocks"
    return [r[0] for r in conn.execute(q).fetchall()]


def fmp_get(path, params, key, limiter: RateLimiter):
    """GET with rate-limit acquire + retry/backoff.  Returns parsed JSON or None."""
    p = {**params, "apikey": key}
    for attempt in range(MAX_RETRIES):
        limiter.acquire()
        try:
            r = requests.get(FMP_BASE + path, params=p, timeout=TIMEOUT)
            if r.status_code == 429:
                # Server-side rate hit — back off harder.
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                continue
            if r.status_code >= 500:
                time.sleep((1.5 ** attempt) + random.uniform(0, 0.5))
                continue
            if r.status_code == 402:
                # Plan-restricted; no point retrying.
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt < MAX_RETRIES - 1:
                time.sleep((1.5 ** attempt) + random.uniform(0, 0.5))
                continue
            return None
    return None


def _to_fraction(v):
    """Normalize a percent-like number to [0,1] to match _parse_pct in
    src/market_screener.py (which stores 0-1 fractions).
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    # Values with |f| > 1 are assumed to be percent representation (e.g.
    # 65.4 -> 0.654).  Values within [-1, 1] assumed already fractional.
    if abs(f) > 1.0:
        f = f / 100.0
    if f < -1.0 or f > 1.0:
        return None
    return f


def fetch_shares_float(ticker, key, limiter):
    """Returns (insider_fraction_or_None, filing_date_or_None).

    Endpoint: /stable/shares-float
    Response: [{"symbol", "date", "freeFloat", "floatShares",
                "outstandingShares", "source"}]
    Insider ownership = 1 - freeFloat/100 (closely-held share fraction).
    """
    data = fmp_get("/stable/shares-float", {"symbol": ticker}, key, limiter)
    if not data or not isinstance(data, list) or not data:
        return None, None
    rec = data[0]
    ff = rec.get("freeFloat")
    raw_dt = rec.get("date")
    # FMP returns "YYYY-MM-DD HH:MM:SS"; keep just the date part.
    dt = None
    if isinstance(raw_dt, str) and raw_dt:
        dt = raw_dt.split(" ", 1)[0]
    ff_frac = _to_fraction(ff)
    if ff_frac is None:
        return None, dt
    insider = 1.0 - ff_frac
    if insider < 0.0:
        insider = 0.0
    if insider > 1.0:
        insider = 1.0
    return insider, dt


def fetch_one(ticker, key, limiter):
    """Aggregate one ticker's ownership record.

    Only shares-float is called on the Starter plan.  If/when the plan
    grants /stable/institutional-ownership/latest and a short-interest
    endpoint, add fetchers here and populate inst_own / short_float.
    """
    insider_pct, ins_date = fetch_shares_float(ticker, key, limiter)
    inst_pct = None
    short_pct = None
    filing_date = ins_date or date.today().isoformat()
    return {
        "insider_own": insider_pct,
        "inst_own": inst_pct,
        "short_float": short_pct,
        "filing_date": filing_date,
    }


def main():
    ap = argparse.ArgumentParser(
        description="Refresh ownership data from FMP into ownership_live."
    )
    ap.add_argument(
        "--top",
        type=int,
        default=200,
        help="Refresh only top N by potential_score. Use 0 for all (default: 200).",
    )
    ap.add_argument(
        "--db",
        default=str(DB_PATH),
        help=f"Path to stock_cache.db (default: {DB_PATH}).",
    )
    args = ap.parse_args()

    api_key = load_api_key()
    if not api_key:
        print(
            "ERROR: FMP_API_KEY not set. Set env var or add to FMP_API.env.",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)

    top_arg = None if args.top == 0 else args.top
    tickers = get_tickers(conn, top_arg)
    if not tickers:
        print("ERROR: No tickers found in database.", file=sys.stderr)
        sys.exit(1)

    limiter = RateLimiter(RATE_CAP_PER_MIN, RATE_WINDOW_SEC)

    print(f"Refreshing ownership for {len(tickers)} tickers from FMP Starter...")
    print(
        "  Endpoints served on Starter: /stable/shares-float (insider_own)."
    )
    print(
        "  NOT served on Starter: institutional-ownership (402), "
        "short-interest (404).  inst_own and short_float will be NULL."
    )
    print(f"  Rate limit: {RATE_CAP_PER_MIN} calls / {RATE_WINDOW_SEC:.0f}s.")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    succeeded = 0
    failed = []
    batch_rows = []
    started = time.monotonic()

    try:
        for idx, ticker in enumerate(tickers, start=1):
            try:
                rec = fetch_one(ticker, api_key, limiter)
            except Exception as e:
                failed.append(ticker)
                print(f"  {ticker}: {e}", file=sys.stderr)
                continue
            if all(
                rec.get(k) is None
                for k in ("insider_own", "inst_own", "short_float")
            ):
                failed.append(ticker)
            else:
                batch_rows.append(
                    (
                        ticker,
                        rec["insider_own"],
                        rec["inst_own"],
                        rec["short_float"],
                        rec["filing_date"],
                        now,
                    )
                )
                succeeded += 1
            # Commit every 100 tickers so a crash doesn't lose all progress.
            if idx % 100 == 0:
                if batch_rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO ownership_live "
                        "(ticker, insider_own, inst_own, short_float, "
                        "filing_date, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                        batch_rows,
                    )
                    conn.commit()
                    batch_rows = []
                elapsed = time.monotonic() - started
                rate = idx / elapsed * 60 if elapsed > 0 else 0
                print(
                    f"  progress: {idx}/{len(tickers)}  "
                    f"({succeeded} ok, {len(failed)} failed, "
                    f"{rate:.0f} calls/min)"
                )
        # Flush tail.
        if batch_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO ownership_live "
                "(ticker, insider_own, inst_own, short_float, filing_date, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                batch_rows,
            )
            conn.commit()
    except KeyboardInterrupt:
        print("\nInterrupted. Saving progress so far...")
        if batch_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO ownership_live "
                "(ticker, insider_own, inst_own, short_float, filing_date, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                batch_rows,
            )
        conn.commit()

    total = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM ownership_live"
    ).fetchone()[0]
    max_date = conn.execute(
        "SELECT MAX(filing_date) FROM ownership_live"
    ).fetchone()[0]
    elapsed = time.monotonic() - started
    rate = len(tickers) / elapsed * 60 if elapsed > 0 else 0

    print()
    print(
        f"Done. {succeeded}/{len(tickers)} tickers updated this run "
        f"({total} total in ownership_live)."
    )
    print(f"Effective call rate: {rate:.0f} calls/min over {elapsed:.0f}s.")
    print(f"Max filing_date in ownership_live: {max_date}")
    if failed:
        show = failed[:20]
        more = "..." if len(failed) > 20 else ""
        print(
            f"WARNING: {len(failed)} tickers returned no ownership data: "
            f"{show}{more}",
            file=sys.stderr,
        )
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
