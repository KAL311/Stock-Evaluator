# Ownership Layer — FMP Substitution for Dead Finviz Scrape

## Status

Data-layer substitution. Frozen scoring math untouched. Reactivation
opt-in behind `USE_FMP_OWNERSHIP=1` env flag.  Partial reactivation only
under the current FMP Starter plan (insider_own; inst_own and short_float
stay NULL until a higher plan is provisioned).

## OOS validation rule (MANDATORY)

**`USE_FMP_OWNERSHIP` MUST be UNSET (price-only) for the 2025 OOS locked
run and any Phase 3/4 validation re-run.**

Why this is not optional:

- **Backtested factor ≠ live factor.** The Phase 3/4 backtest scored the
  sentiment factor with a *price-only fallback*: the backtest explicitly
  passes `finviz={}` (Finviz/ownership data excluded — see
  `scripts/backtest.v2.py`, `compute_snapshot(..., finviz={})` at the
  point-in-time snapshot call).  With the flag ON, the live sentiment
  score adds an `insider_own` rank component.  These are not the same
  factor and their outputs will differ.
- **Look-ahead risk.** FMP `/stable/shares-float` on the Starter tier
  returns a *current* freeFloat snapshot with a `date` field that
  reflects the most recent SEC filing behind that snapshot — it is
  effectively point-in-time only for the *live* moment.  Using it in a
  historical backtest without a settlement-dated time-series would leak
  future information into past periods.
- **Phase 4 validation does NOT cover the FMP-ownership sentiment.**
  Flipping the flag to default-on is a *separate change requiring its
  own validation*, deferred to the Prompt 5B unfreeze package
  post-2025-OOS.

Current live sentiment weight accounting (flag ON, informational):

- `distance_from_52w_high`  weight 0.35 — active (price)
- `return_12m_minus_1m`     weight 0.35 — active (price)
- `insider_own`             weight 0.20 — **active via FMP shares-float**
- `short_float`             weight 0.10 — dormant (FMP short-interest 404 on Starter)
- `inst_own`                (no explicit sentiment weight; N/A)

Total active weight = 0.90 of designed; re-normalized within
`_rank_within`.  This is the documented current state, not a defect
to fix here.  Dormant components will remain dormant until FMP plan
grants the endpoints AND a validation run signs off on the flag flip.

## Problem

`finviz_cache` has 0 rows.  `insider_own`, `inst_own`, `short_float` are
NULL for all 2,514 cached stocks.  The sentiment factor's ownership
components — `SENTIMENT_WEIGHTS['short_float'] = 0.10` (inverted) and
`SENTIMENT_WEIGHTS['insider_own'] = 0.20` in `src/market_screener.py`
~line 477 — are silently inactive.  Live `sentiment_score` runs on
price-only fallback (`distance_from_52w_high` + `return_12m_minus_1m`,
0.35 each).  Finviz probe fails; anti-scrape defenses have hardened.

## Fix

New Financial Modeling Prep pipeline populates a **separate table**
(`ownership_live`); an opt-in adapter in the screener reads it into the
same row fields the Finviz path used to populate.  `finviz_cache` schema
untouched — pure additive change.

### Preflight (`scripts/fmp_preflight.py`) — 2026-07-07

Probed candidate endpoints against AAPL on the live Starter key
(`FMP_API_KEY`).  Result summary:

| Category           | Endpoint                                                | Result           |
| ------------------ | ------------------------------------------------------- | ---------------- |
| `insider_own`      | `/stable/shares-float`                                  | **PASS** — `freeFloat` %.  Insider = 1 - freeFloat/100 (closely-held fraction, same definition Finviz uses). |
| `insider_own`      | `/stable/insider-trading/statistics`                    | PASS (transaction *counts*, not ownership %) — flow signal, not level.  Not consumed here. |
| `insider_own`      | `/stable/insider-trading/latest`                        | PASS (per-filer Form 4 events) — not consumed. |
| `inst_own`         | `/stable/institutional-ownership/latest`                | **RESTRICTED (402)** — plan gate. |
| `inst_own`         | `/stable/institutional-ownership/extract`               | RESTRICTED (402). |
| `inst_own`         | `/stable/institutional-ownership/holder-performance-summary` | RESTRICTED (402). |
| `inst_own`         | `/stable/institutional-ownership/symbol-positions-summary`   | RESTRICTED (402). |
| `short_float`      | `/stable/short-interest`, `/stable/short-interest/latest`, `/stable/stock/short-interest` | **404 — endpoint not on Starter.** |
| `short_float`      | `/stable/quote` `sharesShort`                           | Field no longer present on `/stable/`. |
| profile fallback   | `/stable/profile`                                       | PASS but no ownership fields (address/CEO/beta/etc). |

