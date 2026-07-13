"""FMP quote-fill pass — close the price-coverage gap left by yfinance.

Runs AFTER scripts/refreshprice.py in the daily pipeline. yfinance
rate-limits progressively on a full 2500-ticker pull, dropping coverage
below the 80% liveness floor. Separately, some FMP-confirmed-ACTIVE
tickers never resolve on yfinance at all (89 UNPRICED names on the last
run). This script fills those two gaps from FMP's /stable/quote endpoint —
already trusted by scripts/fetch_fmp_listing_status.py.

Design contract (non-negotiable)
--------------------------------
1. CURRENT PRICE ONLY. FMP /stable/quote returns a point-in-time price.
   We insert a single row per filled ticker with today's date; we do NOT
   splice FMP quotes into the historical daily series used by
   compute_momentum. yfinance adjusted-close semantics differ from FMP
   quote semantics, so mixing them across days would corrupt
   return_12m_1m and dist_52w. If a ticker gets an FMP fill and has no
   yfinance history, the momentum sub-signal simply lacks data — the
   existing compute_momentum path handles that with nulls, not
   fabrication. Report count.

2. NO ZOMBIE PRICING. Only tickers marked ACTIVE by the FMP listing
   oracle (data/fmp/listing_status.json:active_symbols) are eligible for
   a fill. Anything in delisted_symbols is skipped entirely so the
   liveness gate can still flag it DELISTED.

3. NO SCORING / GATE / WEIGHT / DECAY / BACKTEST TOUCH. This is a
   prices_live extension only: adds a `source` column (backfilled
   'yfinance' for existing rows, 'fmp' for these) and inserts new rows.
   The gate (compute_liveness_and_flag) reads prices_live unchanged.

4. IDEMPOTENT. Only fills tickers whose latest prices_live row is
   missing or stale (> STALE_DAYS old). Cheap on re-run: no fetch when
   already fresh.

Rate limit
----------
250/min token bucket, same discipline as scripts/fetch_fundamentals_fmp.py
and scripts/fetch_fmp_listing_status.py. Per-ticker failure log at
data/fmp/fill_prices_failures.log.

Emits
-----
    FILL_JSON={"attempted": N, "filled_fmp": M, "already_fresh_from_yf": Y,
               "skipped_delisted": D, "failed": F,
               "coverage_before_pct": X, "coverage_after_pct": Z}
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / "FMP_API.env"
DB_PATH = REPO_ROOT / "data" / "stock_cache.db"
LISTING_JSON = REPO_ROOT / "data" / "fmp" / "listing_status.json"
FAIL_LOG = REPO_ROOT / "data" / "fmp" / "fill_prices_failures.log"

FMP_BASE = "https://financialmodelingprep.com"
TIMEOUT = 30
MAX_RETRIES = 4
DEFAULT_STALE_DAYS = 3  # tickers with last_px older than table_max - STALE_DAYS get refilled

# FMP /stable/quote returns each ticker's LAST-KNOWN trade timestamp — not
# today's if the ticker has stopped trading. Zombie tickers still listed in
# active_symbols return timestamps from months or years ago. If we wrote
# those verbatim they'd (a) pollute prices_live with fake-recent rows on
# fetched_at, (b) NOT count fresh in the screener's 10-day gate anyway, and
# (c) turn oracle-DELISTED-heuristic candidates into "has a row → maybe
# alive" false positives. Reject any FMP quote whose trade date is older
# than today - FRESH_DAYS. The ticker stays UNPRICED, which is honest.
FRESH_DAYS = 5


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
    def __init__(self, max_per_min: int = 250):
        self.max = max_per_min
        self.window: list[float] = []
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            self.window = [t for t in self.window if now - t < 60]
            if len(self.window) >= self.max:
                sleep_for = 60 - (now - self.window[0]) + 0.05
                if sleep_for > 0:
                    self.lock.release()
                    try:
                        time.sleep(sleep_for)
                    finally:
                        self.lock.acquire()
                    now = time.monotonic()
                    self.window = [t for t in self.window if now - t < 60]
            self.window.append(time.monotonic())


def fmp_quote(ticker: str, key: str, limiter: RateLimiter) -> dict | None:
    """Return {"price": float, "timestamp": int, "date": "YYYY-MM-DD"} or None."""
    path = "/stable/quote"
    params = {"symbol": ticker, "apikey": key}
    url = FMP_BASE + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(MAX_RETRIES):
        limiter.acquire()
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
                data = json.load(r)
            if not isinstance(data, list) or not data:
                return None
            d = data[0]
            price = d.get("price")
            ts = d.get("timestamp")
            if price is None or ts is None:
                return None
            try:
                ts = int(ts)
                price = float(price)
            except (TypeError, ValueError):
                return None
            # Convert unix ts to date string (UTC)
            dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
            return {"price": price, "timestamp": ts, "date": dt.strftime("%Y-%m-%d")}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.0)
                continue
            if 500 <= e.code < 600:
                time.sleep(1.5 ** attempt)
                continue
            return None  # 4xx (403/404) — no retry
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(1.5 ** attempt)
            continue
    return None


def ensure_source_column(conn: sqlite3.Connection) -> tuple[bool, int]:
    """Add prices_live.source column if missing. Idempotently back-fill any
    NULL source rows to 'yfinance' (refreshprice.py writes without a source
    tag, so this must run every fill invocation). Returns (added, backfilled)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(prices_live)").fetchall()]
    added = False
    if "source" not in cols:
        conn.execute("ALTER TABLE prices_live ADD COLUMN source TEXT")
        added = True
    # Idempotent backfill: any row without a source came from the yfinance
    # refresh (which doesn't set the column).
    cur = conn.execute(
        "UPDATE prices_live SET source = 'yfinance' WHERE source IS NULL"
    )
    backfilled = cur.rowcount if cur.rowcount is not None else 0
    conn.commit()
    return added, backfilled


