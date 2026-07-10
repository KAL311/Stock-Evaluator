"""A/B verification harness for USE_FMP_FUNDAMENTALS.

Runs the LIVE screener pipeline twice on the full SimFin universe:
  - Run A: SimFin annual fundamentals (baseline; USE_FMP_FUNDAMENTALS unset).
  - Run B: FMP annual fundamentals with SimFin fallback via
           src.fmp_mapping.load_annual_with_fallback (USE_FMP_FUNDAMENTALS=1).

Everything ELSE is held identical (companies, industries, quarterly income
and cashflow, prices, ownership, momentum, regime, weights). Because both
runs share the full-universe sub-industry rank cohort, only the tickers
whose hist metrics differ move.

Outputs:
  data/fmp/ab_verify_results.csv — per-ticker A vs B subscore + composite
  data/fmp/ab_verify_summary.txt — Spearman rho + decile migration + Jaccard
  Stdout — measured numbers, plus top-decile names IN/OUT under B vs A.

Bit-identical check when the flag is unset is done separately (see
scripts/ab_verify_bit_identical.py — trivial diff of two runs).

Constraint reminders:
  - Zero edits to src/market_screener.py scoring/loader (this file only
    imports and calls; does not modify).
  - Backtest firewall: this script does NOT call anything in
    scripts/backtest*.py.

Reproducing:
  py -3 scripts/ab_verify_fmp.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--max-fiscal-year", type=int, default=2024,
        help="Common FY window; both paths windowed to <= this year.",
    )
    ap.add_argument(
        "--out", type=Path,
        default=REPO_ROOT / "data" / "fmp" / "ab_verify_results.csv",
    )
    ap.add_argument(
        "--summary", type=Path,
        default=REPO_ROOT / "data" / "fmp" / "ab_verify_summary.txt",
    )
    args = ap.parse_args()

    from src import market_screener as ms
    from src.fmp_mapping import load_annual_with_fallback

    print("\n===== A/B verification: USE_FMP_FUNDAMENTALS off vs on =====")
    print(f"  max_fiscal_year: {args.max_fiscal_year}")

    print("\n  Loading SimFin bulk data (shared between both runs)...")
    companies, industries, income, balance, cashflow, income_q, cashflow_q, sp = ms.load_simfin_data()

    print("\n  Computing non-fundamental inputs (prices/momentum/TTM/etc.)...")
    sp_meta = sp.sort_index().groupby(level=0).last()
    sp_meta.columns = [f"sp_{c}" for c in sp_meta.columns]
    betas, vols = ms.compute_betas(sp, sp_meta)
    hi52w, lo52w = ms.compute_52w(sp)
    momo = ms.compute_momentum(sp)
    liq = ms.compute_liquidity(sp)
    ttm = ms.compute_ttm(income_q, cashflow_q)  # STAYS SimFin
    rev_yoy_q = ms.compute_quarterly_yoy_growth(income_q)  # STAYS SimFin
    try:
        t10y = ms.fetch_t10y()
    except Exception as e:
        print(f"    fetch_t10y failed ({e}); using None → scoring path handles missing.")
        t10y = None
    finviz: dict = {}

    # ---- Run A: baseline SimFin ----
    print("\n  === Run A (SimFin baseline) — history + snapshot + score ===")
    m_a = ms.compute_history_metrics_full(income, balance, cashflow)
    hist_a = ms.aggregate_history_metrics(m_a, max_fiscal_year=args.max_fiscal_year)
    print(f"    hist_a: {len(hist_a)} tickers")
    df_a = ms.compute_snapshot(
        companies, industries, income, balance, cashflow,
        sp_meta, betas, vols, t10y, hi52w, lo52w,
        hist=hist_a, ttm=ttm, momo=momo, finviz=finviz,
        liquidity=liq, rev_yoy_q=rev_yoy_q,
    )
    print(f"    df_a: {len(df_a)} rows")
    gics_map = ms.load_gics_for_tickers(companies)
    scored_a = ms.compute_potential_scores_v2(df_a, gics_map=gics_map, verbose=True)

    # ---- Run B: FMP swap ----
    print("\n  === Run B (FMP annual + SimFin fallback) — history + snapshot + score ===")
    inc_b, bal_b, cf_b = load_annual_with_fallback(
        income, balance, cashflow, max_fiscal_year=args.max_fiscal_year, verbose=True
    )
    m_b = ms.compute_history_metrics_full(inc_b, bal_b, cf_b)
    hist_b = ms.aggregate_history_metrics(m_b, max_fiscal_year=args.max_fiscal_year)
    print(f"    hist_b: {len(hist_b)} tickers")
    df_b = ms.compute_snapshot(
        companies, industries, inc_b, bal_b, cf_b,
        sp_meta, betas, vols, t10y, hi52w, lo52w,
        hist=hist_b, ttm=ttm, momo=momo, finviz=finviz,
        liquidity=liq, rev_yoy_q=rev_yoy_q,
    )
    print(f"    df_b: {len(df_b)} rows")
    scored_b = ms.compute_potential_scores_v2(df_b, gics_map=gics_map, verbose=True)

    # ---- TTM-derived-field invariance check (verifies quarterly path untouched) ----
    # PURE-TTM columns (ebitda_ttm, net_income_ttm, revenue_ttm, fcf_ttm,
    # revenue_growth_yoy_q) are computed directly from income_q / cashflow_q,
    # both of which are SimFin on both runs. They MUST be identical to prove
    # the quarterly path is untouched. Derived TTM composites like
    # ev_ebitda_ttm mix balance-sheet EV → not usable for invariance because
    # balance-sheet cur_liab/debt legitimately changed under the swap.
    print("\n  Quarterly-path invariance (pure TTM columns):")
    for col in ("ebitda_ttm", "net_income_ttm", "revenue_ttm", "fcf_ttm",
                "revenue_growth_yoy_q"):
        if col in df_a.columns and col in df_b.columns:
            common = pd.merge(
                df_a[["ticker", col]].rename(columns={col: f"{col}_A"}),
                df_b[["ticker", col]].rename(columns={col: f"{col}_B"}),
                on="ticker",
            ).dropna()
            if len(common):
                delta = (common[f"{col}_A"] - common[f"{col}_B"]).abs()
                same = (delta < 1e-6).mean() * 100
                print(f"    {col:<25} identical on {same:.2f}% of {len(common)} rows  max|d|={delta.max():.4g}")

    # ---- Emit results ----
    subs = ["valuation_score", "quality_score", "growth_score", "sentiment_score", "potential_score"]
    a_keep = ["ticker"] + [c for c in subs if c in scored_a.columns]
    b_keep = ["ticker"] + [c for c in subs if c in scored_b.columns]
    a = scored_a[a_keep].rename(columns={c: f"{c}_A" for c in a_keep if c != "ticker"})
    b = scored_b[b_keep].rename(columns={c: f"{c}_B" for c in b_keep if c != "ticker"})
    merged = a.merge(b, on="ticker", how="outer")
    merged.to_csv(args.out, index=False)
    print(f"\n  Wrote {args.out} — {len(merged)} tickers")

    # ---- Summary ----
    lines = []
    lines.append("A/B verification summary — USE_FMP_FUNDAMENTALS off vs on (MEASURED)")
    lines.append("=" * 70)
    scored = merged.dropna(subset=["potential_score_A", "potential_score_B"]).copy()
    lines.append(f"tickers scored on both paths: {len(scored)}")
    for s in subs:
        a_c, b_c = f"{s}_A", f"{s}_B"
        if a_c in scored.columns and b_c in scored.columns:
            d = scored.dropna(subset=[a_c, b_c])
            if len(d) >= 10:
                rho, _ = spearmanr(d[a_c], d[b_c])
                mad = float((d[a_c] - d[b_c]).abs().mean())
                med = float((d[a_c] - d[b_c]).abs().median())
                lines.append(
                    f"  {s:<20} n={len(d):>4}  rho={rho:>6.4f}  "
                    f"mean|delta|={mad:>6.2f}  median|delta|={med:>6.2f}"
                )
    if len(scored) >= 10:
        scored["dec_A"] = pd.qcut(scored["potential_score_A"].rank(method="first"), 10, labels=False) + 1
        scored["dec_B"] = pd.qcut(scored["potential_score_B"].rank(method="first"), 10, labels=False) + 1
        mig = (scored["dec_A"] != scored["dec_B"]).mean() * 100
        within1 = ((scored["dec_A"] - scored["dec_B"]).abs() <= 1).mean() * 100
        top_a = set(scored[scored["dec_A"] == 10]["ticker"])
        top_b = set(scored[scored["dec_B"] == 10]["ticker"])
        jac = len(top_a & top_b) / max(1, len(top_a | top_b))
        entering = sorted(top_b - top_a)
        leaving = sorted(top_a - top_b)
        lines.append(f"\nDecile migration: {mig:.1f}%   within ±1 decile: {within1:.1f}%")
        lines.append(f"Top-decile Jaccard: {jac:.4f}  |A|={len(top_a)}  |B|={len(top_b)}  overlap={len(top_a & top_b)}")
        lines.append(f"Names ENTERING top decile under B (not in A) — {len(entering)}:")
        lines.append("  " + ", ".join(entering[:60]) + (" ..." if len(entering) > 60 else ""))
        lines.append(f"Names LEAVING top decile under B (in A but not B) — {len(leaving)}:")
        lines.append("  " + ", ".join(leaving[:60]) + (" ..." if len(leaving) > 60 else ""))
    else:
        lines.append("insufficient rows for decile analysis")

    for line in lines:
        print(line)
    args.summary.write_text("\n".join(lines))
    print(f"\n  Wrote {args.summary}")


if __name__ == "__main__":
    main()
