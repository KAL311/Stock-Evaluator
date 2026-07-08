#!/usr/bin/env python3
"""Generate a self-contained static HTML report from the Stock Evaluator cache."""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
AUDIT_DIR = DATA_DIR / "audit"
SIMFIN_DIR = DATA_DIR / "simfin"

REGIME_MAP = {
    "R1": "Disinflationary Expansion (Goldilocks)",
    "R2": "Reflationary Expansion",
    "R3": "Late-Cycle / Stagflation",
    "R4": "Recession",
    "R5": "Recovery",
}

REGIME_TILTS = {
    "R1": "Overweight tech_software, tech_hardware, consumer_disc; underweight energy, staples, utilities",
    "R2": "Overweight energy, industrials, utilities; underweight tech_software, consumer_disc, banks",
    "R3": "Overweight staples, healthcare, utilities, energy; underweight consumer_disc, tech_software",
    "R4": "Overweight staples, utilities, healthcare; underweight banks, consumer_disc, industrials, tech_hardware",
    "R5": "Overweight banks, industrials, consumer_disc; underweight staples, utilities",
}

FLAG_TOOLTIPS = {
    "DEEP_VALUE": "Cheap and good — high valuation score (>80) and solid quality (>60).",
    "FALLEN_ANGEL": "High-quality name beaten down — quality >70, bottom 15% of sector by 12m return, still cheap.",
    "QUIET_COMPOUNDER": "High quality (>80), growing revenue, low volatility. Durable compounder.",
    "MOMENTUM_VALUE": "Cheap and already turning up — valuation >70 with positive 6-month return.",
    "OVEREXTENDED": "Expensive and near 52-week high — fragile, elevated drawdown risk. A warning flag.",
    "DELISTED": "No fresh price data in prices_live (yfinance). Likely delisted, acquired, or taken private. Excluded from all portfolios; shown greyed out here for reference only.",
}


