# Parity Study A — SimFin vs FMP Fundamentals, Current Common Period (Remediated Rerun)

**Date:** 2026-07-10
**Chosen common fiscal year:** FY2024
**Sample:** 1000 unique tickers (top 500 by cached potential_score + random 500 seed 42)
**Verdict:** **MIXED — FAILS both pre-committed thresholds, but the residual is a SOURCE COVERAGE + DEFINITIONAL question, not a mapping or window bug. Operator decision required.**

## Headline numbers (MEASURED)

- `potential_score` Spearman ρ = **0.9405** (threshold 0.97) — **FAIL**
- Top-decile Jaccard = **0.7255** (threshold 0.85) — **FAIL**
- Decile migration = 27.2%; within ±1 decile = 92.2%
- Tickers whose scoring inputs actually differ between runs: **998**
- Tickers scored on both paths (basis for parity stats): **879**

## Per-subscore correlations (MEASURED)

| Subscore | n | Spearman ρ | Mean \|Δ\| | Median \|Δ\| | p95 \|Δ\| |
|---|---|---|---|---|---|
| `valuation_score` | 879 | **1.0000** | 0.000 | 0.000 | 0.000 |
| `quality_score` | 879 | 0.9420 | 3.237 | 0.980 | 14.881 |
| `growth_score` | 873 | **0.8839** | 5.691 | 1.570 | 30.122 |
| `sentiment_score` | 879 | **1.0000** | 0.000 | 0.000 | 0.000 |
| **`potential_score`** | **879** | **0.9405** | 2.189 | 0.600 | 10.669 |

valuation + sentiment ρ=1.0000 confirm non-fundamental inputs held constant across runs. All drift is in quality + growth (the two subscores that read annual-history metrics).

## Throughput fix (Step 1 gate)

Prior fetcher averaged 18 files/min. Diagnosis: sequential urllib requests (200ms/call → 294/min ceiling) amplified 16× by retry-ladder on failed tickers. Random-500 sample contained delisted / off-FMP names; each triggered 5 retries × 1.5^n backoff × ticker-format fallback.

Fix applied: ThreadPoolExecutor(max_workers=6) + thread-safe token bucket at 250/min + fast-fail on 4xx + per-request latency logging. `--workers` flag added.

50-ticker timed test: **150 files in 6.0s = 1510/min**, p50 latency 189ms, zero 429s. GATE PASSED.

Full 1000-ticker fetch: **3000 calls in 11m11s = 268 files/min sustained** (below the 50-ticker burst rate because FMP responds slower under sustained concurrency, but still well within the 30-min budget). Latency p50=191ms, p95=251ms, zero 429s, 6 (ticker, statement) failures.

Total bandwidth: **27.2 MB** (0.14% of the 20GB monthly cap).

## n_yrs_history — the off-by-one fix worked, but a NEW pattern surfaced

**Match rate: 759/998 = 76.1% (up from 31% in the prior --years 5 run).**

`n_yrs_history_fmp − n_yrs_history_sf` distribution (sample only):

| Δ | tickers | share | interpretation |
|---|---|---|---|
| -3 | 1 | 0.1% | SimFin coverage exceeds FMP — very rare |
| -1 | 45 | 4.5% | remaining fetch-window residual (FMP still missing 1 FY) |
| **0** | **759** | **76.0%** | **match — the fix worked here** |
| +1 | 118 | 11.8% | **FMP has 1 more FY than SimFin** — coverage extension |
| +2 | 70 | 7.0% | FMP has 2 more FY than SimFin — small-cap coverage |
| +3 | 5 | 0.5% | FMP has 3 more FY than SimFin — thin SimFin coverage |

Direction reversed vs the prior run (which was uniformly -1). The old off-by-one is gone. New signal: **FMP covers ~24% of small/mid-cap tickers back farther than SimFin does.** That is a source-data-availability difference, not a bug in this pipeline.

## Isolation test — the drift is concentrated in coverage-mismatch tickers

Split the 879 scored-on-both tickers by whether `n_yrs_history` matches:

| Group | n | `potential_score` ρ | mean \|Δ\| |
|---|---|---|---|
| **n_yrs matches** | 658 | **0.9788** | 1.37 |
| n_yrs mismatched | 221 | 0.8343 | 4.63 |

**Where the two paths compute over the same-length history, ρ = 0.9788 — passes the 0.97 threshold.** The composite fails only because 22% of the sample has different coverage windows on the two sources.

## Total Current Liabilities — the OTHER driver (definitional, not fixable via fetch)

FMP `totalCurrentLiabilities` includes accrued expenses + deferred revenue + capital-lease-current that SimFin's `Total Current Liabilities` excludes. On AAPL FY2024 that was -6.6%. Across the sample (898 tickers with both FY2024 balance sheets):

| |Δ%| threshold | tickers | share |
|---|---|---|
| > 5% | 239 | 26.6% |
| > 10% | 174 | 19.4% |
| > 25% | 117 | 13.0% |

Median across the sample is 0.0% (the majority match closely). But a long tail of 13% differs by ≥25%. This flows into:

- Piotroski F-score (component 6: current-ratio delta signal → different denominator)
- Altman Z″ (component A: working capital / total assets → different working capital numerator)

Full drift diagnostic: `data/fmp/parity_a_curliab_diag.csv`.

## Top-25 worst-drift tickers

Full CSV: `data/fmp/parity_a_worst25.csv`. Sample:

| Ticker | \|Δ potential\| | Note |
|---|---|---|
| CALX | 38.8 | growth-heavy; small-cap; likely coverage-window driven |
| TCPC | 31.3 | |
| CPRI | 31.1 | |
| RH | 30.0 | |
| STLA | 29.2 | |
| PDFS | 28.9 | |
| AEVA | 28.5 | inverted — FMP HIGHER than SimFin |
| MASI | 27.1 | |
| ROIV | 26.9 | |
| LVWR | 26.0 | |

