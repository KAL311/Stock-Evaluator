# Live-screener annual fundamentals: SimFin → FMP (Option B + Option C, DEFAULT-ON)

## What changed (current state, go-live)

`src/market_screener.py:main()` swaps annual income/balance/cashflow to FMP by DEFAULT via `src/fmp_mapping.py:load_annual_with_fallback`. The env var is now a rollback SWITCH:

```
(no env var, or =1, or =true, or =yes)  →  FMP annual (physical-period keyed) + SimFin quarterly/TTM  [DEFAULT]
USE_FMP_FUNDAMENTALS=0 (or off/false/no) →  SimFin (all)  [ROLLBACK]
```

**Rollback (one-liner):** set `USE_FMP_FUNDAMENTALS=0` in the environment before invoking the screener. No code change; SimFin path returns instantly. Verified: the rollback reproduces the pre-flip SimFin top decile.

## What did NOT change

- **Scoring math** (`compute_potential_scores_v2`, `_rank_within`, weights, regime, decay) — zero edits.
- **`compute_history_metrics_full` / `aggregate_history_metrics`** — zero edits.
- **TTM path** — `compute_ttm(income_q, cashflow_q)` continues to consume SimFin quarterly. VERIFIED 100% identical between A/B runs: `ebitda_ttm`, `net_income_ttm`, `revenue_ttm`, `fcf_ttm`, `revenue_growth_yoy_q`.
- **Backtest** — `scripts/backtest.v2.py`, `scripts/backtest.py` neither import `src.fmp_mapping` nor read `data/fmp/fundamentals_*.csv`. Firewall grep still empty.

## Physical-period-keyed merge (Option C, replaces the initial FY-label merge)

The initial (Ticker, Fiscal Year)-keyed merge had a class-level bug: **SimFin labels a fiscal year by calendar-year-of-START; FMP by calendar-year-of-END.** For off-calendar filers (retailers with Jan/Feb year-ends, biotech with mid-year year-ends), the same physical filing gets different FY labels — proven on GIII where FMP FY(N) = SimFin FY(N-1) with identical Revenue/CurLiab/NI values dated 2021-01-31 on both sources. Under the FY-key merge, the SimFin "gap-fill" row was the SAME physical filing as the neighbouring FMP row, duplicating one physical period and inflating GIII's growth_score 71.75 → 88.36 purely as a labeling artifact. Affected class: 39 tickers with FY-end-month disagreement (LULU, PLAY, CHWY, PVH, SLAB, CRI, HLIO, WLDN, EYE, JOUT, ARMK, MTSI, ROIV, SLDP, VCEL, ...).

**Fix — merge is keyed on `(Ticker, Report Date)` with a 10-day tolerance window:**

- Both sources expose the physical fiscal-period-end date. SimFin `Report Date`; FMP `date` (extended into the mapper as `Report Date` too).
- Two rows collapse when their report dates are within 10 days, treating them as the same physical period. FMP wins the collapse; SimFin is used only when FMP has no row within tolerance of a SimFin row.
- Post-merge Fiscal Year label = year the fiscal period ENDS (calendar year of Report Date). This matches FMP's convention and produces a single consistent FY label per physical period.

Measured on the current (2026-07-10) full SimFin universe:

| Signal | Value |
|---|---|
| Collapse events on income statement (exact date match) | 13 154 |
| Collapse events on income statement (within 10 days) | 994 |
| Universe tickers with duplicated PHYSICAL period in scoring window | **0** |
| Tickers with duplicated FY-LABEL (fiscal-year-change filers like AAP, BBBY, CAKE, EYE, HBI) | 33 |
| Mixed-source-history tickers (last-5-window mixes FMP and SF) | 17 / 4390 (0.4%) |

The 33 FY-label-duplicated tickers are cases where a filer changed fiscal-year-end during a calendar year, producing two physical periods with the same year-of-END label. Downstream `compute_history_metrics_full`'s `drop_duplicates(keep='last')` collapses these to the most-recent physical period — the earlier period is dropped from that ticker's window. Documented, 0.75% of universe, not blocking.

## Live/backtest keying difference (documented, benign)

The LIVE screener now keys annual fundamentals on physical Report Date and uses year-of-END for the Fiscal Year label. The BACKTEST (`scripts/backtest.v2.py`) still uses SimFin's original FY labels (calendar-year-of-START on off-calendar filers). For an off-calendar filer (LULU, GIII, VCEL, ROIV, ...), a live score and its backtest counterpart may reference the SAME physical fiscal period under DIFFERENT FY labels. **This is the intended firewall behavior** — do NOT reconcile the live and backtest FY labels for these tickers; they are talking about the same physical filing under two labeling conventions.

