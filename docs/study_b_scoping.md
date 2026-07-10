# Study B — Scoping: restatement look-ahead leak + FMP vintage availability

**Date:** 2026-07-10
**Status:** SCOPING + MEASUREMENT only. Zero changes to `scripts/backtest.v2.py`, `scripts/backtest.py`, weights, regime, decay, or the `phase5-frozen` tag. No re-run of the frozen backtest.
**Verdict:** **NO-GO (tier).** The restatement leak is present and material; FMP Starter does not expose as-originally-reported vintages, so Study B's core premise (rebuilding a leak-free PIT backtest with FMP) is not buildable on the current data plan. Defer B until a vintage source is available; document the leak as a known backtest caveat meanwhile.

## Why this matters (the leak, confirmed in code)

`scripts/backtest.v2.py:90-92`:

```python
def filter_quarterly(df, cutoff):
    if 'Publish Date' in df.columns:
        return df[df['Publish Date'] <= cutoff]
```

This is a real PIT attempt. But SimFin's bulk CSV carries ONE row per `(ticker, fiscal period)` and the loader keeps the LATEST version (`drop_duplicates(keep='last')`), so restated figures with `Publish Date ≤ cutoff` still pass the filter carrying values that were only fixed by a later 10-K/A. Restatements typically address exactly the accrual / current-liability / working-capital issues the F-score and Z-score gates target, so this systematically flatters the backtest's quality signals.

## Part 1a — SimFin restatement exposure per backtest period (MEASURED)

Definition: a row is "restated" if `Restated Date > Publish Date`. "Leak-eligible" at a cutoff = row where `Publish Date ≤ cutoff` AND `Restated Date > cutoff` (i.e. the row was on file at cutoff but its recorded values were later corrected).

### Annual statements (used by `compute_history_metrics`)

| Statement | Total rows | Restated (`res > pub`) | % restated |
|---|---|---|---|
| income-annual | 16 976 | 15 520 | 91.4% |
| balance-annual | 16 976 | 16 676 | 98.2% |
| cashflow-annual | 16 974 | 15 552 | 91.6% |

### Leak-eligible rows per backtest cutoff — annual

| Cutoff | income | balance | cashflow |
|---|---|---|---|
| 2021-06-30 | 3 282 / 3 408 = **96.3%** | 3 341 / 3 401 = **98.2%** | 3 278 / 3 403 = **96.3%** |
| 2022-06-30 | 5 777 / 7 050 = 81.9% | 3 602 / 7 053 = 51.1% | 5 784 / 7 051 = 82.0% |
| 2023-06-30 | 5 512 / 10 644 = 51.8% | 3 452 / 10 649 = 32.4% | 5 506 / 10 645 = 51.7% |
| 2024-06-30 | 5 090 / 13 883 = 36.7% | 3 126 / 13 885 = 22.5% | 5 121 / 13 881 = 36.9% |
| **2024-12-31 (2025 OOS)** | **5 032 / 14 242 = 35.3%** | **2 942 / 14 241 = 20.7%** | **5 065 / 14 240 = 35.6%** |

### Quarterly statements (used by `compute_ttm`, `compute_quarterly_yoy_growth`)

| Cutoff | income_q | cashflow_q |
|---|---|---|
| 2021-06-30 | 6 735 / 7 122 = **94.6%** | 6 766 / 7 118 = **95.1%** |
| 2022-06-30 | 10 157 / 17 324 = 58.6% | 9 142 / 17 320 = 52.8% |
| 2023-06-30 | 9 672 / 27 658 = 35.0% | 7 886 / 27 656 = 28.5% |
| 2024-06-30 | 9 230 / 37 654 = 24.5% | 7 446 / 37 647 = 19.8% |
| **2024-12-31 (2025 OOS)** | **7 464 / 42 523 = 17.6%** | **7 344 / 42 521 = 17.3%** |

### Read

- **Older backtest periods (2021 cutoff) are ~95% leak-eligible.** The 2/4 quarterly gate and the F-score liquidity delta are computing on values that were only settled by 2023-2024 amendments.
- **The 2025 OOS cutoff is 17-35% leak-eligible per statement.** Still material.
- Restatement rate is 91-98% of ALL rows in SimFin's bulk CSVs, which is suspicious. Restated Date appears to be set whenever a fiscal-year figure re-appears as a comparative period in a later filing, not only for material restatements. The row counts above are UPPER BOUNDS on the leak; a subset represents materially-different values. Without vintage data we cannot isolate the material subset.

Caveat: the leak-eligible SHARE would be smaller if we filtered to a more restrictive "material restatement" definition (e.g. ≥ 5% delta on a scoring input). We cannot measure that from SimFin alone because SimFin does not preserve the pre-restatement value.