Net: on Starter, only `insider_own` has a first-class source
(`shares-float`).  `inst_own` and `short_float` stay NULL until the plan
is upgraded — **no proxy substituted** per the task's ground rule.

### Components

1. **`scripts/refresh_ownership_fmp.py`** — standalone fetcher.

   - `argparse` `--top` / `--db` (mirrors `scripts/refreshprice.py`).
   - `FMP_API_KEY` required (env var; falls back to `FMP_API.env`).
   - Rate limit: sliding-window `RateLimiter` at 250 calls / 60 s
     (safety margin under the Starter 300/min cap).
   - Retry with exponential backoff on 429 and 5xx; HTTP 402 (plan gate)
     short-circuits without retry.  Per-ticker failures logged, batch
     never crashes.
   - Progress logged every 100 tickers with effective calls/min.
   - Commit every 100 tickers so a crash doesn't lose all progress.
   - Table:

     ```sql
     CREATE TABLE ownership_live (
         ticker TEXT NOT NULL,
         insider_own REAL,     -- fraction [0,1] to match _parse_pct convention
         inst_own REAL,        -- NULL on Starter (endpoint 402'd)
         short_float REAL,     -- NULL on Starter (endpoint 404'd)
         filing_date TEXT NOT NULL,  -- ISO date; from shares-float SEC filing
         fetched_at TEXT,           -- ISO datetime; freshness marker only
         PRIMARY KEY (ticker, filing_date)
     );
     ```

     `_parse_pct` in `src/market_screener.py` (~L1208) divides by 100 —
     screener convention is **0-1 fractions**.  `_to_fraction` in the
     fetcher matches: any `|v| > 1` is treated as a percent and divided
     by 100.

2. **Read adapter — `load_ownership_live(tickers)`** in
   `src/market_screener.py` (near `load_finviz_cache`).  Returns
   `{ticker: {insider_own, inst_own, short_float}}`, the row with the
   **latest** `filing_date <= today` per ticker.  Silent no-op if
   `ownership_live` doesn't exist yet (script never run).

3. **Gate at the fetch call site** (~L4266) in
   `src/market_screener.py`: `USE_FMP_OWNERSHIP=1` short-circuits the
   Finviz block and calls `load_ownership_live(candidate_tickers)`
   instead.  Prints `FMP ownership_live: X/Y tickers gained ownership data`.

## Point-in-Time Semantics

`filing_date` is the **`date` field from `/stable/shares-float`**, which
FMP populates from the SEC filing that established the outstanding /
float snapshot (typically a recent 10-K/10-Q/13F reference date).  It is
**not** the fetch time.

Two consequences:

- The read adapter's `filing_date <= today` guard is meaningful — an
  as-of-past-date replay would substitute `filing_date <= as_of`.  For
  live runs the constraint is trivially satisfied.
- `fetched_at` is a data-freshness / audit marker only.  Never join on
  it, never use it as a PIT anchor.

Historical backtest ingestion of `filing_date` is a **separate, later
task** — not landed here.  No backtest paths changed.

When institutional or short-interest endpoints become available, each
call returns its own filing/settlement date (13F filing date for
institutional; typically bi-monthly settlement date for short interest).
Store each new settlement as its own `(ticker, filing_date)` row —
history accumulates in-place under the existing schema.

## Normalization Convention

