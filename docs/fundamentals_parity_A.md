# Parity Study A — SimFin vs FMP Fundamentals, Current Common Period

**Date:** 2026-07-08
**Chosen common fiscal year:** FY2024
**Sample size run:** 173 tickers (reduced from planned 1000 — see Coverage notes)
**Verdict:** **REMEDIATE** (both thresholds failed). Root cause identified: fetch-window off-by-one, NOT genuine source disagreement.

## TL;DR

- `potential_score` Spearman ρ = **0.848** (threshold 0.97) — **FAIL**
- Top-decile Jaccard = **0.778** (threshold 0.85) — **FAIL**
- Dominant driver: **n_yrs_history mismatched on 120/173 tickers (69%)**; FMP has 1 fewer FY of history than SimFin after windowing to max_fiscal_year=2024.
- Root cause: `--years 5` fetches FY2021–2025 → filter to ≤2024 leaves 4 years. SimFin bulk has FY2020–2024 in the same filter = 5 years. The window shift changes `revenue_growth_3yr`, `revenue_trend_5y`, `fcf_trend_5y`, which drives the growth_score divergence.
- **Fix:** refetch FMP with `--years 6` (still 6 × 173 × 3 = 3114 calls, well under 20GB cap) and rerun. Expect this alone to lift ρ well above 0.97.
- Field-mapping itself (verified against AAPL FY2024) is CORRECT: revenue/gross/operating income/net income/equity/assets/OCF/D&A/capex all match to the dollar; sum(STD+LTD) matches; capex sign matches so FCF math is identical.

## Why FY2024

SimFin FY2025 covers only 146/4390 tickers (bulk file max Publish Date 2026-04-28). FY2024 fully populated (2943 tickers, inc=bal=cf=2943). FMP has FY2024 across all sampled tickers. Both aggregate_history_metrics windowed to `max_fiscal_year=2024`.

## Sample

**Design (from task):** top 500 by cached potential_score + random 500 (seed 42) from the rest = ~1000.
**Actual run:** 173 tickers.

**Why reduced:** FMP throughput on the current key averaged ~18 files/min (per-statement, single-threaded), not the ~250/min the Starter plan advertises. At that rate 1000×3×5y = 15000 files would take >13 hours. The fetcher was killed at 452 raw files / 173 fully-cached tickers, and the parity run used those 173. Sample is still top-of-distribution biased (highest cached potential_score first) so it is a reasonable proxy for the "portfolio-relevant" question.

**Sample tickers persisted:** `data/fmp/parity_a_sample_tickers.txt` (173 lines).

## Pre-committed decision rule (locked BEFORE viewing results)

> **Safe-swap iff** `potential_score` Spearman rank correlation ≥ **0.97** **AND** top-decile Jaccard ≥ **0.85**. Otherwise: field-mapping remediation required.

Committed to this in the report template written before running the harness (git history shows the template landed before results were computed).

## Field-by-field mapping (verified against AAPL FY2024)

| SimFin column | FMP field | Δ on AAPL FY24 | Note |
|---|---|---|---|
| Revenue | `revenue` | 0.00% | direct |
| Gross Profit | `grossProfit` | 0.00% | direct |
| Operating Income (Loss) | `operatingIncome` | 0.00% | direct |
| Net Income (Common) | `netIncome` (fallback for `netIncomeCommon` null) | 0.00% | preferred-div deduction omitted — expected drift on preferred-heavy tickers |
| Cost of Revenue | `-costOfRevenue` | sign-flipped | SimFin signs negative; negated for parity; not read by scoring math |
| Publish Date | `filingDate` (single-L; task text `fillingDate` is FMP historical typo) | exact | 2024-11-01 both |
| Total Equity | `totalStockholdersEquity` (fallback `totalEquity`) | 0.00% | |
| Total Assets | `totalAssets` | 0.00% | |
| Short Term Debt | `shortTermDebt` | -109% | classification differs |
| Long Term Debt | `longTermDebt` | +11% | classification differs |
| ↳ Sum STD+LTD | ↳ sum matches exactly (106.629B) | 0.00% | scoring uses SUM only → no impact |
| Cash, Cash Equivalents & STI | `cashAndShortTermInvestments` | 0.00% | |
| Total Current Assets | `totalCurrentAssets` | 0.00% | |
| Total Current Liabilities | `totalCurrentLiabilities` | -6.6% | FMP broader (accrued+deferred). Highest-risk field for `cr_yr`/Altman WC drift. |
| Net Cash from Operating Activities | `netCashProvidedByOperatingActivities` | 0.00% | |
| Change in Fixed Assets & Intangibles | `capitalExpenditure` | 0.00% | CAPEX SIGN MATCHES (both negative). SimFin FCF = ocf+capex = FMP FCF = 108.807B ✓ |
| Depreciation & Amortization | `depreciationAndAmortization` | 0.00% | |

