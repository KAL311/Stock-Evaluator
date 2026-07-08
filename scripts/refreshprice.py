#!/usr/bin/env python3
"""Fetch fresh daily prices from yfinance for tickers in the screener cache.
Writes to prices_live table. SimFin data and frozen scoring untouched.

Usage:
  python scripts/refreshprice.py              # refresh top 200
  python scripts/refreshprice.py --top 500    # refresh top 500
  python scripts/refreshprice.py --top 0      # refresh ALL tickers
  python scripts/refreshprice.py --period 2y  # lookback (default 1y)
"""
import argparse
import logging
import os
import sqlite3
import sys
import time
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import yfinance as yf

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
os.environ["YFINANCE_LOG_LEVEL"] = "CRITICAL"

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "stock_cache.db"
BATCH = 25
SLEEP_BETWEEN = 2.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices_live (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL,
    volume REAL,
    fetched_at TEXT,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_live_ticker ON prices_live(ticker);
CREATE INDEX IF NOT EXISTS idx_prices_live_date ON prices_live(date);
"""


def get_tickers(conn, top_n=None):
    if top_n:
        q = (
            "SELECT ticker FROM stocks WHERE potential_score IS NOT NULL "
            f"ORDER BY potential_score DESC LIMIT {int(top_n)}"
        )
    else:
        q = "SELECT ticker FROM stocks"
    return [r[0] for r in conn.execute(q).fetchall()]


def fetch_batch(tickers, period="1y"):
    """Return long-format DataFrame: ticker, date, close, volume."""
    try:
        with redirect_stderr(StringIO()):
            df = yf.download(
                tickers,
                period=period,
                interval="1d",
                auto_adjust=True,
                progress=False,
                group_by="ticker",
                threads=True,
            )
    except Exception as e:
        print(f"(batch error: {e})", end=" ")
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    rows = []
    if len(tickers) == 1:
        t = tickers[0]
        try:
            sub = df[["Close", "Volume"]].dropna()
        except KeyError:
            return pd.DataFrame()
        for date, row in sub.iterrows():
            rows.append(
                {
                    "ticker": t,
                    "date": date.strftime("%Y-%m-%d"),
                    "close": float(row["Close"]),
                    "volume": float(row["Volume"]),
                }
            )
    else:
        for t in tickers:
            try:
                sub = df[t][["Close", "Volume"]].dropna()
            except KeyError:
                continue
            for date, row in sub.iterrows():
                rows.append(
                    {
                        "ticker": t,
                        "date": date.strftime("%Y-%m-%d"),
                        "close": float(row["Close"]),
                        "volume": float(row["Volume"]),
                    }
                )
    return pd.DataFrame(rows)


def fetch_single(ticker, period="1y"):
    """Fallback: fetch one ticker at a time via Ticker.history."""
    try:
        with redirect_stderr(StringIO()):
            t = yf.Ticker(ticker)
            hist = t.history(period=period, interval="1d", auto_adjust=True)
    except Exception:
        return None
    if hist.empty:
        return None
    rows = []
    for date, row in hist.iterrows():
        try:
            close = float(row["Close"])
            volume = float(row["Volume"]) if "Volume" in row else 0.0
        except (KeyError, TypeError, ValueError):
            continue
        rows.append(
            {
                "ticker": ticker,
                "date": date.strftime("%Y-%m-%d"),
                "close": close,
                "volume": volume,
            }
        )
    return pd.DataFrame(rows) if rows else None


def main():
    ap = argparse.ArgumentParser(
        description="Refresh daily prices from yfinance into prices_live table."
    )
    ap.add_argument(
        "--top",
        type=int,
        default=200,
        help="Refresh only top N by potential_score. Use 0 for all (default: 200)",
    )
    ap.add_argument(
        "--period",
        default="1y",
        help="yfinance lookback: 1mo, 3mo, 6mo, 1y, 2y, 5y, max (default: 1y)",
    )
    ap.add_argument(
        "--db",
        default=str(DB_PATH),
        help=f"Path to stock_cache.db (default: {DB_PATH})",
    )
    ap.add_argument(
        "--tickers-file",
        default=None,
        help=(
            "Path to a newline-delimited file of tickers to refresh. "
            "When set, overrides --top and only these tickers are fetched. "
            "Useful for retry passes on suspected transient yfinance failures."
        ),
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)

    if args.tickers_file:
        tf_path = Path(args.tickers_file)
        if not tf_path.exists():
            print(f"ERROR: --tickers-file not found: {tf_path}", file=sys.stderr)
            sys.exit(1)
        tickers = [
            line.strip()
            for line in tf_path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not tickers:
            print(f"ERROR: --tickers-file is empty: {tf_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Using --tickers-file ({len(tickers)} tickers) — --top ignored.")
    else:
        top_arg = None if args.top == 0 else args.top
        tickers = get_tickers(conn, top_arg)
        if not tickers:
            print("ERROR: No tickers found in database.", file=sys.stderr)
            sys.exit(1)

    print(
        f"Refreshing {len(tickers)} tickers from yfinance "
        f"(period={args.period})..."
    )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    total_rows = 0
    succeeded_tickers = set()
    failed = []
    n_batches = (len(tickers) - 1) // BATCH + 1

    try:
        for i in range(0, len(tickers), BATCH):
            batch = tickers[i : i + BATCH]
            batch_num = i // BATCH + 1
            print(
                f"  batch {batch_num}/{n_batches}: "
                f"{len(batch)} tickers",
                end=" ",
                flush=True,
            )

            df = fetch_batch(batch, args.period)
            if df is not None and not df.empty:
                got = set(df["ticker"].unique())
                missing = [t for t in batch if t not in got]
            else:
                got = set()
                missing = list(batch)

            if missing:
                print(f"({len(got)} ok, {len(missing)} retrying individually)", end=" ", flush=True)
                singles = []
                for t in missing:
                    sdf = fetch_single(t, args.period)
                    if sdf is not None and not sdf.empty:
                        singles.append(sdf)
                    else:
                        failed.append(t)
                if singles:
                    sdf = pd.concat(singles, ignore_index=True)
                    if df is not None and not df.empty:
                        df = pd.concat([df, sdf], ignore_index=True)
                    else:
                        df = sdf
            else:
                if df is not None and not df.empty:
                    pass

            if df is None or df.empty:
                print("(empty)")
                failed.extend(batch)
                time.sleep(SLEEP_BETWEEN)
                continue

            succeeded_tickers.update(df["ticker"].unique())
            df["fetched_at"] = now
            df.to_sql("prices_live_tmp", conn, if_exists="replace", index=False)
            conn.execute(
                """
                INSERT OR REPLACE INTO prices_live (ticker, date, close, volume, fetched_at)
                SELECT ticker, date, close, volume, fetched_at FROM prices_live_tmp
            """
            )
            conn.execute("DROP TABLE IF EXISTS prices_live_tmp")
            conn.commit()
            total_rows += len(df)
            print(f"-> {len(df)} rows")
            time.sleep(SLEEP_BETWEEN)

    except KeyboardInterrupt:
        print("\nInterrupted. Saving progress so far...")
        conn.commit()

    max_date = conn.execute(
        "SELECT MAX(date) FROM prices_live"
    ).fetchone()[0]
    n_tickers = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM prices_live"
    ).fetchone()[0]

    print()
    print(f"Done. {total_rows} rows across {len(succeeded_tickers)} tickers in this run ({n_tickers} total in DB).")
    print(f"Max date in prices_live: {max_date}")
    if failed:
        msg = (
            f"WARNING: {len(failed)} tickers returned no data: "
            f"{failed[:20]}{'...' if len(failed) > 20 else ''}"
        )
        print(msg, file=sys.stderr)
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
