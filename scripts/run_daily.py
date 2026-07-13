"""Daily orchestration hub for the Stock Evaluator.

Chains the verified daily pipeline into one idempotent, cache-aware run:

    1. scripts/refreshprice.py    --top 0 --period 1y      (prices → prices_live)
    2. scripts/fetch_fundamentals_fmp.py --top 2500 --years 6
                                   --workers 6 --rate-per-min 250 (FMP annual)
    3. scripts/fetch_fmp_listing_status.py                  (listing oracle)
    4. src/market_screener.py --no-repl                     (score → cache)
    5. scripts/generate_html_report.py --out reports/latest.html (dashboard)

Fixed dashboard path: `reports/latest.html` (overwrites each run).
Dated archive:        `reports/archive/<YYYY-MM-DD>.html`.
Health log line:      `reports/run_health.log` (appended, greppable).
Prev-top-10 store:    `reports/.last_top10.json` (gitignored).

Constraints:
 - Fail LOUD and STOP on any step error. Do NOT run downstream steps on
   stale inputs. Never produce a dashboard from a partial pipeline.
 - Idempotent: `--force` re-runs cache-cheap steps; otherwise fetchers hit
   raw JSON cache (data/fmp/raw/) and market_screener respects its 24h
   cache_meta gate.
 - No scoring / gate-logic edits. This script only invokes the existing
   verified scripts as subprocesses (or imports for the screener).

Env vars honored:
 - `USE_FMP_FUNDAMENTALS`: 0/off/false → SimFin rollback. Any other value
   (or unset) → FMP default. Passed through unchanged to the screener.
 - `FMP_API_KEY`: required (or `FMP_API.env` present with key). Fail fast
   if missing.
 - `SCREENER_NO_REPL`: set to 1 by this script so the screener exits
   after scoring.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable  # honor the interpreter that invoked us

REPORTS_DIR = REPO_ROOT / "reports"
ARCHIVE_DIR = REPORTS_DIR / "archive"
LATEST_HTML = REPORTS_DIR / "latest.html"
HEALTH_LOG = REPORTS_DIR / "run_health.log"
LAST_TOP10 = REPORTS_DIR / ".last_top10.json"


# ------------------------------------------------------------------ helpers


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _load_api_key() -> str | None:
    """Same fallback pattern as the fetchers — env then FMP_API.env."""
    key = os.environ.get("FMP_API_KEY")
    if key:
        return key.strip()
    p = REPO_ROOT / "FMP_API.env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            _, val = line.split("=", 1)
            val = val.strip().strip('"').strip("'")
            if val:
                return val
    return None


def _run_step(
    name: str, cmd: list[str], env: dict[str, str], warns: list[str],
) -> tuple[bool, str]:
    """Run a subprocess. Return (ok, combined_output).

    stderr is redirected into stdout so the health log gets one stream.
    """
    print(f"\n===== {name} =====")
    print(f"$ {' '.join(cmd)}")
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        warns.append(f"MISSING_SCRIPT={name}")
        return False, f"ERROR: {e}"
    dur = time.time() - t0
    out = proc.stdout or ""
    # Print output live for the operator watching in a terminal
    sys.stdout.write(out)
    print(f"[{name} exit={proc.returncode} duration={dur:.1f}s]")
    if proc.returncode != 0:
        return False, out
    return True, out


def _parse_screener_output(out: str) -> dict[str, str]:
    """Pull the counts printed by market_screener into a small dict.

    Grep patterns match the actual log strings, not a spec. If the screener
    log format changes, this function silently returns empty strings for the
    missing fields and the corresponding WARN fires elsewhere.
    """
    info: dict[str, str] = {}

    m = re.search(r"FUNDAMENTALS SOURCE:\s*(FMP.*|SimFin.*)", out)
    if m:
        source = m.group(1)
        info["source"] = "FMP" if source.startswith("FMP") else "SimFin"

    m = re.search(r"NON_USD fallback:\s*(\d+) tickers routed to SimFin", out)
    if m:
        info["non_usd_fallback"] = m.group(1)

    m = re.search(r"mixed-source-history tickers:\s*(\d+)/(\d+)", out)
    if m:
        info["mixed_source"] = m.group(1)
        info["universe_annual"] = m.group(2)

    m = re.search(r"Processed\s+(\d+)\s+stocks", out)
    if m:
        info["universe"] = m.group(1)

    m = re.search(r"Scored\s+(\d+)\s*/\s*(\d+)\s+stocks", out)
    if m:
        info["scored"] = m.group(1)
        info["universe"] = info.get("universe") or m.group(2)

    # FMP fetcher — surface effective_rate + failures for the health log
    m = re.search(r"effective_rate=(\d+)/min", out)
    if m:
        info["fetch_rate"] = m.group(1)
    m = re.search(r"api_calls=(\d+) cache_hits=(\d+)", out)
    if m:
        info["fetch_api_calls"] = m.group(1)
        info["fetch_cache_hits"] = m.group(2)

    return info


def _current_top10_tradeable() -> list[str]:
    """Read tradeable top 10 from stock_cache.db using the diagnostic filter."""
    import sqlite3

    sys.path.insert(0, str(REPO_ROOT))
    from src.diagnostic_filters import tradeable_sql_filter  # noqa: E402

    db = REPO_ROOT / "data" / "stock_cache.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    q = (
        f"SELECT ticker FROM stocks "
        f"WHERE potential_score IS NOT NULL AND ({tradeable_sql_filter()}) "
        f"ORDER BY potential_score DESC LIMIT 10"
    )
    rows = [r[0] for r in conn.execute(q).fetchall()]
    conn.close()
    return rows


def _load_prev_top10() -> list[str]:
    if not LAST_TOP10.exists():
        return []
    try:
        data = json.loads(LAST_TOP10.read_text())
        return list(data.get("top10", []))
    except Exception:
        return []


def _save_top10(top10: list[str]) -> None:
    LAST_TOP10.parent.mkdir(parents=True, exist_ok=True)
    LAST_TOP10.write_text(
        json.dumps({"top10": top10, "timestamp": _now_iso()}, indent=2)
    )


def _coverage_pct() -> float | None:
    """FMP fundamentals coverage vs scored universe."""
    import sqlite3
    import pandas as pd

    try:
        conn = sqlite3.connect(str(REPO_ROOT / "data" / "stock_cache.db"))
        universe = {
            t for t, in conn.execute(
                "SELECT ticker FROM stocks WHERE potential_score IS NOT NULL"
            ).fetchall()
        }
        conn.close()
        fmp = pd.read_csv(
            REPO_ROOT / "data" / "fmp" / "fundamentals_income.csv",
            usecols=["ticker"],
        )
        fmp_t = set(fmp["ticker"].unique())
        if not universe:
            return None
        return len(universe & fmp_t) / len(universe) * 100
    except Exception:
        return None


def _flag_counts() -> tuple[int, int, int]:
    """(live_scored, delisted_flagged, unpriced_flagged) from cache."""
    import sqlite3

    try:
        conn = sqlite3.connect(str(REPO_ROOT / "data" / "stock_cache.db"))
        live = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE potential_score IS NOT NULL "
            "AND (flags IS NULL OR (flags NOT LIKE '%DELISTED%' "
            "AND flags NOT LIKE '%UNPRICED%'))"
        ).fetchone()[0]
        delisted = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE flags LIKE '%DELISTED%'"
        ).fetchone()[0]
        unpriced = conn.execute(
            "SELECT COUNT(*) FROM stocks WHERE flags LIKE '%UNPRICED%'"
        ).fetchone()[0]
        conn.close()
        return live, delisted, unpriced
    except Exception:
        return 0, 0, 0


def _inject_status_banner(html_path: Path, banner_text: str) -> None:
    """Insert a status banner right after <body>. Idempotent — replaces
    an existing banner if present."""
    if not html_path.exists():
        return
    html = html_path.read_text(encoding="utf-8", errors="replace")
    banner_html = (
        f'<div id="run-status-banner" '
        f'style="background:#f5f5f5;border-bottom:1px solid #ccc;'
        f'padding:6px 12px;font-family:monospace;font-size:12px;'
        f'color:#333;">{banner_text}</div>'
    )
    # Remove old banner (if this file was already banner-injected)
    html = re.sub(
        r'<div id="run-status-banner"[^>]*>.*?</div>',
        "", html, count=1, flags=re.DOTALL,
    )
    if "<body" in html:
        # Inject after the FIRST <body ...> tag
        html = re.sub(
            r"(<body[^>]*>)", r"\1" + banner_html, html, count=1
        )
    else:
        html = banner_html + html
    html_path.write_text(html, encoding="utf-8")


def _append_health(line: str) -> None:
    HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with HEALTH_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print("\n" + line)


# ------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily orchestrator for Stock Evaluator")
    ap.add_argument(
        "--force", action="store_true",
        help="Force re-fetch even when caches are warm (still respects rate cap).",
    )
    ap.add_argument(
        "--skip-fetch", action="store_true",
        help="Skip FMP fundamentals fetch (use existing CSVs; useful for retries).",
    )
    ap.add_argument(
        "--skip-listing", action="store_true",
        help="Skip FMP listing-status oracle refresh.",
    )
    ap.add_argument(
        "--skip-prices", action="store_true",
        help="Skip yfinance price refresh (use existing prices_live).",
    )
    args = ap.parse_args()

    t0 = time.time()
    ts = _now_iso()
    warns: list[str] = []
    print(f"===== run_daily {ts} =====")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Env checks ----
    if not _load_api_key():
        _append_health(
            f"{ts} | STATUS=FAILED | source=? | universe=0 | live=0 delisted=0 "
            f"unpriced=0 | coverage=- | non_usd_fallback=- | mixed_source=- "
            f"| top10_turnover_vs_prev=- | duration=0s | WARN=NO_FMP_API_KEY"
        )
        print("ERROR: FMP_API_KEY not set (env or FMP_API.env). Aborting.", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["SCREENER_NO_REPL"] = "1"
    # Announce which source the screener will use
    use_fmp_env = env.get("USE_FMP_FUNDAMENTALS", "").strip().lower()
    default_is_fmp = use_fmp_env not in ("0", "off", "false", "no")
    print(f"  USE_FMP_FUNDAMENTALS={env.get('USE_FMP_FUNDAMENTALS', '(unset - FMP default)')} "
          f"-> intended source: {'FMP' if default_is_fmp else 'SimFin'}")

    # ---- Steps ----
    all_output: list[str] = []
    step_durations: dict[str, float] = {}

    def step(name: str, cmd: list[str], skip: bool = False) -> bool:
        if skip:
            print(f"\n===== {name} (SKIPPED via CLI) =====")
            warns.append(f"SKIPPED_{name.replace(' ', '_').upper()}")
            return True
        step_t0 = time.time()
        ok, out = _run_step(name, cmd, env, warns)
        step_durations[name] = time.time() - step_t0
        all_output.append(out)
        if not ok:
            warns.append(f"STEP_FAILED={name}")
        return ok

    # 1. Prices
    ok = step(
        "1_prices_refresh",
        [PY, "-3" if os.name != "nt" else "-u",
         "scripts/refreshprice.py", "--top", "0", "--period", "1y"]
        if False else
        [PY, "-u", "scripts/refreshprice.py", "--top", "0", "--period", "1y"],
        skip=args.skip_prices,
    )
    if not ok:
        _finalize_fail(t0, warns, all_output, ts)
        return 3

    # 2. FMP fundamentals
    fetch_cmd = [
        PY, "-u", "scripts/fetch_fundamentals_fmp.py",
        "--top", "2500", "--years", "6",
        "--workers", "6", "--rate-per-min", "250",
    ]
    ok = step("2_fmp_fundamentals", fetch_cmd, skip=args.skip_fetch)
    if not ok:
        _finalize_fail(t0, warns, all_output, ts)
        return 4

    # 3. Listing oracle
    ok = step(
        "3_fmp_listing_oracle",
        [PY, "-u", "scripts/fetch_fmp_listing_status.py"],
        skip=args.skip_listing,
    )
    if not ok:
        _finalize_fail(t0, warns, all_output, ts)
        return 5

    # 4. Screener (headless)
    if args.force:
        # Delete cache_meta so main() rebuilds
        import sqlite3
        try:
            conn = sqlite3.connect(str(REPO_ROOT / "data" / "stock_cache.db"))
            conn.execute("DELETE FROM cache_meta")
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"  (could not clear cache_meta for --force: {e})")

    ok = step(
        "4_screener_scoring",
        [PY, "-u", "src/market_screener.py", "--no-repl"],
    )
    if not ok:
        _finalize_fail(t0, warns, all_output, ts)
        return 6

    # 5. HTML report
    ok = step(
        "5_html_report",
        [PY, "-u", "scripts/generate_html_report.py",
         "--out", str(LATEST_HTML)],
    )
    if not ok:
        _finalize_fail(t0, warns, all_output, ts)
        return 7

    # Archive copy
    today = _dt.datetime.now().strftime("%Y-%m-%d")
    if LATEST_HTML.exists():
        try:
            shutil.copyfile(LATEST_HTML, ARCHIVE_DIR / f"{today}.html")
        except Exception as e:
            warns.append(f"ARCHIVE_COPY_FAILED={e}")

    # ---- Health line ----
    joined = "\n".join(all_output)
    info = _parse_screener_output(joined)

    # Enrich with cache-derived counts
    coverage = _coverage_pct()
    live, delisted, unpriced = _flag_counts()

    # Top-10 turnover
    today_top10 = _current_top10_tradeable()
    prev_top10 = _load_prev_top10()
    turnover = len(set(today_top10) ^ set(prev_top10)) // 2 if prev_top10 else -1
    _save_top10(today_top10)

    # WARN conditions
    if coverage is not None and coverage < 85:
        warns.append(f"LOW_COVERAGE={coverage:.1f}%")
    if not info.get("source"):
        warns.append("SOURCE_LINE_MISSING")
    elif (info["source"] == "SimFin") != (not default_is_fmp):
        warns.append(f"SOURCE_MISMATCH={info['source']}")
    if live == 0:
        warns.append("NO_LIVE_TICKERS_SCORED")
    if info.get("fetch_api_calls") and int(info.get("fetch_api_calls", 0)) > 0:
        # A partial fetch would show many API calls PLUS a fails counter.
        # The existing fetcher log surfaces it — pass through as WARN only if
        # the failure log grew.
        fetch_fails_log = REPO_ROOT / "data" / "fmp" / "fetch_failures.log"
        if fetch_fails_log.exists() and fetch_fails_log.stat().st_size > 0:
            warns.append("FETCH_HAS_FAILURES")

    status = "OK" if not any(w.startswith("STEP_FAILED") for w in warns) else "FAILED"
    warn_str = f" | WARN={','.join(warns)}" if warns else ""
    duration = time.time() - t0

    cov_str = f"{coverage:.1f}%" if coverage is not None else "-"
    turn_str = str(turnover) if turnover >= 0 else "-"
    health_line = (
        f"{ts} | STATUS={status}"
        f" | source={info.get('source', '?')}"
        f" | universe={info.get('universe', '?')}"
        f" | live={live} delisted={delisted} unpriced={unpriced}"
        f" | coverage={cov_str}"
        f" | non_usd_fallback={info.get('non_usd_fallback', '-')}"
        f" | mixed_source={info.get('mixed_source', '-')}"
        f" | top10_turnover_vs_prev={turn_str}"
        f" | duration={duration:.0f}s{warn_str}"
    )

    _append_health(health_line)

    # Banner into dashboard
    banner = (
        f"[{ts}] source={info.get('source', '?')} |"
        f"universe={info.get('universe', '?')} |"
        f"live={live} |delisted={delisted} |unpriced={unpriced} |"
        f"coverage={cov_str} |"
        f"non_usd={info.get('non_usd_fallback', '-')} |"
        f"mixed={info.get('mixed_source', '-')} |"
        f"top10 turnover={turn_str}"
    )
    if warns:
        banner += f' <span style="color:#b00;font-weight:bold">· WARN: {", ".join(warns)}</span>'
    _inject_status_banner(LATEST_HTML, banner)

    print(f"\nDashboard: {LATEST_HTML}")
    print(f"Archive:   {ARCHIVE_DIR / (today + '.html')}")
    print(f"Health:    {HEALTH_LOG}")
    return 0


def _finalize_fail(t0: float, warns: list[str], all_output: list[str], ts: str) -> None:
    duration = time.time() - t0
    warn_str = f" | WARN={','.join(warns)}" if warns else ""
    _append_health(
        f"{ts} | STATUS=FAILED | source=? | universe=? "
        f"| live=? delisted=? unpriced=? | coverage=- | non_usd_fallback=- "
        f"| mixed_source=- | top10_turnover_vs_prev=- "
        f"| duration={duration:.0f}s{warn_str}"
    )
    print("\n===== RUN FAILED — downstream steps skipped. See run_health.log =====")


if __name__ == "__main__":
    sys.exit(main())