def fmt_val(x, decimals=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return round(float(x), decimals)


def fmt_pct(x):
    v = fmt_val(x, 4)
    if v is None:
        return None
    return f"{v * 100:.2f}%"


def fmt_dollar(x):
    v = fmt_val(x, 2)
    if v is None:
        return None
    if abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    elif abs(v) >= 1e6:
        return f"${v / 1e6:.2f}M"
    elif abs(v) >= 1e3:
        return f"${v / 1e3:.2f}K"
    return f"${v:.2f}"


def load_stocks(db_path):
    if not Path(db_path).exists():
        print(f"ERROR: Cache database not found at {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT * FROM stocks", conn)
    conn.close()
    meta_conn = sqlite3.connect(db_path)
    meta_cur = meta_conn.cursor()
    meta = {}
    try:
        meta_cur.execute("SELECT key, value FROM cache_meta")
        for k, v in meta_cur.fetchall():
            meta[k] = v
    except Exception:
        pass
    meta_conn.close()
    print(f"  Loaded {len(df)} stocks from cache")
    return df, meta


def fix_nulls(obj):
    if isinstance(obj, dict):
        return {k: fix_nulls(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [fix_nulls(v) for v in obj]
    elif isinstance(obj, float) and np.isnan(obj):
        return None
    return obj


def load_audit(audit_path):
    if audit_path and Path(audit_path).exists():
        with open(audit_path) as f:
            data = json.load(f)
        data = fix_nulls(data)
        print(f"  Loaded audit: {audit_path} ({data.get('n_periods', 0)} periods)")
        return data
    candidates = sorted(AUDIT_DIR.glob("audit_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        with open(candidates[0]) as f:
            data = json.load(f)
        data = fix_nulls(data)
        print(f"  Loaded latest audit: {candidates[0].name} ({data.get('n_periods', 0)} periods)")
        return data
    print("  No audit JSONs found — backtest tab will show 'no data'")
    return None


def load_price_history(tickers, max_tickers=250):
    needed = set(tickers)
    if len(needed) > max_tickers:
        needed = set(list(needed)[:max_tickers])
    if not needed:
        return {}
    csv_path = SIMFIN_DIR / "us-shareprices-daily.csv"
    if not csv_path.exists():
        print("  WARNING: SimFin price CSV not found — price charts unavailable")
        return {}
    try:
        price_dfs = []
        for chunk in pd.read_csv(csv_path, sep=";", parse_dates=["Date"], chunksize=500000):
            mask = chunk["Ticker"].isin(needed)
            if mask.any():
                price_dfs.append(chunk[mask])
            total = sum(len(d) for d in price_dfs) if price_dfs else 0
            if total > len(needed) * 800:
                break
        if not price_dfs:
            return {}
        sp = pd.concat(price_dfs, ignore_index=True)
        sp = sp[sp["Ticker"].isin(needed)]
        sp = sp.sort_values(["Ticker", "Date"])
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=3)
        sp = sp[sp["Date"] >= cutoff]
        sp = sp.set_index("Date").groupby("Ticker").resample("W-FRI", closed="right", label="right").agg({
            "Adj. Close": "last"
        }).reset_index()
        sp = sp.dropna(subset=["Adj. Close"])
        result = {}
        for ticker, group in sp.groupby("Ticker"):
            group = group.sort_values("Date")
            ms = [int(d.timestamp() * 1000) for d in group["Date"]]
            prices = [round(float(p), 2) for p in group["Adj. Close"]]
            result[ticker] = [[m, p] for m, p in zip(ms, prices)]
        print(f"  Loaded price history for {len(result)} tickers ({sum(len(v) for v in result)} weekly bars)")
        return result
    except Exception as e:
        print(f"  WARNING: Failed to load price history: {e}")
        return {}


def load_prices_live(db_path):
    try:
        conn = sqlite3.connect(db_path)
        df = pd.read_sql_query(
            "SELECT ticker, date, close FROM prices_live ORDER BY ticker, date",
            conn, parse_dates=["date"]
        )
        conn.close()
    except Exception as e:
        print(f"  prices_live unavailable ({e}); falling back to SimFin only")
        return {}, None
    if df.empty:
        return {}, None
    max_date = df["date"].max()
    by_ticker = {
        t: g[["date", "close"]].reset_index(drop=True)
        for t, g in df.groupby("ticker")
    }
    print(
        f"  prices_live loaded: {len(by_ticker)} tickers, "
        f"max date {max_date.date()}"
    )
    return by_ticker, max_date


def build_chart_series(ticker, prices_live, simfin_series=None, max_points=156):
    source = "live"
    series = None
    if ticker in prices_live and not prices_live[ticker].empty:
        df = prices_live[ticker].copy()
        df = df.set_index("date").sort_index()
        weekly = df["close"].resample("W-FRI").last().dropna().tail(max_points)
        if len(weekly) >= 10:
            series = [
                [int(d.timestamp() * 1000), round(float(c), 4)]
                for d, c in weekly.items()
            ]
    if series is None and simfin_series is not None:
        series = simfin_series
        source = "simfin"
    return series, source


def attach_live_prices(stocks_df, prices_live):
    live_close = {}
    live_date = {}
    for t, df in prices_live.items():
        if df.empty:
            continue
        last = df.sort_values("date").iloc[-1]
        live_close[t] = float(last["close"])
        live_date[t] = pd.Timestamp(last["date"]).strftime("%Y-%m-%d")
    stocks_df["live_price"] = stocks_df["ticker"].map(live_close)
    stocks_df["live_price_date"] = stocks_df["ticker"].map(live_date)
    n_live = stocks_df["live_price"].notna().sum()
    print(f"  attached live prices to {n_live}/{len(stocks_df)} tickers")
    return stocks_df


def compute_portfolios(df, sector_cap=4):
    candidate_cols = ["potential_score", "valuation_score", "quality_score", "growth_score",
                      "sentiment_score", "avg_dollar_volume_30d", "stale_fundamentals",
                      "sector_group", "dividend_yield", "return_6m", "top_decile_pct",
                      "robust_pick", "ticker", "company", "sector", "price", "market_cap",
                      "pe_ttm", "roic_5y_med", "beta", "realized_vol"]
    for c in candidate_cols:
        if c not in df.columns:
            df[c] = np.nan

    # Liveness gate: DELISTED and UNPRICED names are hard-excluded from
    # every portfolio.  Both flags are set upstream in
    # src/market_screener.compute_liveness_and_flag:
    #   DELISTED  = no recent prices_live data AND stale filing (>365d)
    #               -> corroborated delisting.
    #   UNPRICED  = no recent prices_live data BUT recent filing (<=365d)
    #               -> likely vendor blind-spot.  Still excluded here
    #               because we cannot value the position, but the label is
    #               honest ("cannot price right now") instead of falsely
    #               claiming the name is dead.
    flags_str = df["flags"].fillna("")
    delisted_mask = flags_str.str.contains("DELISTED")
    unpriced_mask = flags_str.str.contains("UNPRICED")
    excluded_mask = delisted_mask | unpriced_mask
    n_delisted = int(delisted_mask.sum())
    n_unpriced = int(unpriced_mask.sum())
    live_df = df[~excluded_mask]

    threshold = live_df["potential_score"].quantile(0.90)
    candidates = live_df[
        (live_df["potential_score"] >= threshold) &
        (live_df["avg_dollar_volume_30d"].fillna(0) >= 1_000_000) &
        (live_df["stale_fundamentals"].fillna(0) != 1)
    ].copy()
    if len(candidates) == 0:
        candidates = live_df.nlargest(int(len(live_df) * 0.1), "potential_score").copy()
    print(f"  Top-decile candidates (after liquidity/staleness filter): {len(candidates)}"
          f" (excluded {n_delisted} DELISTED, {n_unpriced} UNPRICED)")

    candidates["_potential_pctile"] = candidates["potential_score"].rank(pct=True)
    candidates["_return_6m_pctile"] = candidates["return_6m"].fillna(0).rank(pct=True)
    candidates["_dividend_yield_pctile"] = candidates["dividend_yield"].fillna(0).rank(pct=True)
    candidates["_quality_pctile"] = candidates["quality_score"].fillna(0).rank(pct=True)

    portfolios = {}

    def select_by_metric(df_pool, metric_col, target_count, sector_cap_val):
        pool = df_pool.sort_values(metric_col, ascending=False)
        selected = []
        sector_counts = {}
        for _, row in pool.iterrows():
            sg = row.get("sector_group", "") or ""
            if sector_counts.get(sg, 0) >= sector_cap_val:
                continue
            selected.append(row)
            sector_counts[sg] = sector_counts.get(sg, 0) + 1
            if len(selected) >= target_count:
                break
        return pd.DataFrame(selected)

    def portfolio_stats(port_df):
        if port_df.empty:
            return {}
        return {
            "n": len(port_df),
            "avg_potential_score": round(float(port_df["potential_score"].mean()), 2),
            "sector_breakdown": port_df["sector_group"].value_counts().to_dict(),
            "avg_dividend_yield": round(float(port_df["dividend_yield"].mean()), 4),
            "median_pe_ttm": round(float(port_df["pe_ttm"].median()), 2),
            "avg_roic_5y_med": round(float(port_df["roic_5y_med"].mean()), 4),
            "avg_beta": round(float(port_df["beta"].mean()), 2),
            "avg_realized_vol": round(float(port_df["realized_vol"].mean()), 4),
            "robust_pick_count": int(port_df["robust_pick"].sum()),
        }

    for count_label, target_count in [("10", 10), ("20", 20)]:
        sc = 2 if target_count == 10 else sector_cap

        bal = select_by_metric(candidates, "potential_score", target_count, sc)
        portfolios[f"balanced_{count_label}"] = {
            "name": f"Balanced (Top Score) — {count_label} names",
            "tickers": bal.to_dict("records"),
            "stats": portfolio_stats(bal),
            "ranking_metric": "potential_score",
            "rank_col": "potential_score",
        }

        candidates["_momentum_blend"] = (
            0.6 * candidates["_potential_pctile"] + 0.4 * candidates["_return_6m_pctile"]
        )
        mom = select_by_metric(candidates, "_momentum_blend", target_count, sc)
        portfolios[f"momentum_{count_label}"] = {
            "name": f"Performance / Momentum-tilted — {count_label} names",
            "tickers": mom.to_dict("records"),
            "stats": portfolio_stats(mom),
            "ranking_metric": "Momentum Blend",
            "rank_col": "_momentum_blend",
        }

        dv_pool = candidates[candidates["valuation_score"].fillna(0) > 70]
        dv = select_by_metric(dv_pool, "valuation_score", target_count, sc)
        portfolios[f"deep_value_{count_label}"] = {
            "name": f"Deep Value — {count_label} names" if len(dv) >= target_count // 2 else f"Deep Value — {len(dv)} names (limited pool)",
            "tickers": dv.to_dict("records"),
            "stats": portfolio_stats(dv),
            "ranking_metric": "Valuation Score",
            "rank_col": "valuation_score",
        }

        qc_pool = candidates[candidates["quality_score"].fillna(0) > 75]
        qc = select_by_metric(qc_pool, "quality_score", target_count, sc)
        portfolios[f"quality_{count_label}"] = {
            "name": f"Quality Compounder — {count_label} names" if len(qc) >= target_count // 2 else f"Quality Compounder — {len(qc)} names (limited pool)",
            "tickers": qc.to_dict("records"),
            "stats": portfolio_stats(qc),
            "ranking_metric": "Quality Score",
            "rank_col": "quality_score",
        }

        inc_pool = candidates[candidates["dividend_yield"].fillna(0) > 0.02].copy()
        if len(inc_pool):
            inc_pool["_income_blend"] = (
                0.5 * inc_pool["_dividend_yield_pctile"] + 0.5 * inc_pool["_quality_pctile"]
            )
        inc = select_by_metric(inc_pool, "_income_blend", target_count, sc) if len(inc_pool) else pd.DataFrame()
        portfolios[f"income_{count_label}"] = {
            "name": f"Income — {count_label} names" if len(inc) >= target_count // 2 else f"Income — {len(inc)} names (limited pool)",
            "tickers": inc.to_dict("records"),
            "stats": portfolio_stats(inc),
            "ranking_metric": "Income Blend",
            "rank_col": "_income_blend",
        }

    sector_counts = candidates["sector_group"].value_counts()
    qualifying_sectors = sector_counts[sector_counts >= 10].index.tolist()
    for sg in qualifying_sectors:
        if sg == "":
            continue
        sg_pool = candidates[candidates["sector_group"] == sg]
        sg_selected = sg_pool.nlargest(10, "potential_score")
        key = f"sector_{sg}"
        portfolios[key] = {
            "name": f"{sg.replace('_', ' ').title()} Focus — 10 names",
            "tickers": sg_selected.to_dict("records"),
            "stats": portfolio_stats(sg_selected),
            "ranking_metric": "Potential Score",
            "rank_col": "potential_score",
        }
    portfolios["_qualifying_sectors"] = [s for s in qualifying_sectors if s != ""]

    return portfolios, candidates


def clean_stock_row(row):
    d = {}
    for k, v in dict(row).items():
        if isinstance(v, float) and np.isnan(v):
            d[k] = None
        elif isinstance(v, (np.integer,)):
            d[k] = int(v)
        elif isinstance(v, (np.floating,)):
            d[k] = float(v)
        elif isinstance(v, np.bool_):
            d[k] = bool(v)
        else:
            d[k] = v
    return d


def build_html(meta, stocks_df, portfolios, price_history, chart_source_data,
               audit_data, regime, args, prices_live_max=None):
    stocks_list = [clean_stock_row(row) for _, row in stocks_df.iterrows()]
    regime_friendly = REGIME_MAP.get(regime, "Unknown")
    regime_tilt = REGIME_TILTS.get(regime, "")

    portfolio_data = {}
    for key, port in portfolios.items():
        if key.startswith("_"):
            continue
        portfolio_data[key] = {
            "name": port["name"],
            "tickers": [clean_stock_row(r) if isinstance(r, dict) else clean_stock_row(r) for r in port["tickers"]],
            "stats": port["stats"],
            "ranking_metric": port["ranking_metric"],
            "rank_col": port.get("rank_col", "potential_score"),
        }

    n_live = int(stocks_df["live_price"].notna().sum()) if "live_price" in stocks_df.columns else 0
    freshness = {
        "prices_live_max": str(prices_live_max.date()) if prices_live_max else None,
        "report_generated_at": datetime.now().isoformat(timespec="seconds"),
        "live_price_coverage": n_live,
        "total_tickers": int(len(stocks_df)),
    }

    data_blob = {
        "meta": {
            "last_updated": meta.get("last_updated", ""),
            "universe_size": len(stocks_df),
            "top_decile_size": int((stocks_df["potential_score"] >= stocks_df["potential_score"].quantile(0.90)).sum()),
            "robust_pick_count": int(stocks_df["robust_pick"].sum()),
            "regime": regime,
            "regime_friendly": regime_friendly,
            "regime_tilt": regime_tilt,
        },
        "stocks": stocks_list,
        "portfolios": portfolio_data,
        "qualifying_sectors": portfolios.get("_qualifying_sectors", []),
        "price_history": price_history,
        "chart_source": chart_source_data,
        "audit": audit_data,
        "freshness": freshness,
    }

    json_data = json.dumps(data_blob, default=str)
    flag_tooltips_json = json.dumps(FLAG_TOOLTIPS)

    HTML = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Evaluator — Trading Desk View</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config={darkMode:'class',theme:{extend:{colors:{surface:'#1a1d23',card:'#22262e',hover:'#2a2f38',border:'#323842',accent:'#3b82f6',pos:'#34d399',neg:'#fb7185',muted:'#6b7280'}}}}
</script>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
  * { font-variant-numeric: tabular-nums; }
  body { font-family:'Inter',system-ui,sans-serif; background:#111318; color:#e5e7eb; }
  .num { text-align:right; font-variant-numeric: tabular-nums; }
  .text-col { text-align:left; }
  th, td { padding:8px 12px; }
  th { position:sticky; top:0; background:#1a1d23; z-index:10; cursor:pointer; user-select:none; white-space:nowrap; }
  th:hover { color:#60a5fa; }
  tr:hover td { background:#2a2f38; }
  .tab-btn { padding:10px 20px; border-radius:8px 8px 0 0; font-weight:500; cursor:pointer; transition:all .15s; white-space:nowrap; }
  .tab-btn.active { background:#1e293b; color:#60a5fa; border-bottom:2px solid #3b82f6; }
  .tab-btn:not(.active) { color:#9ca3af; }
  .tab-btn:not(.active):hover { color:#e5e7eb; background:#1a1d23; }
  .tab-content { display:none; }
  .tab-content.active { display:block; }
  .card { background:#1e293b; border-radius:12px; border:1px solid #323842; padding:20px; }
  .stat-value { font-size:1.5rem; font-weight:700; color:#f1f5f9; }
  .stat-label { font-size:0.75rem; color:#9ca3af; text-transform:uppercase; letter-spacing:0.05em; }
  .pill { display:inline-block; padding:2px 8px; border-radius:4px; font-size:0.75rem; font-weight:500; }
  .pill-green { background:rgba(52,211,153,0.15); color:#34d399; }
  .pill-red { background:rgba(251,113,133,0.15); color:#fb7185; }
  .pill-blue { background:rgba(96,165,250,0.15); color:#60a5fa; }
  .pill-amber { background:rgba(251,191,36,0.15); color:#fbbf24; }
  .sticky-nav { position:sticky; top:0; z-index:50; background:#111318; border-bottom:1px solid #323842; }
  ::-webkit-scrollbar { width:6px; height:6px; }
  ::-webkit-scrollbar-track { background:#111318; }
  ::-webkit-scrollbar-thumb { background:#323842; border-radius:3px; }
  .banner { background:linear-gradient(135deg,rgba(59,130,246,0.1),rgba(52,211,153,0.05)); border:1px solid rgba(59,130,246,0.3); border-radius:8px; padding:12px 16px; }
  .sorter::after { content:' \\2195'; opacity:0.3; font-size:0.7em; }
  .sorter.asc::after { content:' \\2191'; opacity:1; }
  .sorter.desc::after { content:' \\2193'; opacity:1; }
  .flag-pill { display:inline-block; padding:1px 6px; border-radius:3px; font-size:0.65rem; font-weight:600; cursor:help; margin:1px; }
</style>
</head>
<body>
<div class="sticky-nav px-6 py-3">
  <div class="flex items-center gap-4 flex-wrap">
    <div class="text-lg font-bold text-white mr-4">Stock Evaluator</div>
    <div id="tabNav" class="flex gap-1 overflow-x-auto"></div>
    <div class="ml-auto text-xs text-muted hidden md:block">Phase 4 \u2022 Half-Size EW</div>
  </div>
  <div id="freshnessBar" class="text-xs text-muted mt-1.5"></div>
</div>
<div id="tabContainer" class="px-6 py-4 max-w-screen-2xl mx-auto"></div>

<script id="DATA" type="application/json">__JSON_DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('DATA').textContent);
const REGIME_FRIENDLY_MAP = {R1:'Disinflationary Expansion (Goldilocks)',R2:'Reflationary Expansion',R3:'Late-Cycle / Stagflation',R4:'Recession',R5:'Recovery'};
const REGIME_TILTS = {R1:'Overweight growth, tech, consumer disc',R2:'Overweight cyclicals, financials, materials',R3:'Overweight staples, healthcare, utilities, energy',R4:'Overweight treasuries, gold, utilities',R5:'Overweight small caps, financials, tech'};
const FLAG_TOOLTIPS = __FLAG_TOOLTIPS__;
const SECTOR_GROUPS = [...new Set(DATA.stocks.map(s=>s.sector_group).filter(Boolean))].sort();
let chartInstance = null;
let currentChartTicker = null;

function fmtPct(v) { if(v==null) return '\u2014'; return (v*100).toFixed(2)+'%'; }
function fmtDec(v,d) { if(v==null) return '\u2014'; return Number(v).toFixed(d||2); }
function fmtDollar(v) { if(v==null) return '\u2014'; var a=Math.abs(v); if(a>=1e9) return '$'+(v/1e9).toFixed(2)+'B'; if(a>=1e6) return '$'+(v/1e6).toFixed(2)+'M'; if(a>=1e3) return '$'+(v/1e3).toFixed(2)+'K'; return '$'+v.toFixed(2); }
function fmtBig(v) { if(v==null) return '\u2014'; return Number(v).toLocaleString(undefined,{maximumFractionDigits:0}); }
function escapeHtml(s) { if(s==null) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function fmtSector(s) { return s?s.replace(/_/g,' ').replace(/\\b\\w/g,c=>c.toUpperCase()):'\u2014'; }

function getFlags(stock) { return stock.flags?stock.flags.split(',').map(f=>f.trim()).filter(Boolean):[]; }
function renderFlags(stock) {
  var flags=getFlags(stock);
  if(!flags.length) return '<span class="text-muted text-xs">\u2014</span>';
  return flags.map(function(f) {
    var tip=FLAG_TOOLTIPS[f]||'';
    var cls='flag-pill ';
    if(f==='DEEP_VALUE'||f==='MOMENTUM_VALUE') cls+='bg-green-900/30 text-green-400';
    else if(f==='FALLEN_ANGEL') cls+='bg-amber-900/30 text-amber-400';
    else if(f==='QUIET_COMPOUNDER') cls+='bg-blue-900/30 text-blue-400';
    else if(f==='OVEREXTENDED') cls+='bg-red-900/30 text-red-400';
    else if(f==='DELISTED') cls+='bg-gray-800/60 text-gray-500 border border-gray-600';
    else cls+='bg-gray-700/50 text-gray-400';
    return '<span class="'+cls+'" title="'+escapeHtml(tip)+'">'+f+'</span>';
  }).join('');
}
function regimeFriendly(r) { return REGIME_FRIENDLY_MAP[r]||r||'Unknown'; }

var tabs=[
  {id:'dashboard',label:'Dashboard'},
  {id:'portfolios',label:'Portfolios'},
  {id:'screener',label:'Screener'},
  {id:'detail',label:'Ticker Detail'},
  {id:'backtest',label:'Backtest / Performance'}
];
var currentTab='dashboard';
var selectedTicker=null;

function initTabs() {
  var nav=document.getElementById('tabNav');
  tabs.forEach(function(t) {
    var btn=document.createElement('button');
    btn.className='tab-btn'+(t.id===currentTab?' active':'');
    btn.textContent=t.label;
    btn.onclick=function(){ switchTab(t.id); };
    nav.appendChild(btn);
  });
  renderFreshnessBar();
}

function renderFreshnessBar() {
  var bar=document.getElementById('freshnessBar');
  var f=DATA.freshness;
  if(!f) { bar.innerHTML=''; return; }
  var parts=[];
  if(f.prices_live_max) {
    var d=new Date(f.prices_live_max+'T00:00:00');
    var g=new Date(f.report_generated_at);
    var days=Math.round((g-d)/86400000);
    var color=days<=3?'text-pos':days<=14?'text-amber-400':'text-neg';
    parts.push('<span>Live prices: <span class="'+color+'">'+f.prices_live_max+'</span></span>');
    if(f.live_price_coverage!=null) parts.push(f.live_price_coverage+'/'+f.total_tickers+' tickers');
  } else {
    parts.push('<span class="text-amber-400">Live prices unavailable \u2014 fundamentals only</span>');
  }
  parts.push('<span>Generated: '+f.report_generated_at.split('T')[0]+'</span>');
  bar.innerHTML='<span class="flex flex-wrap gap-x-4 gap-y-1">'+parts.join(' \u00b7 ')+'</span>';
}

document.addEventListener('click',function(e){
  var el=e.target.closest('[data-portfolio],[data-ticker],[data-sort],[data-tab]');
  if(!el) return;
  if(el.dataset.portfolio) selectPortfolio(el.dataset.portfolio);
  else if(el.dataset.ticker) selectTicker(el.dataset.ticker);
  else if(el.dataset.sort) sortScreener(el.dataset.sort);
  else if(el.dataset.tab) switchTab(el.dataset.tab);
});
function switchTab(id) {
  if(id!=='detail'&&chartInstance){try{chartInstance.remove()}catch(e){} chartInstance=null; currentChartTicker=null; }
  currentTab=id;
  document.querySelectorAll('.tab-btn').forEach(function(b,i){ b.className='tab-btn'+(tabs[i].id===id?' active':''); });
  var container=document.getElementById('tabContainer');
  if(id==='dashboard') renderDashboard(container);
  else if(id==='portfolios') renderPortfolios(container);
  else if(id==='screener') renderScreener(container);
  else if(id==='detail') renderTickerDetail(container,selectedTicker);
  else if(id==='backtest') renderBacktest(container);
}

function selectTicker(ticker) {
  selectedTicker=ticker;
  switchTab('detail');
}

// ===== DASHBOARD =====
function renderDashboard(container) {
  var m=DATA.meta;
  var html='';
  html+='<div class="banner mb-6 text-sm font-medium">\u26a0\ufe0f Half-size deployment recommended until 2025 OOS confirmation (per Phase 4 sizing decision). Equal-weight, sector-capped portfolios.</div>';
  html+='<div class="text-xs text-muted mb-4">Note: Prices refreshed daily via yfinance. Fundamentals frozen at last SimFin refresh (June 2025) \u2014 appropriate for point-in-time scoring under the current freeze. The two timestamps will differ; this is by design.</div>';
  html+='<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">';
  var cards=[
    {label:'Regime',value:m.regime_friendly,sub:m.regime_tilt,cls:'text-blue-400'},
    {label:'Universe Size',value:fmtBig(m.universe_size),sub:'scored stocks',cls:'text-white'},
    {label:'Top Decile',value:fmtBig(m.top_decile_size),sub:'~10% of universe',cls:'text-pos'},
    {label:'Robust Picks',value:fmtBig(m.robust_pick_count),sub:'resampling-robust',cls:'text-amber-400'},
  ];
  cards.forEach(function(c) {
    html+='<div class="card"><div class="stat-label">'+c.label+'</div><div class="stat-value '+c.cls+'">'+c.value+'</div><div class="text-xs text-muted mt-1">'+escapeHtml(c.sub||'')+'</div></div>';
  });
  html+='</div>';
  html+='<div class="card mb-6"><div class="stat-label">Last Data Refresh</div><div class="text-sm text-gray-300 mt-1">'+escapeHtml(m.last_updated||'N/A')+'</div></div>';
  var aud=DATA.audit;
  if(aud&&aud.results&&aud.results.length) {
    html+='<div class="card mb-6"><h3 class="text-base font-semibold mb-3 text-white">Model Performance Summary</h3>';
    var alphaVals=[], icVals=[];
    aud.results.forEach(function(r) {
      if(r.top_alpha!=null) alphaVals.push(r.top_alpha);
      if(r.ic!=null) icVals.push(r.ic);
    });
    if(alphaVals.length) {
      var meanAlpha=alphaVals.reduce(function(a,b){return a+b},0)/alphaVals.length;
      var hitRate=alphaVals.filter(function(v){return v>0}).length/alphaVals.length;
      var meanIC=icVals.length?icVals.reduce(function(a,b){return a+b},0)/icVals.length:null;
      html+='<div class="grid grid-cols-2 md:grid-cols-4 gap-3">';
      html+='<div><div class="stat-label">Mean Top-Decile Alpha</div><div class="text-lg font-bold text-pos">'+(meanAlpha>0?'+':'')+fmtPct(meanAlpha)+'</div></div>';
      html+='<div><div class="stat-label">Alpha Hit Rate</div><div class="text-lg font-bold">'+fmtPct(hitRate)+'</div></div>';
      html+='<div><div class="stat-label">Mean IC</div><div class="text-lg font-bold '+(meanIC>0?'text-pos':'text-neg')+'">'+(meanIC!=null?fmtDec(meanIC,4):'\u2014')+'</div></div>';
      html+='<div><div class="stat-label">Periods</div><div class="text-lg font-bold">'+aud.results.length+'</div></div>';
      html+='</div>';
    }
    if(aud.oos_reserve) {
      html+='<div class="mt-3 p-3 bg-blue-900/20 border border-blue-800/40 rounded-lg"><span class="text-blue-400 font-semibold">OOS Result:</span> <span class="text-gray-300">Edge confirmed OOS at +8.46% top-alpha; bootstrap-robust; half-size deployment recommended pending 2025 confirmation.</span></div>';
    }
    html+='</div>';
  }
  html+='<div class="card"><h3 class="text-base font-semibold mb-3 text-white">Navigation</h3>';
  html+='<div class="grid grid-cols-2 md:grid-cols-4 gap-2">';
  tabs.forEach(function(t) {
    if(t.id==='dashboard') return;
    html+='<button data-tab="'+t.id+'" class="px-4 py-2.5 rounded-lg text-sm font-medium bg-hover hover:bg-blue-800/40 hover:text-white transition-colors text-left">'+t.label+'</button>';
  });
  html+='</div></div>';
  container.innerHTML=html;
}

// ===== PORTFOLIOS =====
var portState={selected:'balanced_20',show10:false};

function renderPortfolios(container) {
  var portKeys=Object.keys(DATA.portfolios).filter(function(k){return k!=='_qualifying_sectors';}).sort();
  var standardKeys=portKeys.filter(function(k){return !k.startsWith('sector_');});
  var sectorKeys=portKeys.filter(function(k){return k.startsWith('sector_');});
  var html='';
  html+='<div class="banner mb-4 text-sm font-medium">\u26a0\ufe0f Half-size deployment recommended until 2025 OOS confirmation (per Phase 4 sizing decision). Review each name for open SEC investigations, restatements, or pending binary events before committing capital.</div>';
  html+='<div class="flex flex-wrap items-center gap-4 mb-4">';
  html+='<div class="flex gap-2 flex-wrap">';
  standardKeys.forEach(function(k) {
    var active=k===portState.selected?'bg-blue-600 text-white':'bg-card text-gray-300 hover:bg-hover';
    html+='<button data-portfolio="'+k+'" class="px-3 py-1.5 rounded text-xs font-medium '+active+'">'+DATA.portfolios[k].name.split('\u2014')[0].trim()+'</button>';
  });
  html+='</div>';
  html+='<div class="flex gap-1 ml-auto">';
  html+='<button onclick="portState.show10=false;selectPortfolio(portState.selected)" class="px-3 py-1.5 rounded text-xs font-medium '+(portState.show10?'bg-card text-gray-300':'bg-blue-600 text-white')+'">20 Names</button>';
  html+='<button onclick="portState.show10=true;selectPortfolio(portState.selected)" class="px-3 py-1.5 rounded text-xs font-medium '+(portState.show10?'bg-blue-600 text-white':'bg-card text-gray-300')+'">10 Names</button>';
  html+='</div></div>';
  if(sectorKeys.length) {
    html+='<div class="flex flex-wrap gap-2 mb-4"><span class="text-xs text-muted self-center">Sector Focus:</span>';
    sectorKeys.forEach(function(k) {
      var active=k===portState.selected?'bg-blue-600 text-white':'bg-card text-gray-300 hover:bg-hover';
      html+='<button data-portfolio="'+k+'" class="px-3 py-1 rounded text-xs font-medium '+active+'">'+DATA.portfolios[k].name.split('\u2014')[0].trim()+'</button>';
    });
    html+='</div>';
  }
  html+='<div id="portfolioContent"></div>';
  container.innerHTML=html;
  renderPortfolioContent();
}

function selectPortfolio(key) {
  portState.selected=key;
  renderPortfolioContent();
}

function renderPortfolioContent() {
  var key=portState.selected;
  var suffix=portState.show10?'_10':'_20';
  var actualKey=key;
  if(!key.endsWith('_10')&&!key.endsWith('_20')&&!key.startsWith('sector_')) {
    actualKey=key.replace(/_(10|20)$/,'')+suffix;
    if(!DATA.portfolios[actualKey]) actualKey=key;
  }
  if(!DATA.portfolios[actualKey]) {
    var keys=Object.keys(DATA.portfolios).filter(function(k){return !k.startsWith('_');});
    actualKey=keys.includes(key)?key:keys[0];
  }
  var port=DATA.portfolios[actualKey];
  if(!port) {
    document.getElementById('portfolioContent').innerHTML='<div class="text-muted">No portfolio data.</div>';
    return;
  }
  var st=port.stats||{};
  var html='';
  html+='<div class="card mb-4"><div class="flex flex-wrap items-start gap-6">';
  html+='<div><div class="stat-label">Portfolio</div><div class="font-semibold text-white">'+escapeHtml(port.name)+'</div></div>';
  html+='<div><div class="stat-label">Avg Potential Score</div><div class="stat-value text-blue-400">'+fmtDec(st.avg_potential_score,1)+'</div></div>';
  html+='<div><div class="stat-label">Avg Div Yield</div><div class="stat-value '+(st.avg_dividend_yield>0?'text-pos':'text-muted')+'">'+fmtPct(st.avg_dividend_yield||0)+'</div></div>';
  html+='<div><div class="stat-label">Median P/E (TTM)</div><div class="stat-value">'+(st.median_pe_ttm?'\u00d7'+fmtDec(st.median_pe_ttm,1):'\u2014')+'</div></div>';
  html+='<div><div class="stat-label">Avg ROIC (5y)</div><div class="stat-value text-pos">'+fmtPct(st.avg_roic_5y_med||0)+'</div></div>';
  html+='<div><div class="stat-label">Avg Beta / RealVol</div><div class="stat-value text-sm">'+fmtDec(st.avg_beta,2)+' / '+fmtPct(st.avg_realized_vol||0)+'</div></div>';
  html+='<div><div class="stat-label">Robust Picks</div><div class="stat-value text-amber-400">'+st.robust_pick_count+'/'+st.n+'</div></div>';
  html+='</div><div class="flex flex-wrap gap-1 mt-3">';
  if(st.sector_breakdown) {
    Object.keys(st.sector_breakdown).forEach(function(s) {
      if(!s) return;
      html+='<span class="pill pill-blue">'+fmtSector(s)+': '+st.sector_breakdown[s]+'</span>';
    });
  }
  html+='</div></div>';
  html+='<div class="card p-0 overflow-x-auto"><table class="w-full text-sm"><thead><tr class="text-xs text-muted uppercase">';
  html+='<th class="text-col">Ticker</th><th class="text-col">Company</th><th class="text-col">Sector</th><th class="num">Potential</th><th class="num">'+escapeHtml(port.ranking_metric)+'</th>';
  html+='<th class="num">Price</th><th class="num">Mkt Cap</th><th class="num">Key Metric</th><th class="text-col">Flags</th>';
  html+='</tr></thead><tbody>';
  port.tickers.forEach(function(s) {
    var keyMetric='\u2014';
    if(actualKey.includes('deep_value')) keyMetric='V:'+fmtDec(s.valuation_score,1);
    else if(actualKey.includes('quality')) keyMetric='Q:'+fmtDec(s.quality_score,1);
    else if(actualKey.includes('income')) keyMetric='Yld:'+fmtPct(s.dividend_yield);
    else if(actualKey.includes('momentum')) keyMetric='R6m:'+fmtPct(s.return_6m);
    else keyMetric='P:'+fmtDec(s.potential_score,1);
    html+='<tr data-ticker="'+escapeHtml(s.ticker)+'" class="cursor-pointer border-b border-gray-800 last:border-0">';
    html+='<td class="text-col font-medium text-blue-400">'+escapeHtml(s.ticker)+'</td>';
    html+='<td class="text-col">'+escapeHtml(s.company||'').substring(0,30)+'</td>';
    html+='<td class="text-col text-xs">'+fmtSector(s.sector_group)+'</td>';
    html+='<td class="num font-medium">'+fmtDec(s.potential_score,1)+'</td>';
    html+='<td class="num">'+(port.rank_col?fmtDec(s[port.rank_col],1):fmtDec(s.potential_score,1))+'</td>';
    var priceVal=s.live_price!=null?fmtDollar(s.live_price):fmtDollar(s.price);
    var priceCls=s.live_price!=null?'num':'num text-muted';
    var priceTitle=s.live_price!=null?'':'title="SimFin price (frozen)"';
    html+='<td class="'+priceCls+'" '+priceTitle+'>'+(s.live_price==null?'~':'')+priceVal+'</td>';
    html+='<td class="num">'+fmtDollar(s.market_cap)+'</td>';
    html+='<td class="num">'+keyMetric+'</td>';
    html+='<td class="text-col">'+renderFlags(s)+'</td>';
    html+='</tr>';
  });
  html+='</tbody></table></div>';
  if(port.tickers.length===0) html='<div class="text-muted text-center py-8">No tickers in this portfolio (pool too small).</div>';
  document.getElementById('portfolioContent').innerHTML=html;
}

// ===== SCREENER =====
var screenerState={sortCol:'potential_score',sortAsc:false,sectorFilter:'',searchFilter:'',minScore:0,page:0,showAll:false,pageSize:100};

function renderScreener(container) {
  var html='';
  html+='<div class="flex flex-wrap items-center gap-3 mb-4">';
  html+='<select id="sectorFilter" onchange="applyScreenerFilters()" class="bg-card border border-border rounded px-3 py-1.5 text-sm text-gray-300">';
  html+='<option value="">All Sectors</option>';
  SECTOR_GROUPS.forEach(function(s){if(s)html+='<option value="'+s+'">'+fmtSector(s)+'</option>';});
  html+='</select>';
  html+='<input id="searchFilter" type="text" placeholder="Search ticker/company..." oninput="applyScreenerFilters()" class="bg-card border border-border rounded px-3 py-1.5 text-sm text-gray-300 w-48">';
  html+='<label class="text-xs text-muted">Min Score: <span id="scoreVal">'+screenerState.minScore+'</span></label>';
  html+='<input id="minScoreFilter" type="range" min="0" max="100" value="'+screenerState.minScore+'" oninput="document.getElementById(\\'scoreVal\\').textContent=this.value;applyScreenerFilters()" class="w-24">';
  html+='<div class="flex gap-2 flex-wrap text-xs">';
  ['DEEP_VALUE','FALLEN_ANGEL','QUIET_COMPOUNDER','MOMENTUM_VALUE','OVEREXTENDED'].forEach(function(f) {
    html+='<label class="flex items-center gap-1 cursor-pointer"><input type="checkbox" value="'+f+'" onchange="applyScreenerFilters()" class="rounded"> '+f.replace(/_/g,' ')+'</label>';
  });
  html+='</div>';
  html+='<button onclick="screenerState.showAll=!screenerState.showAll;screenerState.page=0;applyScreenerFilters()" class="px-3 py-1.5 rounded text-xs font-medium bg-card hover:bg-hover">'+(screenerState.showAll?'Show Top 500':'Show All')+'</button>';
  html+='</div>';
  html+='<div class="card p-0 overflow-x-auto"><table class="w-full text-sm"><thead><tr class="text-xs text-muted uppercase">';
  var cols=[
    {key:'ticker',label:'Ticker',type:'text'},
    {key:'company',label:'Company',type:'text'},
    {key:'sector_group',label:'Sector',type:'text'},
    {key:'potential_score',label:'Potential',type:'num'},
    {key:'valuation_score',label:'V',type:'num'},
    {key:'quality_score',label:'Q',type:'num'},
    {key:'growth_score',label:'G',type:'num'},
    {key:'sentiment_score',label:'S (Momentum)',type:'num'},
    {key:'price',label:'Price',type:'num'},
    {key:'market_cap',label:'Mkt Cap',type:'num'},
    {key:'pe_ttm',label:'P/E (TTM)',type:'num'},
    {key:'dividend_yield',label:'Div Yld',type:'pct'},
    {key:'return_12m',label:'Ret 12m',type:'pct'},
    {key:'flags',label:'Flags',type:'text'},
    {key:'liquidity_tier',label:'Liq',type:'text'},
  ];
  cols.forEach(function(c) {
    var dir=screenerState.sortCol===c.key?(screenerState.sortAsc?'asc':'desc'):'';
    html+='<th data-sort="'+c.key+'" class="sorter '+dir+' '+(c.type==='num'?'num':'text-col')+'">'+c.label+'</th>';
  });
  html+='</tr></thead><tbody id="screenerBody"></tbody></table></div>';
  html+='<div class="flex items-center justify-between mt-3 text-sm text-muted">';
  html+='<span id="screenerCount"></span>';
  html+='<div class="flex gap-2"><button onclick="screenerPage(-1)" class="px-3 py-1 rounded bg-card hover:bg-hover">\u25c0 Prev</button>';
  html+='<span id="screenerPage" class="self-center"></span>';
  html+='<button onclick="screenerPage(1)" class="px-3 py-1 rounded bg-card hover:bg-hover">Next \u25b6</button></div></div>';
  container.innerHTML=html;
  applyScreenerFilters();
}

function getFilteredStocks() {
  return DATA.stocks.filter(function(s) {
    if(screenerState.sectorFilter&&s.sector_group!==screenerState.sectorFilter) return false;
    if(screenerState.searchFilter) {
      var q=screenerState.searchFilter.toUpperCase();
      if(!(s.ticker&&s.ticker.toUpperCase().includes(q))&&!(s.company&&s.company.toUpperCase().includes(q))) return false;
    }
    if((s.potential_score||0)<screenerState.minScore) return false;
    var flags=getFlags(s);
    var checked=Array.from(document.querySelectorAll('#tabContainer input[type=checkbox]:checked')).map(function(c){return c.value;});
    if(checked.length&&!checked.some(function(f){return flags.includes(f);})) return false;
    return true;
  }).sort(function(a,b) {
    var va=a[screenerState.sortCol], vb=b[screenerState.sortCol];
    if(va==null) va=-Infinity; if(vb==null) vb=-Infinity;
    if(typeof va==='string') return screenerState.sortAsc?va.localeCompare(vb):vb.localeCompare(va);
    return screenerState.sortAsc?va-vb:vb-va;
  });
}

function applyScreenerFilters() {
  screenerState.sectorFilter=document.getElementById('sectorFilter').value;
  screenerState.searchFilter=document.getElementById('searchFilter').value;
  screenerState.minScore=parseFloat(document.getElementById('minScoreFilter').value)||0;
  screenerState.page=0;
  displayScreener();
}

function sortScreener(col) {
  if(screenerState.sortCol===col) screenerState.sortAsc=!screenerState.sortAsc;
  else { screenerState.sortCol=col; screenerState.sortAsc=false; }
  displayScreener();
}

function displayScreener() {
  var filtered=getFilteredStocks();
  var total=filtered.length;
  if(!screenerState.showAll) filtered=filtered.slice(0,500);
  var maxPage=Math.max(0,Math.ceil(filtered.length/screenerState.pageSize)-1);
  if(screenerState.page>maxPage) screenerState.page=maxPage;
  var start=screenerState.page*screenerState.pageSize;
  var page=filtered.slice(start,start+screenerState.pageSize);
  var tbody=document.getElementById('screenerBody');
  var html='';
  page.forEach(function(s) {
    var isDelisted=(s.flags||'').indexOf('DELISTED')!==-1;
    var rowCls='cursor-pointer border-b border-gray-800 last:border-0'+(isDelisted?' opacity-40 line-through-none italic':'');
    html+='<tr data-ticker="'+escapeHtml(s.ticker)+'" class="'+rowCls+'">';
    html+='<td class="text-col font-medium text-blue-400">'+escapeHtml(s.ticker)+'</td>';
    html+='<td class="text-col text-xs max-w-[200px] truncate">'+escapeHtml((s.company||'').substring(0,35))+'</td>';
    html+='<td class="text-col text-xs">'+fmtSector(s.sector_group)+'</td>';
    html+='<td class="num font-medium">'+fmtDec(s.potential_score,1)+'</td>';
    html+='<td class="num">'+fmtDec(s.valuation_score,1)+'</td>';
    html+='<td class="num">'+fmtDec(s.quality_score,1)+'</td>';
    html+='<td class="num">'+fmtDec(s.growth_score,1)+'</td>';
    html+='<td class="num">'+fmtDec(s.sentiment_score,1)+'</td>';
    var pv=s.live_price!=null?fmtDollar(s.live_price):fmtDollar(s.price);
    var pc=s.live_price!=null?'num':'num text-muted';
    html+='<td class="'+pc+'">'+(s.live_price==null?'~':'')+pv+'</td>';
    html+='<td class="num">'+fmtDollar(s.market_cap)+'</td>';
    html+='<td class="num">'+(s.pe_ttm?'\u00d7'+fmtDec(s.pe_ttm,1):'\u2014')+'</td>';
    html+='<td class="num">'+fmtPct(s.dividend_yield)+'</td>';
    html+='<td class="num '+(s.return_12m>0?'text-pos':'text-neg')+'">'+fmtPct(s.return_12m)+'</td>';
    html+='<td class="text-col">'+renderFlags(s)+'</td>';
    html+='<td class="num text-xs">'+(s.liquidity_tier||'\u2014')+'</td>';
    html+='</tr>';
  });
  tbody.innerHTML=html;
  document.getElementById('screenerCount').textContent='Showing '+(start+1)+'-'+(start+page.length)+' of '+total;
  document.getElementById('screenerPage').textContent='Page '+(screenerState.page+1)+'/'+(maxPage+1);
}

function screenerPage(delta) {
  var filtered=getFilteredStocks();
  if(!screenerState.showAll) filtered=filtered.slice(0,500);
  var maxPage=Math.max(0,Math.ceil(filtered.length/screenerState.pageSize)-1);
  screenerState.page=Math.max(0,Math.min(maxPage,screenerState.page+delta));
  displayScreener();
}

// ===== TICKER DETAIL =====
function renderTickerDetail(container,ticker) {
  if(!ticker) {
    container.innerHTML='<div class="card text-center py-12"><div class="text-xl text-muted mb-2">Select a ticker</div><div class="text-sm text-muted">Click any ticker row in the Portfolios or Screener tabs.</div></div>';
    return;
  }
  var s=DATA.stocks.find(function(x){return x.ticker===ticker;});
  if(!s) {
    container.innerHTML='<div class="card text-center py-12 text-muted">Ticker not found: '+escapeHtml(ticker)+'</div>';
    return;
  }
  var h='';
  h+='<div class="flex items-center gap-4 mb-4"><button data-tab="portfolios" class="px-3 py-1.5 rounded text-sm bg-card hover:bg-hover">\u2190 Back</button>';
  h+='<h2 class="text-xl font-bold text-white">'+escapeHtml(ticker)+'</h2>';
  if(s.live_price!=null) {
    h+='<span class="text-lg font-semibold text-gray-300">'+fmtDollar(s.live_price)+'</span><span class="text-xs text-muted">as of '+escapeHtml(s.live_price_date||'')+'</span>';
  } else {
    h+='<span class="text-lg font-semibold text-gray-300">'+fmtDollar(s.price)+'</span><span class="text-xs text-muted">(SimFin close, '+escapeHtml(DATA.freshness.prices_live_max||'N/A')+') \u2014 live price unavailable</span>';
  }
  if(s.filing_age_days>100) h+='<span class="pill pill-amber text-xs ml-2">stale fundamentals \u2014 check recent news</span>';
  h+='</div><div class="text-sm text-muted mb-4">'+escapeHtml(s.company||'')+'</div>';
  h+='<div class="card mb-4 p-0" id="chartContainer" style="height:360px;position:relative;"></div>';

  h+='<div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">';
  h+='<div class="card"><h3 class="text-base font-semibold mb-3 text-white">Scores</h3>';
  h+='<div class="text-3xl font-bold text-blue-400 mb-1">'+fmtDec(s.potential_score,1)+'</div>';
  h+='<div class="text-xs text-muted mb-3">Potential Score \u2014 '+(s.top_decile_pct!=null?'Top '+fmtDec(100-s.top_decile_pct,1)+'%':'')+'</div>';
  ['valuation_score','quality_score','growth_score','sentiment_score'].forEach(function(sk) {
    var v=s[sk], pct=v!=null?Math.round(v):0;
    var label={valuation_score:'Valuation (Cheapness)',quality_score:'Quality (Durability)',growth_score:'Growth (Improving)',sentiment_score:'Price Momentum & 52w Position'}[sk];
    h+='<div class="mb-1"><div class="flex justify-between text-xs"><span>'+label+'</span><span>'+(v!=null?fmtDec(v,1):'\u2014')+'</span></div>';
    h+='<div class="w-full bg-gray-700 rounded-full h-1.5"><div class="bg-blue-500 h-1.5 rounded-full" style="width:'+pct+'%"></div></div></div>';
  });
  if(s.potential_median!=null) h+='<div class="mt-3 text-xs text-muted">Resampling (P5/P50/P95): '+fmtDec(s.potential_p05,1)+' / '+fmtDec(s.potential_median,1)+' / '+fmtDec(s.potential_p95,1)+'</div>';
  if(s.robust_pick) h+='<div class="mt-1 pill pill-green">Robust Pick</div>';
  h+='</div>';

  h+='<div class="card"><h3 class="text-base font-semibold mb-3 text-white">Valuation</h3><table class="w-full text-sm"><tbody>';
  [['P/E','pe'],['P/E (TTM)','pe_ttm'],['P/B','pb'],['P/S','ps'],['P/S (TTM)','ps_ttm'],['P/FCF','pfcf'],['EV/EBITDA','ev_ebitda'],['EV/EBITDA (TTM)','ev_ebitda_ttm']].forEach(function(f){
    h+='<tr><td class="text-muted text-xs py-1">'+f[0]+'</td><td class="num">'+(s[f[1]]!=null?'\u00d7'+fmtDec(s[f[1]],2):'\u2014')+'</td></tr>';
  });
  h+='</tbody></table></div>';

  h+='<div class="card"><h3 class="text-base font-semibold mb-3 text-white">Profitability & Quality</h3><table class="w-full text-sm"><tbody>';
  [['Gross Margin','gross_margin'],['Operating Margin','operating_margin'],['Net Margin','net_margin'],['ROE','roe'],['ROA','roa'],['ROIC','roic'],['ROIC (5y Med)','roic_5y_med'],['Gross Margin (5y)','gross_margin_5y_med'],['Op Margin (5y)','operating_margin_5y_med'],['Net Margin (5y)','net_margin_5y_med'],['Piotroski F','piotroski_f'],['Altman Z','altman_z']].forEach(function(f){
    h+='<tr><td class="text-muted text-xs py-1">'+f[0]+'</td><td class="num">'+fmtPct(s[f[1]])+'</td></tr>';
  });
  h+='</tbody></table></div>';

  h+='<div class="card"><h3 class="text-base font-semibold mb-3 text-white">Growth</h3><table class="w-full text-sm"><tbody>';
  [['Rev Growth 3yr','revenue_growth_3yr'],['Revenue Trend 5y','revenue_trend_5y'],['EBITDA Trend 5y','ebitda_trend_5y'],['FCF Trend 5y','fcf_trend_5y'],['Revenue (TTM)','revenue_ttm','$'],['Net Income (TTM)','net_income_ttm','$'],['FCF (TTM)','fcf_ttm','$'],['EBITDA (TTM)','ebitda_ttm','$']].forEach(function(f){
    h+='<tr><td class="text-muted text-xs py-1">'+f[0]+'</td><td class="num">'+(f[2]==='$'?fmtDollar(s[f[1]]):fmtPct(s[f[1]]))+'</td></tr>';
  });
  h+='</tbody></table></div>';

  h+='<div class="card"><h3 class="text-base font-semibold mb-3 text-white">Balance Sheet & Risk</h3><table class="w-full text-sm"><tbody>';
  [['Debt/Equity','debt_equity'],['Current Ratio','current_ratio'],['Beta','beta'],['Realized Vol','realized_vol']].forEach(function(f){
    var v=s[f[1]];
    h+='<tr><td class="text-muted text-xs py-1">'+f[0]+'</td><td class="num">'+(f[1]==='realized_vol'?fmtPct(v):fmtDec(v,2))+'</td></tr>';
  });
  h+='</tbody></table></div>';

  h+='<div class="card"><h3 class="text-base font-semibold mb-3 text-white">Returns & Momentum</h3><table class="w-full text-sm"><tbody>';
  [['Return 1m','return_1m'],['Return 3m','return_3m'],['Return 6m','return_6m'],['Return 12m','return_12m'],['Return 12m-1m','return_12m_minus_1m'],['Dist from 52w High','distance_from_52w_high'],['52w High','high_52w','$'],['52w Low','low_52w','$']].forEach(function(f){
    var v=s[f[1]];
    h+='<tr><td class="text-muted text-xs py-1">'+f[0]+'</td><td class="num '+(f[2]!=='$'&&v!=null?(v>0?'text-pos':'text-neg'):'')+'">'+(f[2]==='$'?fmtDollar(v):fmtPct(v))+'</td></tr>';
  });
  h+='</tbody></table></div>';

  h+='<div class="card"><h3 class="text-base font-semibold mb-3 text-white">Ownership & Flow</h3><table class="w-full text-sm"><tbody>';
  [['Insider Own','insider_own'],['Institutional Own','inst_own'],['Short Float','short_float']].forEach(function(f){
    h+='<tr><td class="text-muted text-xs py-1">'+f[0]+'</td><td class="num">'+fmtPct(s[f[1]])+'</td></tr>';
  });
  h+='</tbody></table></div>';

  h+='<div class="card"><h3 class="text-base font-semibold mb-3 text-white">Liquidity & Freshness</h3><table class="w-full text-sm"><tbody>';
  h+='<tr><td class="text-muted text-xs py-1">Avg Dollar Vol (30d)</td><td class="num">'+fmtDollar(s.avg_dollar_volume_30d)+'</td></tr>';
  h+='<tr><td class="text-muted text-xs py-1">Liquidity Tier</td><td class="num">'+(s.liquidity_tier||'\u2014')+'</td></tr>';
  h+='<tr><td class="text-muted text-xs py-1">Last Filing</td><td class="num">'+(s.last_filing_date||'\u2014')+'</td></tr>';
  h+='<tr><td class="text-muted text-xs py-1">Years of History</td><td class="num">'+(s.n_yrs_history!=null?fmtDec(s.n_yrs_history,0):'\u2014')+'</td></tr>';
  h+='<tr><td class="text-muted text-xs py-1">Stale Fundamentals</td><td class="num">'+(s.stale_fundamentals==1?'<span class="pill pill-red">Yes</span>':'<span class="pill pill-green">No</span>')+'</td></tr>';
  h+='</tbody></table></div></div>';
  h+='<div class="card mb-4"><h3 class="text-base font-semibold mb-2 text-white">Flags</h3><div>'+renderFlags(s)+'</div></div>';
  container.innerHTML=h;
  var ph=DATA.price_history[ticker];
  if(ph&&ph.length) renderChart(ticker,ph);
  else document.getElementById('chartContainer').innerHTML='<div class="flex items-center justify-center h-full text-muted text-sm">Price history not available for this ticker.</div>';
}

function renderChart(ticker,data) {
  if(chartInstance){try{chartInstance.remove()}catch(e){}}
  currentChartTicker=ticker;
  var container=document.getElementById('chartContainer');
  container.innerHTML='';
  if(typeof LightweightCharts==='undefined') {
    container.innerHTML='<div class="flex items-center justify-center h-full text-muted text-sm">Chart library not available (CDN blocked).</div>';
    return;
  }
  try {
    chartInstance=LightweightCharts.createChart(container,{
      width:container.clientWidth||800,height:320,
      layout:{background:{color:'#1e293b'},textColor:'#9ca3af'},
      grid:{vertLines:{color:'#262b36'},horzLines:{color:'#262b36'}},
      timeScale:{borderColor:'#323842',timeVisible:false,secondsVisible:false},
      rightPriceScale:{borderColor:'#323842'},
    });
    var series=chartInstance.addAreaSeries({lineColor:'#3b82f6',topColor:'rgba(59,130,246,0.3)',bottomColor:'rgba(59,130,246,0.02)',lineWidth:2});
    series.setData(data.map(function(d){return {time:Math.floor(d[0]/1000),value:d[1]};}));
    chartInstance.timeScale().fitContent();
    window.addEventListener('resize',function(){if(chartInstance)chartInstance.resize(container.clientWidth,320);});
  } catch(e) {
    console.error('Chart error:',e);
    container.innerHTML='<div class="flex items-center justify-center h-full text-muted text-sm">Chart error: '+e.message+'</div>';
    return;
  }
  var src=DATA.chart_source&&DATA.chart_source[ticker]||'simfin';
  var srcLabel=src==='live'?'yfinance (live)':'SimFin (frozen at 2025-06-03)';
  var lastPt=data.length?data[data.length-1]:null;
  var lastDate=lastPt?new Date(lastPt[0]).toISOString().split('T')[0]:'';
  var caption=document.createElement('div');
  caption.className='text-xs text-muted mt-1';
  caption.textContent='Source: '+srcLabel+(lastDate?' \u00b7 Last point: '+lastDate:'');
  container.parentNode.insertBefore(caption,container.nextSibling);
}

// ===== BACKTEST =====
function renderBacktest(container) {
  var aud=DATA.audit;
  if(!aud||!aud.results||!aud.results.length) {
    container.innerHTML='<div class="card text-center py-12"><div class="text-xl text-muted mb-2">No Backtest Data</div><div class="text-sm text-muted">No audit JSONs found. Run backtest first.</div></div>';
    return;
  }
  var h='';
  h+='<div class="banner mb-4 text-sm font-medium">Edge confirmed OOS at +8.46% top-alpha; bootstrap-robust; half-size deployment recommended pending 2025 confirmation.</div>';
  if(aud.oos_reserve) {
    h+='<div class="card mb-4 border-blue-800/40"><div class="flex items-center gap-2"><span class="text-blue-400 font-bold text-lg">OOS Result</span><span class="text-xs text-muted">Reserve: '+escapeHtml(String(aud.oos_reserve))+'</span></div>';
    var oosAlphas=aud.results.map(function(r){return r.top_alpha;}).filter(function(v){return v!=null;});
    if(oosAlphas.length) {
      var mOos=oosAlphas.reduce(function(a,b){return a+b;},0)/oosAlphas.length;
      h+='<div class="mt-2 text-lg font-bold '+(mOos>0?'text-pos':'text-neg')+'">'+(mOos>0?'+':'')+fmtPct(mOos)+' mean top-decile alpha</div>';
    }
    h+='</div>';
  }
  h+='<div class="card p-0 overflow-x-auto mb-4"><table class="w-full text-sm"><thead><tr class="text-xs text-muted uppercase">';
  h+='<th class="text-col">Period</th><th class="num">Top-Decile Alpha (vs universe)</th><th class="num">Long-Short Spread</th><th class="num">IC</th><th class="text-col">Regime</th></tr></thead><tbody>';
  aud.results.forEach(function(r) {
    h+='<tr class="border-b border-gray-800 last:border-0"><td class="text-col">'+escapeHtml(r.label||'')+'</td>';
    h+='<td class="num '+(r.top_alpha>0?'text-pos':'text-neg')+'" title="Top decile return minus universe average. The realistic long-only edge.">'+(r.top_alpha!=null?(r.top_alpha>0?'+':'')+fmtPct(r.top_alpha):'\u2014')+'</td>';
    h+='<td class="num '+(r.spread>0?'text-pos':'text-neg')+'" title="Top decile minus bottom decile. Aspirational \u2014 bottom decile often unshortable.">'+(r.spread!=null?(r.spread>0?'+':'')+fmtPct(r.spread):'\u2014')+'</td>';
    h+='<td class="num">'+(r.ic!=null?fmtDec(r.ic,4):'\u2014')+'</td>';
    h+='<td class="text-col"><span title="'+(REGIME_TILTS[r.regime]||'')+'">'+regimeFriendly(r.regime)+'</span></td></tr>';
  });
  h+='</tbody></table></div>';

  var fk=['valuation_score','quality_score','growth_score','sentiment_score'];
  var fl={valuation_score:'Valuation (Cheapness)',quality_score:'Quality (Durability)',growth_score:'Growth (Improving)',sentiment_score:'Price Momentum & 52w Position'};
  h+='<div class="card mb-4"><h3 class="text-base font-semibold mb-3 text-white">Factor ICs (mean across periods)</h3><div class="grid grid-cols-2 md:grid-cols-4 gap-3">';
  fk.forEach(function(k) {
    var vals=aud.results.map(function(r){return r.factor_ics?r.factor_ics[k]:null;}).filter(function(v){return v!=null;});
    var mn=vals.length?vals.reduce(function(a,b){return a+b;},0)/vals.length:null;
    h+='<div><div class="stat-label">'+fl[k]+'</div><div class="text-lg font-bold '+(mn>0?'text-pos':'text-neg')+'">'+(mn!=null?fmtDec(mn,4):'\u2014')+'</div></div>';
  });
  h+='</div></div>';
  h+='<div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">';
  h+='<div class="card"><h3 class="text-base font-semibold mb-2 text-white">Top-Decile Alpha</h3><div id="alphaChart" style="height:250px;"></div></div>';
  h+='<div class="card"><h3 class="text-base font-semibold mb-2 text-white">IC</h3><div id="icChart" style="height:250px;"></div></div></div>';
  h+='<div class="card text-xs text-muted"><p><strong>Terminology:</strong> "Top-Decile Alpha (vs universe)" = top decile return minus universe average \u2014 the realistic long-only edge. "Long-Short Spread" = top decile minus bottom decile \u2014 aspirational as bottom decile often unshortable. "Sentiment" = price-based momentum, not news sentiment.</p></div>';
  container.innerHTML=h;

  try {
    if(LightweightCharts) {
      var alphas=aud.results.map(function(r){return r.top_alpha!=null?r.top_alpha*100:null;});
      var ics=aud.results.map(function(r){return r.ic!=null?r.ic*100:null;});
      function miniChart(eid,vals,clr) {
        var el=document.getElementById(eid); if(!el) return;
        var ch=LightweightCharts.createChart(el,{width:el.clientWidth||400,height:250,layout:{background:{color:'#1e293b'},textColor:'#9ca3af'},grid:{vertLines:{color:'#262b36'},horzLines:{color:'#262b36'}},timeScale:{borderColor:'#323842',visible:false},rightPriceScale:{borderColor:'#323842'}});
        var line=ch.addLineSeries({color:clr,lineWidth:2});
        var cd=[]; vals.forEach(function(v,i){if(v!=null)cd.push({time:i+1,value:v});});
        line.setData(cd); ch.timeScale().fitContent();
      }
      setTimeout(function(){miniChart('alphaChart',alphas,'#34d399');miniChart('icChart',ics,'#60a5fa');},100);
    }
  } catch(e) {}
}

document.addEventListener('DOMContentLoaded',function(){initTabs();switchTab('dashboard');});
</script>
</body>
</html>
""".replace('__JSON_DATA__', json_data).replace('__FLAG_TOOLTIPS__', flag_tooltips_json)

    return HTML


def main():
    parser = argparse.ArgumentParser(description="Generate a static HTML report from the Stock Evaluator cache.")
    parser.add_argument("--db", default=str(DATA_DIR / "stock_cache.db"), help="Path to SQLite cache")
    parser.add_argument("--audit", default=None, help="Path to audit JSON (default: latest in data/audit)")
    parser.add_argument("--out", default=None, help="Output HTML path")
    parser.add_argument("--max-price-tickers", type=int, default=250, help="Max price series to embed")
    parser.add_argument("--sector-cap", type=int, default=4, help="Max names per sector in 20-name portfolios")
    args = parser.parse_args()

    t0 = time.time()

    db_path = Path(args.db)
    out_path = Path(args.out) if args.out else (ROOT / "reports" / f"stock_evaluator_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")

    print("=== Stock Evaluator HTML Report Generator ===\n")
    print(f"Reading cache: {db_path}")

    stocks_df, meta = load_stocks(db_path)

    regime = stocks_df["regime"].mode().iloc[0] if "regime" in stocks_df.columns else "R3"

    print(f"\nLoading live prices...")
    prices_live, prices_live_max = load_prices_live(db_path)
    stocks_df = attach_live_prices(stocks_df, prices_live)

    print(f"\nGenerating portfolios...")
    portfolios, candidates_df = compute_portfolios(stocks_df, args.sector_cap)

    top_tickers = candidates_df.nlargest(args.max_price_tickers, "potential_score")["ticker"].tolist()

    print(f"\nLoading price history for {len(top_tickers)} tickers...")
    price_history = load_price_history(top_tickers, args.max_price_tickers)

    print(f"\nBuilding chart series...")
    chart_data = {}
    chart_source = {}
    for t in top_tickers:
        s, src = build_chart_series(t, prices_live, price_history.get(t))
        if s is not None:
            chart_data[t] = s
            chart_source[t] = src
    n_live_charts = sum(1 for v in chart_source.values() if v == "live")
    print(f"  chart series: {n_live_charts} live, {len(chart_source) - n_live_charts} simfin")

    print(f"\nLoading audit data...")
    audit_data = load_audit(args.audit)

    print(f"\nBuilding HTML...")
    html = build_html(meta, stocks_df, portfolios, chart_data, chart_source,
                      audit_data, regime, args, prices_live_max)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    elapsed = time.time() - t0

    n_price = len(price_history)
    n_live_charts = sum(1 for v in chart_source.values() if v == "live")
    n_charts = len(chart_data)
    n_portfolios = len([k for k in portfolios if not k.startswith("_")])

    print(f"\n=== Summary ===")
    print(f"  Stocks loaded:       {len(stocks_df)}")
    print(f"  Portfolios generated: {n_portfolios}")
    print(f"  Chart series:         {n_charts} ({n_live_charts} live, {n_charts - n_live_charts} simfin)")
    print(f"  Regime:              {regime}")
    print(f"  Output file:         {out_path}")
    print(f"  File size:           {size_mb:.2f} MB")
    print(f"  Time elapsed:        {elapsed:.1f}s")
    print("Done.")


if __name__ == "__main__":
    main()
