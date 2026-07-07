# Build prompt: Static HTML trading-interface viewer for the Stock Evaluator

## What you are building

A **single self-contained static HTML file** that visualizes the screener's output as a fluid, trading-desk-style interface. No server, no live data fetching, no Python running when the user opens it. The user runs one Python generator script that reads existing data (the SQLite cache + audit JSONs + SimFin price history), bakes everything into one `.html` file, and opens it in a browser.

This is a **presentation layer only**. It must NOT modify, re-run, or re-implement any scoring logic. The scoring config is frozen (git tag `phase4-complete`); this viewer reads outputs, it does not compute scores. Do not import or call `compute_potential_scores`, `compute_potential_scores_v2`, or anything that ranks/scores. Read the already-computed values from the cache database.

## Deliverables

1. `scripts/generate_html_report.py` — a Python generator script (~500-700 lines) that reads data and writes one HTML file.
2. The HTML file itself is the output (e.g. `reports/stock_evaluator_<timestamp>.html`), produced by running the script.

No other files. The HTML must be fully self-contained: all CSS, JS, data, and the price-chart library embedded or CDN-linked. It must open correctly via `file://` with no web server.

## Data sources (exact, verified against the codebase)

### Source 1 — the SQLite cache: `data/stock_cache.db`, table `stocks`

This is the primary data source. One row per ticker. The full column list (verified from `market_screener.py` CACHE_SCHEMA, lines 731-771):

```
ticker, company, sector, industry, sector_group,
price, market_cap, enterprise_value,
pe, pb, ps, pfcf, peg, ev_ebitda,
revenue, net_income, fcf, ebitda,
gross_margin, operating_margin, net_margin,
roe, roa, roic,
debt_equity, current_ratio,
dividend_yield, payout_ratio,
revenue_growth_3yr,
beta, realized_vol, high_52w, low_52w,
gross_margin_3y_med, operating_margin_3y_med, net_margin_3y_med,
roe_3y_med, roic_3y_med, fcf_margin_3y_med,
gross_margin_5y_med, operating_margin_5y_med, net_margin_5y_med,
roe_5y_med, roic_5y_med, fcf_margin_5y_med,
revenue_cv_3y, op_inc_cv_3y, revenue_cv_5y, op_inc_cv_5y,
revenue_trend_5y, ebitda_trend_5y, fcf_trend_5y,
revenue_ttm, net_income_ttm, fcf_ttm, ebitda_ttm,
pe_ttm, ps_ttm, pfcf_ttm, ev_ebitda_ttm,
piotroski_f, altman_z, regime,
return_1m, return_3m, return_6m, return_12m,
return_12m_minus_1m, distance_from_52w_high,
insider_own, inst_own, short_float,
valuation_score, quality_score, growth_score, sentiment_score, potential_score,
potential_median, potential_p05, potential_p95,
potential_iqr, top_decile_pct, robust_pick,
impact_haircut_bps, impact_haircut_points,
crowding_count, crowded,
flags, n_yrs_history, stale_fundamentals, stale_last_pub_date,
last_filing_date, avg_dollar_volume_30d, liquidity_tier, last_updated
```

Read it with `sqlite3` + `pandas.read_sql_query("SELECT * FROM stocks", conn)`. Do not assume column presence; defensively handle missing columns with `.get()` patterns since older caches may lack newer columns.

### Source 2 — price history for charts: SimFin daily shareprices

The per-ticker price chart needs historical prices. These come from SimFin's local data:

```python
import simfin as sf
sf.set_api_key(os.environ.get('SIMFIN_API_KEY', 'placeholder'))
sf.set_data_dir(str(ROOT / 'data' / 'simfin'))
sp = sf.load_shareprices(variant='daily', market='us')
# sp is multi-indexed (Ticker, Date); the close column is 'Adj. Close', volume is 'Volume'
```