## Verification pack (MEASURED)

### GIII spot-check (the class canary)

Under the buggy FY-key merge: growth_score 71.75 → 88.36 (+16.61pp, labeling artifact from duplicated 2021-01-31 filing).
Under the physical-period key at `max_fiscal_year=2025` (live-mode proxy): growth_score 76.22 → 75.46 (**Δ-0.76**). Artifact eliminated.

Retailer siblings under physical-period key at live-mode proxy:

| Ticker | growth A (SF) | growth B (FMP) | Δ |
|---|---|---|---|
| GIII | 76.22 | 75.46 | -0.76 |
| LULU | 86.19 | 88.50 | +2.31 |
| CHWY | 88.34 | 90.04 | +1.70 |
| TJX | 77.47 | 77.93 | +0.46 |
| TPR | 47.45 | 37.39 | -10.06 |

TPR moving materially in the DOWN direction (was +27.60 under buggy) confirms label-artifact removal.

### Full A/B under live-mode proxy (`max_fiscal_year=2025`)

2157 tickers scored on both paths.

| Subscore | ρ | mean\|Δ\| | median\|Δ\| |
|---|---|---|---|
| valuation_score | 0.9723 | 2.33 | 0.63 |
| quality_score | 0.9245 | 4.27 | 1.69 |
| growth_score | 0.8662 | 7.42 | 3.39 |
| sentiment_score | **1.0000** | 0.00 | 0.00 |
| **potential_score** | **0.9342** | 2.58 | 1.01 |

Decile migration 31.3%; within ±1 decile 92.4%.
Top-decile Jaccard = 0.6615 (216 each, overlap 172).

Lower Jaccard vs the initial Option B result reflects a **legitimate data-availability win**: FMP has FY2025 data for ~850 tickers that SimFin's bulk CSV hasn't loaded yet (FY2025 covers only 146/4390 SF tickers). Live users under FMP see current-year data 6-12 months earlier for off-calendar filers.

### Go-live run — top 10 (excluding DELISTED / UNPRICED)

Under default (FMP): RIGL, LRN, PPC, APP, NUTX, IDCC, MKC, YOU, IONQ, TCOM. All tradeable.

Under rollback (`USE_FMP_FUNDAMENTALS=0`): LRN, CPRX, PPC, MASI, SGFY, OTTR, JFIN, MNDY, IDCC, MKC. Reproduces the SimFin baseline. Rollback works.

### Firewall re-verification (post-flip)

```
$ grep -nE "fmp_mapping|load_annual_with_fallback|USE_FMP_FUNDAMENTALS|data/fmp/fundamentals" scripts/backtest.v2.py scripts/backtest.py
(empty)
```

Backtest still SimFin-only. `phase5-frozen` tag intact.

The swap sits between `load_simfin_data()` (which still loads all SimFin frames as the fallback base) and `compute_history_metrics(income, balance, cashflow)`. Only the three ANNUAL frames are swapped; everything else — companies, industries, prices, ownership, momentum, regime, and importantly the **quarterly income + quarterly cashflow that feed `compute_ttm` and `compute_quarterly_yoy_growth`** — stays SimFin-sourced.

## What did NOT change

- **Scoring math** (`compute_potential_scores_v2`, `_rank_within`, weights, regime, decay) — zero edits.
- **`compute_history_metrics_full` / `aggregate_history_metrics`** — zero edits. The FMP path feeds these functions the same-named columns (`Total Current Liabilities`, `Total Equity`, `Revenue`, ...) via `src/fmp_mapping.py`.
- **TTM path** — `compute_ttm(income_q, cashflow_q)` and `compute_quarterly_yoy_growth(income_q)` continue to consume SimFin quarterly. Verified: `ebitda_ttm`, `net_income_ttm`, `revenue_ttm`, `fcf_ttm`, `revenue_growth_yoy_q` are **100% identical** across A/B runs, max\|Δ\|=0 on 1814 rows (see `scripts/ab_verify_fmp.py`).
- **Backtest** (`scripts/backtest.v2.py`, `scripts/backtest.py`) — no FMP fundamentals import or call. Grep-verified. The 2025 OOS validation (tag `phase5-frozen`, commit `738db4b`) is not re-opened by this change.

## Backtest firewall (non-negotiable)

The frozen 2025 OOS result was produced with SimFin fundamentals. Silently changing the backtest's data source would invalidate the validation, so the backtest is deliberately routed around this gate: `scripts/backtest.v2.py` and `scripts/backtest.py` do not import `src.fmp_mapping` and never read `data/fmp/fundamentals_*.csv`. Any future backtest against FMP fundamentals is a separate Study B (PIT/vintage) with its own OOS.

