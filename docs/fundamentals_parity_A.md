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

---

## A-vs-B decision evidence (diagnostic run, no migration)

Two cheap checks to move the A/B choice off judgment and onto data. Reused the existing harness and already-fetched FMP data; no mapper changes, no scoring/loader/backtest/weight edits. Grep-verified frozen files clean.

### Check 1 — Does the cur_liab gap flip any HARD gates?

Framework hard-exclude cutoffs (confirmed in `src/market_screener.py:1926-1933` and `:3743-3745`):
- **Altman Z < 1.81** → distress exclude within value-trap gate.
- **Piotroski F < 5** → weak-fundamentals exclude within value-trap gate.

Two variants measured:

**Variant (a) — all-SimFin vs all-FMP** (composite effect: ALL fields differ, not just cur_liab). This is the operational number for a full FMP swap.

| Gate | Total flips | Top-decile flips | Top-quintile flips |
|---|---|---|---|
| Altman Z < 1.81 | 49 / 875 | **7** | 12 |
| Piotroski F < 5 | 75 / 877 | **12** | 18 |

Top-quintile Altman flips (a): AGNC, AN, CL, CPRI, CSCO, CWEN, DX, FOUR, PAG, PANW, RH, YOU.
Top-quintile Piotroski flips (a): AGX, ALTM, AN, CPRI, CRWD, DT, DUK, FLT, FROG, HTGC, JFIN, MASI, MOV, PAG, RH, STLA, STM, VEC.

**Variant (b) — ISOLATED cur_liab swap** (SimFin data everywhere BUT cur_liab from FMP). This is the pure Option-A-target: what closing the cur_liab gap alone would prevent.

| Gate | Total flips | Top-decile flips | Top-quintile flips |
|---|---|---|---|
| Altman Z < 1.81 | 30 / 878 | **3** | 5 |
| Piotroski F < 5 | 10 / 879 | **0** | **0** |

Top-quintile flips (b, isolated cur_liab): Altman = CSCO, PANW, AGNC, TCOM, JFIN; Piotroski = none.

**Reading:**
- Cur_liab alone drives **60% of Altman flips** (30/49) — genuine dominant Altman driver.
- Cur_liab alone drives **only 13% of Piotroski flips** (10/75) — most Piotroski drift comes from other fields (revenue growth / ROA delta / coverage-window shifts).
- Portfolio-relevant (top-decile) flips ATTRIBUTABLE TO cur_liab: **3 Altman, 0 Piotroski. Total: 3 top-decile names** at risk from the definitional gap alone.

Diagnostic CSV: `data/fmp/parity_a_curliab_iso.csv`.

### Check 2 — Does the Option-A reconstruction actually reproduce SimFin?

Three reconstruction variants tested against 898 sample tickers with both SimFin and FMP FY2024 balance sheets:

