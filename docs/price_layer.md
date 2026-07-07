# Dual-Source Price Layer

## Why

SimFin free-tier US daily share-prices feed stopped advancing past
2025-06-03. The frozen scoring pipeline still reads from SimFin (correct
for backtest reproducibility and freeze integrity). For current/live use
in the HTML interface, we maintain a parallel fresh price source via
yfinance, written to `prices_live` in the existing SQLite cache.

## Boundaries

- **SimFin** (`us-shareprices-daily`) → frozen scoring, backtest, all
  fundamental computations. Stops at 2025-06-03 until upgrade.
- **yfinance** (`prices_live` table) → HTML interface, live charts, current
  market cap displays, post-freeze portfolio monitoring.

These two sources NEVER mix in the scoring pipeline. The freeze tag
`phase4-complete` is intact; this layer only adds, never modifies.

## Refresh cadence

Run `scripts/refresh_prices_yfinance.py` daily. Suggested cron:

    0 6 * * 1-5 cd /path/to/Stock\ Evaluator && \
        python scripts/refresh_prices_yfinance.py --top 1000

The `--top 1000` flag refreshes the top 1000 by potential_score, which
covers the entire top-decile pool plus margin. Full universe refresh
(~2400 tickers) is also viable but slower; use for weekend deep refresh.

## Caveats

- yfinance uses Yahoo Finance, which has been known to restate historical
  prices without notice. Do NOT use this data for backtest computation.
- Yahoo's `Close` here is auto-adjusted (split + dividend adjusted),
  matching SimFin's `Adj. Close` semantics.
- Free, no API key. Rate limits exist but are generous for daily-refresh
  use; the 1.5s sleep between batches keeps us well under thresholds.
- Some tickers will fail (delisted, OTC, ADR variants). These are logged
  and silently skipped — the failure list should be reviewed periodically.