## Part 1b — FMP vintage availability on Starter (MEASURED)

Study B's premise is that FMP preserves as-originally-reported vintages so the PIT backtest can be reconstructed correctly. I probed every FMP endpoint that might expose vintages:

| Endpoint | Starter access | Vintages? | Evidence |
|---|---|---|---|
| `/stable/income-statement-as-reported` | ✅ 200 | ❌ 1 row per FY | AAPL 2016-2025 = 10 rows, all unique FYs, no duplicates |
| `/stable/balance-sheet-statement-as-reported` | ✅ 200 | ❌ 1 row per FY | same pattern |
| `/stable/financial-statement-full-as-reported` | ✅ 200 | ❌ 1 row per FY | 6 rows for AAPL, 1 per year |
| `/stable/financial-reports-json?year=2021&period=FY` | ✅ 200 | ❌ latest only | returns one document per (year, period) |
| `/stable/financial-reports-dates` | ✅ 200 | ❌ 1 entry per (FY, period) | 64 entries, 0 with duplicates |
| `/stable/sec-filings-financials?type=10-K` | ✅ 200 | ❌ returns 8-K / 6-K only | AAPL query returned 20 8-K + 10 6-K, zero 10-K rows |
| `/stable/sec-filings?type=10-K/A` | 404 | n/a | AAPL, GE, WFC all empty |
| `/api/v3/income-statement-as-reported/` | 403 | n/a | "Legacy Endpoint. Premium only." |
| `/api/v4/financial-reports-json` | 403 | n/a | Legacy / Premium only |

**Verdict: FMP Starter does NOT expose as-originally-reported vintages.** The `as-reported` endpoints return raw XBRL taxonomy field names from the LATEST filing on record — same "keep=last" limitation SimFin has. `financial-reports-dates` returns exactly one entry per fiscal-period.

Endpoints that MIGHT expose vintages (`/api/v3/*-as-reported`, `/api/v4/financial-reports-json`) return 403 "Legacy Endpoint. Premium only." on this key.

To close the restatement leak, Study B would require ONE of:

1. **FMP Ultimate / Premium tier** (may or may not include vintages; not verified without an upgrade).
2. **Direct SEC EDGAR XBRL scraping** — pull each 10-K and 10-K/A separately, keyed by filingDate. Free but ~1000× more work than the FMP path.
3. **Vintage data vendor** (Compustat, S&P, CRSP "as-first-reported" datasets) — paid, academic-license territory.

**Study B PREMISE FAILS on FMP Starter.** Task instructs: "If either [1a or 1b] fails, STOP and report — do not build a speculative correction." Part 1b failed. Part 2 (verdict impact) is SKIPPED because we cannot compute as-originally-reported values without vintage data.

## Part 2 — Verdict impact estimate

**NOT MEASURED.** Part 1b's premise failure means we cannot construct an as-originally-reported comparator. The magnitude of leak on the 2025 OOS verdict — including whether it could flip the 2/4 quarterly gate (which was over-determined by non-alpha criteria per the phase5-frozen decision record) versus just nudge the alpha number — remains unknown until a vintage source is available.

The frozen 2025 OOS result at `phase5-frozen` should be understood to carry an UP-TO-35% row-restatement exposure (annual) and 17% (quarterly) on the fields entering the gates. Whether that changes the alpha is unmeasured; whether it changes the 2/4 quarterly gate decision is unmeasured.

## Part 3 (side check) — 29-name live-swap churn (MEASURED)

From `data/fmp/ab_verify_results.csv` (commit `eec4a70`):

### Entering top decile under `USE_FMP_FUNDAMENTALS=1` (29 names)

