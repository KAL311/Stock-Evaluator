# FMP top-of-book audit — post go-live diagnostic

**Date:** 2026-07-10 (post commit `5a669bc`)
**Verdict:** **ISSUES.** Two named artifacts must be addressed before a hub is built on the FMP list:
1. **Currency-conversion bug on 19 non-USD ADRs** — SimFin normalizes ALL foreign filers to USD, FMP preserves local currency. TCOM at #6 (V=100.0 max) is the visible symptom.
2. **My earlier "top 10" display code did not filter DELISTED/UNPRICED flags** — SGFY at #5 in the rollback print was a display leak, not a gate leak (gate WORKS on both paths).

Everything else looks CLEAN. FY2025 newcomers (APP/PLTR/NVDA/CRWD/YOU/RIGL/NUTX/IONQ) are LEGIT-FRESH — genuine complete FY2025 filings with real growth SimFin hasn't loaded. compute_history_metrics does not annualize partial fiscal years.

## Part 1 — FMP top-20 table + classification (MEASURED, from `data/fmp/ab_verify_results.csv` `max_fiscal_year=2025` live-mode proxy)

| # | Ticker | Sector | PS_SF | PS_FMP | V | Q | G | S | n_SF | n_FMP | FY_SF | FY_FMP | Latest FMP date | Age | Flags | **Class** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | APP | tech_software | 52.54 | **87.60** | 37.7 | 98.3 | 99.1 | 83.2 | 5 | 6 | 2024 | 2025 | 2025-12-31 | 498 | | **LEGIT-FRESH** |
| 2 | PLTR | tech_software | 75.77 | **85.86** | 5.2 | 94.4 | 97.7 | 98.5 | 5 | 6 | 2024 | 2025 | 2025-12-31 | 507 | | **LEGIT-FRESH** |
| 3 | CEPU | utilities | 83.02 | 83.64 | 99.5 | 93.2 | — | 81.6 | 1 | 0 | 2020 | — | (SF-fallback) | 1901 | | STABLE-STALE |
| 4 | MLI | industrials | 82.53 | 82.86 | 86.9 | 69.4 | 94.1 | 89.4 | 3 | 0 | 2022 | — | (SF-fallback) | 1228 | | STABLE-STALE |
| 5 | GSL | industrials | 82.11 | 82.46 | 94.5 | 93.0 | 93.0 | 71.2 | 4 | 0 | 2023 | — | (SF-fallback) | 842 | | STABLE-STALE |
| 6 | **TCOM** | consumer_disc | 70.87 | **80.39** | **100.0** | 90.0 | 82.8 | 62.7 | 5 | 6 | 2024 | 2025 | 2025-12-31 | 455 | | ⚠️ **ARTIFACT-SUSPECT (currency)** |
| 7 | NEM | industrials | 67.09 | 78.57 | 71.0 | 87.5 | 94.2 | 69.3 | 5 | 6 | 2024 | 2025 | 2025-12-31 | 504 | MOMENTUM_VALUE | LEGIT-FRESH |
| 8 | CTRA | energy | 77.07 | 78.06 | 87.6 | 78.0 | 97.6 | 57.4 | 3 | 0 | 2022 | — | (SF-fallback) | 1229 | **UNPRICED** | GATED-OUT |
| 9 | QDEL | healthcare | 77.86 | 77.86 | 100.0 | 93.8 | — | 63.3 | 2 | 0 | 2021 | — | (SF-fallback) | 1603 | | STABLE-STALE |
| 10 | YOU | tech_software | 80.40 | 77.86 | 86.2 | 95.7 | 79.2 | 76.5 | 5 | 6 | 2024 | 2025 | 2025-12-31 | 499 | | LEGIT-FRESH |
| 11 | NEU | industrials | 78.54 | 77.25 | 66.6 | 95.6 | 68.0 | 88.9 | 5 | 6 | 2024 | 2025 | 2025-12-31 | 511 | QUIET_COMPOUNDER | STABLE |
| 12 | MNDY | tech_software | 79.72 | 77.06 | 14.6 | 93.4 | 91.4 | 77.0 | 5 | 6 | 2024 | 2025 | 2025-12-31 | 480 | | STABLE |
| 13 | AAWW | industrials | 78.32 | 76.76 | 85.0 | 62.2 | 90.7 | 63.5 | 3 | 6 | 2022 | 2022 | 2022-12-31 | 1233 | **DELISTED** | GATED-OUT |
| 14 | POWL | industrials | 71.20 | 76.63 | 69.0 | 85.0 | 94.1 | 86.7 | 5 | 6 | 2024 | 2025 | 2025-09-30 | 597 | | LEGIT-FRESH |
| 15 | META | tech_software | 71.40 | 76.58 | 42.2 | 88.6 | 92.9 | 85.3 | 5 | 6 | 2024 | 2025 | 2025-12-31 | 526 | CROWDED | LEGIT-FRESH |
| 16 | CRWD | tech_software | 77.10 | 76.44 | 3.4 | 78.6 | 93.7 | 77.9 | 5 | 6 | 2024 | 2026 | 2026-01-31 | 487 | | LEGIT-FRESH |
| 17 | NVDA | tech_hardware | 75.71 | 76.37 | 27.6 | 97.0 | 98.8 | 81.2 | 5 | 6 | 2024 | 2026 | 2026-01-25 | 499 | CROWDED | LEGIT-FRESH |
| 18 | ZS | tech_software | 80.57 | 76.36 | 5.9 | 78.5 | 89.6 | 88.7 | 4 | 6 | 2024 | 2025 | 2025-07-31 | 666 | | STABLE-DECREASE |
| 19 | RL | consumer_disc | 77.99 | 75.98 | 27.7 | 87.4 | 86.9 | 97.3 | 5 | 6 | 2024 | 2026 | 2026-03-28 | 414 | | STABLE |
| 20 | EAT | consumer_disc | 71.03 | 75.69 | 74.6 | 56.1 | 75.1 | 100.0 | 5 | 6 | 2025 | 2025 | 2025-06-25 | 329 | | LEGIT-FRESH |

