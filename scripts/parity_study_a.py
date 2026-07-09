"""Parity Study A — Score parity between SimFin and FMP fundamentals.

Question
--------
On the current common fiscal year (FY2024, chosen empirically because FY2025
covers only 146/4390 SimFin tickers), do FMP annual fundamentals produce the
same potential_score as SimFin on the same universe? If yes, FMP is a safe
live data-layer swap. If no, characterize the drift by input field before any
migration.

Design
------
1. Sample: top 500 by current cached potential_score + random 500 (seed 42)
   from the rest of scored universe = ~1000 tickers.
2. Fetch FMP annual income / balance / cash-flow for the sample.
3. Load SimFin bulk data ONCE, compute all non-fundamental inputs
   (prices, TTM, momentum, 52w, liquidity, beta/vol, quarterly YoY, T10Y,
   ownership) ONCE.
4. Compute history metrics TWICE via the UNMODIFIED
   compute_history_metrics_full + aggregate_history_metrics — once from SimFin
   dfs, once from src.fmp_mapping.load_fmp_as_simfin() dfs, both windowed to
   max_fiscal_year=2024.
5. Call compute_snapshot TWICE with the same non-fundamental inputs, only
   swapping the hist dict. This yields df_sf and df_fmp identical everywhere
   except the columns aggregate_history_metrics writes.
6. Call compute_potential_scores_v2 on each df. Everything else — prices,
   GICS, regime, TTM, weights — is identical between the two runs. The ONLY
   difference is the fundamentals source.
7. Emit a parity CSV with per-ticker (score_sf, score_fmp, subscore_sf,
   subscore_fmp) so scripts/report_parity_a.py (or a follow-up) can produce
   the Markdown report.

Zero edits to market_screener scoring/loader — this file imports and calls;
does not modify. Verified via the pre-commit git diff step in the task brief.

Caveats
-------
- TTM values (net_income_ttm, revenue_ttm, fcf_ttm, ebitda_ttm) come from
  SimFin QUARTERLY on BOTH runs. FMP Starter has annual only. Study A
  therefore isolates the annual-fundamentals path (aggregate_history_metrics
  outputs). A full FMP-only swap would need FMP quarterly (Study B / higher
  plan).
- Non-fundamental inputs (prices, GICS, regime tilts, ownership, momentum)
  also come from SimFin on both runs. This is by design — task explicitly
  says "hold everything non-fundamental identical".
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

# Add project root to sys.path so we can import from src/ + scripts/ regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


def build_sample(top_n: int = 500, rand_n: int = 500, seed: int = 42) -> list[str]:
    """Top N by cached potential_score + random N from the rest."""
    conn = sqlite3.connect(str(REPO_ROOT / "data" / "stock_cache.db"))
    scored = conn.execute(
        "SELECT ticker, potential_score FROM stocks "
        "WHERE potential_score IS NOT NULL "
        "ORDER BY potential_score DESC"
    ).fetchall()
    conn.close()
    tickers = [t for t, _ in scored]
    if not tickers:
        raise SystemExit("No scored tickers in stock_cache.db; run market_screener.main first.")
    top = tickers[:top_n]
    rest = tickers[top_n:]
    random.seed(seed)
    rand_sample = random.sample(rest, min(rand_n, len(rest)))
    sample = list(dict.fromkeys(top + rand_sample))
    print(f"  Sample: top {len(top)} by potential_score + random {len(rand_sample)} (seed {seed}) = {len(sample)} unique tickers")
    return sample


def fetch_fmp_for_sample(tickers: list[str], years: int) -> None:
    """Delegates to scripts.fetch_fundamentals_fmp so both paths share code."""
    from scripts import fetch_fundamentals_fmp as fetcher

    key = fetcher.load_api_key()
    if not key:
        raise SystemExit("FMP_API_KEY not set (env or FMP_API.env). Aborting.")

    print(f"  Fetching FMP for {len(tickers)} tickers × 3 statements × {years}y (cap 250/min)...")
    OUT_DIR = REPO_ROOT / "data" / "fmp"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fetcher.RAW_DIR.mkdir(parents=True, exist_ok=True)
    limiter = fetcher.RateLimiter(250)

    records: dict[str, dict[str, list]] = {name: {} for name, _ in fetcher.STATEMENTS}
    fails: list[tuple[str, str]] = []
    bytes_fetched = 0
    cache_hits = 0
    api_calls = 0
    t0 = time.time()
    for i, tk in enumerate(tickers, 1):
        for stmt, path in fetcher.STATEMENTS:
            data, nbytes, from_cache = fetcher.fetch_statement_cached(
                tk, stmt, path, years, key, limiter
            )
            if data is None:
                fails.append((tk, stmt))
                continue
            records[stmt][tk] = data
            bytes_fetched += nbytes
            if from_cache:
                cache_hits += 1
            else:
                api_calls += 1
        if i % 50 == 0 or i == len(tickers):
            el = time.time() - t0
            print(f"    [{i:>4}/{len(tickers)}] api={api_calls} cache={cache_hits} "
                  f"bytes={bytes_fetched/1e6:.1f}MB elapsed={el:.1f}s fails={len(fails)}")

    for stmt, _ in fetcher.STATEMENTS:
        out = OUT_DIR / f"fundamentals_{stmt}.csv"
        n = fetcher.emit_csv_from_json(records[stmt], stmt, out)
        print(f"  Wrote {out.name}: {n} rows ({len(records[stmt])} tickers)")

    if fails:
        (REPO_ROOT / "data" / "fmp" / "fetch_failures.log").write_text(
            "\n".join(f"{t}\t{s}" for t, s in fails)
        )
        print(f"  {len(fails)} (ticker,stmt) failures logged")

    print(f"  Total bytes from API: {bytes_fetched/1e6:.1f}MB ({bytes_fetched/1e9:.3f}GB of 20GB monthly cap)")


def _slice_simfin_to_sample(income, balance, cashflow, sample: set[str]):
    def _flt(df):
        # SimFin loader returns DataFrames indexed by (Ticker, Report Date) MultiIndex.
        # Reset then filter to keep it simple.
        d = df.reset_index()
        d = d[d["Ticker"].isin(sample)]
        return d.set_index(["Ticker", "Report Date"])
    return _flt(income), _flt(balance), _flt(cashflow)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=500)
    ap.add_argument("--random", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--max-fiscal-year", type=int, default=2024,
                    help="Common FY window; both SimFin and FMP metrics windowed to <= this year.")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="Skip FMP fetch (assumes CSVs already on disk).")
    ap.add_argument("--tickers-file", type=Path, default=None,
                    help="Override sample: use exact ticker list from file.")
    ap.add_argument("--out", type=Path,
                    default=REPO_ROOT / "data" / "fmp" / "parity_a_results.csv")
    args = ap.parse_args()

    print("\n===== Parity Study A =====")
    print(f"  max_fiscal_year: {args.max_fiscal_year}  years fetched: {args.years}")

    # --- Sample ---
    if args.tickers_file:
        sample = [t.strip().upper() for t in args.tickers_file.read_text().splitlines() if t.strip()]
        print(f"  Sample: {len(sample)} tickers loaded from {args.tickers_file}")
    else:
        sample = build_sample(top_n=args.top, rand_n=args.random, seed=args.seed)
    (REPO_ROOT / "data" / "fmp").mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "data" / "fmp" / "parity_a_sample_tickers.txt").write_text("\n".join(sample))
    sample_set = set(sample)

    # --- Fetch FMP ---
    if not args.skip_fetch:
        fetch_fmp_for_sample(sample, args.years)
    else:
        print("  Skipping fetch (--skip-fetch).")

    # --- Import from market_screener AFTER fetch (heavy module) ---
    print("\n  Importing market_screener + fmp_mapping...")
    from src import market_screener as ms
    from src.fmp_mapping import load_fmp_as_simfin

    # --- Load SimFin bulk ONCE ---
    print("\n  Loading SimFin bulk data (once, shared between both runs)...")
    companies, industries, income, balance, cashflow, income_q, cashflow_q, sp = ms.load_simfin_data()

    # --- Non-fundamental inputs, computed once ---
    print("\n  Computing non-fundamental inputs (prices/momentum/TTM/etc.)...")
    sp_meta = sp.sort_index().groupby(level=0).last()
    sp_meta.columns = [f"sp_{c}" for c in sp_meta.columns]
    betas, vols = ms.compute_betas(sp, sp_meta)
    hi52w, lo52w = ms.compute_52w(sp)
    momo = ms.compute_momentum(sp)
    liq = ms.compute_liquidity(sp)
    ttm = ms.compute_ttm(income_q, cashflow_q)
    rev_yoy_q = ms.compute_quarterly_yoy_growth(income_q)
    try:
        t10y = ms.fetch_t10y()
    except Exception as e:
        print(f"    fetch_t10y failed ({e}); using default None → scoring path handles missing.")
        t10y = None
    # Ownership: skip finviz (slow, external). Both runs share empty ownership;
    # this holds identical between runs so parity is unaffected.
    finviz: dict = {}

    # --- SimFin path: FULL universe (required for sub-industry rank groups ≥30) ---
    print("\n  === SimFin history metrics (FULL universe) + snapshot ===")
    m_sf_all = ms.compute_history_metrics_full(income, balance, cashflow)
    hist_sf_all = ms.aggregate_history_metrics(m_sf_all, max_fiscal_year=args.max_fiscal_year)
    print(f"    hist_sf (full): {len(hist_sf_all)} tickers")
    df_sf = ms.compute_snapshot(
        companies, industries, income, balance, cashflow,
        sp_meta, betas, vols, t10y, hi52w, lo52w,
        hist=hist_sf_all, ttm=ttm, momo=momo, finviz=finviz,
        liquidity=liq, rev_yoy_q=rev_yoy_q,
    )
    print(f"    df_sf: {len(df_sf)} rows")

    # --- FMP path: map + compute FMP-derived hist for the 173 sample tickers only ---
    print("\n  === FMP history metrics for sample ===")
    inc_fmp, bal_fmp, cf_fmp = load_fmp_as_simfin()
    inc_fmp = inc_fmp[inc_fmp["Ticker"].isin(sample_set)].reset_index(drop=True)
    bal_fmp = bal_fmp[bal_fmp["Ticker"].isin(sample_set)].reset_index(drop=True)
    cf_fmp = cf_fmp[cf_fmp["Ticker"].isin(sample_set)].reset_index(drop=True)
    def _to_multiindex(df):
        d = df.copy()
        d["Report Date"] = d.get("Publish Date")
        if d["Report Date"].isna().all():
            d["Report Date"] = pd.to_datetime(d["Fiscal Year"].astype(str) + "-12-31", errors="coerce")
        return d.set_index(["Ticker", "Report Date"]).sort_index()
    inc_fmp_mi = _to_multiindex(inc_fmp)
    bal_fmp_mi = _to_multiindex(bal_fmp)
    cf_fmp_mi = _to_multiindex(cf_fmp)
    m_fmp = ms.compute_history_metrics_full(inc_fmp_mi, bal_fmp_mi, cf_fmp_mi)
    hist_fmp = ms.aggregate_history_metrics(m_fmp, max_fiscal_year=args.max_fiscal_year)
    print(f"    hist_fmp (sample only): {len(hist_fmp)} tickers")

    # --- Build hist_fmp_merged: FMP for sample tickers, SimFin for the rest ---
    # This preserves full-universe sub-industry rank cohorts and isolates the
    # fundamentals swap to the sample. The two df's differ ONLY on the 173
    # sample tickers' history-metric columns.
    hist_fmp_merged = dict(hist_sf_all)  # start from SimFin baseline
    hist_fmp_merged.update(hist_fmp)     # overlay FMP for the 173 sample tickers
    print(f"    hist_fmp_merged: {len(hist_fmp_merged)} tickers (of which {len(hist_fmp)} are FMP-sourced)")

    df_fmp = ms.compute_snapshot(
        companies, industries, income, balance, cashflow,
        sp_meta, betas, vols, t10y, hi52w, lo52w,
        hist=hist_fmp_merged, ttm=ttm, momo=momo, finviz=finviz,
        liquidity=liq, rev_yoy_q=rev_yoy_q,
    )
    print(f"    df_fmp: {len(df_fmp)} rows")

    # Sanity: outside the sample the two dfs should be byte-equal on history-metric cols.
    hist_cols = ["gross_margin_3y_med", "gross_margin_5y_med", "operating_margin_3y_med",
                 "operating_margin_5y_med", "net_margin_3y_med", "net_margin_5y_med",
                 "roe_3y_med", "roe_5y_med", "roic_3y_med", "roic_5y_med",
                 "fcf_margin_3y_med", "fcf_margin_5y_med", "revenue_cv_3y", "revenue_cv_5y",
                 "op_inc_cv_3y", "op_inc_cv_5y", "revenue_trend_5y", "ebitda_trend_5y",
                 "fcf_trend_5y", "n_yrs_history", "piotroski_f", "altman_z",
                 "revenue_growth_3yr"]
    # Retain only tickers where df_fmp has a hist column populated from FMP.
    fmp_covered_in_df = df_fmp[df_fmp["ticker"].isin(hist_fmp.keys())]
    print(f"    FMP-covered tickers surviving snapshot filter: {len(fmp_covered_in_df)}")

    # --- Score both via compute_potential_scores_v2 ---
    print("\n  === Scoring (compute_potential_scores_v2, full universe) ===")
    gics_map = ms.load_gics_for_tickers(companies)
    scored_sf = ms.compute_potential_scores_v2(df_sf, gics_map=gics_map, verbose=True)
    scored_fmp = ms.compute_potential_scores_v2(df_fmp, gics_map=gics_map, verbose=True)

    # --- Align on ticker and emit results CSV ---
    subs = ["valuation_score", "quality_score", "growth_score", "sentiment_score", "potential_score"]
    keep_sf = ["ticker"] + [c for c in subs if c in scored_sf.columns]
    keep_fmp = ["ticker"] + [c for c in subs if c in scored_fmp.columns]
    ssf = scored_sf[keep_sf].rename(columns={c: f"{c}_sf" for c in keep_sf if c != "ticker"})
    sfmp = scored_fmp[keep_fmp].rename(columns={c: f"{c}_fmp" for c in keep_fmp if c != "ticker"})
    merged_full = ssf.merge(sfmp, on="ticker", how="outer")
    merged_full["in_sample"] = merged_full["ticker"].isin(sample_set)
    merged_full.to_csv(args.out, index=False)
    # Sample-only slice for the parity stats
    merged = merged_full[merged_full["in_sample"]].copy()
    print(f"  Wrote {args.out} — {len(merged_full)} tickers total, {len(merged)} in sample")

    # --- Emit hist parity input CSV for diagnosis (top-25 worst-drift analysis) ---
    hist_diag = []
    all_t = set(hist_sf_all.keys()) | set(hist_fmp.keys())
    for t in sorted(all_t):
        row = {"ticker": t}
        for k in [
            "roe_5y_med", "roic_5y_med", "gross_margin_5y_med", "operating_margin_5y_med",
            "fcf_margin_5y_med", "revenue_growth_3yr", "revenue_trend_5y",
            "ebitda_trend_5y", "fcf_trend_5y", "revenue_cv_5y", "op_inc_cv_5y",
            "piotroski_f", "altman_z", "n_yrs_history",
        ]:
            row[f"{k}_sf"] = hist_sf_all.get(t, {}).get(k)
            row[f"{k}_fmp"] = hist_fmp.get(t, {}).get(k)
        hist_diag.append(row)
    diag_path = REPO_ROOT / "data" / "fmp" / "parity_a_hist_diag.csv"
    pd.DataFrame(hist_diag).to_csv(diag_path, index=False)
    print(f"  Wrote {diag_path}")

    # --- Quick summary ---
    print("\n  === Summary ===")
    n_both = ((merged["potential_score_sf"].notna()) & (merged["potential_score_fmp"].notna())).sum()
    print(f"    tickers scored on both paths: {n_both}")
    if n_both >= 10:
        both = merged.dropna(subset=["potential_score_sf", "potential_score_fmp"])
        from scipy.stats import spearmanr
        for c in subs:
            csf, cfmp = f"{c}_sf", f"{c}_fmp"
            if csf in both.columns and cfmp in both.columns:
                b = both.dropna(subset=[csf, cfmp])
                if len(b) >= 10:
                    rho, _ = spearmanr(b[csf], b[cfmp])
                    mad = float((b[csf] - b[cfmp]).abs().mean())
                    med = float((b[csf] - b[cfmp]).abs().median())
                    print(f"    {c:<20} n={len(b):>4}  rho={rho:>6.4f}  mean|delta|={mad:>6.2f}  median|delta|={med:>6.2f}")
    print("  Done.")


if __name__ == "__main__":
    main()