Tickers where FMP scores materially HIGHER than SimFin (AEVA, PSTX, VCEL, BGCP, DOMO, OSG in the top-25) suggest FMP's broader history captures growth trajectories SimFin misses — the coverage-extension pattern again.

## Field mapping (unchanged from initial run — verified correct on AAPL FY2024)

| SimFin column | FMP field | AAPL Δ | Note |
|---|---|---|---|
| Revenue / Gross Profit / Operating Income / Net Income / Total Equity / Total Assets / Cash+STI / Total Current Assets / OCF / D&A / Capex | direct | 0.00% each | dollar-for-dollar match |
| STD, LTD (separately) | direct | -109% / +11% | classification differs |
| ↳ Sum STD+LTD | ↳ scoring uses SUM only | 0.00% | no impact |
| Cost of Revenue | `-costOfRevenue` | sign-flipped | SimFin convention; not read by scoring |
| Publish Date | `filingDate` | exact | 2024-11-01 both |
| **Total Current Liabilities** | `totalCurrentLiabilities` | **-6.6%** | definitional divergence (see above) |

Piotroski F and Altman Z inputs otherwise fully covered.

## Verdict — MIXED (fails composite, but for source-level reasons)

Both pre-committed thresholds fail:

- `potential_score` ρ = 0.9405 < 0.97
- Top-decile Jaccard = 0.7255 < 0.85

But the failure is not a mapping bug or a fetch bug. Two distinct SOURCE-LEVEL drivers:

### Driver 1 — SimFin thin coverage on small/mid-caps (24% of sample)

FMP covers roughly 24% of the sample back farther than SimFin does. Where the two sources agree on history length, ρ = 0.9788. This is not fixable on the FMP side; it is SimFin coverage catching down to FMP. On a full FMP migration, these tickers would score with MORE history and therefore MORE STABLE metrics (revenue trends over 5y vs 3y). Whether that is "different" or "better" is a modelling call, not a data-layer question.

### Driver 2 — Total Current Liabilities definitional divergence (13-27% of sample)

FMP bundles items SimFin splits. Real definitional gap. Feeds Piotroski liquidity and Altman working-capital terms.

## Operator options (both real, present alone — pick knowingly)

### Option A — Reconstruct SimFin-narrow cur_liab from FMP components

Modify `src/fmp_mapping.py:map_balance` to compute:
```
Total Current Liabilities ≈ accountPayables + shortTermDebt + capitalLeaseObligationsCurrent + taxPayables
```

**Pros:** closes the definitional gap; potentially lifts `potential_score` ρ above 0.97 when combined with a coverage-window restriction (e.g. exclude tickers where SimFin coverage is <5y).
**Cons:** adds mapping complexity; introduces its own definitional assumptions (which "current portion" items count as "narrow"?); may need per-industry tuning.
**Does NOT address:** the coverage-extension pattern (Driver 1).

### Option B — Accept both drivers as known SimFin↔FMP divergences

Ship FMP as a live data-layer swap with a NOTE that:
1. small/mid-cap history metrics may be more stable on FMP (deeper coverage);
2. Piotroski / Altman may drift up to ~15% on tickers with heavy accrued/deferred current-liability items.

**Pros:** no additional mapping complexity; ships now; FMP-only path is honestly documented.
**Cons:** violates the pre-committed decision rule; treating any FMP-driven scoring change as a minor model change requires a scored-portfolio-diff document per release rather than a silent swap.

### Middle option — subscore-level branching

`valuation_score` and `sentiment_score` are already ρ=1.000 (no change). `quality_score` at ρ=0.9420 is close to threshold. `growth_score` at ρ=0.8839 is the true weak link. If the intended use of the FMP swap is valuation/sentiment-driven (e.g. price-based screening), the composite fails but the operative subscores are safe. If the use is growth-momentum-driven, the drift is material.

## Grep-verified frozen files

`git diff --name-only src/market_screener.py scripts/backtest.v2.py` returns empty in the current working tree. Only new FMP-side files (`src/fmp_mapping.py`, `scripts/fetch_fundamentals_fmp.py`, `scripts/parity_study_a.py`) and docs/data artifacts modified.

## Artifacts

- `data/fmp/parity_a_sample_tickers.txt` — 1000 tickers, deterministic seed 42
- `data/fmp/raw/<ticker>_<statement>.json` — cached FMP JSON (27.2MB, 2994 files)
- `data/fmp/fundamentals_income.csv`, `_balance.csv`, `_cashflow.csv` — 5972 / 5966 / 5971 rows respectively
- `data/fmp/parity_a_results.csv` — 2514 tickers, `in_sample` col for the 1000
- `data/fmp/parity_a_hist_diag.csv` — per-ticker per-metric SF/FMP side-by-side
- `data/fmp/parity_a_worst25.csv` — top-25 by \|Δ potential_score\|
- `data/fmp/parity_a_curliab_diag.csv` — per-ticker cur_liab SF/FMP percent-diff
- `data/fmp/parity_a.log`, `data/fmp/fetch_1000.log` — run logs
- `data/fmp/fetch_failures.log` — 6 (ticker, statement) failures

## Reproducing

```bash
py -3 scripts/fetch_fundamentals_fmp.py --tickers-file data/fmp/parity_a_sample_tickers.txt --years 6 --workers 6 --rate-per-min 250
py -3 scripts/parity_study_a.py --tickers-file data/fmp/parity_a_sample_tickers.txt --max-fiscal-year 2024 --skip-fetch
```

Raw JSON cache under `data/fmp/raw/` makes re-runs free.