def build_fill_set(
    conn: sqlite3.Connection, active_set: set[str], delisted_set: set[str],
    stale_days: int,
) -> tuple[list[str], dict[str, str]]:
    """Return (tickers_to_fill, per-ticker reason for skipped).

    Universe = ALL rows in `stocks`, not just potential_score IS NOT NULL.
    Reason: the UNPRICED cohort we want to resolve is exactly the set with
    no recent price and therefore often no score (rank-group filter drops
    them). Filtering to scored tickers would miss the very names we exist
    to price.
    """
    universe = [
        r[0] for r in conn.execute("SELECT ticker FROM stocks").fetchall()
    ]
    # Latest price date per ticker
    row_last = dict(conn.execute(
        "SELECT ticker, MAX(date) FROM prices_live GROUP BY ticker"
    ).fetchall())
    row_table_max = conn.execute("SELECT MAX(date) FROM prices_live").fetchone()[0]
    table_max = _dt.date.fromisoformat(row_table_max) if row_table_max else _dt.date.today()
    cutoff = table_max - _dt.timedelta(days=stale_days)

    to_fill: list[str] = []
    skipped: dict[str, str] = {}
    for t in universe:
        if t in delisted_set:
            skipped[t] = "delisted-per-oracle"
            continue
        if t not in active_set:
            # Not in FMP-active list — skip. This includes oracle UNKNOWN
            # names; we defer to yfinance for those.
            skipped[t] = "not-active-per-oracle"
            continue
        last = row_last.get(t)
        if last is None:
            to_fill.append(t)
            continue
        try:
            if _dt.date.fromisoformat(last) < cutoff:
                to_fill.append(t)
        except ValueError:
            to_fill.append(t)
    return to_fill, skipped


def compute_coverage(conn: sqlite3.Connection) -> float:
    """Coverage = distinct tickers in stocks (potential_score not null)
    with prices_live row in the last 10 days / universe."""
    n_universe = conn.execute(
        "SELECT COUNT(*) FROM stocks WHERE potential_score IS NOT NULL"
    ).fetchone()[0]
    if not n_universe:
        return 0.0
    n_recent = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM prices_live "
        "WHERE date >= date('now','-10 days') "
        "AND ticker IN (SELECT ticker FROM stocks WHERE potential_score IS NOT NULL)"
    ).fetchone()[0]
    return n_recent / n_universe * 100