Values stored in `ownership_live` are **[0, 1] fractions**, matching
`_parse_pct` in `src/market_screener.py` (~L1208), which divides the
Finviz percent string by 100.  The fetcher's `_to_fraction` performs the
same conversion: `65.4 → 0.654`; anything already in `[-1, 1]` passes
through unchanged.  The screener does not need to re-scale.

Spot-check values (2026-07-07, insider_own = 1 - freeFloat/100):

| Ticker | insider_own | Sanity                                       |
| ------ | ----------- | -------------------------------------------- |
| AAPL   | 0.0017      | Widely held megacap, ~99.83% free float ✓    |
| JPM    | 0.0053      | Widely held megacap ✓                        |
| LNTH   | 0.1035      | Mid-cap, moderate insider stake ✓            |
| PPC    | 0.8225      | Pilgrim's Pride — JBS owns ~82% ✓            |

## Freeze Rationale — Why the Gate

The scoring FREEZE holds.  Sentiment component weights (0.35 / 0.35 /
0.10 / 0.20) and scoring math are unchanged.  However:

- Ownership components have been silently ~0-weighted in production
  since Finviz scraping died.  `_rank_within` of an all-NaN column
  produces NaN, which the weighted sum treats as effectively
  zero-weight — the live sentiment score has been a re-normalization of
  the two price signals only.
- Restoring the ownership feed **will shift live sentiment scores** for
  every ticker that gains data.  That is a distributional change to
  live output.  Rankings will move.

Whether that shift is "a bug fix restoring intended behavior" or "a
model change requiring its own OOS validation note" is a judgment call.
The gate defers that decision:

- **Default (flag unset)**: byte-identical to today.  Finviz probe still
  fails (or `DISABLE_FINVIZ=1`); ownership fields stay NULL; sentiment
  runs on price-only fallback.  Acceptance check:
  `SELECT COUNT(*) FROM stocks WHERE insider_own IS NOT NULL` → 0.
- **Flag set (`USE_FMP_OWNERSHIP=1`)**: opt-in reactivation for
  research/sizing runs.  Sentiment scores shift for tickers with
  `ownership_live` coverage.  A summary line reports how many tickers
  gained data.

Decision on flipping the default is deferred until after the 2025 OOS
evaluation lands (per `docs/phase5_oos_2025_decision.md` protocol).  If
flipped on, log the change and treat as a distinct model version.

## Coverage — reconciled denominator (2026-07-07)

The screener's adapter print previously showed `FMP ownership_live:
2362/3528 tickers gained ownership data`.  **The 3528 was misleading**:
it is the pre-liquidity market-cap-filtered candidate set built inside
the adapter block, not the scored universe.  The meaningful denominator
is the scored-universe count.

Query and canonical numbers:

```sql
SELECT
  (SELECT COUNT(*) FROM stocks) AS scored_universe,
  (SELECT COUNT(*) FROM stocks
     WHERE ticker IN (SELECT ticker FROM ownership_live)) AS scored_with_ownership;