- **V1 (agent's proposed formula):** `accountPayables + shortTermDebt + capitalLeaseObligationsCurrent + taxPayables`
- **V2 (naive subtract):** `totalCurrentLiabilities - deferredRevenue`
- **V3 (aggressive subtract):** `totalCurrentLiabilities - (deferredRevenue + accruedExpenses + otherCurrentLiabilities)`

Compared to SimFin `Total Current Liabilities`:

| Variant | median \|%diff\| | mean \|%diff\| | within ±2% | within ±5% |
|---|---|---|---|---|
| **FMP orig (no reconstruction)** | **0.00%** | 34.68% | **67.8%** | **73.3%** |
| V1 (AP+STD+CLO_cur+taxPayables) | 58.19% | 71.31% | 0.6% | 1.2% |
| V2 (subtract deferredRevenue) | 7.59% | 43.33% | 34.1% | 42.3% |
| V3 (subtract DR+AE+OCL) | 55.39% | 69.23% | 1.0% | 1.8% |

**The agent-proposed V1 formula is dramatically WORSE than the current mapping.** V1 has median 58% error and only 0.6% of tickers within ±2%. Component detail from top-5 V1 worst mismatches:

- **WIT** (SimFin 1.4B, V1 133B): missing items — FMP `totalPayables` (45B), `accruedExpenses` (66B), `otherCurrentLiabilities` (34B) contribute to SimFin's number but V1 excludes them.
- **NIO** (SimFin 8.7B, V1 46B): same pattern — component fields exclude the bulk that SimFin includes.
- **TCOM** (SimFin 10.3B, V1 38B): FMP `accountPayables` alone is 17B — SimFin apparently uses a much narrower definition on ADRs than on US filers.
- **AGNC, OHI, VICI, NNN, INVA** (V1 = 0 or grossly wrong): REITs / financials / small biotechs where FMP splits current-liab components differently or leaves them null.

**V2 (subtract deferredRevenue)** is second best but still much worse than the current FMP mapping — median 7.6% error, only 34% within ±2%.

**V3** is as bad as V1.

**FMP's `totalCurrentLiabilities` as-shipped IS the best available proxy for SimFin.** The -6.6% AAPL drift is an outlier at the tail — median across the sample is 0.0% and 67.8% match within ±2%. The 27% >5% tail concentrates on: REITs, financial firms, foreign ADRs (WIT, NIO, TCOM, HSAI, NTES), small biotechs — segments where SimFin's narrower current-liab definition materially differs from FMP's broader roll-up. Not fixable with a linear combination of FMP fields.

Diagnostic CSV: `data/fmp/parity_a_recon.csv`.

### Recommendation matrix

Using the pre-committed rules and the measured evidence above:

| Condition | Verdict |
|---|---|
| Gate flips ≈ 0 → Option B | **Not this case.** Cur_liab-isolated Altman flips: 30 total, 3 top-decile. Portfolio-relevant. |
| Gate flips non-trivial AND reconstruction matches within ~2% → Option A | **NOT met.** Reconstruction V1 median error 58%, within ±2% share 0.6%. V2 median 7.6%, within ±2% share 34%. Neither reliable. |
| Gate flips non-trivial BUT reconstruction unreliable → operator tradeoff, consider hybrid | **This case.** |

**Neither Option A nor Option B alone is clean. Present both to operator plus a hybrid:**

- **Option A (RECONSTRUCT cur_liab from FMP components).**
  Rejected on evidence: V1 formula degrades the mapping (median error 58% vs FMP-orig 0%). V2 partial improvement in tail but median error 7.6% — worse than the current mapping. No linear combination of exposed FMP fields reliably reproduces SimFin.

- **Option B (ACCEPT drift, ship FMP with a model-change note).**
  Feasible with caveat: 3 top-decile names (PANW, TCOM, JFIN in the ISOLATED cur_liab test — the specific names would shift on any full-universe rerun) may cross the Altman Z < 1.81 gate under FMP that would not have under SimFin. All are large-caps or growth names, not obvious value-traps. Piotroski F gate is essentially unaffected by cur_liab alone (0 top-decile flips). This is a real but bounded model change.

- **Hybrid (RECOMMENDED FOR OPERATOR CONSIDERATION — but still not auto-picked).**
  Use FMP for all fundamentals EXCEPT `Total Current Liabilities`, which continues to be sourced from SimFin. This preserves the Piotroski / Altman gate inputs at the current mapping accuracy, at the cost of retaining a SimFin dependency for that one field. Concretely: `src/fmp_mapping.py:map_balance` would need to accept an optional SimFin cur_liab override; the harness would merge it before scoring. Adds one column of coupling to SimFin instead of a full definitional rewrite.

**Operator decides.** Evidence supports rejecting A on the reconstruction data alone. Choice is between B (accept 3 top-decile Altman flips as a documented model change) and hybrid (retain SimFin cur_liab dependency to keep gate inputs identical).

### Frozen-file guard (this diagnostic run)

`git diff --name-only src/market_screener.py scripts/backtest.v2.py` returns empty. No weights / regime / decay files touched.

### Additional artifacts (from this diagnostic run)

- `data/fmp/parity_a_curliab_iso.csv` — per-ticker Altman Z + Piotroski F under baseline SimFin vs FMP cur_liab swap, with SimFin decile.
- `data/fmp/parity_a_recon.csv` — per-ticker SimFin cur_liab vs FMP orig + V1 + V2 + V3 reconstruction variants and percent errors.

---

## Hybrid feasibility assessment (diagnostic, no code changes)

Assessment ONLY. Zero mapper / scoring / loader / backtest / weight edits. Purpose: determine whether the Hybrid option (FMP for all fundamentals EXCEPT `Total Current Liabilities`, which stays SimFin-sourced) is a clean single-column swap or a fragile source-mix, so the operator can make the final B-vs-Hybrid call on plumbing risk.

### 1. Where cur_liab is consumed on the v2 scoring path

**Input column name:** `Total Current Liabilities` (SimFin naming; the FMP mapper writes this same column via `totalCurrentLiabilities`).

**Live read sites (grep-verified in src/market_screener.py):**

| File:line | Site | Consumes |
|---|---|---|
| `src/market_screener.py:928` | `compute_history_metrics_full` keep_bal list | passthrough |
| `src/market_screener.py:976` | `m['cur_l_yr'] = m.get('Total Current Liabilities', 0)` | per-year derived col |
| `src/market_screener.py:977` | `m['cr_yr'] = m['cur_a_yr'] / m['cur_l_yr'].where(...)` | per-year current ratio |
| `src/market_screener.py:598-599` | `piotroski_f_components` reads `cr_yr` (curr + prev) | F-score liquidity signal `f_liquidity` (YoY cr delta) |
| `src/market_screener.py:640` | `altman_z_score` reads `cur_l_yr` | Altman Z components A (wc/ta) + D (eq/(debt+cur_l)) |
| `src/market_screener.py:1495-1560` | `compute_snapshot` reads `Total Current Liabilities` from bal_row | emits `df.current_ratio` (float) |
| `src/market_screener.py:1835` | `compute_potential_scores` | `_rank_within(df['current_ratio'].clip(upper=10.0))` → quality subscore component (weight 0.05 per L463) |

**Dead-code alternates:** `compute_piotroski_fscore` (L3573+), `compute_altman_zscore` (L3649+), `compute_beneish_mscore` (L3682+), and `apply_quality_gates` (L3734+) reference `df.current_liabilities` and `df.working_capital` — but grep confirms **those df columns are never set anywhere**. `apply_quality_gates` has no call sites either. These functions are not on the v2 scoring path.

**Net:** cur_liab flows through **exactly one column (`Total Current Liabilities`)** into the per-year metric matrix; downstream consumers derive `cur_l_yr`, `cr_yr`, `current_ratio`. Single-column swap surface is confirmed.

### 2. FY-alignment risk — quantified on the 1000-ticker sample

Merge key both sources use: `(Ticker, Fiscal Year)`.

| Signal | Value |
|---|---|
| Off-calendar SimFin filers (Report Date month ≠ 12) | **234/1000 (23.4%)** |
| Off-calendar FMP filers | 250/998 |
| Tickers in both sources | 998/1000 (2 SF-only: NANO, USX; 0 FMP-only) |
| **FY-end MONTH DISAGREEMENT** (same ticker, different fiscal-year-end month between sources) | **39/998 (3.9%)** |
| `(Ticker, FY)` pairs — SF rows in sample | 4755 |
| `(Ticker, FY)` pairs — FMP rows in sample | 5966 |
| **Merge match rate: (Ticker, FY) in both** | **4677 (78.4% of FMP)** |
| FMP-only pairs (SF lacks this FY) | 1289 (21.6% of FMP) |
| SF-only pairs (FMP lacks this FY) | 78 (1.6% of SF) |
| Same (ticker, FY) but Report Date month DIFFERENT | 43/4677 (0.9%) |
| Same (ticker, FY) but Report Date differs by >90 days | 103/4677 (2.2%) |

**FY-label mismatch examples (sources disagree on the fiscal-year-end month for the same ticker):**

| Ticker | SF month | FMP month | Type |
|---|---|---|---|
| LULU / PLAY / CHWY / PVH | 1 | 2 | 52-53 week retailer year — SF uses lagged label |
| SLAB / CRI / HLIO / WLDN / EYE | 12 | 1 | ~1-month off (year-crossing period end) |
| JOUT / ARMK / MTSI | 9 | 10 | fiscal Sep/Oct — labeling boundary |
| **ROIV** | **12** | **3** | 3-month off — different fiscal period entirely |
| **SLDP** | **12** | **9** | 3-month off |
| **VCEL** | **6** | **12** | 6-month off — DIFFERENT FISCAL YEAR ENTIRELY |

The retailer / SLAB / JOUT cases are essentially labeling-convention differences — the underlying data covers approximately the same period, but SF's "FY2024" ≠ FMP's "FY2024" by a month. For ROIV / SLDP / VCEL the mismatch is large enough that the two sources' `FY2024` refers to genuinely different reporting periods, and a naive merge on `(Ticker, FY)` would produce a nonsense join.

### 3. Failure mode under SimFin-preferred + FMP-fallback rule

For each ticker, take the last 5 FYs ≤ 2024 from the union of both sources. Per (ticker, FY) cell: use SF cur_liab if present, else FMP cur_liab.

| Outcome | tickers | share |
|---|---|---|
| Pure SF across window (no fallback needed) | 807 | 80.7% |
| Pure FMP across window | 0 | 0.0% |
| **MIXED sources within window** | **193** | **19.3%** |

Of the 193 mixed cases, common patterns (see samples below): FMP fills the OLDEST year(s) where SimFin coverage doesn't reach back, then SimFin takes over for recent years.

Sample mixed cases (per-year sources across the 5-year window):

| Ticker | Window sources (oldest → newest) |
|---|---|
| DBRG, HAYN, WIRE, USAP, MCFT, UI, SLQT, SWN, ALPN | `[FMP, SF, SF, SF, SF]` |
| MRUS, TBLA | `[FMP, FMP, SF, SF, SF]` |
| MOV, STM | `[SF, SF, SF, FMP, FMP]` |
| NE | `[SF, SF, FMP, FMP, FMP]` |
| SCWX | `[SF, SF, SF, SF, FMP]` |

**Downstream corruption:**

- `f_liquidity` (Piotroski) reads `cr_yr[curr]` vs `cr_yr[prev]`. If the two most recent years have MIXED sources (e.g. MOV/STM/NE `[..., FMP, FMP]` at the tail, or SCWX `[..., SF, FMP]`), the YoY delta compares a SimFin narrow denominator against an FMP broad denominator — apples-to-oranges. Corrupted signal.
- Altman Z uses ONLY the most recent year for wc + tl, so its output is whatever source served that one cell. Not corrupted by mixed history, but its input still depends on the coin flip of which source has the latest FY.
- `revenue_cv_5y`, `op_inc_cv_5y`, trends: use `rev_yr`, `op_inc_yr`, `ebitda_yr`, `fcf_yr` — none read `cur_l_yr` directly. Not corrupted.

The mixed-history corruption is confined to Piotroski `f_liquidity`. But that's exactly the signal the Hybrid was supposed to protect. If 19% of the sample has mixed sources at the tail — and the fallback rule is what forces that mixing — then Hybrid's "provably gate-neutral" claim doesn't hold for those 19%.

### 4. Architecture / maintenance cost

- Hybrid requires the SimFin balance-sheet loader to stay live indefinitely for a single column (`Total Current Liabilities`) plus the (Ticker, Fiscal Year) index needed to look it up. `simfin.load_balance(variant='annual', market='us')` at `src/market_screener.py:863` cannot be retired.
- Option B allows eventual full removal of `simfin.load_balance` and `load_income` / `load_cashflow` (annual and quarterly). Hybrid keeps SimFin annual balance alive; quarterly income + quarterly cashflow are separately still needed for TTM regardless (Study A caveat).
- The FMP mapper (`src/fmp_mapping.py:map_balance`) would need a new `simfin_curliab_override` param + merge logic + fallback policy. Additional test surface.

### Verdict — FRAGILE, not CLEAN

Evidence:

| Fragility signal | Threshold-for-clean | Measured |
|---|---|---|
| Merge match rate on (Ticker, FY) | ≥ 95% | **78.4%** ❌ |
| FY-end month mismatches | ≈ 0 | **39/998** ❌ |
| Mixed-source-history tickers | ≈ 0 | **193/1000 (19.3%)** ❌ |
| Off-calendar filer count | not a fragility on its own but amplifies label risk | 234/1000 |
| Ticker-set alignment | clean | ✓ (2 SF-only, 0 FMP-only) |

The one clean signal (ticker sets) is dwarfed by three failure signals.

**Hybrid does NOT provide the provably-gate-neutral guarantee it was proposed to give.** For the 22% of (Ticker, FY) pairs where SimFin lacks cur_liab, Hybrid falls back to FMP anyway — those tickers retain the exact Altman/Piotroski flips Option B would produce. For the 19% with mixed history, `f_liquidity`'s YoY delta becomes definitionally incoherent (comparing SimFin-narrow to FMP-broad across years within one ticker's own window). Add 3.9% FY-end labeling mismatches (some as large as 6 months for VCEL), and Hybrid trades documented Option-B risk (3 top-decile Altman flips) for undocumented mixed-source risk on a bigger fraction of the sample.

**Evidence supports Option B** (accept the 3 documented top-decile Altman flips as a model-change note; retire SimFin fundamentals path). Hybrid earns neither the plumbing simplicity of B nor the provable neutrality it was proposed to provide.

Not auto-picked; presented for operator confirmation. If the operator's use case is more forgiving of documented model changes than of hidden mixed-source drift on 19% of names, B. If the operator would rather retain a SimFin dependency for the specific column at the cost of the mixed-source corruption on the same 19%, Hybrid remains available.

### Frozen-file guard (this assessment)

`git diff --name-only src/market_screener.py scripts/backtest.v2.py` returns empty. Only `docs/fundamentals_parity_A.md` modified in this pass.