For each ticker that will appear in the report, extract its `Adj. Close` series. To keep the HTML file size manageable, **downsample to weekly closes** (resample to W-FRI, last) and **limit to the last 3 years**. Store as `{ticker: [[unix_ms, close], ...]}` so the chart library can consume it directly. Only include price series for tickers that appear in at least one generated portfolio OR the top 250 by potential_score (don't embed 2,400 price series — that bloats the file to tens of MB). Target total HTML file size under 8 MB.

If SimFin price load fails or is slow, the script must still produce the report with a graceful "price history unavailable" state on the chart — do not let chart-data failure block the whole report.

### Source 3 — backtest audit JSONs: `data/audit/audit_*.json`

For the backtest/performance tab. Each has `results` (list of per-period dicts with `label`, `spread`, `top_alpha`, `ic`, `regime`, `factor_ics`, `hrp`) and metadata. Load the most recent by mtime, or accept a `--audit` path argument. If none exist, render the backtest tab with a "no backtest data found" message rather than crashing.

## The portfolio generator (the most important new feature)

The viewer must generate **several named portfolios** from the top-decile pool, each reflecting a different objective. The user picks a portfolio type and sees the resulting 10-20 name list. Implement this in the Python generator (compute all portfolios at generation time, bake the results into the HTML; the user switches between pre-computed portfolios in the UI rather than recomputing live).

### Universe for portfolio construction

Start from the top decile: stocks where `potential_score` is in the top 10% of all scored stocks (compute the 90th percentile threshold from the data). This is the candidate pool (~180-240 names). Apply a liquidity floor: exclude any name with `avg_dollar_volume_30d < 1_000_000` (already the screener floor, but enforce again defensively). Exclude any name where `stale_fundamentals == 1`.

### Selection rules per portfolio

All portfolios are **equal-weighted** (Phase 4.2 proved HRP underperforms; do not implement HRP weighting). For each, produce both a 10-name and a 20-name version. Apply a configurable **sector cap** (default: max 4 names per `sector_group` for the 20-name, max 2 per sector for the 10-name) using a greedy descending-score fill: sort candidates by the portfolio's ranking metric descending, walk the list adding names, skip any whose sector is already at cap, stop at target count.

Generate these portfolio types:

1. **"Balanced (Top Score)"** — rank by `potential_score` descending. The default. This is the model's straight recommendation with sector diversification applied.

2. **"Performance / Momentum-tilted"** — rank by a blend: `0.6 * potential_score_pctile + 0.4 * return_6m_pctile` (convert both to within-pool percentiles first). Favors names the model likes that also have price momentum. Label it clearly as higher-turnover / higher-beta.

3. **"Deep Value"** — filter to candidates with `valuation_score > 70`, then rank by `valuation_score` descending (tiebreak `quality_score`). For users who want the cheap end of the quality universe.

4. **"Quality Compounder"** — filter to `quality_score > 75`, rank by `quality_score` desc (tiebreak `roic_5y_med`). Lower-volatility, durable-business tilt.

5. **"Income"** — filter to `dividend_yield > 0.02`, rank by a blend of `dividend_yield_pctile` and `quality_score_pctile` (so it's not just high-yield value traps). Show the portfolio's weighted-average dividend yield prominently.

6. **Sector-favoring portfolios** — generate one portfolio per `sector_group` that has ≥10 candidates in the pool: "Energy Focus", "Industrials Focus", etc. Each is the top 10 names within that single sector by `potential_score`. These let the user lean into a sector view. Only generate for sectors meeting the ≥10 threshold; list which sectors qualified.

For every portfolio, compute and display these aggregate stats:
- Equal-weighted average `potential_score`
- Sector breakdown (count per sector, shown as a small horizontal bar or pill list)
- Weighted-average `dividend_yield`, `pe_ttm` (median, since means blow up on negatives), `roic_5y_med`
- Average `beta` and average `realized_vol` (portfolio risk proxy)
- Count of names flagged `robust_pick == 1` (these are the Phase-1 resampling-robust names; surface this as a quality indicator)
- A prominent note: **"Half-size deployment recommended until 2025 OOS confirmation (per Phase 4 sizing decision)."**

## UI / UX requirements

### Overall feel

A fluid, modern trading-desk interface. Dark theme by default (trading interfaces are dark; reduces eye strain on dense data). Think Bloomberg-terminal-meets-modern-web, not a spreadsheet. Use:

- **Tailwind CSS via CDN** (`<script src="https://cdn.tailwindcss.com"></script>`) — no build step.
- **A charting library via CDN** for the price charts — use **Lightweight Charts by TradingView** (`https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js`), which is purpose-built for financial price charts and gives the trading-interface feel you want. Fall back to Chart.js only if Lightweight Charts proves difficult.
- **Vanilla JavaScript** for tab switching, sorting, filtering, and row selection. No React, no build tooling. All data baked into a `<script>` block as a JS object/JSON.

Critical theming and legibility constraints (do not skip these — they are the difference between "looks like a real product" and "looks like AI slop"):
- Consistent spacing scale: use Tailwind's spacing tokens, never arbitrary pixel values scattered around. Tables get generous row padding (`py-2` minimum), not cramped.
- Monospaced/tabular numerals for all numeric columns so they align vertically (`font-variant-numeric: tabular-nums` or Tailwind `tabular-nums`). Misaligned numbers are the #1 thing that makes financial tables look broken.
- Right-align all numeric columns, left-align text columns.
- Color semantics: green for positive returns/alpha, red for negative, neutral gray for zero/NA. But keep it tasteful — muted greens/reds (e.g. emerald-400/rose-400), not pure #00FF00.
- A clear visual hierarchy: section headers, sub-headers, body. Don't let everything be the same weight.
- Adequate whitespace between sections. Dense ≠ cramped. Give cards and tables breathing room with consistent gaps.
- Responsive enough to be usable on a laptop screen (1280px+); does not need to work on mobile.
- Sticky table headers when scrolling long tables.
- Sticky top nav bar with the tab switcher so the user never loses navigation on long pages.

### Tabs / pages (within the single file, switched via JS, no page reload)

**Tab 1 — Dashboard / Overview**
- Header: current regime (read from the `regime` column or audit), shown as the friendly label not the code. Map: R1 = "Disinflationary Expansion (Goldilocks)", R2 = "Reflationary Expansion", R3 = "Late-Cycle / Stagflation", R4 = "Recession", R5 = "Recovery". Show the regime with a one-line tooltip explaining its sector tilt.
- Key stats cards: universe size, number in top decile, last data refresh (`last_updated`), count of robust picks.
- A "model performance" summary pulled from the latest audit: mean top-alpha, mean IC, alpha hit rate, OOS result if present. Label these with the honest terminology (see Terminology section below).
- A prominent banner stating the deployment posture: "Half-size · Equal-weight · 20 names · 4/sector cap · revisit at 2025 OOS."

**Tab 2 — Portfolios**
- A selector (buttons or dropdown) for the portfolio types listed above.
- A toggle for 10-name vs 20-name.
- The selected portfolio renders as a clean table: ticker, company, sector, potential_score, the portfolio's ranking metric, price, market cap, key relevant metric for that portfolio type (e.g. dividend yield for Income, valuation_score for Deep Value), and a robust-pick indicator.
- The portfolio aggregate stats panel (described above) beside or above the table.
- Each row clickable → opens the ticker detail (Tab 4) for that name.
- An "analyst override reminder" note: "Review each name for open SEC investigations, restatements, or pending binary events before committing capital — the model is purely quantitative." (This reflects the Framework's explicit analyst-override recommendation.)

**Tab 3 — Screener (full universe)**
- The full scored universe as a sortable, filterable table. This replaces the terminal REPL.
- Columns: ticker, company, sector_group (friendly label), potential_score, V/Q/G/S sub-scores, price, market_cap, pe_ttm, dividend_yield, return_12m, flags, liquidity_tier.
- Click any column header to sort (toggle asc/desc).
- Filter controls: a sector dropdown, a text search on ticker/company, a min-score slider, and flag filter checkboxes (DEEP_VALUE, FALLEN_ANGEL, QUIET_COMPOUNDER, MOMENTUM_VALUE, OVEREXTENDED — the flags live in the `flags` column as a delimited string; parse and filter).
- Row click → ticker detail.
- Paginate or virtualize if rendering 2,000+ rows is slow; aim for smooth scroll. Showing top 500 by score with a "show all" toggle is acceptable.

**Tab 4 — Ticker Detail (the deep-dive view)**
- Opens when a user selects any ticker anywhere in the interface.
- A price chart at the top using Lightweight Charts — the 3-year weekly close series for that ticker. Trading-interface styling: dark, with a subtle area fill under the line, crosshair on hover showing date + price. If the ticker has no price series embedded, show a clean "price history not available for this ticker" placeholder.
- Below the chart, ALL available data for that ticker, organized into labeled cards/sections:
  - **Scores**: potential_score (large, prominent) with its percentile context, plus V/Q/G/S sub-scores shown as labeled bars (0-100). Include the resampling band: potential_p05 / potential_median / potential_p95 and whether it's a robust_pick.
  - **Valuation**: pe, pe_ttm, pb, ps, ps_ttm, pfcf, peg, ev_ebitda, ev_ebitda_ttm.
  - **Profitability & Quality**: gross/operating/net margins (current + 3y/5y medians), roe, roa, roic (+ 5y median), piotroski_f, altman_z.
  - **Growth**: revenue_growth_3yr, revenue_trend_5y, ebitda_trend_5y, fcf_trend_5y, plus TTM figures.
  - **Balance sheet & risk**: debt_equity, current_ratio, beta, realized_vol.
  - **Returns & momentum**: return_1m/3m/6m/12m, return_12m_minus_1m, distance_from_52w_high, 52w high/low.
  - **Ownership & flow**: insider_own, inst_own, short_float (note these may be NaN if Finviz was disabled — show "N/A" gracefully).
  - **Liquidity & freshness**: avg_dollar_volume_30d, liquidity_tier, last_filing_date, n_yrs_history, stale flag.
  - **Flags**: render any flags as labeled pills with a tooltip explaining each (definitions in the Terminology section).
- Every numeric value formatted appropriately: percentages as %, large dollar figures with K/M/B suffixes, ratios to 1-2 decimals. NaN/None renders as a muted "—", never "nan" or "None" or "NaN".

**Tab 5 — Backtest / Performance**
- Render the latest audit JSON into readable form.
- The per-period table: period label, top_alpha, spread, IC, regime, with the honest column labels.
- A small line chart (Lightweight Charts or Chart.js) of top_alpha across periods, and another of IC across periods.
- The OOS result called out separately and prominently if present in the audit.
- Factor IC table (V/Q/G/S mean ICs).
- A plain-English summary line interpreting the numbers (you can hardcode the Phase 4 conclusion: "Edge confirmed OOS at +8.46% top-alpha; bootstrap-robust; half-size deployment recommended pending 2025 confirmation").

## Terminology corrections (apply everywhere in the UI)

The terminal output uses misleading terms. Fix them in the HTML with both corrected labels and hover-tooltips:

- **"Spread"** → label as **"Long-Short Spread"** with tooltip: "Top decile minus bottom decile. Aspirational — the bottom decile is often unshortable for retail, so this overstates the realistic edge."
- **"Top-decile alpha"** / **top_alpha** → label as **"Top-Decile Alpha (vs universe)"** with tooltip: "Top decile return minus universe average. This is the realistic long-only edge." Make this the primary performance metric everywhere, more prominent than spread.
- **"Sentiment" / sentiment_score** → label as **"Price Momentum & 52w Position"** with tooltip: "NOT analyst or news sentiment. Computed from price momentum (12m-minus-1m return) and distance from 52-week high. Purely price-based."
- **"Potential score"** → keep the name but always show a calibration legend somewhere visible: "Score 1-100 (sector-relative percentile). 70+ = top decile, 80+ = top 5%, 90+ = top 1%."
- **Regime codes R1-R5** → always show the friendly name (mapping above), never the bare code, with the tilt explanation in a tooltip.
- **V/Q/G/S** → expand on hover: V = Valuation (cheapness), Q = Quality (business durability), G = Growth (improving fundamentals), S = Price Momentum & 52w Position.

Flag tooltips (from the codebase, lines 2014-2034):
- **DEEP_VALUE**: "Cheap and good — high valuation score (>80) and solid quality (>60)."
- **FALLEN_ANGEL**: "High-quality name beaten down — quality >70, bottom 15% of sector by 12m return, still cheap."
- **QUIET_COMPOUNDER**: "High quality (>80), growing revenue, low volatility. Durable compounder."
- **MOMENTUM_VALUE**: "Cheap and already turning up — valuation >70 with positive 6-month return."
- **OVEREXTENDED**: "Expensive and near 52-week high — fragile, elevated drawdown risk. A warning flag."

## Generator script requirements

- CLI args: `--db data/stock_cache.db` (default), `--audit <path>` (default: latest in data/audit/), `--out reports/stock_evaluator_<timestamp>.html` (default), `--max-price-tickers 250` (cap on embedded price series), `--sector-cap 4` (portfolio sector cap for 20-name).
- Read-only on all inputs. Never write to the cache or any data file.
- Print a summary on completion: rows loaded, portfolios generated, price series embedded, output file path and size.
- Must not require network access except the CDN scripts loaded by the browser at view time. The generator itself runs offline against local data.
- Handle the empty/missing cases gracefully: no cache → clear error and exit; no audit → backtest tab shows "no data"; SimFin unavailable → charts show placeholder, rest of report works.
- Embed data as a single JSON blob in a `<script id="DATA" type="application/json">` tag, parsed once by JS on load. Keep the JS modular: a render function per tab, a shared formatter module for numbers/percentages/NA handling.

## Validation

After building, run:

```bash
python scripts/generate_html_report.py
```

Then verify by opening the output file in a browser and checking:

1. File opens via `file://` with no console errors and no server.
2. All 5 tabs render and switch without reload.
3. Dashboard shows the friendly regime name and the half-size deployment banner.
4. Portfolios tab: all portfolio types selectable; 10/20 toggle works; sector cap is respected (no more than 4 names per sector in 20-name lists); aggregate stats compute; sector-focus portfolios only appear for sectors with ≥10 candidates.
5. Screener tab: sorting works on every column; sector/search/score/flag filters work; clicking a row opens that ticker's detail.
6. Ticker detail: price chart renders for tickers with embedded series, placeholder for those without; all data sections populate; NaN shows as "—" not "nan".
7. Backtest tab: per-period table and charts render from the audit JSON; OOS result called out if present.
8. Terminology: nowhere does the raw word "Spread" appear without the "Long-Short" qualifier and tooltip; "sentiment" is always relabeled; regime codes always show friendly names.
9. Numbers are right-aligned, tabular, and properly formatted (%, K/M/B, decimals).
10. File size under 8 MB.

## Hard constraints

1. **No scoring logic.** Read computed values only. Do not import scoring functions. Do not recompute scores. The freeze (`phase4-complete`) is not touched.
2. **Single self-contained HTML file** that works offline via `file://` (CDN scripts are the only external dependency, loaded at view time).
3. **No server, no build step, no React.** Vanilla JS + Tailwind CDN + Lightweight Charts CDN.
4. **Equal-weight portfolios only.** No HRP (Phase 4.2 rejected it).
5. **Half-size deployment messaging** must appear on the dashboard and portfolios tab.
6. **Graceful degradation** on every missing-data path; never crash the whole report because one input is absent.
7. **The generator is read-only** on all data files.

## Out of scope

- No live data fetching, no API calls from the generator (except SimFin local-disk load) or the HTML.
- No trade execution, no broker integration, no order tickets.
- No editing of scores or weights from the UI.
- No mobile layout.
- No multi-file output, no separate CSS/JS files — one HTML file.

When the script runs clean and the 10 validation checks pass, the viewer is complete.