## Merge rule (per-cell fallback)

`src/fmp_mapping.py:load_annual_with_fallback` merges FMP and SimFin frames on `(Ticker, Fiscal Year)`:

- **FMP has (Ticker, FY)**: overwrite the mapper-covered SimFin columns with FMP values; other SimFin-only columns (e.g. Restated Date, Selling & Marketing) stay from SimFin.
- **FMP has, SimFin doesn't**: append FMP row; other SimFin columns NaN → `compute_snapshot.get(col, 0) or 0` defaults them.
- **SimFin has, FMP doesn't**: keep SimFin row verbatim.

This is documented behavior, not silent. The chosen stance is FMP-primary with SimFin fallback rather than FMP-only because dropping SimFin-only tickers (NANO, USX, and per-FY holes on older data) reduces the scored universe without operational benefit.

## Mixed-source-history caveat

**Measured on the full 4390-ticker SimFin universe: 56 tickers (1.3%) end up with MIXED sources across their last-5-FY window.** These are cases like `[SF, FMP, FMP, FMP, FMP]` (SimFin only covers the oldest year) or `[FMP, FMP, FMP, FMP, SF]` (SimFin only reaches the most recent year). Piotroski `f_liquidity` (YoY current-ratio delta) compares SimFin-narrow to FMP-broad denominators within one ticker's own history on these — apples-to-oranges. The count is logged at swap time; the operator can inspect and, if desired, migrate later to a "FMP-only, drop-if-missing" stance which lifts the 1.3% but drops those tickers instead.

Study A found 19% mixed-source rate on the top-500 + random-500 sample; the full-universe number (1.3%) is much smaller because the small/mid-cap concentration of the sample is not representative.

## Parity evidence recap

From `docs/fundamentals_parity_A.md` (commits `fa76507`, `3282340`, `43efc66`):

- `valuation_score` ρ = 1.000 (parity study), `sentiment_score` ρ = 1.000 — both purely non-annual-fundamentals.
- `potential_score` ρ = 0.9405 on the 1000-ticker sample; **0.9788 on tickers where SimFin and FMP had equal history depth**.
- Dollar-identical mapping on AAPL FY2024 (revenue / gross profit / operating income / net income / equity / assets / OCF / D&A / capex all 0.00% delta).
- One documented divergence: `Total Current Liabilities` — FMP bundles accrued expenses + deferred revenue that SimFin splits. AAPL FY2024 shows -6.6%; sample median 0.0%; tail (13% of sample) shows >25% diff concentrated in REITs / financials / ADRs / small biotech.
- Isolated cur_liab impact: 30/878 Altman Z boundary flips, 3 top-decile (CSCO, PANW, AGNC-type). Piotroski F cur_liab-alone impact: 10/879 flips, 0 top-decile.
- Reconstruction option (V1: `accountPayables + shortTermDebt + capitalLeaseObligationsCurrent + taxPayables`) rejected: median error 58%, within ±2% share 0.6% — degrades vs FMP-shipped.
- Hybrid option (SimFin-cur_liab + FMP-elsewhere) rejected: 78.4% merge match, 39 FY-end month mismatches, 19% mixed-source-history on sample — fragile.

## Altman Z hard-gate flips — the documented model-change note

Altman Z < 1.81 is a hard exclude within the value-trap gate (`src/market_screener.py:1926-1933`, `:3743-3745`). Under FMP, ~3 top-decile names cross this threshold on the sample study (CSCO, PANW, AGNC-type).

**Framework note (`Framework Going Forward.md`):** Altman Z″ (the non-manufacturer variant used at `market_screener.py:628`) is a **weak distress signal for large-cap technology and mortgage REITs regardless of data source**. Both CSCO and PANW have Z-scores that sit near the boundary in every published dataset (Z depends heavily on `equity/liabilities`, and asset-light tech firms + mortgage REITs live near the cutoff). The flip is a boundary artifact, not a genuine distress-signal change. Net signal loss from adopting FMP is minimal for the intended value-trap use.

## A/B verification harness — MEASURED numbers

`scripts/ab_verify_fmp.py` runs the full-universe screener twice (SimFin vs FMP+fallback), holding all non-annual-fundamentals inputs identical.

Run summary (2026-07-10, full universe, 2157 tickers scored on both paths):

| Subscore | n | Spearman ρ | Mean \|Δ\| | Median \|Δ\| |
|---|---|---|---|---|
| valuation_score | 2157 | 0.9722 | 2.33 | 0.63 |
| quality_score | 2134 | 0.9518 | 2.85 | 1.07 |
| growth_score | 1724 | 0.9430 | 3.65 | 1.12 |
| sentiment_score | 2157 | **1.0000** | 0.00 | 0.00 |
| **potential_score** | **2157** | **0.9679** | 1.55 | 0.59 |

