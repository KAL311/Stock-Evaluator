# Daily orchestration hub — `scripts/run_daily.py`

One-liner:

```
py -3 scripts\run_daily.py
```

Runs the verified daily pipeline end-to-end, writes today's dashboard to a
fixed path, and appends a one-line health record to a running log.

## What each step does

| # | Step | Script | Cache behavior |
|---|---|---|---|
| 1 | Price refresh (yfinance → `prices_live`) | `scripts/refreshprice.py --top 0 --period 1y` | Fetches all universe prices |
| 2 | FMP annual fundamentals | `scripts/fetch_fundamentals_fmp.py --top 2500 --years 6 --workers 6 --rate-per-min 250` | Cheap on re-run — hits `data/fmp/raw/` cache |
| 3 | FMP listing oracle | `scripts/fetch_fmp_listing_status.py` | Cheap on re-run — hits listing cache |
| 4 | Screener scoring (headless) | `src/market_screener.py --no-repl` | `<24h` cache skip via `cache_meta` |
| 5 | HTML dashboard | `scripts/generate_html_report.py --out reports/latest.html` | Overwrites |

Fails LOUD and stops on any step error; downstream steps do NOT run on
stale inputs.

## Fixed paths

| Path | Purpose |
|---|---|
| `reports/latest.html` | Today's dashboard. Bookmark this — always current. |
| `reports/archive/<YYYY-MM-DD>.html` | Dated copy after each successful run. |
| `reports/run_health.log` | Appended one line per run. Greppable. |
| `reports/.last_top10.json` | Previous run's tradeable top 10, for turnover comparison. `.gitignore`d. |

## Flags

| Flag | Behavior |
|---|---|
| `--force` | Delete `cache_meta` before step 4 so the screener rebuilds instead of hitting its 24h cache. |
| `--skip-fetch` | Skip step 2 (use existing `data/fmp/fundamentals_*.csv`). |
| `--skip-listing` | Skip step 3. |
| `--skip-prices` | Skip step 1 (use existing `prices_live`). |

`--no-repl` (on `src/market_screener.py`, added in the current commit) is
set automatically by `run_daily.py` via `SCREENER_NO_REPL=1`. The guard is
a single `if not no_repl: interactive_loop(df)` wrapper around the REPL
entry; scoring math is untouched (grep `git diff src/market_screener.py`
to confirm — only the guard block changes).

## Env checks (fail-fast)

- `FMP_API_KEY` must be present in env OR in `FMP_API.env`. Missing → immediate abort with `WARN=NO_FMP_API_KEY` in the health line.
- `USE_FMP_FUNDAMENTALS`: unset / `1` / `true` / `yes` → FMP path (default). `0` / `off` / `false` / `no` → SimFin rollback. Both paths are supported; the run announces which is active at startup and the health line records `source=FMP` or `source=SimFin`.

## Reading the health line

Example:

```
2026-07-12T09:30:14 | STATUS=OK | source=FMP | universe=2514 | live=1481 delisted=32 unpriced=11 | coverage=99.9% | non_usd_fallback=19 | mixed_source=17 | top10_turnover_vs_prev=2 | duration=612s
```

| Field | Meaning |
|---|---|
| `STATUS` | `OK` if every step succeeded, `FAILED` otherwise |
| `source` | Which annual-fundamentals source served the run (FMP / SimFin) |
| `universe` | Tickers passing the screener's market-cap + liquidity floor |
| `live` / `delisted` / `unpriced` | Post-liveness counts from `compute_liveness_and_flag` |
| `coverage` | % of scored universe with FMP annual data cached |
| `non_usd_fallback` | Tickers routed to SimFin because `reportedCurrency != USD` (fix from commit `bf6e876`) |
| `mixed_source` | Tickers whose 5-year FMP+SF fallback window mixed sources (near-0 under physical-period keying) |
| `top10_turnover_vs_prev` | Names that changed in the tradeable top-10 vs yesterday's run (0 = quiet, high = book moved) |
| `duration` | Wall clock seconds |
| `WARN=...` | Comma-separated warnings if any triggered |

## WARN conditions (greppable)

`grep WARN reports/run_health.log`

| Warning | Fires when |
|---|---|
| `NO_FMP_API_KEY` | Env check failed, run aborted |
| `LOW_COVERAGE=XX.X%` | Cached FMP < 85% of scored universe |
| `SOURCE_LINE_MISSING` | Screener didn't print its source line — regression suspected |
| `SOURCE_MISMATCH=X` | Active source ≠ intended default from `USE_FMP_FUNDAMENTALS` |
| `NO_LIVE_TICKERS_SCORED` | Every scored ticker is flagged DELISTED/UNPRICED — impossible in healthy state |
| `FETCH_HAS_FAILURES` | `data/fmp/fetch_failures.log` grew during this run |
| `STEP_FAILED=<name>` | Named step exited non-zero |
| `SKIPPED_<STEP>` | An operator `--skip-*` flag was used (informational, not a fault) |

## Dashboard status banner

`reports/latest.html` gets a status banner injected at the top of `<body>`:

```
[2026-07-12T09:30:14] source=FMP · universe=2514 · live=1481 · delisted=32 · unpriced=11 · coverage=99.9% · non_usd=19 · mixed=17 · top10 turnover=2
```

Any `WARN` fields render in red. So opening the hub shows both the picks
and whether the run was clean.

## Rollback

Two independent rollbacks, both zero-code:

- **Data-source rollback**: set `USE_FMP_FUNDAMENTALS=0` in the invoking
  env. The next `run_daily.py` uses SimFin annual fundamentals instead of
  FMP. `latest.html` gets a `source=SimFin` banner so the change is
  visible.
- **Full-run rollback**: `reports/archive/<YYYY-MM-DD>.html` retains
  every past dashboard. Compare to `latest.html` if today looks wrong.

## Windows Task Scheduler — the cron equivalent

Operator step, NOT auto-configured. Command line to paste into a
Scheduled Task:

```
py -3 "C:\Users\fraun\OneDrive\Documents\Stock Evaluator\scripts\run_daily.py"
```

Suggested schedule: weekday 06:30 America/Chicago (after the FMP oracle
refresh window and before the US open). The screener writes `latest.html`
regardless of whether anyone is watching — bookmark that file and open it
whenever.

To create the task interactively:
1. Task Scheduler → Create Task.
2. General: name `StockEvaluator daily`, "Run whether user is logged on or not".
3. Triggers: Weekly, Mon–Fri, 06:30 local.
4. Actions: `py.exe` with the above args in `Add arguments`; `Start in` = the repo root.
5. Conditions: uncheck "Start the task only if the computer is on AC power" (laptops).
6. Settings: "If the running task does not end when requested, force it to stop" ON.

## Firewall

Live path uses FMP annual + SimFin quarterly/TTM. **Backtest
(`scripts/backtest.v2.py`, `scripts/backtest.py`) is not touched by
`run_daily.py`** and continues to source fundamentals from SimFin only —
grep verifies. `phase5-frozen` tag intact.
