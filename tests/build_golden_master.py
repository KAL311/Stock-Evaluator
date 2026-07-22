#!/usr/bin/env python3
"""Build (or rebuild) the golden-master regression fixture.

WHEN TO RUN: only on a build you have just verified (post-DQ-v3 gates
passed, or any future deliberate model change you are willing to certify).
The golden master is the executable definition of "the model I validated";
rebuilding it on an unverified build defeats the point.

WHAT IT FREEZES:
  1. tests/fixtures/frozen_stocks.pkl      -- full stocks table (scorer inputs)
  2. tests/fixtures/factor_decay.json      -- copy of data/factor_decay.json
  3. tests/fixtures/factor_etf_holdings.json -- copy of holdings file
  4. tests/fixtures/golden_scores.json     -- expected scorer outputs:
       - per-ticker records for a sentinel set (~60 names: fixed liquid
         anchors + top 25 + bottom 10 by potential + every xcheck-flagged
         ticker)
       - full-universe distribution stats
       - freeze metadata: git HEAD, module knobs (seed, n_resamples,
         decay enabled), sha256 of the calibration files

The paired test (test_golden_master.py) re-runs the production scorer
(`compute_potential_scores`) on the frozen inputs and requires exact
agreement. It SKIPS (loudly) if data/factor_decay.json or the holdings
file has drifted from the frozen sha — recalibration legitimately changes
scores, and the correct response is to verify + re-freeze, not to let the
test rot red.

Usage:
    python tests/build_golden_master.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
DB = REPO / "data" / "stock_cache.db"
DECAY = REPO / "data" / "factor_decay.json"
HOLDINGS = REPO / "data" / "factor_etf_holdings.json"

ANCHOR_TICKERS = ["AAPL", "MSFT", "KO", "JPM", "XOM", "PLD", "NEE",
                  "LLY", "COST", "NVDA", "MKC"]

SCORE_COLS_WANTED = [
    "potential_score", "valuation_score", "quality_score",
    "growth_score", "sentiment_score",
    "resampled_median", "resampled_p05", "resampled_p95",
    "resampled_iqr", "top_decile_pct", "robust",
]


def _sha(p: Path) -> str | None:
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True
        ).strip()
    except Exception:
        return "unknown"


def main() -> int:
    # Pin env exactly as conftest.py does, BEFORE importing the module.
    os.environ.setdefault("SIMFIN_API_KEY", "test-dummy-key")
    os.environ["RESAMPLE_SEED"] = "42"
    os.environ["N_RESAMPLES"] = "1000"
    os.environ["DECAY_ENABLED"] = "1"
    sys.path.insert(0, str(REPO))

    import pandas as pd
    import src.market_screener as ms

    if not DB.exists():
        print(f"ERROR: {DB} missing — run the pipeline first.")
        return 2

    FIXTURES.mkdir(parents=True, exist_ok=True)

    # ---- 1. Freeze inputs ---------------------------------------------------
    conn = sqlite3.connect(str(DB))
    try:
        meta = dict(conn.execute("SELECT key, value FROM cache_meta"))
        df = pd.read_sql("SELECT * FROM stocks", conn)
    finally:
        conn.close()

    if str(meta.get("schema_version")) != str(ms.CACHE_SCHEMA_VERSION):
        print(f"ERROR: DB schema_version={meta.get('schema_version')!r} != "
              f"code CACHE_SCHEMA_VERSION={ms.CACHE_SCHEMA_VERSION}. "
              f"Run a fresh rebuild before freezing.")
        return 2

    df.to_pickle(FIXTURES / "frozen_stocks.pkl")
    print(f"  frozen inputs: {len(df)} rows -> fixtures/frozen_stocks.pkl")

    # ---- 2. Freeze calibration files ---------------------------------------
    for src, name in ((DECAY, "factor_decay.json"),
                      (HOLDINGS, "factor_etf_holdings.json")):
        if src.exists():
            (FIXTURES / name).write_bytes(src.read_bytes())
            print(f"  frozen calib:  {name} (sha {_sha(src)[:12]})")
        else:
            print(f"  NOTE: {src.name} absent — recorded as null")

    # ---- 3. Score deterministically ----------------------------------------
    print("  scoring frozen universe with compute_potential_scores "
          "(production v1 path, seed=42)...")
    scored = ms.compute_potential_scores(df.copy(), verbose=False)

    score_cols = [c for c in SCORE_COLS_WANTED if c in scored.columns]
    pot = pd.to_numeric(scored["potential_score"], errors="coerce")

    xcheck = pd.to_numeric(
        scored.get("dq_share_xcheck_failed", 0), errors="coerce"
    ).fillna(0).astype(bool)

    sentinels = set(ANCHOR_TICKERS)
    sentinels |= set(scored.assign(_p=pot).dropna(subset=["_p"])
                     .nlargest(25, "_p")["ticker"])
    sentinels |= set(scored.assign(_p=pot).dropna(subset=["_p"])
                     .nsmallest(10, "_p")["ticker"])
    sentinels |= set(scored.loc[xcheck, "ticker"])

    records = {}
    for t in sorted(sentinels):
        row = scored.loc[scored["ticker"] == t]
        if row.empty:
            continue
        rec = {}
        for c in score_cols:
            v = row[c].iloc[0]
            rec[c] = None if pd.isna(v) else round(float(v), 6)
        records[t] = rec

    stats = {
        "n_rows": int(len(scored)),
        "n_potential_nonnull": int(pot.notna().sum()),
        "potential_mean": round(float(pot.mean()), 6),
        "potential_std": round(float(pot.std()), 6),
        "potential_q10": round(float(pot.quantile(0.10)), 6),
        "potential_q50": round(float(pot.quantile(0.50)), 6),
        "potential_q90": round(float(pot.quantile(0.90)), 6),
        "n_xcheck_flagged": int(xcheck.sum()),
    }

    golden = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_head(),
        "cache_last_updated": meta.get("last_updated"),
        "schema_version": meta.get("schema_version"),
        "knobs": {
            "RESAMPLE_SEED": ms.RESAMPLE_SEED,
            "N_RESAMPLES": ms.N_RESAMPLES,
            "DECAY_ENABLED": ms.DECAY_ENABLED,
        },
        "calib_sha256": {
            "factor_decay.json": _sha(DECAY),
            "factor_etf_holdings.json": _sha(HOLDINGS),
        },
        "score_cols": score_cols,
        "stats": stats,
        "sentinels": records,
    }
    (FIXTURES / "golden_scores.json").write_text(json.dumps(golden, indent=2))
    print(f"  golden master: {len(records)} sentinel tickers, "
          f"universe stats recorded")
    print(f"  git HEAD at freeze: {golden['git_head']}")
    print()
    print("Done. Commit tests/fixtures/ so the certification travels with "
          "the repo:")
    print("  git add tests/ && git commit -m "
          "\"Golden master: freeze verified scorer state\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
