#!/usr/bin/env python3
"""Post-DQ-v3 verification gates (docs/post_dq_v3_verification.md).

Read-only by default: inspects data/stock_cache.db and reports/.last_top10.json
and prints PASS/FAIL per gate. Exits 0 iff all HARD gates pass.

Usage:
    python scripts/verify_dq_v3.py                # run gates A-G
    python scripts/verify_dq_v3.py --clear-cache  # ONLY clears cache_meta
                                                  # (forces fresh rebuild on
                                                  # next screener run), then
                                                  # exits. Runs no gates.

No imports from src/ — this script must work without SIMFIN_API_KEY and
without triggering any module-import side effects.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data" / "stock_cache.db"
LAST_TOP10 = REPO / "reports" / ".last_top10.json"

# ---- Pre-registered reference values (go-live 2026-07-19, commit 8fb175e) ----
REF_UNIVERSE = 2514
REF_UNIVERSE_TOL = 50
REF_XCHECK_COUNT = 96          # shares_xcheck entries in derived-DQ log
REF_XCHECK_MAX_RATIO = 2.0     # HARD-fail if count > 2x reference
TOP10_TURNOVER_MAX = 5
ANCHORS = {
    # ticker: (pe_lo, pe_hi, dy_lo, dy_hi)  -- deliberately loose bands
    "AAPL": (15.0, 50.0, 0.001, 0.02),
    "MSFT": (20.0, 55.0, 0.002, 0.02),
    "KO":   (15.0, 35.0, 0.015, 0.05),
}
STAPLES_N_MIN = 50
STAPLES_MEAN_BAND = (40.0, 60.0)
STAPLES_STD_BAND = (5.0, 20.0)
PE_ABS_MAX = 1000.0
DY_MAX = 0.50

HARD_FAILS: list[str] = []
WARNS: list[str] = []


def _gate(name: str, ok: bool, detail: str, hard: bool = True) -> None:
    tag = "PASS" if ok else ("FAIL" if hard else "WARN")
    print(f"  [{tag}] Gate {name}: {detail}")
    if not ok:
        (HARD_FAILS if hard else WARNS).append(f"{name}: {detail}")


def _fetch_df(conn: sqlite3.Connection):
    import pandas as pd  # local import: keep --clear-cache dependency-free

    cols = [r[1] for r in conn.execute("PRAGMA table_info(stocks)")]
    need = [
        "ticker", "sector_group", "pe", "pe_ttm", "dividend_yield",
        "valuation_score", "quality_score", "growth_score",
        "sentiment_score", "potential_score", "dq_share_xcheck_failed",
        "flags",
    ]
    have = [c for c in need if c in cols]
    missing = sorted(set(need) - set(have))
    if missing:
        print(f"  NOTE: columns absent from stocks table (ok if renamed): {missing}")
    df = pd.read_sql(f"SELECT {', '.join(have)} FROM stocks", conn)
    return df


def clear_cache_meta() -> int:
    if not DB.exists():
        print(f"ERROR: {DB} not found")
        return 2
    conn = sqlite3.connect(str(DB))
    try:
        n = conn.execute("SELECT COUNT(*) FROM cache_meta").fetchone()[0]
        conn.execute("DELETE FROM cache_meta")
        conn.commit()
        print(f"cache_meta cleared ({n} rows). Next screener run will do a "
              f"fresh rebuild (schema/version check fails on empty meta).")
        return 0
    finally:
        conn.close()


def main() -> int:
    if "--clear-cache" in sys.argv:
        return clear_cache_meta()

    if not DB.exists():
        print(f"ERROR: {DB} not found")
        return 2

    import pandas as pd

    conn = sqlite3.connect(str(DB))
    try:
        meta = dict(conn.execute("SELECT key, value FROM cache_meta"))
        df = _fetch_df(conn)
    finally:
        conn.close()

    print("=" * 72)
    print("Post-DQ-v3 verification gates")
    print(f"  db: {DB}")
    print(f"  cache_meta: schema_version={meta.get('schema_version')!r} "
          f"last_updated={meta.get('last_updated')!r}")
    print(f"  rows: {len(df)}")
    print("=" * 72)

    if str(meta.get("schema_version")) != "14":
        _gate("PRE", False,
              f"schema_version={meta.get('schema_version')!r}, expected 14 — "
              f"the DB predates the xcheck flag; run the pipeline first")
        # Nothing else is meaningful.
        print("\nVERDICT: FAIL (stale schema)")
        return 1

    xmask = pd.to_numeric(df.get("dq_share_xcheck_failed", 0),
                          errors="coerce").fillna(0).astype(bool)
    n_x = int(xmask.sum())
    pot = pd.to_numeric(df["potential_score"], errors="coerce")

    # ---- Gate A: every xcheck row has NULL valuation_score -------------------
    val = pd.to_numeric(df["valuation_score"], errors="coerce")
    offenders = df.loc[xmask & val.notna(), "ticker"].tolist()
    _gate("A", len(offenders) == 0,
          f"{n_x} xcheck-flagged rows; "
          f"{len(offenders)} with non-NULL valuation_score"
          + (f" -> {offenders[:10]}" if offenders else ""))

    # ---- Gate B: MKC specifically + xcheck names still in top decile ---------
    mkc = df.loc[df["ticker"] == "MKC"]
    if mkc.empty:
        _gate("B", True, "MKC not in universe this run (nothing to assert)",
              hard=False)
    else:
        mkc_val = pd.to_numeric(mkc["valuation_score"], errors="coerce").iloc[0]
        mkc_x = bool(xmask.loc[mkc.index[0]])
        ok = (not mkc_x) or (isinstance(mkc_val, float) and math.isnan(mkc_val))
        _gate("B", ok,
              f"MKC xcheck={int(mkc_x)} valuation_score="
              f"{'NULL' if pd.isna(mkc_val) else round(float(mkc_val), 2)} "
              f"potential={round(float(pd.to_numeric(mkc['potential_score'], errors='coerce').iloc[0]), 2) if pd.notna(mkc['potential_score'].iloc[0]) else 'NULL'}")
    if n_x:
        thr = pot.quantile(0.9)
        in_top = df.loc[xmask & (pot >= thr), "ticker"].tolist()
        if in_top:
            print(f"  [REVIEW] xcheck-flagged tickers in top decile "
                  f"(Q/G/S-carried — eyeball each): {in_top}")

    # ---- Gate C: healthy anchors -------------------------------------------
    for t, (plo, phi, dlo, dhi) in ANCHORS.items():
        row = df.loc[df["ticker"] == t]
        if row.empty:
            _gate("C", False, f"{t} missing from universe")
            continue
        pe = pd.to_numeric(row["pe"], errors="coerce").iloc[0]
        dy = pd.to_numeric(row["dividend_yield"], errors="coerce").iloc[0]
        ok = (pd.notna(pe) and plo <= pe <= phi
              and pd.notna(dy) and dlo <= dy <= dhi)
        _gate("C", ok, f"{t}: pe={pe} (band {plo}-{phi}), "
                       f"dy={dy} (band {dlo}-{dhi})")

    # ---- Gate D: DQ volume + universe size stable ---------------------------
    _gate("D", abs(len(df) - REF_UNIVERSE) <= REF_UNIVERSE_TOL,
          f"universe={len(df)} vs ref {REF_UNIVERSE} ±{REF_UNIVERSE_TOL}")
    _gate("D", n_x <= REF_XCHECK_COUNT * REF_XCHECK_MAX_RATIO,
          f"xcheck count={n_x} vs ref ~{REF_XCHECK_COUNT} "
          f"(hard ceiling {int(REF_XCHECK_COUNT * REF_XCHECK_MAX_RATIO)})")

    # ---- Gate E: top-10 turnover -------------------------------------------
    top10_now = (df.assign(_p=pot)
                   .dropna(subset=["_p"])
                   .nlargest(10, "_p")["ticker"].tolist())
    if LAST_TOP10.exists():
        prev = json.loads(LAST_TOP10.read_text()).get("top10", [])
        leavers = [t for t in prev if t not in top10_now]
        entrants = [t for t in top10_now if t not in prev]
        turnover = len(entrants)
        _gate("E", turnover <= TOP10_TURNOVER_MAX,
              f"turnover={turnover} (max {TOP10_TURNOVER_MAX}); "
              f"out={leavers} in={entrants}")
        mkc_in_now = "MKC" in top10_now
        if mkc_in_now:
            mkc_val_ok = pd.isna(
                pd.to_numeric(
                    df.loc[df["ticker"] == "MKC", "valuation_score"],
                    errors="coerce").iloc[0])
            _gate("E", mkc_val_ok,
                  "MKC still top-10 — acceptable ONLY with NULL "
                  "valuation_score (Q/G/S-carried)")
        else:
            print("  [INFO] MKC exited the top-10 (expected outcome of the fix)")
    else:
        _gate("E", True, "no prior .last_top10.json — turnover not assessable",
              hard=False)
    print(f"  [INFO] new top-10: {top10_now}")

    # ---- Gate F: no implausible valuations ----------------------------------
    pe_ttm = pd.to_numeric(df.get("pe_ttm"), errors="coerce")
    dy_all = pd.to_numeric(df.get("dividend_yield"), errors="coerce")
    bad_pe = df.loc[pe_ttm.abs() > PE_ABS_MAX, "ticker"].tolist()
    bad_dy = df.loc[dy_all > DY_MAX, "ticker"].tolist()
    _gate("F", not bad_pe, f"|pe_ttm|>{PE_ABS_MAX}: {bad_pe or 'none'}")
    _gate("F", not bad_dy, f"dividend_yield>{DY_MAX}: {bad_dy or 'none'}")

    # ---- Gate G (soft): staples distribution --------------------------------
    st = pot[df["sector_group"] == "consumer_staples"].dropna()
    if len(st):
        ok = (len(st) >= STAPLES_N_MIN
              and STAPLES_MEAN_BAND[0] <= st.mean() <= STAPLES_MEAN_BAND[1]
              and STAPLES_STD_BAND[0] <= st.std() <= STAPLES_STD_BAND[1])
        _gate("G", ok,
              f"staples n={len(st)} mean={st.mean():.2f} std={st.std():.2f} "
              f"(ref n=68 mean=51.0 std=10.5)", hard=False)
    else:
        _gate("G", False, "no consumer_staples potential scores", hard=False)

    print("=" * 72)
    if HARD_FAILS:
        print(f"VERDICT: FAIL — {len(HARD_FAILS)} hard gate(s):")
        for f_ in HARD_FAILS:
            print(f"  - {f_}")
        print("Per docs/post_dq_v3_verification.md: do not push, do not schedule.")
        return 1
    print(f"VERDICT: PASS ({len(WARNS)} soft warn(s))")
    for w in WARNS:
        print(f"  warn: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