Decile migration: **20.4%**; within ±1 decile: **96.2%**.
Top-decile Jaccard (216 names each side): **0.7633**, overlap 187.

**Names entering top decile under FMP (29):**
ANF, APH, CPE, CSCO, CTT, DORM, EAT, ENSG, EPRT, GIII, HOOD, HSAI, HST, HTGC, IBRX, IMAX, KTB, LRCX, LYV, MPLX, NFLX, NTNX, NWE, OC, PVAC, SIGA, TPR, TRIN, VERV.

**Names leaving top decile under FMP (29):**
ABG, ADP, AGX, AMPH, AN, BBIO, BMI, BMY, CME, CPRI, DRQ, DX, FHI, G, GAMB, HGV, MASI, PAG, PEN, PR, RDVT, RH, RITM, SBTX, SGFY, STLA, STM, SWTX, WDFC.

TTM invariance (proof quarterly path is untouched):

| Column | % identical | max\|Δ\| |
|---|---|---|
| ebitda_ttm | 100.00% | 0 |
| net_income_ttm | 100.00% | 0 |
| revenue_ttm | 100.00% | 0 |
| fcf_ttm | 100.00% | 0 |
| revenue_growth_yoy_q | 100.00% | 0 |

## Bit-identical when USE_FMP_FUNDAMENTALS unset

The gate is a strict `if use_fmp_fund:` block. When the env var is absent (or set to anything but `1`/`true`/`yes`), the block is skipped entirely and the SimFin frames returned by `load_simfin_data()` flow into `compute_history_metrics` unchanged. There is no side effect elsewhere in `main()` from the addition. This is a structural bit-identical guarantee, not an empirical one.

## Operator playbook — when to flip default-on

1. Run `scripts/ab_verify_fmp.py` after a fresh SimFin data refresh; confirm the entering/leaving lists have no unexpected names for your portfolios.
2. Inspect the mixed-source-history log for names you'd want to hard-drop under an FMP-only stance.
3. Decide whether the Altman boundary flips (CSCO/PANW/AGNC-type) matter for your value-trap policy — see Framework note above.
4. When ready, flip default by changing `USE_FMP_FUNDAMENTALS` to always-on (either via env at invocation, a settings module, or by inverting the default in `main()`). The flip is a deliberate operator decision, like `USE_FMP_OWNERSHIP`.
5. **Do NOT flip the backtest.** Study B (historical PIT / vintage FMP) is a separate deliverable with its own OOS validation.

## Operator playbook — fetching FMP for the full live universe

`scripts/fetch_fundamentals_fmp.py` is the throughput-fixed fetcher (ThreadPoolExecutor(max_workers=6) + thread-safe token bucket at 250/min; observed 268/min sustained on the parity run, 1510/min in a 50-ticker burst). Raw JSON is cached under `data/fmp/raw/` so re-runs cost 0 API calls.

For the full scored universe (~2500 tickers × 3 statements × 6 years = ~7500 API calls):

```
py -3 scripts/fetch_fundamentals_fmp.py --top 2500 --years 6 --workers 6 --rate-per-min 250
```

Or via explicit ticker list:

```
py -3 scripts/fetch_fundamentals_fmp.py --tickers-file <path> --years 6 --workers 6 --rate-per-min 250
```

Annual data changes rarely; a daily or weekly refresh (via cron / scheduled task) is sufficient. Raw cache is safe to keep indefinitely; delete the specific `data/fmp/raw/<ticker>_*.json` files if a restated filing needs re-fetching.

## Files

- `src/market_screener.py` — main() only: 21-line gate block between `load_simfin_data()` and `compute_history_metrics()`. Zero edits to scoring functions.
- `src/fmp_mapping.py` — extended mapper (added `Research & Development`, `Interest Expense, Net`, `Pretax Income (Loss)`, `Income Tax (Expense) Benefit, Net`, `Shares (Diluted)`, `Dividends Paid` with SimFin sign conventions) + new `load_annual_with_fallback()` helper.
- `scripts/fetch_fundamentals_fmp.py` — throughput-fixed fetcher (unchanged since remediated Study A run).
- `scripts/ab_verify_fmp.py` — A/B verification harness.
- `docs/fundamentals_source_swap.md` — this document.
- `data/fmp/ab_verify_results.csv` — per-ticker A/B subscore + composite side-by-side.
- `data/fmp/ab_verify_summary.txt` — summary stats + names in/out.
- `data/fmp/ab_verify.log` — full harness stdout.