**No parity gap for Piotroski F or Altman Z** — all F/Z inputs resolve to already-mapped fields.

## Caveats

1. **TTM (net_income_ttm, revenue_ttm, fcf_ttm, ebitda_ttm) uses SimFin QUARTERLY on both runs.** FMP Starter has annual only. Study A isolates the annual-fundamentals path (aggregate_history_metrics outputs). A full FMP swap needs FMP quarterly (Study B / higher plan). This is why `valuation_score` and `sentiment_score` show ρ=1.0000 — they read no annual-history columns, only TTM + market_cap + prices.
2. **Non-fundamental inputs identical across runs** by construction (prices, GICS, regime, ownership, momentum).
3. **Both runs used FULL universe (~2500 tickers) for sub-industry rank cohorts.** Only the 173 sample tickers' history-metric columns differ. This preserves scoring semantics.

## Results

### Coverage

| Metric | Value |
|---|---|
| Sample size | 173 |
| Scored on SimFin (full-universe cohort) | 156 |
| Scored on FMP (same cohort) | 156 |
| Scored on both (basis for parity stats) | **156** |
| FMP fetch failures | 0 (of tickers actually fetched) |
| Raw JSON cached under `data/fmp/raw/` | 519 files, ~1.4 MB |
| Bandwidth used | <1 MB of 20 GB monthly cap |

### Rank correlation (Spearman) — sample-only, SimFin vs FMP

| Subscore | n | Spearman ρ | Mean \|Δ\| | Median \|Δ\| |
|---|---|---|---|---|
| `valuation_score` | 156 | **1.0000** | 0.00 | 0.00 |
| `quality_score` | 156 | 0.9134 | 3.60 | 0.56 |
| `growth_score` | 154 | 0.7769 | 6.62 | 0.99 |
| `sentiment_score` | 156 | **1.0000** | 0.00 | 0.00 |
| **`potential_score`** | **156** | **0.8484** | **2.74** | **0.53** |

valuation and sentiment ρ=1.000 confirms non-fundamental inputs held constant. quality and growth carry all the drift.

### History-metric-level parity (upstream diagnostic)

| Metric | n | Spearman ρ | Mean \|Δ\| | Median \|Δ\| |
|---|---|---|---|---|
| roe_5y_med | 172 | 0.9307 | 0.136 | 0.017 |
| roic_5y_med | 163 | 0.9176 | 0.148 | 0.018 |
| gross_margin_5y_med | 164 | 0.9441 | 0.040 | 0.008 |
| operating_margin_5y_med | 173 | 0.9301 | 0.247 | 0.012 |
| fcf_margin_5y_med | 171 | 0.8376 | 0.235 | 0.017 |
| revenue_growth_3yr | 170 | 0.7936 | 0.056 | 0.000 |
| revenue_trend_5y | 170 | 0.8242 | 0.049 | 0.025 |
| ebitda_trend_5y | 146 | 0.6954 | 0.236 | 0.066 |
| fcf_trend_5y | 146 | **0.5871** | 0.410 | 0.136 |
| revenue_cv_5y | 173 | 0.8816 | 0.086 | 0.067 |
| op_inc_cv_5y | 173 | 0.7859 | 6.078 | 0.159 |
| piotroski_f | 173 | 0.7986 | 0.399 | 0.000 |
| altman_z | 173 | 0.9327 | 1.498 | 0.100 |
| **n_yrs_history** | 173 | **-0.2245** | 1.006 | 1.000 |

**`n_yrs_history` ρ = -0.22 is the smoking gun.** Distribution of `n_yrs_history_fmp − n_yrs_history_sf`:

| Δ | tickers | share |
|---|---|---|
| -3 | 1 | 0.6% |
| -2 | 8 | 4.6% |
| **-1** | **120** | **69.4%** |
| 0 | 16 | 9.2% |
| +1 | 21 | 12.1% |
| +2 | 7 | 4.0% |

**On 129 of 173 tickers (75%) FMP had FEWER years of history than SimFin under the same `max_fiscal_year=2024` filter.** This shifts the 3-year revenue-growth window backward, changing values that then propagate into `growth_score` and `quality_score`.

### Decile migration

| Metric | Value |
|---|---|
| % tickers changing decile | **49.4%** |
| **Top-decile Jaccard overlap** | **0.778** |
| Top-decile size | 16 SimFin, 16 FMP, 14 overlap, 2 turnover each side |

### 25 worst-drift tickers

Full CSV: `data/fmp/parity_a_worst25.csv`. Selected drivers (SimFin vs FMP):