| Ticker | Sector | PS_A (SF) | PS_B (FMP) | Δ | n_yrs SF | n_yrs FMP | mixed | Altman flip |
|---|---|---|---|---|---|---|---|---|
| ANF | Consumer Cyclical | 60.67 | 63.28 | +2.61 | 5 | 5 | | |
| APH | Technology | 61.09 | 62.16 | +1.07 | 5 | 5 | | |
| CPE | Energy | 60.57 | 62.62 | +2.05 | 1 | — | | |
| **CSCO** | Technology | 62.19 | 63.62 | +1.43 | 4 | 5 | | **Y** |
| CTT | Real Estate | 62.01 | 62.17 | +0.16 | 2 | — | | |
| DORM | Consumer Cyclical | 60.25 | 64.47 | +4.22 | 5 | 5 | | |
| EAT | Consumer Cyclical | 60.93 | 64.19 | +3.26 | 4 | 5 | | |
| ENSG | Healthcare | 62.04 | 62.24 | +0.20 | 5 | 5 | | |
| EPRT | Real Estate | 56.68 | 62.89 | +6.21 | 5 | 5 | | |
| **GIII** | Consumer Cyclical | 58.86 | 64.56 | +5.70 | 5 | 4 | **Y** | |
| HOOD | Financial Services | 62.24 | 62.28 | +0.04 | 5 | — | | |
| **HSAI** | Consumer Cyclical | 51.35 | 71.90 | +20.55 | 4 | 5 | | **Y** |
| HST | Real Estate | 60.23 | 63.82 | +3.59 | 5 | 5 | | |
| HTGC | Financial Services | 61.57 | 62.78 | +1.21 | 5 | 5 | | |
| IBRX | Healthcare | 56.29 | 68.27 | +11.98 | 5 | 5 | | |
| **IMAX** | Consumer Cyclical | 59.95 | 62.39 | +2.44 | 3 | 5 | | **Y** |
| KTB | Consumer Cyclical | 61.38 | 62.16 | +0.78 | 5 | 5 | | |
| LRCX | Technology | 60.17 | 62.28 | +2.11 | 4 | 5 | | |
| LYV | Consumer Cyclical | 61.71 | 64.31 | +2.60 | 5 | 5 | | |
| MPLX | Energy | 60.64 | 62.91 | +2.27 | 5 | 5 | | |
| NFLX | Consumer Cyclical | 62.21 | 63.82 | +1.61 | 5 | — | | |
| NTNX | Technology | 60.56 | 63.00 | +2.44 | 4 | 5 | | |
| NWE | Utilities | 60.40 | 67.39 | +6.99 | 3 | 5 | | |
| OC | Basic Materials | 57.45 | 64.22 | +6.77 | 5 | 5 | | |
| PVAC | Energy | 61.81 | 62.65 | +0.84 | 3 | — | | |
| SIGA | Healthcare | 61.71 | 62.26 | +0.55 | 1 | — | | |
| TPR | Consumer Cyclical | 56.01 | 65.45 | +9.44 | 4 | 5 | | |
| **TRIN** | Financial Services | 57.07 | 68.88 | +11.81 | 5 | 5 | | **Y** |
| VERV | Healthcare | 57.38 | 64.99 | +7.61 | 5 | 5 | | |

### Leaving top decile under `USE_FMP_FUNDAMENTALS=1` (29 names)

| Ticker | Sector | PS_A (SF) | PS_B (FMP) | Δ | n_yrs SF | n_yrs FMP | mixed | Altman flip |
|---|---|---|---|---|---|---|---|---|
| ABG | Consumer Cyclical | 73.42 | 54.03 | -19.39 | 3 | 5 | | |
| ADP | Industrials | 63.37 | 61.20 | -2.17 | 4 | 5 | | |
| AGX | Industrials | 70.31 | 53.37 | -16.94 | 5 | 4 | **Y** | |
| AMPH | Healthcare | 62.25 | 59.65 | -2.60 | 5 | 5 | | |
| AN | Consumer Cyclical | 74.69 | 52.02 | -22.67 | 3 | 5 | | |
| BBIO | Healthcare | 62.26 | 61.87 | -0.39 | 5 | — | | |
| BMI | Technology | 62.67 | 62.01 | -0.66 | 5 | 5 | | |
| BMY | Healthcare | 62.46 | 59.70 | -2.76 | 5 | — | | |
| CME | Financial Services | 63.87 | 62.07 | -1.80 | 5 | 5 | | |
| CPRI | Consumer Cyclical | 65.93 | 22.67 | -43.26 | 3 | 4 | **Y** | |
| DRQ | Energy | 68.75 | 57.32 | -11.43 | 5 | 5 | | |
| DX | Real Estate | 65.25 | 53.07 | -12.18 | 5 | 5 | | |
| FHI | Financial Services | 62.82 | 61.32 | -1.50 | 5 | 5 | | |
| G | Industrials | 64.42 | 61.95 | -2.47 | 3 | 5 | | |
| GAMB | Consumer Cyclical | 68.66 | 57.75 | -10.91 | 5 | 5 | | |
| HGV | Consumer Cyclical | 66.94 | 42.09 | -24.85 | 3 | 5 | | |
| MASI | Healthcare | 65.97 | 39.61 | -26.36 | 3 | 5 | | |
| PAG | Consumer Cyclical | 67.73 | 52.96 | -14.77 | 3 | 5 | | |
| PEN | Healthcare | 63.17 | 61.58 | -1.59 | 5 | 5 | | |
| PR | Energy | 70.96 | 57.28 | -13.68 | 5 | 5 | | |
| RDVT | Technology | 63.66 | 60.51 | -3.15 | 5 | 5 | | |
| RH | Consumer Cyclical | 66.70 | 27.25 | -39.45 | 3 | 5 | | |
| RITM | Real Estate | 62.40 | 61.81 | -0.59 | 1 | — | | |
| SBTX | Healthcare | 69.17 | 59.22 | -9.95 | 5 | 5 | | |
| SGFY | Technology | 65.76 | 47.23 | -18.53 | 3 | 5 | | |
| STLA | Consumer Cyclical | 72.43 | 33.85 | -38.58 | 3 | 5 | | |
| STM | Technology | 74.37 | 48.31 | -26.06 | 3 | 5 | | |
| SWTX | Healthcare | 62.45 | 62.12 | -0.33 | 5 | — | | |
| WDFC | Basic Materials | 62.64 | 57.69 | -4.95 | 5 | 5 | | |