Backing CSV: `data/fmp/topofbook_audit_top20.csv`.

## Newcomer why-it-rose (RIGL, APP, NUTX, YOU, IONQ, TCOM — from the earlier main() go-live top 10)

Each newcomer's FMP FY2025 revenue vs SimFin's last available FY2024 revenue (measured from raw fundamentals CSVs; all FY2025 filings are COMPLETE, published in Feb-Mar 2026):

| Ticker | SimFin last (FY, rev) | FMP FY2025 (date, rev) | YoY change | Verdict |
|---|---|---|---|---|
| RIGL | 2024, 179.3M | 2025-12-31, 294.3M | **+64.1%** | LEGIT-FRESH — real biotech commercial ramp |
| APP | 2024, 3.224B | 2025-12-31, 5.481B | **+70.0%** | LEGIT-FRESH — AppLovin AXON ramp |
| NUTX | 2024, 480M | 2025-12-31, 875M | **+82.4%** | LEGIT-FRESH — Nutex Health footprint growth |
| YOU | 2024, 770.5M | 2025-12-31, 900.8M | +16.9% | LEGIT-FRESH — Clear Secure steady growth |
| **IONQ** | 2024, 43.1M | 2025-12-31, 130.0M | **+201.7%** | LEGIT-FRESH — quantum revenue explosion (per FMP 10-K filed 2026-02-25) |
| **TCOM** | 2024, 7.066B **USD** | 2025-12-31, 60.71B **CNY** | see below | ⚠️ **CURRENCY ARTIFACT** |

**TCOM specifically:** SimFin FY2024 rev = 7.066B, FMP FY2024 rev = 53.294B. Ratio 7.5x — approximately the CNY:USD exchange rate. SimFin normalizes to USD, FMP reports in local currency. TCOM's rev goes to Q/G/V computation in the SAME units as `market_cap` (USD). Under FMP path: `P/S = mkt_cap_USD / rev_CNY = mkt_cap_USD / (rev_USD × ~7.5)` → looks 7.5× cheaper → **V=100.0** (the max). This is the top-20's only currency-conversion artifact. Same class hits all 19 non-USD ADRs.

IONQ is not a currency artifact — its +201.7% is a genuine reported number in USD (`reportedCurrency=USD`).

## Part 2 — FY2025 completeness (MEASURED)

`compute_history_metrics` DOES NOT annualize partial fiscal years. It reads the last N `Fiscal Year` rows keyed by physical Report Date and treats each as a complete fiscal-year observation. There is no fractional-year weighting or annualization code path.

**Every FY2025 (and FY2026) row present in the FMP data is a COMPLETE 10-K filing:**

| Ticker | FMP latest FY | Report Date | Filing Date | Complete? |
|---|---|---|---|---|
| APP | 2025 | 2025-12-31 | 2026-02-19 | ✓ |
| PLTR | 2025 | 2025-12-31 | 2026-02-17 | ✓ |
| RIGL | 2025 | 2025-12-31 | 2026-03-03 | ✓ |
| NUTX | 2025 | 2025-12-31 | 2026-03-05 | ✓ |
| YOU | 2025 | 2025-12-31 | 2026-02-25 | ✓ |
| IONQ | 2025 | 2025-12-31 | 2026-02-25 | ✓ |
| NEM | 2025 | 2025-12-31 | 2026-02 | ✓ |
| MNDY | 2025 | 2025-12-31 | 2026-02 | ✓ |
| META | 2025 | 2025-12-31 | 2026-01 | ✓ |
| NVDA | 2026 | 2026-01-25 | 2026-02-25 | ✓ (Jan year-end filer) |
| CRWD | 2026 | 2026-01-31 | 2026-03-05 | ✓ (Jan year-end filer) |
| RL | 2026 | 2026-03-28 | 2026-05 | ✓ (Mar year-end filer) |
| EAT | 2025 | 2025-06-25 | 2025-08 | ✓ (Jun year-end filer) |
| POWL | 2025 | 2025-09-30 | 2025-11 | ✓ (Sep year-end filer) |

