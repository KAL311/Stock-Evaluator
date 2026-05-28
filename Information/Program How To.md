# Program How-To

## Market Screener (`src/market_screener.py`)

Primary stock screener. Fetches SimFin financials + Finviz ownership data, computes sector-percentile-ranked potential scores (0–100), tags mispricing patterns, and provides an interactive query REPL.

### Run

```bash
python src/market_screener.py
```

No CLI arguments. Runs cache-first: if `data/stock_cache.db` is under 24h old, skips the full data pipeline and loads directly into the REPL.

### Environment Variables

| Variable | Required | Effect |
|---|---|---|
| `SIMFIN_API_KEY` | **Yes** | SimFin API key. Exits with error if unset. |
| `DISABLE_FINVIZ` | No | `1` / `true` / `yes` skips Finviz scraping. `sentiment_score` uses only price-based components. |
| `FINVIZ_HEALTH_CHECK_ONLY` | No | `1` / `true` / `yes` probes Finviz connectivity on AAPL then exits. |

### REPL Commands

| Command | Description |
|---|---|
| `quit`, `exit`, `q` | Exit the REPL |
| `help` | Print example query patterns |
| `correlations` | Print sub-score correlation matrix + highest-cross-correlation by sector |
| `export` | Write full results CSV to `data/stocks_YYYYMMDD_HHMM.csv` |
| `why <TICKER>` | Full breakdown for one ticker: sub-scores, component ranks within sector, filing age, liquidity tier, flags |
| `compare <T1> <T2> [T3...]` | Side-by-side comparison of 2+ tickers (19 fields) |

### REPL Query Patterns

```
tech top 20                                    Top 20 tech stocks (by potential_score)
healthcare with quality > 80                   Numeric filter on any field
banks with pe_ttm < 15 sorted by dividend      Filter + custom sort
fresh in tech                                  filing_age_days < 365
liquid in tech                                 liquidity_tier = large or mid
tradeable in tech                              liquidity_tier = large only
sorted by growth / sort by valuation           Override default sort (potential_score desc)
undervalued / cheap                            Sort by valuation_score asc
expensive / overvalued                         Sort by valuation_score desc
reverse / desc / lowest / smallest             Flip sort direction
FALLEN_ANGEL in energy                         Filter by flag
consumer staples top 50                        Limit to N rows (<N> results / <N> rows also work)
consumer disc with roe > 0.15 sorted by roic   Compound query
```

**Numeric comparison operators:** `>`, `<`, `>=`, `<=`, `=`. Supports `k`/`m`/`b` suffixes.

### Query Field Aliases

`pe`, `p/e`, `price`, `market cap` / `marketcap`, `pb` / `p/b`, `ps` / `p/s`, `pfcf` / `p/fcf`,
`ev/ebitda` / `ev_ebitda`, `revenue`, `net income` / `net_income`, `fcf`, `ebitda`,
`roe`, `roa`, `roic`, `debt/equity` / `d/e`, `current ratio`, `dividend yield` / `dividend`,
`payout ratio`, `revenue growth` / `growth_3yr`, `beta`, `enterprise value` / `ev`,
`gross margin`, `operating margin`, `net margin`, `potential` / `pot`,
`valuation`, `quality`, `growth`, `sentiment`,
`pe_ttm`, `ps_ttm`, `pfcf_ttm`, `ev_ebitda_ttm`,
`return_1m` / `r1m`, `return_3m` / `r3m`, `return_6m` / `r6m`, `return_12m` / `r12m`,
`momentum` / `12m-1m`, `dist_52w` / `distance_from_52w_high`,
`insider` / `insider_own`, `institutional` / `inst_own`, `short` / `short_float`,
`roic_3y`, `roe_3y`, `roic_5y`, `roe_5y`, `revenue trend` / `revenue_trend_5y`,
`fcf trend` / `fcf_trend_5y`, `ebitda trend` / `ebitda_trend_5y`,
`52w_high`, `52w_low`, `filing_age_days`, `revenue_cv_3y`, `op_inc_cv_3y`