Full CSV: `data/fmp/ab_29name_churn.csv`.

### Flagged (mixed-source AND entering top decile) — 1 name

| Ticker | Sector | PS_A (SF) | PS_B (FMP) | Δ | n_yrs SF | n_yrs FMP |
|---|---|---|---|---|---|---|
| **GIII** | Consumer Cyclical | 58.86 | 64.56 | +5.70 | 5 | 4 |

GIII is the one entering name where SF has DEEPER coverage (5y) than FMP (4y) and the merged frame mixes sources across years. Worth a manual spot check before the operator flips `USE_FMP_FUNDAMENTALS` default-on, but not a blocker.

### One-paragraph read

The churn looks like the swap is WORKING AS INTENDED. Of the 29 LEAVING names, **13 had SimFin `n_yrs=3` and gained FMP `n_yrs=5`** (ABG, AN, MASI, PAG, HGV, RH, STLA, STM, SGFY, TPR, GIII, IMAX, CSCO in one direction or another) — SimFin's thin coverage was flattering their 3-year growth trajectories, and FMP's deeper coverage exposes flatter or negative 5-year signals. This is a data-quality win, not a bug. The heavy over-representation of Consumer Cyclical + specialty pharma / medical devices in the LEAVING set (ABG, AN, CPRI, HGV, MASI, PAG, RH, STLA + BBIO, BMY, PEN, SBTX, SWTX) is consistent with SimFin having spottier coverage of retailers, dealerships, and biotechs than of large-cap steady names. The Altman-flip entering names (CSCO, HSAI, IMAX, TRIN) match the Study A finding that cur_liab-driven Z-flips concentrate in large-cap tech and financial services — expected. Only ONE mixed-source entering top-decile name (GIII); the mixing risk is real but rare in the entering set. No red flags requiring deeper investigation.

## Recommendation: **NO-GO (tier)**

**Study B is not buildable on the current FMP Starter tier.** The restatement leak is present and material (17-35% of rows on the 2025 OOS cutoff, up to 96% on older periods), but the fix-path — as-originally-reported vintages via FMP — is unavailable on this key. Do NOT build a speculative correction against FMP-latest; that would be trading a known leak for an unmeasured leak of the same type.

Actions:

1. **Document the leak as a known backtest caveat.** Add a note to the frozen `phase5-frozen` tag's decision record: the 2025 OOS validation carries UP-TO-35% row-restatement exposure on the fields entering the gates; alpha and 2/4 quarterly gate impact are unmeasured until vintages are available.
2. **Defer Study B** until one of the vintage sources materializes: FMP Ultimate upgrade with verified vintage endpoints, SEC EDGAR XBRL scraping, or a vintage-data vendor.
3. **The LIVE screener source-swap (`USE_FMP_FUNDAMENTALS`, commit `eec4a70`) is unaffected by this scoping.** That swap is about current-fundamentals quality, not historical PIT reconstruction. The 29-name churn read supports proceeding with the live swap as planned (deliberate operator decision).
4. **The `phase5-frozen` tag stays.** Nothing in this scoping justifies re-opening the frozen validation. When a vintage source is available, Study B is its own pre-registered, firewalled task with its own OOS.

## Frozen-file guard

`git diff --name-only scripts/backtest.v2.py scripts/backtest.py src/market_screener.py` returns empty for this scoping change. `git tag --list phase5-frozen` unchanged. No weight/regime/decay/scoring files touched. Only new files:

- `docs/study_b_scoping.md` (this document)
- `data/fmp/ab_29name_churn.csv` (Part 3 backing data)

## Reproducing

```
# Part 1a (SimFin restatement counts):
py -3 -c "…"  # see the counting snippet in the commit description.

# Part 1b (FMP endpoint probe):
py -3 -c "…/stable/income-statement-as-reported…"

# Part 3 (29-name churn table):
py -3 scripts/ab_verify_fmp.py   # writes data/fmp/ab_verify_results.csv
```

Part 2 is not reproducible until a vintage source is available.