def main() -> int:
    ap = argparse.ArgumentParser(description="Fill prices_live gaps from FMP quotes.")
    ap.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                    help="Fill tickers whose latest prices_live row is older than table_max - N days")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rate-per-min", type=int, default=250)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    key = load_api_key()
    if not key:
        print("ERROR: FMP_API_KEY not set. Aborting.", file=sys.stderr)
        return 2

    if not LISTING_JSON.exists():
        print(f"ERROR: FMP listing_status.json not found ({LISTING_JSON}). "
              f"Run scripts/fetch_fmp_listing_status.py first.", file=sys.stderr)
        return 3

    listing = json.loads(LISTING_JSON.read_text())
    active_syms = listing.get("active_symbols", [])
    delisted_syms = listing.get("delisted_symbols", [])
    active_set = set(active_syms) if isinstance(active_syms, list) else set(active_syms.keys())
    delisted_set = set(delisted_syms) if isinstance(delisted_syms, list) else set(delisted_syms.keys())

    conn = sqlite3.connect(str(args.db))
    added_col, backfilled_n = ensure_source_column(conn)
    if added_col:
        print(f"  prices_live: added `source` column; back-filled {backfilled_n} rows to 'yfinance'")
    elif backfilled_n:
        print(f"  prices_live: back-filled {backfilled_n} yfinance-refreshed rows to source='yfinance'")
    else:
        print("  prices_live: `source` column present, no back-fill needed")

    cov_before = compute_coverage(conn)
    to_fill, skipped = build_fill_set(conn, active_set, delisted_set, args.stale_days)

    n_delisted_skipped = sum(1 for r in skipped.values() if r == "delisted-per-oracle")
    n_notactive_skipped = sum(1 for r in skipped.values() if r == "not-active-per-oracle")

    print(f"  Coverage before fill: {cov_before:.1f}%")
    print(f"  Fill candidates: {len(to_fill)}")
    print(f"    (skipped {n_delisted_skipped} delisted-per-oracle, "
          f"{n_notactive_skipped} not-active-per-oracle)")

    if not to_fill:
        print("  Nothing to fill. Exiting cleanly.")
        _emit(cov_before, cov_before, 0, 0, n_delisted_skipped, 0)
        conn.close()
        return 0

    limiter = RateLimiter(args.rate_per_min)
    filled: list[tuple[str, str, float, int]] = []  # (ticker, date, price, ts)
    failed: list[str] = []
    stale_quote: list[tuple[str, str]] = []  # (ticker, quote_date) — zombies
    lock = threading.Lock()
    fresh_cutoff = _dt.date.today() - _dt.timedelta(days=FRESH_DAYS)

    def _one(t: str):
        q = fmp_quote(t, key, limiter)
        if q is None:
            with lock:
                failed.append(t)
            return
        try:
            q_date = _dt.date.fromisoformat(q["date"])
        except (KeyError, ValueError):
            with lock:
                failed.append(t)
            return
        if q_date < fresh_cutoff:
            with lock:
                stale_quote.append((t, q["date"]))
            return
        with lock:
            filled.append((t, q["date"], q["price"], q["timestamp"]))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_one, t): t for t in to_fill}
        done = 0
        for fut in as_completed(futs):
            fut.result()
            done += 1
            if done % 50 == 0 or done == len(to_fill):
                el = time.time() - t0
                rate = done / el * 60 if el > 0 else 0
                print(f"    [{done:>4}/{len(to_fill)}] filled={len(filled)} "
                      f"failed={len(failed)} elapsed={el:.1f}s rate={rate:.0f}/min")

    now_iso = _dt.datetime.now().isoformat(timespec="seconds")
    # INSERT OR REPLACE — one FMP row per ticker for today. Volume=NULL because
    # /stable/quote does not report a canonical daily volume (it reports last-trade
    # volume, not daily; leaving NULL avoids poisoning the volume series).
    rows = [(t, d, p, None, now_iso, "fmp") for (t, d, p, _ts) in filled]
    conn.executemany(
        "INSERT OR REPLACE INTO prices_live "
        "(ticker, date, close, volume, fetched_at, source) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()

    cov_after = compute_coverage(conn)
    conn.close()

    if failed:
        FAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
        FAIL_LOG.write_text("\n".join(failed))
        print(f"  {len(failed)} FMP-quote failures logged to {FAIL_LOG}")
    if stale_quote:
        stale_log = FAIL_LOG.parent / "fill_prices_stale.log"
        stale_log.write_text("\n".join(f"{t}\t{d}" for t, d in stale_quote))
        print(f"  {len(stale_quote)} stale-quote skips (quote date older than "
              f"{FRESH_DAYS} days) logged to {stale_log}")

    _emit(
        cov_before, cov_after, len(filled), len(failed),
        n_delisted_skipped, len(to_fill), stale_quote=len(stale_quote),
    )
    print(f"  Coverage after fill: {cov_after:.1f}%  "
          f"(filled_fmp={len(filled)}, failed={len(failed)}, "
          f"stale_quote={len(stale_quote)})")
    return 0


def _emit(cov_before: float, cov_after: float, filled: int, failed: int,
          delisted_skipped: int, attempted: int, stale_quote: int = 0) -> None:
    payload = {
        "attempted": attempted,
        "filled_fmp": filled,
        "failed": failed,
        "stale_quote": stale_quote,
        "skipped_delisted": delisted_skipped,
        "coverage_before_pct": round(cov_before, 1),
        "coverage_after_pct": round(cov_after, 1),
    }
    print("FILL_JSON=" + json.dumps(payload))


if __name__ == "__main__":
    sys.exit(main())