Every filing date is at least 30 days after the fiscal-year-end date, consistent with a real 10-K. FMP does NOT preemptively expose partial-year fiscals ahead of their annual filings.

**Top-20 dependence on FY2025+ data:**
- 12 of 20 top scores depend on an FMP FY2025 (or FY2026) row that SimFin lacks: APP, PLTR, TCOM, NEM, YOU, NEU, MNDY, POWL, META, CRWD, NVDA, ZS, RL, EAT = 14 including RL/EAT with later year-ends → **14 of 20**. Of these, only TCOM is a currency-artifact case; the other 13 are legit.
- 5 of 20 use SimFin fallback (n_FMP=0): CEPU, MLI, GSL, CTRA, QDEL. They score the same on both paths (both use SF).
- 1 (RIGL — not in the max_fy=2025 top-20 but in the main() go-live top-10) similarly FY2025-driven, LEGIT-FRESH.

**Top-100 dependence** (`data/fmp/ab_verify_results.csv` top-100 by potential_score_B):
- 61 depend on a post-2024 FMP row.
- 9 use SF fallback (n_FMP=0).
- 30 have identical n_SF and n_FMP for the newest year.
- **Non-USD ADRs in top-100:** TCOM (#6), plus any of {SPOT, STLA, NIO, NTES, WIT, SAP, HMC, TM, GDS, HUYA, IMCR, IMO, HSAI, JFIN, SNDL, MRUS, DAVA, IMCR, ENB} that reach top-100 — need to spot-scan and confirm. Non-USD count in top-100 is bounded by the 19 non-USD tickers universe-wide.

## Part 3 — SGFY rollback gate finding

### Is the gate applied on both paths?

**YES.** `src/market_screener.py:main()` calls `compute_liveness_and_flag(df, CACHE_DB)` at L4572 AFTER `compute_potential_scores*` returns the scored df, and BEFORE `_persist_flags_to_cache` at L4576. This is downstream of the fundamentals-source gate at L4477. Both the FMP-default path and the SimFin-rollback path flow through the same liveness computation. Verified: SGFY has `flags='DELISTED'` in the current cache state (after the rollback run) with `filing_age_days=1229`.

### So where did SGFY appear at #5 in the rollback top 10?

**In my earlier stdout print.** The diagnostic snippet I ran (`SELECT ... ORDER BY potential_score DESC LIMIT 10`) sorted purely by score without a `WHERE flags NOT LIKE '%DELISTED%' AND flags NOT LIKE '%UNPRICED%'` filter. **The liveness gate flagged SGFY correctly; my display code did not respect the flag.** This is a display-side leak in a diagnostic script, not a screener leak.

The FMP-default path handles SGFY identically — SGFY is `DELISTED`-flagged whether the run source is FMP or SimFin; it does not appear in the FMP top 10 because its FMP-side potential_score is 63.68 (below the top-10 cutoff of ~68), and it would also be gate-excluded even if it did.

### Fix (named, not built)

Any diagnostic / operator-facing "top N" query must include the exclusion:
```sql
WHERE potential_score IS NOT NULL
  AND (flags IS NULL OR (flags NOT LIKE '%DELISTED%' AND flags NOT LIKE '%UNPRICED%'))
```

The screener's own `print_table` and `execute_query` paths (`src/market_screener.py`) do respect flags for interactive display; the leak is only in ad-hoc SQL like the ones I ran during verification.

## Verdict: **ISSUES**

Two things to fix before building the hub on the FMP list:

1. **Currency normalization for 19 non-USD ADRs.** TCOM is the visible case; the same fix protects SAP, STLA, NIO, NTES, WIT, HMC, TM, GDS, HUYA, IMCR, IMO, HSAI, DAVA, JFIN, MRUS, SNDL, SPOT, ENB, and any others that FMP reports in local currency. Options (do NOT auto-pick — operator scopes the fix):
   - **Convert FMP local-currency financials to USD** using FX rate at Report Date (need an FX vendor; FMP has `/stable/historical-price-full-forex` on Starter — cheap).
   - **Skip non-USD tickers** on the FMP path (fall back to SimFin for those 19, which already normalizes to USD).
   - **Flag non-USD tickers** as a new category and exclude from the top-of-book display until fixed.

2. **Display-code hygiene.** Every top-N diagnostic query must apply the DELISTED/UNPRICED flag filter. The screener's interactive path does this; the ad-hoc SQL I ran during go-live verification did not. Update the verification runbook (`scripts/ab_verify_fmp.py` / any go-live smoke check) to apply the same filter when printing tradeable names.

Everything else in the top-of-book audit is CLEAN. FY2025 newcomers are legit; no partial-year annualization; compute_liveness_and_flag applies on both paths.

## Frozen-file guard

`git diff --name-only src/market_screener.py scripts/backtest.v2.py scripts/backtest.py` returns empty for this diagnostic. `phase5-frozen` tag intact. Only `docs/fmp_topofbook_audit.md` and `data/fmp/topofbook_audit_top20.csv` created.