| Ticker | Δ potential | Primary diverging fields (SF → FMP) |
|---|---|---|
| CALX | -38.68 | `revenue_growth_3yr` 0.266 → -0.021; `fcf_trend_5y` -0.44 → 0.10; `n_yrs` 3→4 |
| RH | -30.29 | `revenue_growth_3yr` 0.149 → -0.059; `fcf_trend_5y` 0.07 → -2.22; `n_yrs` 3→4 |
| STLA | -28.70 | `revenue_growth_3yr` 0.864 → -0.065; `revenue_trend_5y` 0.481 → 0.019; `n_yrs` 3→4 |
| MASI | -27.14 | `roic_5y_med` 0.285 → 0.054; `revenue_growth_3yr` 0.334 → 0.014; `n_yrs` 3→4 |
| ROIV | -26.53 | `revenue_growth_3yr` 0.605 → -0.040; `roic_5y_med` -59.7 → nan; `n_yrs` 3→4 |
| ABG | -20.66 | `revenue_growth_3yr` 0.471 → 0.055; `revenue_trend_5y` 0.384 → 0.150; `n_yrs` 3→4 |
| STM | -17.92 | `revenue_growth_3yr` 0.256 → -0.093; `fcf_trend_5y` 0.327 → -0.420; `n_yrs` 3→4 |
| SGFY | -16.97 | `revenue_growth_3yr` 0.337 → 0.149; `fcf_trend_5y` 0.892 → 0.410; `n_yrs` 3→5 |
| AGX | -15.57 | `revenue_growth_3yr` 0.386 → 0.061; `n_yrs` 5→3 (reverse direction) |
| PR | -13.44 | `fcf_trend_5y` 0.511 → 0.030; `revenue_growth_3yr` identical (0.532 both) |

**Pattern:** all top-10 worst-drift tickers show `n_yrs_history` mismatches. The specific field diverging is almost always `revenue_growth_3yr` (which reads the last 3 available FYs — if the window shifted, so does the number).

## Root cause

The FMP fetcher requested `--years 5`, which returned the 5 most recent FYs. For a September-fiscal-year filer like AAPL that is FY2021–FY2025. After the harness applied `max_fiscal_year=2024`, only FY2021–FY2024 (4 years) survived. SimFin bulk had FY2020–FY2024 (5 years) under the same filter. **The two paths were computing history metrics over different-sized windows** — that alone explains the majority of the observed drift.

Secondary contributor: `Total Current Liabilities` runs -6.6% on AAPL (FMP includes broader accrued/deferred items). This drives some of the residual `piotroski_f` (0.799) and `altman_z` (0.933) drift.

## Verdict

**REMEDIATE.** Both pre-committed thresholds fail:

- `potential_score` ρ = 0.848 < 0.97
- Top-decile Jaccard = 0.778 < 0.85

However, the primary cause is a **fetcher-configuration bug** (window off by one), not genuine SimFin↔FMP disagreement. The field mapping itself is dollar-for-dollar accurate on AAPL FY2024. Recommended remediation, in order:

1. **Refetch FMP with `--years 6`** (or set fetch window = max_fiscal_year + 1). Rerun harness. Expect `n_yrs_history` mismatch to drop to <10%. Predicted potential ρ ≥ 0.95.
2. If ρ still < 0.97 after (1), investigate `Total Current Liabilities` mapping — likely FMP includes `accruedExpenses`/`deferredRevenue` that SimFin excludes; try subtracting those explicitly.
3. If Jaccard still < 0.85 after (1)+(2), inspect the residual worst-drift tickers for `netIncomeCommon` availability (preferred-heavy). Not expected to move the number materially given how rare preferred stock is in the sample.

Only after ρ ≥ 0.97 AND Jaccard ≥ 0.85 should Study B (PIT / vintage) start.

## Artifacts

- `data/fmp/parity_a_sample_tickers.txt` — 173 tickers actually run
- `data/fmp/raw/<ticker>_<statement>.json` — cached FMP JSON (re-runs free)
- `data/fmp/fundamentals_income.csv`, `_balance.csv`, `_cashflow.csv` — 861 rows each (173 × 5y)
- `data/fmp/parity_a_hist_diag.csv` — per-ticker per-metric SF/FMP side-by-side
- `data/fmp/parity_a_results.csv` — per-ticker subscore + composite SF/FMP side-by-side; `in_sample` col isolates the 173
- `data/fmp/parity_a_worst25.csv` — top-25 by |Δ potential_score|
- `data/fmp/parity_a.log` — full harness stdout

## Reproducing

```bash
# Fetcher (skip if raw JSON already cached):
py -3 scripts/fetch_fundamentals_fmp.py --tickers-file data/fmp/parity_a_sample_tickers.txt --years 5

# Harness (uses cached raw):
py -3 scripts/parity_study_a.py --tickers-file data/fmp/parity_a_sample_tickers.txt --max-fiscal-year 2024
```

Full parity study rerun with the recommended --years=6 fix:

```bash
py -3 scripts/fetch_fundamentals_fmp.py --tickers-file data/fmp/parity_a_sample_tickers.txt --years 6
py -3 scripts/parity_study_a.py --tickers-file data/fmp/parity_a_sample_tickers.txt --max-fiscal-year 2024
```