### Flags (Mispricing Patterns)

`DEEP_VALUE`, `FALLEN_ANGEL`, `QUIET_COMPOUNDER`, `MOMENTUM_VALUE`, `AVOID_VALUE_TRAP`, `OVEREXTENDED`

### Key Configuration Constants (at top of file)

| Constant | Default | Purpose |
|---|---|---|
| `MIN_MARKET_CAP` | `300_000_000` ($300M) | Minimum market cap to include |
| `MIN_DOLLAR_VOLUME` | `1_000_000` ($1M) | Minimum median daily dollar volume |
| `CONTRARIAN_MODE` | `True` | `True` = recent underperformance scores higher on sentiment |
| `POTENTIAL_WEIGHTS` | valuation 0.35, quality 0.30, growth 0.20, sentiment 0.15 | Master sub-score weights (must sum to 1.0) |
| `CACHE_SCHEMA_VERSION` | 6 | Bump when cache schema changes; forces rebuild on mismatch |

---

## Stock Info (`src/stock_info.py`)

Fetches and displays a single ticker's fundamentals from Finviz. One-shot script (no REPL).

### Run

```bash
python src/stock_info.py <TICKER>
```

**Positional argument (required):** Stock ticker symbol (case-insensitive).

### Output Sections

VALUATION → FINANCIALS → LIQUIDITY → DIVIDENDS → TRADING → PERFORMANCE → OWNERSHIP → FORECAST

### Environment Variables

None.

---

## Backtest (`scripts/backtest.py`)

Multi-period point-in-time backtest of `potential_score` across four 1-year windows using SimFin historical data. Batch script (no REPL).

### Run

```bash
python scripts/backtest.py [--acknowledge-bias]
```

### CLI Arguments

| Argument | Description |
|---|---|
| `--acknowledge-bias` | Skip the 3-second survivorship-bias warning pause |

### Environment Variables

| Variable | Required | Effect |
|---|---|---|
| `SIMFIN_API_KEY` | **Yes** | SimFin API key. Exits with error if unset. |

### Backtest Periods

| Cutoff | Forward | T10Y Rate | Notes |
|---|---|---|---|
| 2020-12-31 | 2021-12-31 | 0.93% | ~6mo price history (marginal) |
| 2021-12-31 | 2022-12-31 | 1.52% | |
| 2022-12-31 | 2023-12-31 | 3.88% | |
| 2023-12-31 | 2024-12-31 | 3.88% | Most recent |

**To add a period:** append `(cutoff_date, forward_date, t10y_rate)` to the `PERIODS` list (line 55) and look up the correct T10Y rate from FRED series DGS10.

### Output Sections

1. **Per-period scoring summary** — number scored, flagged, flagged breakdown
2. **Top/bottom decile stocks** by potential_score for the most recent period (ticker, sector, sub-scores, forward return)
3. **By-sector top-vs-bottom spread** (quintiles, sectors with ≥20 stocks)
4. **Sector balance** — reads `stock_cache.db`, shows per-sector counts of stocks scoring > 80
5. **Spot-check** — 10 well-known tickers (`AAPL, MSFT, NVDA, JPM, XOM, KO, JNJ, T, WMT, BAC`)
6. **Multi-period summary table** — Period, n, TopDec mean, BotDec mean, Spread, Universe mean, Survivorship %
7. **Consistency interpretation** — STRONG EDGE / WEAK EDGE / NO RELIABLE EDGE

### Limitations (documented in code)

- Price data starts 2020-06-10 (SimFin data limits); 2019 periods untestable
- Finviz data explicitly excluded (`finviz={}`) — prevents look-ahead contamination of `sentiment_score`
- A safety check exits if any `short_float` / `insider_own` leaks into a historical period
- Earlier periods suffer more survivorship bias (more delistings since)
- Periods skipped when < 60 trading days at cutoff or < 100 tickers with price-sentiment coverage