```

| Metric                                  | Value        |
|-----------------------------------------|--------------|
| scored_universe                         | 2,514        |
| scored_with_ownership                   | 2,362        |
| **coverage of scored universe**         | **93.95%**   |
| ownership_live tickers outside universe | 0            |

All 2,362 FMP-fetched tickers are inside the scored universe.  No
external tickers inflated the count — the 3,528 was purely a
denominator-choice artifact.  Report `2362/2514 (93.95%)` in any live
coverage summary.

## 49 "recovered" stocks — audit (2026-07-07)

With the flag ON, 1,652 stocks receive a `potential_score`.  With the
flag OFF, 1,603 do.  Delta = **49 stocks** newly-scored because
`insider_own` populated their previously-empty sentiment component.

Reproduce: cache-invalidate then run screener each way; diff scored
ticker sets.  Full CSV: `.scratch_recovered_audit.csv` (regenerable;
gitignored).

Risk-flag scan of the 49:

| Risk                           | Count | Note                                   |
|--------------------------------|------:|----------------------------------------|
| DELISTED (flags contains)      |     0 | Liveness gate skipped — see below      |
| `liquidity_tier == 'micro'`    |     0 |                                        |
| `market_cap < $300M`           |     0 |                                        |
| `filing_age_days > 365`        | **49** | **All 49 have stale filings**         |
| `stale_fundamentals == 1`      |    41 | compute_portfolios excludes these      |
| Not-stale AND `>= p90 (57.03)` |     0 | None reach portfolio pool              |

Character of the 49: nearly all are M&A / delisted names whose SimFin
filings froze at their last pre-acquisition report.  Spot-check of the
top-15 by score: HZNP (Amgen 2023), CPE (APA 2024), NEX (Patterson-UTI
2023), PVAC (Ranger Oil 2023), HEP (2023), ONEM (Amazon 2023), PXD
(Exxon 2024), SWAV (JnJ 2024), WWE (TKO 2023), RUTH (Darden 2023),
KDNY (Novartis 2023), OSH (Amazon 2023), SEAS→PRKS (renamed), PKI→RVTY
(renamed), FBHS→FBIN (renamed), NCR (split 2023).

Portfolio-build safety verification (`scripts/generate_html_report.py`
`compute_portfolios`, L237–L246):

1. **DELISTED flag exclusion (L237–L239).**  Dormant.  Requires
   `prices_live` to be populated by `scripts/refreshprice.py`; that table
   is currently missing (`no such table: prices_live`), so the upstream
   `compute_liveness_and_flag` gate short-circuits with a warning
   (`market_screener.py` L2319).  Consequence: no ticker in the current
   cache carries the `DELISTED` flag, regardless of actual delisting.
   **Fix path is orthogonal to this task** — populate `prices_live`
   (Prompt 1's liveness pipeline) so the gate activates.
2. **`stale_fundamentals != 1` (L245).**  Active.  Excludes 41 of 49.
3. **`avg_dollar_volume_30d >= $1M` (L244).**  All 49 pass — they were
   liquid pre-delisting.
4. **`potential_score >= p90` (L241–L243).**  The current p90 is 57.03.
   9 of 49 clear this bar; **but** 9 of those 9 are also
   `stale_fundamentals == 1`, so gate #2 catches them first.
5. **Intersection**: `not stale AND >= p90` → **0 stocks**.

**Bottom line: `compute_portfolios` currently excludes all 49 recovered
stocks from every generated portfolio** — the stale_fundamentals gate
(41) plus the p90 threshold (drops the remaining 8, all of which score
below 57.03) form a full block.  No filter-logic change is required.

Residual risk: the interactive REPL / ranked-output view does *not*
apply the p90 or staleness gates by default (only `compute_portfolios`
in the HTML report does).  The 49 will surface in top-decile ranked
queries if the user does not opt into `stale_fundamentals != 1` or a
liveness filter.  This is a display-time visibility issue, not a
portfolio-inclusion one, and is inherited from the dormant liveness
gate — not caused by the FMP integration.

## Acceptance Checks

1. `python scripts/fmp_preflight.py` — reports per-endpoint pass/fail on
   the live key (documented above; re-run periodically to detect FMP
   changes or plan upgrades).
2. `python scripts/refresh_ownership_fmp.py --top 50` — populates
   `ownership_live` with values in `[0, 1]`; spot-checks above confirm
   AAPL / JPM / small-cap values match reality.
3. Screener run WITHOUT the flag: no change vs prior output; ownership
   fields NULL; `ownership_live` never read.
4. Screener run WITH `USE_FMP_OWNERSHIP=1`: fields populated for tickers
   present in `ownership_live`; `sentiment_score` changes only for those
   tickers; summary line prints coverage count.
5. Full `--top 0` completes without tripping 429s — the sliding-window
   `RateLimiter` holds effective throughput at or under 250 calls/min.

## Non-Goals (this task)

- Historical PIT backtest ingestion of FMP filing dates.  Separate task.
- Any change to `SENTIMENT_WEIGHTS` or scoring math.
- Flipping `USE_FMP_OWNERSHIP` on by default.  Deferred to post-2025-OOS
  decision.
- Substituting a proxy metric for `inst_own` or `short_float`.  Both
  remain NULL until the FMP plan grants the corresponding endpoints,
  per the task's explicit constraint.
