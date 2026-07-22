#!/usr/bin/env python3
"""Screens all US stocks, computes sector-percentile-ranked 1-100 potential_score
across four sub-scores (Valuation, Quality, Growth, Sentiment), tags mispricing
patterns via flags, exposes interactive query REPL with why/compare commands.

Sentiment design: sentiment_score relies SOLELY on price-based signals
(distance_from_52w_high, return_12m_minus_1m). The SENTIMENT_WEIGHTS dict
retains entries for insider_own and short_float so that the opt-in
USE_FMP_OWNERSHIP=1 adapter (see scripts/refresh_ownership_fmp.py + the
ownership_live table) can populate them; when that adapter is off (default),
those fields are NaN universe-wide and _weighted_avg renormalizes to the
price pair. The Finviz scraper that formerly populated ownership fields has
been removed — validated 2025 OOS ran with an empty ownership map, so the
frozen scoring model is unchanged."""

import simfin as sf
from simfin.names import *
import pandas as pd
import numpy as np
import sqlite3, os, sys, re, csv, json, ssl
from datetime import datetime, timedelta, date
from urllib.request import urlopen
from pathlib import Path
import itertools

import warnings
warnings.filterwarnings('ignore', category=RuntimeWarning)

SIMFIN_API_KEY = os.environ.get('SIMFIN_API_KEY')
if not SIMFIN_API_KEY:
    sys.stderr.write('ERROR: SIMFIN_API_KEY environment variable not set.\n')
    sys.exit(1)
sf.set_api_key(SIMFIN_API_KEY)

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / 'data'
CACHE_DB = DATA_DIR / 'stock_cache.db'
SIMFIN_DIR = DATA_DIR / 'simfin'
# FMP listing oracle path.  Refreshed by scripts/fetch_fmp_listing_status.py;
# consumed read-only inside compute_liveness_and_flag to override the
# filing-age heuristic.  Absence of the file makes the gate degrade
# gracefully to heuristic-only (with a stderr warning).
FMP_LISTING_STATUS_PATH = DATA_DIR / 'fmp' / 'listing_status.json'
# A FMP quote timestamp older than this is treated as evidence of delisting
# (frozen quote); anything newer is treated as evidence of active trading
# even if this environment's yfinance cannot pull the ticker.
FMP_QUOTE_ACTIVE_MAX_AGE_DAYS = 180
sf.set_data_dir(str(SIMFIN_DIR))
sys.path.insert(0, str(BASE))

# ----------------------------------------------------------------------------
# Unified config (Framework Going Forward §5.2/5.3/5.4): merged from former
# market_screener.v2.py. Provides sector-specific metric maps, probabilistic
# regime classifier inputs, GICS lookup tables, and metric name aliases.
# ----------------------------------------------------------------------------
# Self-alias: the merged v2 scorer block (further below) uses
# getattr(ms, 'DECAY_ENABLED', ...) style lookups inherited from when it was an
# external module. After unification those calls resolve against this module.
ms = sys.modules[__name__]

import config as cfg
SCONFIG = cfg.load_config()
SECTOR_METRICS = cfg.load_sector_metrics()
REGIME_CFG = cfg.load_regime_config()
GICS_MAP = cfg.build_simfin_to_gics()
GICS_LOOKUP = cfg.build_gics_lookup()

SENTIMENT_DEFAULT_WEIGHT = SCONFIG.get('scoring', {}).get('sentiment_default_weight', 0.15)
RANK_GROUP_MIN = SCONFIG.get('scoring', {}).get('rank_group_min', 8)
MIN_SECTOR_POP = SCONFIG.get('scoring', {}).get('min_sector_population', 30)
FUNDA_LAG_DAYS = SCONFIG.get('data_lag', {}).get('fundamentals_min_days', 45)
FUNDA_MAX_DAYS = SCONFIG.get('data_lag', {}).get('fundamentals_max_days', 90)

METRIC_NAME_MAP = {
    'p_pe_trailing': 'earnings_yield',
    'p_pe_forward': 'earnings_yield',
    'p_book': 'book_yield',
    'ev_ebitda': 'ebitda_yield',
    'fcf_yield': 'fcf_yield',
    'ev_sales': 'ev_sales_yield',
    'p_tbv': 'ptbv_yield',
    'p_ffo': 'p_ffo_yield',
    'p_affo': 'p_ffo_yield',
    'premium_to_nav': 'premium_to_nav',
    'dividend_yield_inv': 'dividend_yield_inv',
    'price_mom_12_1': 'return_12m_minus_1m',
}

MIN_MARKET_CAP = 300_000_000
MIN_DOLLAR_VOLUME = 1_000_000  # $1M median daily dollar volume (liquidity floor)
BETA_MIN_DAYS = 60
EQUITY_RISK_PREMIUM = 0.05
CORP_TAX_RATE = 0.21
# Bump this whenever the schema or persisted column set changes. load_cache
# treats any mismatch as stale and forces a rebuild.
CACHE_SCHEMA_VERSION = 14  # v14: add dq_share_xcheck_failed flag (drives scorer mkt/EV mask)

# ============================================================================
# Phase 3: Potential score weights and configuration.
# ============================================================================
# POTENTIAL = w_v*VALUATION + w_q*QUALITY + w_g*GROWTH + w_s*SENTIMENT.
# Tune these to shift the model's bias. Weights must sum to 1.0.
POTENTIAL_WEIGHTS = {
    'valuation': 0.40,  # cheapness (most weight — anchors fair value)
    'quality':   0.40,  # business goodness
    'growth':    0.20,  # improving fundamentals
    'sentiment': 0.0,  # market mispricing signal
}
assert abs(sum(POTENTIAL_WEIGHTS.values()) - 1.0) < 1e-9, 'POTENTIAL_WEIGHTS must sum to 1.0'

# CONTRARIAN_MODE = True means recent underperformance scores HIGHER on sentiment
# (the value/mean-reversion thesis). Flip to False for momentum bias.
CONTRARIAN_MODE = False

# ============================================================================
# Quantum Approach §Stage 1.1 — Resampled scoring ("superposition as Bayesian
# uncertainty"). Composite score is a point estimate of a noisy quantity; the
# weights themselves are uncertain. Resample weights uniformly within their
# published bands, recompute the composite, then report median and IQR.
# Stocks whose IQR straddles the buy threshold are "in superposition" — robust
# names rank in the top decile across >= ROBUST_TOP_DECILE_THRESHOLD of draws.
# Bands per Michaud-style resampled efficiency (V=35-40, Q=30-40, G=15-30,
# S=0-20). Reference: Michaud, Efficient Asset Management (OUP, 2008);
# Avramov, Journal of Finance, 2023.
# ============================================================================
WEIGHT_BANDS = {
    'valuation': (0.35, 0.40),
    'quality':   (0.30, 0.40),
    'growth':    (0.15, 0.30),
    'sentiment': (0.00, 0.20),
}
N_RESAMPLES = int(os.environ.get('N_RESAMPLES', '1000'))
ROBUST_TOP_DECILE_THRESHOLD = 0.90  # frac of draws that must keep a stock in top decile
RESAMPLE_SEED = 42  # deterministic for reproducibility; set RESAMPLE_SEED=0 for random

# ============================================================================
# Quantum Approach §Stage 1.3 — Square-root market-impact haircut
# ("observer effect"). Trading moves the price; impact scales as sqrt(Q/ADV).
# Estimated cost subtracted from raw potential before final rank, penalizing
# thinly-traded names where a size-adjusted signal would otherwise be inflated.
# Reference: Tóth/Bouchaud et al., Phys. Rev. X (2011); Almgren et al. (2005).
# IMPACT_Y ≈ 1 for US large caps from CFM-style calibrations. Q sized to
# TARGET_POSITION_USD (default $1M = small retail). Output in basis points.
# Disable with IMPACT_ENABLED=0.
# ============================================================================
IMPACT_ENABLED = os.environ.get('IMPACT_ENABLED', '1').lower() in ('1', 'true', 'yes')
IMPACT_Y = float(os.environ.get('IMPACT_Y', '1.0'))
TARGET_POSITION_USD = float(os.environ.get('TARGET_POSITION_USD', '1_000_000'))
IMPACT_CAP_POINTS = float(os.environ.get('IMPACT_CAP_POINTS', '10.0'))  # max points removed

# ============================================================================
# Quantum Approach §Stage 2.5 — Hyperbolic factor decay (Lee 2025).
# alpha(t) = K / (1 + lambda*t)  — derived from game-theoretic crowding
# equilibrium with R^2 ≈ 0.65 for momentum. Factors with high lambda decay
# fast (alpha vanishes quickly); downweight them in POTENTIAL_WEIGHTS.
# Calibration done offline via scripts/calibrate_factor_decay.py reading
# the audit log; output written to data/factor_decay.json.
# Reference: Chorok Lee, KAIST, arXiv:2512.11913 (Dec 2025).
# ============================================================================
DECAY_ENABLED = os.environ.get('DECAY_ENABLED', '1').lower() in ('1', 'true', 'yes')
DECAY_HORIZON_PERIODS = float(os.environ.get('DECAY_HORIZON_PERIODS', '1.0'))

# ============================================================================
# Quantum Approach §Stage 2.6 — Crowding flag via factor-ETF overlap.
# 13F filings are expensive to parse; cheap proxy = top holdings of replicating
# factor ETFs (MTUM/VLUE/QUAL/SIZE). A stock held in the top CROWDING_TOP_N
# of >= CROWDING_THRESHOLD ETFs is "crowded" — apply small score penalty.
# Reference: Chorok Lee (arXiv:2512.11913) — crowded reversal factors show
# 1.7-1.8x higher crash probability OOS.
# Holdings cached at data/factor_etf_holdings.json. Refresh via
# scripts/fetch_factor_etf_holdings.py (iShares public JSON endpoints).
# ============================================================================
CROWDING_ENABLED = os.environ.get('CROWDING_ENABLED', '1').lower() in ('1', 'true', 'yes')
CROWDING_TOP_N = int(os.environ.get('CROWDING_TOP_N', '100'))
CROWDING_THRESHOLD = int(os.environ.get('CROWDING_THRESHOLD', '2'))
CROWDING_PENALTY_POINTS = float(os.environ.get('CROWDING_PENALTY_POINTS', '3.0'))


def load_factor_etf_holdings(path=None):
    """Load data/factor_etf_holdings.json -> dict[etf -> list[ticker]].
    Schema: {"etfs": {"MTUM": {"top_holdings": ["AAPL", ...], "fetched_at": "..."}, ...}}
    Returns None if file missing or empty."""
    p = Path(path) if path else (DATA_DIR / 'factor_etf_holdings.json')
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    out = {}
    for etf, info in (data.get('etfs') or {}).items():
        holdings = info.get('top_holdings') or []
        if holdings:
            out[etf] = [str(t).upper().strip() for t in holdings[:CROWDING_TOP_N]]
    return out if out else None


def compute_crowding(df, etf_holdings):
    """Per-ticker count of factor ETFs holding it in their top CROWDING_TOP_N.
    Returns (count: float Series, crowded_flag: Int64 Series).
    NaN when no holdings file present so downstream code can short-circuit."""
    if not etf_holdings:
        nan_f = pd.Series(np.nan, index=df.index, dtype=float)
        return nan_f, pd.Series(pd.NA, index=df.index, dtype='Int64')
    tickers = df['ticker'].astype(str).str.upper()
    count = pd.Series(0, index=df.index, dtype=int)
    for etf, holdings in etf_holdings.items():
        holdings_set = set(holdings)
        count += tickers.isin(holdings_set).astype(int)
    crowded = (count >= CROWDING_THRESHOLD).astype(int)
    return count.astype(float), crowded.astype('Int64')


def fit_hyperbolic_decay(ic_series, t_series=None, ic_floor=0.005):
    """Fit alpha(t) = K / (1 + lambda*t) by linearizing 1/alpha = 1/K + (lambda/K)*t.
    Lee (2025). Returns (K, lambda, r_squared) on the linearized fit.

    Robust against negative/near-zero IC: periods with IC < ic_floor are
    DROPPED from the linear fit (Lee filters negative IC since alpha is
    positive by construction). If linear fit fails (intercept <= 0) or
    only one positive IC exists, falls back to scipy.optimize.curve_fit
    if available; otherwise reports a coarse two-window estimate."""
    ic = np.asarray(ic_series, dtype=float)
    if t_series is None:
        t = np.arange(len(ic), dtype=float)
    else:
        t = np.asarray(t_series, dtype=float)
    mask = np.isfinite(ic) & np.isfinite(t)
    ic_all, t_all = ic[mask], t[mask]
    # Per Lee 2025: alpha is positive by construction; drop negative IC.
    pos = ic_all >= ic_floor
    ic_pos, t_pos = ic_all[pos], t_all[pos]
    n_pos = len(ic_pos)

    def _coarse_two_window(ic_v, t_v):
        # If the linear fit collapses, infer a crude (K, lambda) from
        # comparing early vs late windows.
        if len(ic_v) < 2:
            return (float(ic_v[0]) if len(ic_v) == 1 else np.nan, 0.0, 0.0)
        n_half = max(1, len(ic_v) // 2)
        early_ic = float(np.mean(ic_v[:n_half]))
        late_ic = float(np.mean(ic_v[-n_half:]))
        t_early = float(np.mean(t_v[:n_half]))
        t_late = float(np.mean(t_v[-n_half:]))
        K_est = max(early_ic, late_ic, ic_floor)
        # alpha(t_late)/alpha(t_early) = (1+lam*t_early)/(1+lam*t_late) = late/early
        ratio = late_ic / early_ic if early_ic > 0 else 1.0
        dt = max(1e-9, t_late - t_early)
        if ratio >= 1.0:  # no decay observed
            lam_est = 0.0
        else:
            lam_est = max(0.0, (1.0 / ratio - 1.0) / dt)
        return (K_est, lam_est, 0.0)

    # Path 1: linear fit on positive ICs (preferred)
    if n_pos >= 3:
        inv_ic = 1.0 / ic_pos
        try:
            A = np.vstack([np.ones_like(t_pos), t_pos]).T
            coefs, *_ = np.linalg.lstsq(A, inv_ic, rcond=None)
            a, b = float(coefs[0]), float(coefs[1])
            if a > 0:
                K = 1.0 / a
                lam = max(0.0, b * K)
                y_pred = a + b * t_pos
                ss_res = float(((inv_ic - y_pred) ** 2).sum())
                ss_tot = float(((inv_ic - inv_ic.mean()) ** 2).sum())
                r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
                return (float(K), float(lam), float(r2))
        except Exception:
            pass

    # Path 2: nonlinear curve_fit if scipy available
    try:
        from scipy.optimize import curve_fit  # type: ignore
        if n_pos >= 2:
            def _hyp(tt, KK, ll):
                return KK / (1.0 + ll * tt)
            K0 = max(float(np.max(ic_pos)), ic_floor)
            popt, _ = curve_fit(_hyp, t_pos, ic_pos, p0=[K0, 0.1],
                                bounds=([ic_floor, 0.0], [np.inf, np.inf]),
                                maxfev=5000)
            K, lam = float(popt[0]), float(popt[1])
            y_pred = _hyp(t_pos, K, lam)
            ss_res = float(((ic_pos - y_pred) ** 2).sum())
            ss_tot = float(((ic_pos - ic_pos.mean()) ** 2).sum())
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
            return (K, lam, float(r2))
    except Exception:
        pass

    # Path 3: coarse two-window estimate
    if n_pos >= 1:
        K, lam, r2 = _coarse_two_window(ic_pos, t_pos)
        return (float(K), float(lam), float(r2))
    return (np.nan, np.nan, np.nan)


def load_factor_decay(path=None):
    """Load decay calibration JSON. Schema:
       {"factors": {"valuation": {"K":..., "lambda":..., "r2":...}, ...},
        "calibrated_at": "ISO", "n_periods": int}
    Returns None when file missing."""
    p = Path(path) if path else (DATA_DIR / 'factor_decay.json')
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def apply_decay_to_weights(base_weights, decay_cfg, horizon=None):
    """For each factor compute mult = 1/(1+lambda*horizon); renormalize so total
    sum is preserved. High-lambda -> low multiplier -> downweighted."""
    if not decay_cfg or 'factors' not in decay_cfg:
        return dict(base_weights)
    if horizon is None:
        horizon = DECAY_HORIZON_PERIODS
    factors = decay_cfg.get('factors', {})
    raw = {}
    for k, w in base_weights.items():
        info = factors.get(k, {})
        lam = info.get('lambda')
        if lam is None or not np.isfinite(lam):
            mult = 1.0
        else:
            mult = 1.0 / (1.0 + max(0.0, float(lam)) * float(horizon))
        raw[k] = float(w) * mult
    total_raw = sum(raw.values())
    total_base = sum(base_weights.values())
    if total_raw <= 0:
        return dict(base_weights)
    return {k: raw[k] / total_raw * total_base for k in base_weights}

# Default valuation component weights when sector isn't explicitly mapped.
# Components: earnings_yield, fcf_yield, sales_yield (1/PS), book_yield (1/PB),
# ebitda_yield (1/EV/EBITDA). Higher = cheaper. Must sum to 1.
DEFAULT_VALUATION_WEIGHTS = {
    'earnings_yield': 0.30, 'fcf_yield': 0.25, 'sales_yield': 0.15,
    'book_yield': 0.10, 'ebitda_yield': 0.20,
}

# Per-sector valuation weights. Economic rationale on each line.
SECTOR_VALUATION_WEIGHTS = {
    # Banks: book value and earnings both anchor valuation. Bias toward
    # book (0.55) because TTM earnings volatility from credit cycles
    # exaggerates over- or under-valuation in a single window.
    'banks': {'earnings_yield': 0.45, 'book_yield': 0.55,
              'fcf_yield': 0.0, 'sales_yield': 0.0, 'ebitda_yield': 0.0},
    # REITs: FFO ≈ NI + D&A, so earnings_yield understates cash gen badly.
    # FCF-yield and price/book lead; downweight raw P/E.
    'reits': {'fcf_yield': 0.40, 'book_yield': 0.25, 'earnings_yield': 0.10,
              'sales_yield': 0.15, 'ebitda_yield': 0.10},
    # Tech software: SaaS multiples — revenue and FCF dominant, book irrelevant.
    'tech_software': {'fcf_yield': 0.35, 'sales_yield': 0.25, 'ebitda_yield': 0.20,
                      'earnings_yield': 0.20, 'book_yield': 0.0},
    # Tech hardware: cyclical, FCF + EV/EBITDA primary, P/E secondary.
    'tech_hardware': {'fcf_yield': 0.25, 'ebitda_yield': 0.25, 'earnings_yield': 0.35,
                      'sales_yield': 0.15, 'book_yield': 0.0},
    # Energy: commodity-cyclical, EV/EBITDA dominant (price-takers, capex-heavy).
    'energy': {'ebitda_yield': 0.40, 'fcf_yield': 0.25, 'book_yield': 0.10,
               'earnings_yield': 0.15, 'sales_yield': 0.10},
    # Utilities: regulated returns, book + earnings primary, no growth premium.
    'utilities': {'earnings_yield': 0.50, 'book_yield': 0.25,
                  'fcf_yield': 0.15, 'ebitda_yield': 0.10, 'sales_yield': 0.0},
    # Consumer staples: stable, FCF + earnings matter, dividend coverage implied.
    'consumer_staples': {'fcf_yield': 0.35, 'earnings_yield': 0.40,
                         'ebitda_yield': 0.15, 'sales_yield': 0.10, 'book_yield': 0.0},
    # Consumer discretionary: cyclical, balanced.
    'consumer_disc': DEFAULT_VALUATION_WEIGHTS,
    # Healthcare: balanced; biotechs can have negative earnings — components handle.
    'healthcare': DEFAULT_VALUATION_WEIGHTS,
    # Industrials: balanced.
    'industrials': DEFAULT_VALUATION_WEIGHTS,
    # Non-bank financials: insurance carriers + asset managers + brokers +
    # exchanges + credit services. Combined because insurance alone is too
    # small (~12 stocks) for reliable sector-relative ranking. Weights blend
    # insurance's book-value anchor with asset management's earnings/FCF emphasis.
    'non_bank_financial': {
        'earnings_yield': 0.35,
        'fcf_yield': 0.20,
        'book_yield': 0.30,
        'sales_yield': 0.10,
        'ebitda_yield': 0.05,
    },
}
for sg, w in SECTOR_VALUATION_WEIGHTS.items():
    assert abs(sum(w.values()) - 1.0) < 1e-6, f'SECTOR_VALUATION_WEIGHTS[{sg}] must sum to 1'

# ============================================================================
# Framework Going Forward §5.4: sector-specific factor weights
# Override the global POTENTIAL_WEIGHTS (40/40/20/0) per sector based on the
# economic logic that different sectors deserve different factor emphasis.
# Banks: valuation/quality dominate. Biotech: growth dominates. SaaS: growth
# heavy. Utilities: quality+valuation. REITs: balanced with valuation lead.
# ============================================================================
SECTOR_POTENTIAL_WEIGHTS = {
    'energy':              {'valuation': 0.40, 'quality': 0.25, 'growth': 0.15, 'sentiment': 0.20},
    'industrials':         {'valuation': 0.30, 'quality': 0.30, 'growth': 0.25, 'sentiment': 0.15},
    'consumer_disc':       {'valuation': 0.25, 'quality': 0.25, 'growth': 0.30, 'sentiment': 0.20},
    'consumer_staples':    {'valuation': 0.35, 'quality': 0.35, 'growth': 0.15, 'sentiment': 0.15},
    # Quantum §Stage 2.4 MI finding: V<->Q nMI=+0.70 in healthcare (redundant).
    # Drop Q from 0.30 -> 0.20 to de-double-count; redistribute G+0.05, S+0.05.
    # Biotech (large slice) has weak Q (neg FCF pre-approval); V handles mature
    # pharma; G captures pipeline; S captures clinical-trial momentum.
    'healthcare':          {'valuation': 0.30, 'quality': 0.20, 'growth': 0.30, 'sentiment': 0.20},
    'banks':               {'valuation': 0.40, 'quality': 0.35, 'growth': 0.15, 'sentiment': 0.10},
    'non_bank_financial':  {'valuation': 0.30, 'quality': 0.35, 'growth': 0.20, 'sentiment': 0.15},
    'tech_software':       {'valuation': 0.15, 'quality': 0.25, 'growth': 0.45, 'sentiment': 0.15},
    'tech_hardware':       {'valuation': 0.25, 'quality': 0.25, 'growth': 0.30, 'sentiment': 0.20},
    'utilities':           {'valuation': 0.35, 'quality': 0.35, 'growth': 0.20, 'sentiment': 0.10},
    'reits':               {'valuation': 0.35, 'quality': 0.30, 'growth': 0.25, 'sentiment': 0.10},
}
for sg, w in SECTOR_POTENTIAL_WEIGHTS.items():
    assert abs(sum(w.values()) - 1.0) < 1e-6, f'SECTOR_POTENTIAL_WEIGHTS[{sg}] must sum to 1'

# ----------------------------------------------------------------------------
# Quantum Approach §Stage 2.5 — apply hyperbolic-decay-adjusted weights if
# data/factor_decay.json exists. Affects BOTH the global POTENTIAL_WEIGHTS
# and per-sector SECTOR_POTENTIAL_WEIGHTS. Each weight is multiplied by
# 1/(1+lambda*DECAY_HORIZON_PERIODS) then renormalized to its prior sum.
# Effect: factors whose IC decayed fast (high lambda) get smaller weight.
# Run scripts/calibrate_factor_decay.py first to populate the JSON.
# ----------------------------------------------------------------------------
FACTOR_DECAY_CFG = load_factor_decay() if DECAY_ENABLED else None
if FACTOR_DECAY_CFG:
    _orig_pw = dict(POTENTIAL_WEIGHTS)
    POTENTIAL_WEIGHTS = apply_decay_to_weights(POTENTIAL_WEIGHTS, FACTOR_DECAY_CFG)
    SECTOR_POTENTIAL_WEIGHTS = {
        sg: apply_decay_to_weights(w, FACTOR_DECAY_CFG)
        for sg, w in SECTOR_POTENTIAL_WEIGHTS.items()
    }
    _lam = {k: FACTOR_DECAY_CFG.get('factors', {}).get(k, {}).get('lambda')
            for k in _orig_pw}
    print(f'  §Stage2.5 factor-decay loaded ({FACTOR_DECAY_CFG.get("calibrated_at", "?")}, '
          f'n_periods={FACTOR_DECAY_CFG.get("n_periods", "?")})')
    print(f'    lambdas: {_lam}')
    print(f'    POTENTIAL_WEIGHTS: {_orig_pw} -> '
          f'{ {k: round(v, 3) for k, v in POTENTIAL_WEIGHTS.items()} }')

# Killer-KPI hard exclusions: sectors with deal-breaker thresholds. Stocks
# tripping any rule for their sector get potential_score = NaN (excluded from
# rankings). Rules expressed as (column, op, value) — op in {'<','>'}.
# Framework Going Forward §5.2 "Hard exclude if..." column.
KILLER_KPI = {
    'energy':       [('debt_equity', '>', 2.5)],          # Net Debt/EBITDA proxy via D/E
    'banks':        [('debt_equity', '>', 12.0)],         # banks leveraged but extreme = stress
    'utilities':    [('debt_equity', '>', 3.0)],
    'reits':        [('debt_equity', '>', 4.0)],          # leverage ceiling, AFFO payout n/a here
    'tech_software':[('operating_margin_3y_med', '<', -0.30)],  # cash-burning unprofitable SaaS
    'consumer_staples':[('revenue_growth_3yr', '<', -0.05)],     # negative organic growth = trap
}

# Quality component weights (applied uniformly across sectors).
QUALITY_WEIGHTS = {
    'roic_3y_med': 0.25,           # capital efficiency (most important)
    'roe_3y_med': 0.15,            # equity returns (less informative for leveraged sectors)
    'operating_margin_3y_med': 0.15,  # pricing power and cost control
    'fcf_margin_3y_med': 0.15,     # cash conversion
    'low_leverage': 0.10,          # 1/(1+debt_equity), capped
    'current_ratio': 0.05,         # liquidity (small weight — easily gameable)
    'stability': 0.15,             # 1/(revenue_cv_3y), capped
}
assert abs(sum(QUALITY_WEIGHTS.values()) - 1.0) < 1e-6, 'QUALITY_WEIGHTS must sum to 1'

# Growth component weights. Phase 1.2 (revisited): added
# revenue_growth_yoy_q (5th component) at 0.15 to cover early backtest
# periods where annual-derived metrics are NaN (SimFin annual file starts
# FY2020 — see compute_quarterly_yoy_growth).
GROWTH_WEIGHTS = {
    'revenue_growth_3yr':   0.25,  # 3yr CAGR
    'revenue_trend_5y':     0.20,  # 5yr trend slope (catches deceleration)
    'fcf_trend_5y':         0.20,  # cash growth trumps accounting growth
    'ebitda_trend_5y':      0.20,
    'revenue_growth_yoy_q': 0.15,  # quarterly YoY, available with only 5 quarters of history
}
assert abs(sum(GROWTH_WEIGHTS.values()) - 1.0) < 1e-6, 'GROWTH_WEIGHTS must sum to 1'

# Sentiment component weights. Price signals (distance_from_52w_high +
# return_12m_minus_1m, 0.70 combined) are the DEFAULT sole path — insider_own
# and short_float are NaN universe-wide unless the opt-in USE_FMP_OWNERSHIP=1
# adapter populates them, in which case _weighted_avg picks them up
# automatically. When ownership is NaN, _weighted_avg renormalizes to the
# price pair (weight_sum=0.70, coverage=0.70 > 0.50 min → sentiment scored);
# the resulting score is (s_dist + s_momo)/2 regardless of whether the
# ownership entries are present in the dict (kept for optional-in path).
SENTIMENT_WEIGHTS = {
    'distance_from_52w_high': 0.35,  # deep drawdown = potential bargain
    'return_12m_minus_1m': 0.35,     # contrarian (or momentum) signal
    'short_float': 0.10,             # opt-in via USE_FMP_OWNERSHIP; NaN otherwise
    'insider_own': 0.20,             # opt-in via USE_FMP_OWNERSHIP; NaN otherwise
}
assert abs(sum(SENTIMENT_WEIGHTS.values()) - 1.0) < 1e-6, 'SENTIMENT_WEIGHTS must sum to 1'

# ============================================================================
# Framework Going Forward §5.3: 5-regime macro classifier and sector tilts.
# State variables (all user-overridable via env vars for backtest replay):
#   ISM_PMI       — ISM Manufacturing (>50 = expansion)
#   YOY_CORE_CPI  — YoY core CPI (>3% = sticky inflation)
#   YIELD_10Y_2Y  — 10Y-2Y Treasury spread (basis points)
#   HY_OAS        — ICE BofA US HY OAS (basis points, >500 = stress)
# Defaults reflect May 2026 readings (stagflationary late-cycle = R2/R3 mix).
# ============================================================================
REGIME_DEFAULTS = {
    'ISM_PMI':       52.7,   # April 2026 ISM Manufacturing
    'YOY_CORE_CPI':  2.8,    # April 2026 core CPI
    'YIELD_10Y_2Y':  58.0,   # bps, May 2026 spread
    'HY_OAS':        320.0,  # bps, current
}

# Sector tilt vectors per regime (additive percentile-point adjustment on
# potential_score, capped at ±10 per §5.3 Stangl/Jacobsen/Visaltanachoti).
REGIME_SECTOR_TILTS = {
    # R1 — Disinflationary expansion (Goldilocks)
    'R1': {'tech_software': +7, 'tech_hardware': +5, 'consumer_disc': +5,
           'energy': -5, 'consumer_staples': -5, 'utilities': -3},
    # R2 — Reflationary expansion (current 2026 paradigm)
    'R2': {'energy': +7, 'industrials': +5, 'utilities': +5,
           'tech_software': -5, 'consumer_disc': -3, 'banks': -3},
    # R3 — Late-cycle / stagflation
    'R3': {'consumer_staples': +5, 'healthcare': +3, 'utilities': +5,
           'energy': +3, 'consumer_disc': -5, 'tech_software': -3},
    # R4 — Recession
    'R4': {'consumer_staples': +7, 'utilities': +7, 'healthcare': +5,
           'banks': -7, 'consumer_disc': -7, 'industrials': -5,
           'tech_hardware': -3},
    # R5 — Recovery
    'R5': {'banks': +5, 'industrials': +5, 'consumer_disc': +5,
           'non_bank_financial': +3, 'consumer_staples': -5, 'utilities': -5},
}

def classify_regime_simple(ism=None, core_cpi=None, curve_10y_2y=None, hy_oas=None):
    """Rule-based 5-regime classifier per §5.3. Returns 'R1'..'R5'.
    Legacy simple classifier kept for the screener REPL/CLI. The
    probabilistic classify_regime() defined later (used by backtest+v2 scorer)
    returns (label, prob_dict) for smooth transitions.
    Defaults fall back to REGIME_DEFAULTS; env vars override (REGIME_ISM_PMI etc)."""
    def _get(name, default):
        v = os.environ.get(f'REGIME_{name}')
        if v is not None:
            try:
                return float(v)
            except ValueError:
                pass
        return default
    ism = ism if ism is not None else _get('ISM_PMI', REGIME_DEFAULTS['ISM_PMI'])
    cpi = core_cpi if core_cpi is not None else _get('YOY_CORE_CPI', REGIME_DEFAULTS['YOY_CORE_CPI'])
    curve = curve_10y_2y if curve_10y_2y is not None else _get('YIELD_10Y_2Y', REGIME_DEFAULTS['YIELD_10Y_2Y'])
    oas = hy_oas if hy_oas is not None else _get('HY_OAS', REGIME_DEFAULTS['HY_OAS'])
    # R4 recession: ISM contraction + stress
    if ism < 48 and oas > 500:
        return 'R4'
    # R5 recovery: ISM turning up from below 50 + tight credit
    if ism < 50 and oas < 400 and curve > 0:
        return 'R5'
    # R1 Goldilocks: expansion + low inflation + tight credit
    if ism > 50 and cpi < 2.5 and oas < 350:
        return 'R1'
    # R2 reflationary: expansion + rising inflation
    if ism > 50 and cpi >= 3.0:
        return 'R2'
    # R3 stagflation default for late-cycle / sticky inflation
    return 'R3'

def regime_tilt(sector_group, regime):
    return REGIME_SECTOR_TILTS.get(regime, {}).get(sector_group, 0)

# ============================================================================
# Framework Going Forward §Phase 2: Piotroski F-score and Altman Z-score
# helpers. F-score 9 binary tests (0-9). Z-score: <1.81 distress, >2.99 safe.
# Both computed per fiscal year row in compute_history_metrics_full so the
# latest value is available downstream as a quality hard-gate input.
# ============================================================================
def piotroski_f_components(row_curr, row_prev):
    """Compute 9 Piotroski binary signals from current + prior fiscal-year rows.
    Returns dict of {signal: 0|1}. Total = sum() in [0,9]."""
    out = {}
    ni = row_curr.get('ni_yr')
    cfo = row_curr.get('ocf_yr')
    ta_curr = row_curr.get('ta_yr')
    ta_prev = row_prev.get('ta_yr') if row_prev is not None else None
    ni_prev = row_prev.get('ni_yr') if row_prev is not None else None
    # 1. Positive ROA
    out['f_roa_pos']      = int(ni is not None and ta_curr and ta_curr > 0 and (ni / ta_curr) > 0)
    # 2. Positive CFO
    out['f_cfo_pos']      = int(cfo is not None and cfo > 0)
    # 3. ROA increasing
    if ni_prev is not None and ta_prev and ta_prev > 0 and ta_curr and ta_curr > 0:
        out['f_roa_delta'] = int((ni / ta_curr) > (ni_prev / ta_prev))
    else:
        out['f_roa_delta'] = 0
    # 4. CFO > NI (earnings quality)
    out['f_accruals']     = int(cfo is not None and ni is not None and cfo > ni)
    # 5. Leverage falling
    debt_curr = row_curr.get('debt_yr')
    debt_prev = row_prev.get('debt_yr') if row_prev is not None else None
    if debt_curr is not None and debt_prev is not None and ta_curr and ta_prev and ta_curr > 0 and ta_prev > 0:
        out['f_leverage'] = int((debt_curr / ta_curr) < (debt_prev / ta_prev))
    else:
        out['f_leverage'] = 0
    # 6. Current ratio improving
    cr_curr = row_curr.get('cr_yr')
    cr_prev = row_prev.get('cr_yr') if row_prev is not None else None
    if cr_curr is not None and cr_prev is not None:
        out['f_liquidity'] = int(cr_curr > cr_prev)
    else:
        out['f_liquidity'] = 0
    # 7. No share dilution (Piotroski uses shares; we proxy via equity/share ~ constant)
    sh_curr = row_curr.get('sh_yr')
    sh_prev = row_prev.get('sh_yr') if row_prev is not None else None
    if sh_curr is not None and sh_prev is not None and sh_prev > 0:
        out['f_no_dilution'] = int(sh_curr <= sh_prev * 1.01)  # tolerate 1% buyback rounding
    else:
        out['f_no_dilution'] = 0
    # 8. Gross margin improving
    gm_curr = row_curr.get('gm')
    gm_prev = row_prev.get('gm') if row_prev is not None else None
    if gm_curr is not None and gm_prev is not None:
        out['f_gm_delta'] = int(gm_curr > gm_prev)
    else:
        out['f_gm_delta'] = 0
    # 9. Asset turnover improving (rev / total assets)
    rev_curr = row_curr.get('rev_yr')
    rev_prev = row_prev.get('rev_yr') if row_prev is not None else None
    if (rev_curr is not None and rev_prev is not None and
        ta_curr and ta_prev and ta_curr > 0 and ta_prev > 0):
        out['f_turnover'] = int((rev_curr / ta_curr) > (rev_prev / ta_prev))
    else:
        out['f_turnover'] = 0
    return out

def altman_z_score(row):
    """Altman Z″ (non-manufacturer variant — 4 components, dropping sales/TA).
    Z″ = 6.56*A + 3.26*B + 6.72*C + 1.05*D
      A = working_capital / total_assets
      B = retained_earnings / total_assets  (proxied here via equity / TA)
      C = EBIT / total_assets
      D = book_equity / total_liabilities
    Returns float or None. Cutoffs: Z>2.6 safe, Z<1.1 distress, in between grey."""
    ta = row.get('ta_yr')
    eq = row.get('eq_yr')
    op = row.get('op_inc_yr')
    cur_a = row.get('cur_a_yr')
    cur_l = row.get('cur_l_yr')
    debt = row.get('debt_yr')
    if not ta or ta <= 0 or eq is None or op is None:
        return None
    wc = (cur_a or 0) - (cur_l or 0)
    tl = (debt or 0) + (cur_l or 0)
    if tl <= 0:
        return None
    A = wc / ta
    B = eq / ta  # RE proxy via book equity
    C = op / ta
    D = eq / tl
    return float(6.56*A + 3.26*B + 6.72*C + 1.05*D)

SECTOR_GROUP = {
    'Application Software': 'tech_software',
    'Online Media': 'tech_software',
    'Computer Hardware': 'tech_hardware',
    'Semiconductors': 'tech_hardware',
    'Communication Equipment': 'tech_hardware',
    'Banks': 'banks',
    'Medical Diagnostics & Research': 'healthcare',
    'Biotechnology': 'healthcare',
    'Medical Instruments & Equipment': 'healthcare',
    'Medical Devices': 'healthcare',
    'Drug Manufacturers': 'healthcare',
    'Health Care Plans': 'healthcare',
    'Health Care Providers': 'healthcare',
    'Medical Distribution': 'healthcare',
    'Entertainment': 'consumer_disc',
    'Retail - Apparel & Specialty': 'consumer_disc',
    'Restaurants': 'consumer_disc',
    'Manufacturing - Apparel & Furniture': 'consumer_disc',
    'Autos': 'consumer_disc',
    'Advertising & Marketing Services': 'consumer_disc',
    'Homebuilding & Construction': 'consumer_disc',
    'Travel & Leisure': 'consumer_disc',
    'Packaging & Containers': 'consumer_disc',
    'Personal Services': 'consumer_disc',
    'Publishing': 'consumer_disc',
    'Retail - Defensive': 'consumer_staples',
    'Consumer Packaged Goods': 'consumer_staples',
    'Tobacco Products': 'consumer_staples',
    'Beverages - Alcoholic': 'consumer_staples',
    'Beverages - Non-Alcoholic': 'consumer_staples',
    'Education': 'consumer_staples',
    'Agriculture': 'consumer_staples',
    'Oil & Gas - Refining & Marketing': 'energy',
    'Oil & Gas - E&P': 'energy',
    'Oil & Gas - Midstream': 'energy',
    'Oil & Gas - Services': 'energy',
    'Oil & Gas - Integrated': 'energy',
    'Oil & Gas - Drilling': 'energy',
    'Alternative Energy Sources & Other': 'energy',
    'Coal': 'energy',
    'Industrial Products': 'industrials',
    'Business Services': 'industrials',
    'Engineering & Construction': 'industrials',
    'Waste Management': 'industrials',
    'Industrial Distribution': 'industrials',
    'Airlines': 'industrials',
    'Consulting & Outsourcing': 'industrials',
    'Aerospace & Defense': 'industrials',
    'Farm & Construction Machinery': 'industrials',
    'Transportation & Logistics': 'industrials',
    'Employment Services': 'industrials',
    'Truck Manufacturing': 'industrials',
    'Conglomerates': 'industrials',
    'Communication Services': 'industrials',
    'Consulting': 'industrials',
    'HR & Staffing': 'industrials',
    'Chemicals': 'industrials',
    'Building Materials': 'industrials',
    'Metals & Mining': 'industrials',
    'Forest Products': 'industrials',
    'Steel': 'industrials',
    'Diversified Holdings': 'industrials',
    'Other': 'industrials',
    'REITs': 'reits',
    'Real Estate Services': 'reits',
    'Utilities - Regulated': 'utilities',
    'Utilities - Independent Power Producers': 'utilities',
    'Asset Management': 'non_bank_financial',
    'Brokers, Exchanges & Other': 'non_bank_financial',
    'Credit Services': 'non_bank_financial',
    'Insurance - Specialty': 'non_bank_financial',
    'Insurance - Property & Casualty': 'non_bank_financial',
    'Insurance': 'non_bank_financial',
    'Insurance - Life': 'non_bank_financial',
}

SECTOR_LABEL = {
    'tech_software': 'Tech (Software)', 'tech_hardware': 'Tech (Hardware)',
    'banks': 'Banks', 'healthcare': 'Healthcare',
    'consumer_disc': 'Consumer Disc', 'consumer_staples': 'Consumer Staples',
    'energy': 'Energy', 'industrials': 'Industrials',
    'reits': 'REITs', 'utilities': 'Utilities',
    'non_bank_financial': 'Non-Bank Financial',
}

CACHE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS cache_meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS stocks (
    ticker TEXT PRIMARY KEY,
    company TEXT, sector TEXT, industry TEXT, sector_group TEXT,
    price REAL, market_cap REAL, enterprise_value REAL,
    pe REAL, pb REAL, ps REAL, pfcf REAL, peg REAL, ev_ebitda REAL,
    revenue REAL, net_income REAL, fcf REAL, ebitda REAL,
    gross_margin REAL, operating_margin REAL, net_margin REAL,
    roe REAL, roa REAL, roic REAL,
    debt_equity REAL, current_ratio REAL,
    dividend_yield REAL, payout_ratio REAL,
    revenue_growth_3yr REAL,
    beta REAL, realized_vol REAL, rsi REAL, sma20 REAL, sma50 REAL, sma200 REAL,
    high_52w REAL, low_52w REAL,
    target_price REAL, recommendation REAL,
    gross_margin_3y_med REAL, operating_margin_3y_med REAL, net_margin_3y_med REAL,
    roe_3y_med REAL, roic_3y_med REAL, fcf_margin_3y_med REAL,
    gross_margin_5y_med REAL, operating_margin_5y_med REAL, net_margin_5y_med REAL,
    roe_5y_med REAL, roic_5y_med REAL, fcf_margin_5y_med REAL,
    revenue_cv_3y REAL, op_inc_cv_3y REAL, revenue_cv_5y REAL, op_inc_cv_5y REAL,
    revenue_trend_5y REAL, ebitda_trend_5y REAL, fcf_trend_5y REAL,
    revenue_ttm REAL, net_income_ttm REAL, fcf_ttm REAL, ebitda_ttm REAL,
    pe_ttm REAL, ps_ttm REAL, pfcf_ttm REAL, ev_ebitda_ttm REAL,
    piotroski_f INTEGER, altman_z REAL, regime TEXT,
    return_1m REAL, return_3m REAL, return_6m REAL, return_12m REAL,
    return_12m_minus_1m REAL, distance_from_52w_high REAL,
    insider_own REAL, inst_own REAL, short_float REAL,
    valuation_score REAL, quality_score REAL, growth_score REAL, sentiment_score REAL,
    potential_score REAL,
    potential_median REAL, potential_p05 REAL, potential_p95 REAL,
    potential_iqr REAL, top_decile_pct REAL, robust_pick INTEGER,
    impact_haircut_bps REAL, impact_haircut_points REAL,
    crowding_count REAL, crowded INTEGER,
    flags TEXT,
    n_yrs_history INTEGER,
    stale_fundamentals INTEGER,
    stale_last_pub_date TEXT,
    last_filing_date TEXT,
    avg_dollar_volume_30d REAL,
    liquidity_tier TEXT,
    dq_share_xcheck_failed INTEGER,
    last_updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_stocks_sector_group ON stocks(sector_group);
CREATE INDEX IF NOT EXISTS idx_stocks_potential ON stocks(potential_score DESC);
'''

DATA_FIELDS = [
    'ticker', 'company', 'sector', 'industry', 'sector_group',
    'price', 'market_cap', 'enterprise_value',
    'pe', 'pb', 'ps', 'pfcf', 'peg', 'ev_ebitda',
    'revenue', 'net_income', 'fcf', 'ebitda',
    'gross_margin', 'operating_margin', 'net_margin',
    'roe', 'roa', 'roic',
    'debt_equity', 'current_ratio',
    'dividend_yield', 'payout_ratio',
    'revenue_growth_3yr',
    'beta', 'realized_vol', 'high_52w', 'low_52w',
    'gross_margin_3y_med', 'operating_margin_3y_med', 'net_margin_3y_med',
    'roe_3y_med', 'roic_3y_med', 'fcf_margin_3y_med',
    'gross_margin_5y_med', 'operating_margin_5y_med', 'net_margin_5y_med',
    'roe_5y_med', 'roic_5y_med', 'fcf_margin_5y_med',
    'revenue_cv_3y', 'op_inc_cv_3y', 'revenue_cv_5y', 'op_inc_cv_5y',
    'revenue_trend_5y', 'ebitda_trend_5y', 'fcf_trend_5y',
    'revenue_ttm', 'net_income_ttm', 'fcf_ttm', 'ebitda_ttm',
    'pe_ttm', 'ps_ttm', 'pfcf_ttm', 'ev_ebitda_ttm',
    'piotroski_f', 'altman_z', 'regime',
    'return_1m', 'return_3m', 'return_6m', 'return_12m',
    'return_12m_minus_1m', 'distance_from_52w_high',
    'insider_own', 'inst_own', 'short_float',
    'valuation_score', 'quality_score', 'growth_score', 'sentiment_score', 'potential_score',
    'potential_median', 'potential_p05', 'potential_p95',
    'potential_iqr', 'top_decile_pct', 'robust_pick',
    'impact_haircut_bps', 'impact_haircut_points',
    'crowding_count', 'crowded',
    'flags', 'n_yrs_history', 'stale_fundamentals', 'stale_last_pub_date',
    'last_filing_date', 'filing_age_days',
    'avg_dollar_volume_30d', 'liquidity_tier',
    'dq_share_xcheck_failed',
]

T10Y_URL = ('https://api.fiscaldata.treasury.gov/api/v1/accounting/od/'
            'avg_interest_rates?page[size]=1&sort=-record_date'
            '&filter=security_desc:eq:Treasury%20Nominal%20Coupon%20Issuance%20Rate%20Indices%20(10-Year)'
            '&filter=avg_interest_rate_amt:gt:0')

def fetch_t10y():
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urlopen(T10Y_URL, timeout=10, context=ctx) as r:
            data = json.loads(r.read().decode())
            rate = float(data['data'][0]['avg_interest_rate_amt']) / 100
            print(f'  10yr Treasury: {rate*100:.2f}%')
            return rate
    except Exception:
        rate = 0.045
        print(f'  Using default 10yr Treasury: {rate*100:.2f}%')
        return rate

def load_simfin_data():
    print('  Loading companies...')
    companies = sf.load_companies(market='us')
    print(f'    {len(companies)} companies')

    print('  Loading industries...')
    industries = sf.load_industries()
    print(f'    {len(industries)} industries')

    print('  Loading annual income statements...')
    income = sf.load_income(variant='annual', market='us')
    print(f'    {len(income)} rows')

    print('  Loading annual balance sheets...')
    balance = sf.load_balance(variant='annual', market='us')
    print(f'    {len(balance)} rows')

    print('  Loading annual cash flow...')
    cashflow = sf.load_cashflow(variant='annual', market='us')
    print(f'    {len(cashflow)} rows')

    print('  Loading quarterly income statements...')
    income_q = sf.load_income(variant='quarterly', market='us')
    print(f'    {len(income_q)} rows')

    print('  Loading quarterly cash flow...')
    cashflow_q = sf.load_cashflow(variant='quarterly', market='us')
    print(f'    {len(cashflow_q)} rows')

    print('  Loading daily share prices...')
    sp = sf.load_shareprices(variant='daily', market='us')
    print(f'    {len(sp)} rows')

    return companies, industries, income, balance, cashflow, income_q, cashflow_q, sp

def _slope_normalized(years, vals):
    # Linear regression slope normalized by mean. Unit-free trend per year.
    # Returns None if <3 points, non-positive mean, or all-NaN.
    arr = np.asarray(vals, dtype=float)
    yrs = np.asarray(years, dtype=float)
    mask = ~np.isnan(arr) & ~np.isnan(yrs)
    if mask.sum() < 3:
        return None
    a, y = arr[mask], yrs[mask]
    m = a.mean()
    if m <= 0 or not np.isfinite(m):
        return None
    # polyfit deg=1 returns [slope, intercept]
    try:
        slope = np.polyfit(y, a, 1)[0]
    except (np.linalg.LinAlgError, ValueError):
        return None
    return slope / m if np.isfinite(slope) else None

def _cv(vals):
    arr = np.asarray(vals, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 2:
        return None
    m = arr.mean()
    if m == 0 or not np.isfinite(m):
        return None
    s = arr.std(ddof=1)
    return float(abs(s / m)) if np.isfinite(s) else None

def compute_history_metrics_full(income, balance, cashflow):
    # Build per-(ticker,fiscal_year) row with per-year margins, ROE, ROIC, FCF margin.
    # Returns a DataFrame indexed by (Ticker, Fiscal Year) with per-year metric columns.
    # Caller is responsible for tail(N) windowing via aggregate_history_metrics.
    print('  Computing multi-year history metrics...')
    inc = income.reset_index()
    bal = balance.reset_index()
    cf = cashflow.reset_index()
    keep_inc = ['Ticker', 'Fiscal Year', 'Revenue', 'Gross Profit',
                'Operating Income (Loss)', 'Net Income (Common)', 'Cost of Revenue',
                'Publish Date']
    keep_bal = ['Ticker', 'Fiscal Year', 'Total Equity',
                'Total Assets', 'Short Term Debt', 'Long Term Debt',
                'Cash, Cash Equivalents & Short Term Investments',
                'Total Current Assets', 'Total Current Liabilities']
    keep_cf = ['Ticker', 'Fiscal Year', 'Net Cash from Operating Activities',
               'Change in Fixed Assets & Intangibles', 'Depreciation & Amortization']
    inc = inc[[c for c in keep_inc if c in inc.columns]]
    bal = bal[[c for c in keep_bal if c in bal.columns]]
    cf = cf[[c for c in keep_cf if c in cf.columns]]
    # Some tickers have multiple rows per fiscal year (restatements). Keep last.
    inc = inc.sort_values(['Ticker', 'Fiscal Year']).drop_duplicates(['Ticker', 'Fiscal Year'], keep='last')
    bal = bal.sort_values(['Ticker', 'Fiscal Year']).drop_duplicates(['Ticker', 'Fiscal Year'], keep='last')
    cf = cf.sort_values(['Ticker', 'Fiscal Year']).drop_duplicates(['Ticker', 'Fiscal Year'], keep='last')
    m = inc.merge(bal, on=['Ticker', 'Fiscal Year'], how='inner')
    m = m.merge(cf, on=['Ticker', 'Fiscal Year'], how='inner')
    m = m[m['Fiscal Year'].apply(lambda x: isinstance(x, (int, np.integer)) or (isinstance(x, float) and not np.isnan(x)))]
    m['Fiscal Year'] = m['Fiscal Year'].astype(int)
    # Per-year metrics. Guard against zero/negative denominators.
    rev = m['Revenue'].astype(float)
    eq = m['Total Equity'].astype(float)
    debt = m.get('Short Term Debt', 0).fillna(0).astype(float) + m.get('Long Term Debt', 0).fillna(0).astype(float)
    cash = m['Cash, Cash Equivalents & Short Term Investments'].astype(float)
    ic = eq + debt - cash  # invested capital
    op_inc = m['Operating Income (Loss)'].astype(float)
    ni = m['Net Income (Common)'].astype(float)
    gp = m['Gross Profit'].astype(float)
    ocf = m['Net Cash from Operating Activities'].astype(float)
    capex = m['Change in Fixed Assets & Intangibles'].astype(float)
    fcf = ocf + capex
    da = m['Depreciation & Amortization'].astype(float)
    ebitda = op_inc + da
    safe_rev = rev.where(rev > 0, np.nan)
    safe_eq = eq.where(eq > 0, np.nan)
    safe_ic = ic.where(ic > 0, np.nan)
    m['gm'] = gp / safe_rev
    m['opm'] = op_inc / safe_rev
    m['nm'] = ni / safe_rev
    m['roe_yr'] = ni / safe_eq
    m['roic_yr'] = ni / safe_ic
    m['fcfm'] = fcf / safe_rev
    m['rev_yr'] = rev
    m['op_inc_yr'] = op_inc
    m['ebitda_yr'] = ebitda
    m['fcf_yr'] = fcf
    # Extra columns for Piotroski F-score and Altman Z-score (§Phase 2).
    m['ni_yr']    = ni
    m['ocf_yr']   = ocf
    m['ta_yr']    = m['Total Assets'].astype(float)
    m['debt_yr']  = debt
    m['eq_yr']    = eq
    m['cur_a_yr'] = m.get('Total Current Assets', 0).fillna(0).astype(float)
    m['cur_l_yr'] = m.get('Total Current Liabilities', 0).fillna(0).astype(float)
    m['cr_yr']    = m['cur_a_yr'] / m['cur_l_yr'].where(m['cur_l_yr'] > 0, np.nan)
    # share count via NI / EPS proxy unavailable here — use equity as scale-stable
    # signal for "dilution" check: if total equity grew faster than retained
    # earnings, dilution likely happened. Cheap proxy: just use total equity.
    m['sh_yr']    = eq  # proxy — caveat noted in piotroski_f_components
    m = m.sort_values(['Ticker', 'Fiscal Year'])
    return m


def aggregate_history_metrics(m, max_fiscal_year=None):
    # Filter to fiscal years <= max_fiscal_year (or all if None),
    # then take last 5 fiscal years per ticker and compute
    # 3y/5y medians, CV(rev/op_inc), trend slopes.
    if max_fiscal_year is not None:
        m = m[m['Fiscal Year'] <= max_fiscal_year]
    out = {}
    def _last_n(g, col, n):
        vals = g[col].tail(n).values
        vals = vals[~np.isnan(vals.astype(float))]
        return vals
    for ticker, g in m.groupby('Ticker'):
        g = g.tail(5)  # last 5 fiscal years
        g3 = g.tail(3)
        def _med(col, sub):
            vals = sub[col].values.astype(float)
            vals = vals[~np.isnan(vals)]
            return float(np.median(vals)) if len(vals) else None
        rec = {
            'gross_margin_3y_med': _med('gm', g3),
            'operating_margin_3y_med': _med('opm', g3),
            'net_margin_3y_med': _med('nm', g3),
            'roe_3y_med': _med('roe_yr', g3),
            'roic_3y_med': _med('roic_yr', g3),
            'fcf_margin_3y_med': _med('fcfm', g3),
            'gross_margin_5y_med': _med('gm', g),
            'operating_margin_5y_med': _med('opm', g),
            'net_margin_5y_med': _med('nm', g),
            'roe_5y_med': _med('roe_yr', g),
            'roic_5y_med': _med('roic_yr', g),
            'fcf_margin_5y_med': _med('fcfm', g),
            'revenue_cv_3y': _cv(g3['rev_yr'].values),
            'op_inc_cv_3y': _cv(g3['op_inc_yr'].values),
            'revenue_cv_5y': _cv(g['rev_yr'].values),
            'op_inc_cv_5y': _cv(g['op_inc_yr'].values),
            'revenue_trend_5y': _slope_normalized(g['Fiscal Year'].values, g['rev_yr'].values),
            'ebitda_trend_5y': _slope_normalized(g['Fiscal Year'].values, g['ebitda_yr'].values),
            'fcf_trend_5y': _slope_normalized(g['Fiscal Year'].values, g['fcf_yr'].values),
            'n_yrs_history': int(len(g)),
            'last_filing_date': (g['Publish Date'].max().isoformat()
                                 if 'Publish Date' in g.columns
                                    and g['Publish Date'].notna().any()
                                 else None),
        }
        # Piotroski F-score on most recent FY using prior FY for delta signals.
        # Altman Z″ on most recent FY only.
        if len(g) >= 2:
            curr = g.iloc[-1].to_dict()
            prev = g.iloc[-2].to_dict()
            comps = piotroski_f_components(curr, prev)
            rec['piotroski_f'] = int(sum(comps.values()))
        elif len(g) >= 1:
            rec['piotroski_f'] = None  # need 2 yrs for delta-based F-score
        else:
            rec['piotroski_f'] = None
        if len(g) >= 1:
            z = altman_z_score(g.iloc[-1].to_dict())
            rec['altman_z'] = round(z, 2) if z is not None else None
        else:
            rec['altman_z'] = None
        g3_rev = g3['rev_yr'].dropna()
        # revenue_growth_3yr: CAGR over the last 3 fiscal years.
        # Requires exactly 3+ data points; 2-point growth is too noisy to
        # represent as a 3yr trend.
        if len(g3_rev) >= 3:
            first, last = g3_rev.iloc[0], g3_rev.iloc[-1]
            n = len(g3_rev) - 1
            if first > 0 and last > 0:
                rec['revenue_growth_3yr'] = (last / first) ** (1.0/n) - 1
            else:
                rec['revenue_growth_3yr'] = None
        else:
            rec['revenue_growth_3yr'] = None
        out[ticker] = rec
    print(f'    Computed history metrics for {len(out)} tickers')
    return out


def compute_history_metrics(income, balance, cashflow):
    m = compute_history_metrics_full(income, balance, cashflow)
    return aggregate_history_metrics(m)

def compute_ttm(income_q, cashflow_q):
    # TTM = trailing four quarters. Sum last 4 quarterly rows of key flow items.
    # Quarterly data is needed because annual filings can be 12+ months stale.
    print('  Computing TTM (trailing twelve months)...')
    out = {}
    # Sort by publish date (or fiscal period+year) per ticker, take last 4.
    iq = income_q.reset_index().sort_values(['Ticker', 'Publish Date'])
    cq = cashflow_q.reset_index().sort_values(['Ticker', 'Publish Date'])
    # Group then tail(4) — vectorize per ticker.
    for ticker, g in iq.groupby('Ticker'):
        g4 = g.tail(4)
        if len(g4) < 4:
            continue
        rev = float(g4['Revenue'].fillna(0).sum())
        ni = float(g4['Net Income (Common)'].fillna(0).sum())
        op_inc_sum = float(g4['Operating Income (Loss)'].fillna(0).sum())
        out[ticker] = {'revenue_ttm': rev, 'net_income_ttm': ni, '_op_inc_ttm': op_inc_sum}
    for ticker, g in cq.groupby('Ticker'):
        g4 = g.tail(4)
        if len(g4) < 4:
            continue
        ocf = float(g4['Net Cash from Operating Activities'].fillna(0).sum())
        capex = float(g4['Change in Fixed Assets & Intangibles'].fillna(0).sum())
        da = float(g4['Depreciation & Amortization'].fillna(0).sum())
        rec = out.setdefault(ticker, {})
        rec['fcf_ttm'] = ocf + capex
        rec['_da_ttm'] = da
    # Compute ebitda_ttm where both pieces available.
    for ticker, rec in out.items():
        if '_op_inc_ttm' in rec and '_da_ttm' in rec:
            rec['ebitda_ttm'] = rec['_op_inc_ttm'] + rec['_da_ttm']
        rec.pop('_op_inc_ttm', None)
        rec.pop('_da_ttm', None)
    print(f'    Computed TTM for {len(out)} tickers')
    return out


def compute_quarterly_yoy_growth(income_q):
    """Per-ticker YoY revenue growth from quarterly filings.

    Phase 1.2 (revisited): 5th growth component that requires only 5 quarters
    of history (latest + same Fiscal Period one year prior), unlike the
    annual-derived growth metrics that need 3+ annual filings. This fills the
    growth signal gap in early backtest periods where SimFin's annual file
    has too little history.

    Caller is responsible for any point-in-time filter (e.g.
    `income_q = filter_quarterly(income_q, cutoff)`) before passing in.

    Returns: dict[ticker] -> float in [-1.0, 5.0], or empty dict if input
    lacks required columns.
    """
    if income_q is None or len(income_q) == 0:
        return {}
    required = {'Ticker', 'Fiscal Year', 'Fiscal Period', 'Revenue'}
    df = income_q.reset_index() if hasattr(income_q, 'reset_index') else income_q
    if not required.issubset(df.columns):
        return {}
    # Sort by Report Date when present, else (Fiscal Year, Fiscal Period).
    if 'Report Date' in df.columns:
        df = df.sort_values(['Ticker', 'Report Date'])
    else:
        df = df.sort_values(['Ticker', 'Fiscal Year', 'Fiscal Period'])
    out = {}
    for ticker, g in df.groupby('Ticker'):
        if len(g) < 2:
            continue
        latest = g.iloc[-1]
        fp = latest.get('Fiscal Period')
        fy = latest.get('Fiscal Year')
        rev_latest = latest.get('Revenue')
        if pd.isna(fy) or pd.isna(rev_latest):
            continue
        prior = g[(g['Fiscal Period'] == fp) & (g['Fiscal Year'] == int(fy) - 1)]
        if prior.empty:
            continue
        rev_prior = prior.iloc[-1].get('Revenue')
        if pd.isna(rev_prior) or rev_prior <= 0:
            continue
        try:
            growth = (float(rev_latest) - float(rev_prior)) / abs(float(rev_prior))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if not np.isfinite(growth):
            continue
        out[ticker] = float(np.clip(growth, -1.0, 5.0))
    print(f'    Computed quarterly YoY revenue growth for {len(out)} tickers')
    return out


def compute_momentum(sp):
    # Daily close-based returns over 1m/3m/6m/12m horizons + 12m-1m (academic momentum factor)
    # + distance from 52w high (price / 52w_high - 1, so 0 = at high, -0.30 = 30% below).
    print('  Computing price momentum and 52w distance...')
    sp_sorted = sp.sort_index()
    closes = sp_sorted['Adj. Close']
    # Need: latest close per ticker; close ~21/63/126/252 trading days ago; 52w high.
    latest_per_ticker = closes.groupby(level=0).last()
    latest_date = closes.index.get_level_values(1).max()
    # Build a closes-by-date DataFrame for fast lookback.
    cm = closes.unstack(level=0)  # rows=date, cols=ticker
    # Trading-day offsets approximate calendar windows: 21=1m, 63=3m, 126=6m, 252=12m.
    offsets = {'1m': 21, '3m': 63, '6m': 126, '12m': 252}
    out = {}
    # Forward-fill so weekend/holiday gaps don't poison lookbacks.
    cm = cm.sort_index().ffill()
    last_idx = len(cm) - 1
    cur = cm.iloc[last_idx]
    ret = {}
    for label, n in offsets.items():
        if last_idx - n < 0:
            continue
        prev = cm.iloc[last_idx - n]
        ret[label] = (cur / prev - 1.0)
    # 52w high = max over last ~252 sessions
    cutoff = max(0, last_idx - 251)
    last_year = cm.iloc[cutoff:last_idx + 1]
    hi_52w = last_year.max()
    dist_high = cur / hi_52w - 1.0
    for t in cur.index:
        rec = {}
        for label in offsets:
            v = ret.get(label, pd.Series()).get(t)
            if v is not None and np.isfinite(v):
                rec[f'return_{label}'] = float(v)
        r12 = rec.get('return_12m')
        r1 = rec.get('return_1m')
        if r12 is not None and r1 is not None:
            rec['return_12m_minus_1m'] = r12 - r1
        d = dist_high.get(t)
        if d is not None and np.isfinite(d):
            rec['distance_from_52w_high'] = float(d)
        if rec:
            out[t] = rec
    print(f'    Computed momentum for {len(out)} tickers')
    return out

def compute_liquidity(sp):
    print('  Computing median daily dollar volume (trailing 30d)...')
    sp_sorted = sp.sort_index()
    dollar_vol = sp_sorted['Adj. Close'] * sp_sorted['Volume']
    out = {}
    for ticker, g in dollar_vol.groupby(level=0):
        last_30 = g.tail(30).dropna()
        if len(last_30) >= 10:
            out[ticker] = float(last_30.median())
    print(f'    Computed liquidity for {len(out)} tickers')
    return out

def load_ownership_live(tickers):
    """Read latest ownership_live row per ticker (filing_date <= today).
    Returns dict[ticker] -> {insider_own, inst_own, short_float}. Values
    already normalized to [0,1] fractions by refresh_ownership_fmp.py.

    Silent no-op if the ownership_live table does not exist (script never run).
    Consumed only when USE_FMP_OWNERSHIP=1 env var is set; see docs/ownership_layer.md.
    """
    if not CACHE_DB.exists():
        return {}
    conn = sqlite3.connect(str(CACHE_DB))
    try:
        # Check table exists — script may not have been run yet.
        has_tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ownership_live'"
        ).fetchone()
        if not has_tbl:
            return {}
        today = datetime.now().date().isoformat()
        placeholders = ','.join('?' for _ in tickers)
        # Latest filing_date per ticker, gated on filing_date <= today for PIT hygiene.
        q = (
            "SELECT o.ticker, o.insider_own, o.inst_own, o.short_float "
            "FROM ownership_live o "
            "JOIN (SELECT ticker, MAX(filing_date) AS md FROM ownership_live "
            f"      WHERE filing_date <= ? AND ticker IN ({placeholders}) GROUP BY ticker) m "
            "ON o.ticker = m.ticker AND o.filing_date = m.md"
        )
        cur = conn.execute(q, (today, *tickers))
        return {
            r[0]: {'insider_own': r[1], 'inst_own': r[2], 'short_float': r[3]}
            for r in cur.fetchall()
        }
    finally:
        conn.close()


def compute_52w(sp):
    # Per-ticker 52-week high/low from daily closes (price-only, ignoring splits via Adj. Close).
    print('  Computing 52-week high/low...')
    sp_sorted = sp.sort_index()
    cutoff = sp_sorted.index.get_level_values(1).max() - pd.DateOffset(weeks=52)
    recent = sp_sorted[sp_sorted.index.get_level_values(1) >= cutoff]
    grp = recent['Adj. Close'].groupby(level=0)
    hi = grp.max().to_dict()
    lo = grp.min().to_dict()
    print(f'    Computed 52w high/low for {len(hi)} tickers')
    return hi, lo

def compute_betas(sp, sp_meta):
    # Cap-weighted market proxy: each ticker's daily return weighted by its
    # latest market cap (static cross-sectional weight). True point-in-time
    # weighting per date is too slow for ~3000 tickers; static weight gives
    # ~90% of the accuracy without the cost.
    print('  Computing Beta from daily returns (cap-weighted proxy)...')
    # Copy is required: backtest reuses the same sp across periods, and
    # writing 'Return' as a column would persist across iterations.
    sp = sp.copy()
    sp = sp.sort_index()
    sp['Return'] = sp['Adj. Close'].groupby(level=0).pct_change()
    sp_ret = sp[sp['Return'].notna()].copy()
    cutoff = sp_ret.index.get_level_values(1).max() - pd.DateOffset(years=2)
    sp_ret = sp_ret[sp_ret.index.get_level_values(1) >= cutoff]
    tickers_used = sp_ret.index.get_level_values(0).unique()
    print(f'    {len(tickers_used)} tickers with return data')
    ret_matrix = sp_ret['Return'].unstack(level=0)
    ret_matrix = ret_matrix.dropna(how='all', axis=1)
    # Drop garbage: inf/-inf from zero-price pct_change events.
    # Cap at ±100%: moves above 100% in one day are almost certainly unadjusted
    # splits or data errors. ±50% was too aggressive — biotech catalysts, small-cap
    # squeezes, and earnings beats routinely produce 50-100% daily returns, and
    # filtering them dampens beta for the volatile names we want measured accurately.
    ret_matrix = ret_matrix.replace([np.inf, -np.inf], np.nan)
    n_filtered = (ret_matrix.abs() > 1.0).sum().sum()
    ret_matrix = ret_matrix.where(ret_matrix.abs() <= 1.0)
    print(f'    Filtered {n_filtered} extreme daily returns (>100%)')
    # Build per-ticker static cap weight from sp_meta (close * shares outstanding).
    caps = (sp_meta['sp_Close'] * sp_meta['sp_Shares Outstanding']).reindex(ret_matrix.columns)
    caps = caps.where(caps > 0)  # drop non-positive
    if caps.notna().sum() > 0:
        weights = caps / caps.sum(skipna=True)
        # Cap-weighted market return = sum(w_i * r_i,t) over tickers with data each day.
        weighted = ret_matrix.mul(weights, axis=1)
        market_ret = weighted.sum(axis=1, min_count=1, skipna=True)
    else:
        # Fallback to equal-weight if cap data unavailable (shouldn't happen in practice).
        market_ret = ret_matrix.mean(axis=1, skipna=True)
    betas = {}
    for t in ret_matrix.columns:
        col = ret_matrix[t].dropna()
        common = market_ret.reindex(col.index).dropna()
        col = col.reindex(common.index).dropna()
        if len(col) < BETA_MIN_DAYS:
            continue
        cov = np.cov(col.values, common.values)[0, 1]
        var = common.var()
        betas[t] = round(cov / var, 3) if var > 1e-12 else None
    print(f'    Computed Beta for {len(betas)} tickers')
    # Realized volatility: annualized standard deviation of daily returns.
    # Falls back to a shorter window if 60 days unavailable.
    vols = {}
    for t in ret_matrix.columns:
        col = ret_matrix[t].dropna()
        if len(col) < 30:  # require at least 30 days for any vol estimate
            continue
        daily_vol = col.std()
        vols[t] = round(daily_vol * np.sqrt(252), 4) if daily_vol > 0 else None
    print(f'    Computed realized volatility for {len(vols)} tickers')
    return betas, vols

def compute_snapshot(companies, industries, income, balance, cashflow, sp_meta, betas, vols, t10y,
                     hi52w=None, lo52w=None, hist=None, ttm=None, momo=None, ownership=None,
                     liquidity=None, reference_date=None, rev_yoy_q=None,
                     dq_guard=False):
    hi52w = hi52w or {}
    lo52w = lo52w or {}
    hist = hist or {}
    ttm = ttm or {}
    momo = momo or {}
    ownership = ownership or {}
    rev_yoy_q = rev_yoy_q or {}
    ref_dt = reference_date if reference_date is not None else datetime.now()
    _unmapped_industries = {}
    # Data-quality guard on derived valuation ratios. Off by default so
    # backtest / AB-verify / parity harnesses stay bit-identical. Live path
    # (main() below) opts in with dq_guard=True. Catches upstream input-scale
    # bugs (e.g. SimFin Shares(Diluted)=89 for HESM 2024 producing div_yield
    # ~6.8M%, or SimFin NI 1000x-overstated on SAND producing P/E=0.04) by
    # NULLing implausible derived metrics rather than ranking on garbage.
    # NULL (not fabricate) so the valuation subscore degrades gracefully via
    # the coverage-min gate.
    _DQ_GUARD_ENABLED = bool(dq_guard)
    _DQ_PE_MIN, _DQ_PE_MAX = -1000.0, 1000.0
    _DQ_DY_MIN, _DQ_DY_MAX = 0.0, 0.5
    _DQ_LOG: list[dict] = []
    print('  Building stock snapshot...')
    companies = companies.reset_index()
    companies['Industry'] = companies['IndustryId'].map(industries['Industry'])
    companies['Sector'] = companies['IndustryId'].map(industries['Sector'])

    i = income.sort_index(level=1)
    i_latest = i.groupby(level=0).last()
    bal = balance.sort_index(level=1)
    bal_latest = bal.groupby(level=0).last()
    cf = cashflow.sort_index(level=1)
    cf_latest = cf.groupby(level=0).last()
    STALE_DAYS = 548  # ~18 months

    rows = []
    total = len(companies)
    for idx, comp in companies.iterrows():
        ticker = comp['Ticker']
        if ticker not in sp_meta.index:
            continue
        sp_row = sp_meta.loc[ticker]
        close = sp_row.get('sp_Close', 0)
        shares = sp_row.get('sp_Shares Outstanding', 0)
        mkt_cap = close * shares if (close and shares and close > 0 and shares > 0) else 0
        if mkt_cap < MIN_MARKET_CAP:
            continue
        dollar_vol = liquidity.get(ticker, 0) if liquidity else 0
        if dollar_vol < MIN_DOLLAR_VOLUME:
            continue
        inc_row = i_latest.loc[ticker] if ticker in i_latest.index else None
        bal_row = bal_latest.loc[ticker] if ticker in bal_latest.index else None
        cf_row = cf_latest.loc[ticker] if ticker in cf_latest.index else None
        if inc_row is None or bal_row is None or cf_row is None:
            continue
        rev = float(inc_row.get('Revenue', 0) or 0)
        rnd = float(inc_row.get('Research & Development', 0) or 0)
        depr = float(cf_row.get('Depreciation & Amortization', 0) or 0)
        op_inc = float(inc_row.get('Operating Income (Loss)', 0) or 0)
        int_exp = float(inc_row.get('Interest Expense, Net', 0) or 0)
        pretax = float(inc_row.get('Pretax Income (Loss)', 0) or 0)
        tax = float(inc_row.get('Income Tax (Expense) Benefit, Net', 0) or 0)
        net_inc = float(inc_row.get('Net Income (Common)', 0) or 0)
        shares_inc = float(inc_row.get('Shares (Diluted)', 0) or 0)
        cogs = float(inc_row.get('Cost of Revenue', 0) or 0)
        gp = float(inc_row.get('Gross Profit', 0) or 0)
        cash = float(bal_row.get('Cash, Cash Equivalents & Short Term Investments', 0) or 0)
        st_debt = float(bal_row.get('Short Term Debt', 0) or 0)
        lt_debt = float(bal_row.get('Long Term Debt', 0) or 0)
        tot_assets = float(bal_row.get('Total Assets', 0) or 0)
        tot_eq = float(bal_row.get('Total Equity', 0) or 0)
        cur_assets = float(bal_row.get('Total Current Assets', 0) or 0)
        cur_liab = float(bal_row.get('Total Current Liabilities', 0) or 0)
        ops_cf = float(cf_row.get('Net Cash from Operating Activities', 0) or 0)
        capex = float(cf_row.get('Change in Fixed Assets & Intangibles', 0) or 0)
        div_paid = float(cf_row.get('Dividends Paid', 0) or 0)

        ebitda = op_inc + depr
        # SimFin "Change in Fixed Assets & Intangibles" is reported as a negative outflow
        # (verified on AAPL/MSFT/XOM/KO 2026-05-10). FCF = ops_cf + capex is correct.
        fcf = ops_cf + capex
        tot_debt = st_debt + lt_debt
        ev = mkt_cap + tot_debt - cash
        eff_tax_rate = min(abs(tax / pretax), 0.35) if pretax != 0 else CORP_TAX_RATE
        invested_capital = tot_eq + tot_debt - cash

        prev_close = float(close)
        annual_div_ps = abs(div_paid) / shares_inc if (shares_inc > 0 and div_paid and div_paid != 0) else 0
        p_e = (mkt_cap / net_inc) if net_inc > 0 else None
        p_b = (mkt_cap / tot_eq) if tot_eq > 0 else None
        p_s = (mkt_cap / rev) if rev > 0 else None
        p_fcf = (mkt_cap / fcf) if fcf > 0 else None
        ev_ebitda_val = (ev / ebitda) if ebitda > 0 else None
        roe = (net_inc / tot_eq) if tot_eq > 0 else None
        roa = (net_inc / tot_assets) if tot_assets > 0 else None
        roic = (net_inc / invested_capital) if invested_capital > 0 else None
        gross_m = (gp / rev) if rev > 0 else None
        op_m = (op_inc / rev) if rev > 0 else None
        net_m = (net_inc / rev) if rev > 0 else None
        d_e = (tot_debt / tot_eq) if tot_eq > 0 else None
        cur_r = (cur_assets / cur_liab) if cur_liab > 0 else None
        div_yield = (annual_div_ps / prev_close) if (prev_close > 0 and annual_div_ps > 0) else None
        payout_r = (abs(div_paid) / net_inc) if (net_inc > 0 and div_paid and div_paid != 0) else None

        # DQ guard (part 1): NULL derived pe/dividend_yield outside plausible
        # bounds. FMP-path only; SF-standalone remains bit-identical.
        _share_xcheck_failed = False
        if _DQ_GUARD_ENABLED:
            if p_e is not None and not (_DQ_PE_MIN <= p_e <= _DQ_PE_MAX):
                _DQ_LOG.append({
                    'ticker': ticker, 'field': 'pe', 'value': p_e,
                    'mkt_cap': mkt_cap, 'net_income': net_inc,
                    'shares_inc': shares_inc, 'div_paid': div_paid,
                    'reason': 'pe out of [-1000,1000]',
                })
                p_e = None
            if div_yield is not None and not (_DQ_DY_MIN <= div_yield <= _DQ_DY_MAX):
                _DQ_LOG.append({
                    'ticker': ticker, 'field': 'dividend_yield', 'value': div_yield,
                    'mkt_cap': mkt_cap, 'net_income': net_inc,
                    'shares_inc': shares_inc, 'div_paid': div_paid,
                    'reason': 'dividend_yield out of [0,0.5]',
                })
                div_yield = None
                annual_div_ps = 0
                payout_r = None

            # DQ guard (part 2): share-count cross-check. mkt_cap uses
            # sp_Shares_Outstanding (from prices file); shares_inc uses
            # Shares(Diluted) (from income file). When they disagree >50%,
            # at least one source is corrupted — NULL every share/mkt_cap
            # derived valuation ratio so ranking degrades rather than
            # runs on bad shares. Catches the class of ~10x-wrong share
            # counts that the <100K gross guard misses (e.g. MKC:
            # sp_Shares=15.3M vs SF diluted=269M -> pe=1.40 looked
            # plausible but mkt_cap was 17x too low; NTNX: SF diluted=245K
            # vs implied 268M -> ok mkt_cap, but silent for div paths).
            if shares_inc > 0 and prev_close > 0:
                implied_shares = mkt_cap / prev_close if mkt_cap > 0 else 0
                if implied_shares > 0:
                    _share_dev = abs(implied_shares / shares_inc - 1.0)
                    if _share_dev > 0.5:
                        _DQ_LOG.append({
                            'ticker': ticker, 'field': 'shares_xcheck',
                            'value': _share_dev,
                            'mkt_cap': mkt_cap, 'net_income': net_inc,
                            'shares_inc': shares_inc, 'div_paid': div_paid,
                            'reason': (
                                f'implied={implied_shares:.3g} vs sf_diluted='
                                f'{shares_inc:.3g} dev={_share_dev:.2f}'
                            ),
                        })
                        _share_xcheck_failed = True
                        # NULL every share- or mkt_cap-derived valuation ratio.
                        p_e = None
                        p_b = None
                        p_s = None
                        p_fcf = None
                        ev_ebitda_val = None
                        div_yield = None
                        annual_div_ps = 0
                        payout_r = None

        beta = betas.get(ticker, None)
        h = hist.get(ticker, {})

        industry = comp.get('Industry', '')
        sector = comp.get('Sector', '')
        sector_group = SECTOR_GROUP.get(industry, '')
        if not sector_group and industry:
            _unmapped_industries[industry] = _unmapped_industries.get(industry, 0) + 1

        row = {
            'ticker': ticker,
            'company': comp.get('Company Name', ''),
            'sector': sector,
            'industry': industry,
            'sector_group': sector_group,
            'price': round(prev_close, 2),
            'market_cap': round(mkt_cap, 0),
            'enterprise_value': round(ev, 0),
            'pe': round(p_e, 2) if p_e else None,
            'pb': round(p_b, 2) if p_b else None,
            'ps': round(p_s, 2) if p_s else None,
            'pfcf': round(p_fcf, 2) if p_fcf else None,
            'ev_ebitda': round(ev_ebitda_val, 2) if ev_ebitda_val else None,
            'revenue': round(rev, 0),
            'net_income': round(net_inc, 0),
            'fcf': round(fcf, 0),
            'ebitda': round(ebitda, 0),
            'gross_margin': round(gross_m, 4) if gross_m else None,
            'operating_margin': round(op_m, 4) if op_m else None,
            'net_margin': round(net_m, 4) if net_m else None,
            'roe': round(roe, 4) if roe else None,
            'roa': round(roa, 4) if roa else None,
            'roic': round(roic, 4) if roic else None,
            'debt_equity': round(d_e, 2) if d_e else None,
            'current_ratio': round(cur_r, 2) if cur_r else None,
            'dividend_yield': round(div_yield, 4) if div_yield else None,
            'payout_ratio': round(payout_r, 4) if payout_r else None,
            'revenue_growth_3yr': None,
            'beta': round(beta, 3) if beta else None,
            'realized_vol': vols.get(ticker) if vols else None,
            'high_52w': round(hi52w[ticker], 2) if ticker in hi52w else None,
            'low_52w': round(lo52w[ticker], 2) if ticker in lo52w else None,
            # DQ flag: shares cross-check failed -> mkt_cap/EV corrupt.
            # Propagated to compute_potential_scores where cap-derived
            # yields are masked. SF-only path never sets True (guard off).
            'dq_share_xcheck_failed': int(_share_xcheck_failed),
        }
        rev_g_hist = h.get('revenue_growth_3yr')
        row['revenue_growth_3yr'] = round(rev_g_hist, 4) if rev_g_hist is not None else None
        # Multi-year aggregates / stability / trend.
        for k in ('gross_margin_3y_med', 'operating_margin_3y_med', 'net_margin_3y_med',
                  'roe_3y_med', 'roic_3y_med', 'fcf_margin_3y_med',
                  'gross_margin_5y_med', 'operating_margin_5y_med', 'net_margin_5y_med',
                  'roe_5y_med', 'roic_5y_med', 'fcf_margin_5y_med',
                  'revenue_cv_3y', 'op_inc_cv_3y', 'revenue_cv_5y', 'op_inc_cv_5y',
                  'revenue_trend_5y', 'ebitda_trend_5y', 'fcf_trend_5y'):
            v = h.get(k)
            row[k] = round(v, 4) if (v is not None and np.isfinite(v)) else None
        row['n_yrs_history'] = h.get('n_yrs_history', 0)
        row['last_filing_date'] = h.get('last_filing_date')
        # §Phase 2 quality gates
        pf = h.get('piotroski_f')
        row['piotroski_f'] = int(pf) if pf is not None else None
        az = h.get('altman_z')
        row['altman_z'] = float(az) if az is not None else None
        pub_date = inc_row.get('Publish Date') if inc_row is not None else None
        if pub_date is not None:
            try:
                pd_dt = pd.Timestamp(pub_date).to_pydatetime()
                row['stale_fundamentals'] = int((ref_dt - pd_dt).days > STALE_DAYS)
                row['stale_last_pub_date'] = pd_dt.strftime('%Y-%m-%d')
            except Exception:
                row['stale_fundamentals'] = 1  # could not parse filing date — treat as stale
                row['stale_last_pub_date'] = None
        else:
            row['stale_fundamentals'] = 1  # unknown filing date — treat as stale
            row['stale_last_pub_date'] = None
        # Filing age: days since last filing date, computed via ref_dt so
        # backtest uses the period's cutoff instead of datetime.now().
        lfd = h.get('last_filing_date')
        if lfd is not None:
            try:
                lfd_dt = pd.Timestamp(lfd).to_pydatetime()
                row['filing_age_days'] = (ref_dt - lfd_dt).days
            except Exception:
                row['filing_age_days'] = None
        else:
            row['filing_age_days'] = None
        # TTM values + TTM valuation ratios (more current than annual).
        tt = ttm.get(ticker, {})
        rev_ttm = tt.get('revenue_ttm')
        ni_ttm = tt.get('net_income_ttm')
        fcf_ttm = tt.get('fcf_ttm')
        ebitda_ttm = tt.get('ebitda_ttm')
        row['revenue_ttm'] = round(rev_ttm, 0) if rev_ttm is not None else None
        row['net_income_ttm'] = round(ni_ttm, 0) if ni_ttm is not None else None
        row['fcf_ttm'] = round(fcf_ttm, 0) if fcf_ttm is not None else None
        row['ebitda_ttm'] = round(ebitda_ttm, 0) if ebitda_ttm is not None else None
        # Valuation ratios on TTM: only meaningful when denominator positive.
        row['pe_ttm'] = round(mkt_cap / ni_ttm, 2) if (ni_ttm and ni_ttm > 0) else None
        row['ps_ttm'] = round(mkt_cap / rev_ttm, 2) if (rev_ttm and rev_ttm > 0) else None
        row['pfcf_ttm'] = round(mkt_cap / fcf_ttm, 2) if (fcf_ttm and fcf_ttm > 0) else None
        row['ev_ebitda_ttm'] = round(ev / ebitda_ttm, 2) if (ebitda_ttm and ebitda_ttm > 0) else None
        # DQ guard: NULL pe_ttm / ps_ttm / pfcf_ttm / ev_ebitda_ttm outside
        # bounds or when the share cross-check failed above (mkt_cap is
        # suspect either way).
        if _DQ_GUARD_ENABLED:
            if row['pe_ttm'] is not None and (
                _share_xcheck_failed
                or not (_DQ_PE_MIN <= row['pe_ttm'] <= _DQ_PE_MAX)
            ):
                _DQ_LOG.append({
                    'ticker': ticker, 'field': 'pe_ttm', 'value': row['pe_ttm'],
                    'mkt_cap': mkt_cap, 'net_income': ni_ttm,
                    'shares_inc': shares_inc, 'div_paid': None,
                    'reason': ('share_xcheck_failed'
                               if _share_xcheck_failed
                               else 'pe_ttm out of [-1000,1000]'),
                })
                row['pe_ttm'] = None
            if _share_xcheck_failed:
                # mkt_cap-derived TTM ratios all suspect
                row['ps_ttm'] = None
                row['pfcf_ttm'] = None
                row['ev_ebitda_ttm'] = None
        # Phase 1.2 (revisited): YoY-quarterly revenue growth from quarterly
        # filings (requires only 5 quarters vs 3+ annuals — fills early-period
        # growth signal gap when annual-derived growth metrics are NaN).
        yoy_q = rev_yoy_q.get(ticker)
        row['revenue_growth_yoy_q'] = round(yoy_q, 4) if (yoy_q is not None and np.isfinite(yoy_q)) else None
        # Price momentum.
        mo = momo.get(ticker, {})
        for k in ('return_1m', 'return_3m', 'return_6m', 'return_12m',
                  'return_12m_minus_1m'):
            v = mo.get(k)
            row[k] = round(v, 4) if v is not None else None
        # Distance from 52w high: use canonical hi52w (gated on real recent activity)
        # rather than momentum's ffill'd max which produces 0.0 for stale/delisted tickers.
        hi = hi52w.get(ticker)
        row['distance_from_52w_high'] = (
            round(prev_close / hi - 1.0, 4) if (hi and hi > 0 and prev_close > 0) else None
        )
        # Ownership fields (insider_own, inst_own, short_float). Populated
        # from the ownership_live table when USE_FMP_OWNERSHIP=1; otherwise
        # the dict is empty and these stay NaN (sentiment renormalizes to
        # the price pair via _weighted_avg — see module docstring).
        fv = ownership.get(ticker, {})
        for k in ('insider_own', 'inst_own', 'short_float'):
            v = fv.get(k)
            row[k] = round(v, 4) if v is not None else None
        row['avg_dollar_volume_30d'] = round(dollar_vol, 0) if dollar_vol > 0 else None
        if dollar_vol > 50_000_000:
            row['liquidity_tier'] = 'large'
        elif dollar_vol >= 10_000_000:
            row['liquidity_tier'] = 'mid'
        elif dollar_vol >= MIN_DOLLAR_VOLUME:
            row['liquidity_tier'] = 'small'
        else:
            row['liquidity_tier'] = 'micro'
        rows.append(row)
        if (idx + 1) % 500 == 0:
            print(f'    Processed {idx + 1}/{total}')
    df_out = pd.DataFrame(rows)
    n_unknown_filing = ((df_out['stale_fundamentals'] == 1) & df_out['stale_last_pub_date'].isna()).sum()
    print(f'    Processed {len(rows)} stocks (Market Cap > ${MIN_MARKET_CAP:,}, Min Daily Vol ${MIN_DOLLAR_VOLUME:,})')
    print(f'    {n_unknown_filing} tickers have unknown filing date (defaulted to stale)')
    if _unmapped_industries:
        print()
        print(f'  Unmapped industries ({sum(_unmapped_industries.values())} stocks total):')
        sorted_unmapped = sorted(_unmapped_industries.items(),
                                 key=lambda x: x[1], reverse=True)
        for industry, count in sorted_unmapped:
            print(f'    {count:>4d}  {industry}')
    # Flush DQ guard log so scale regressions are caught loudly rather than
    # silently surfacing as top-ranked garbage.
    if _DQ_GUARD_ENABLED and _DQ_LOG:
        try:
            _dq_dir = Path('data/audit')
            _dq_dir.mkdir(parents=True, exist_ok=True)
            _dq_path = _dq_dir / f'data_quality_{ref_dt.strftime("%Y%m%d")}.csv'
            pd.DataFrame(_DQ_LOG).to_csv(_dq_path, index=False)
            by_field = pd.Series([r['field'] for r in _DQ_LOG]).value_counts().to_dict()
            print(f'  DQ GUARD: NULLed {len(_DQ_LOG)} implausible derived metrics '
                  f'({by_field}); offenders logged to {_dq_path}')
        except Exception as e:
            print(f'  DQ GUARD: failed to write log ({e}); {len(_DQ_LOG)} offenders NULLed in memory')
    # Publish DQ derived count at module level so main() can fold it into
    # HEALTH_JSON without changing the return signature (backtest/AB harnesses
    # unpack df_out only).
    global LAST_DQ_DERIVED_COUNT  # noqa: PLW0603
    LAST_DQ_DERIVED_COUNT = len(_DQ_LOG) if _DQ_GUARD_ENABLED else 0
    return df_out


# Populated by compute_snapshot on each call. Read by main()'s HEALTH_JSON
# emitter. Zero when the DQ guard is off (SF-only rollback path).
LAST_DQ_DERIVED_COUNT: int = 0

def _rank_within(values, sector_group, force_zero=None):
    # Percentile-rank `values` within each sector. NaN → NaN (skipped in aggregation).
    # force_zero entries get score 0 explicitly (had data but it's actively bad,
    # e.g. negative P/E — should not be treated as "infinitely cheap").
    out = pd.Series(np.nan, index=values.index, dtype=float)
    if force_zero is None:
        force_zero = pd.Series(False, index=values.index)
    rankable = values.notna() & ~force_zero
    if rankable.any():
        ranked = values.where(rankable).groupby(sector_group).rank(pct=True, method='average') * 100.0
        out.loc[rankable] = ranked.loc[rankable]
    out.loc[force_zero.fillna(False)] = 0.0
    return out

def _weighted_avg(score_df, weights, min_coverage=0.5):
    # Per-row weighted mean of available (non-NaN) components, weights renormalized.
    # If coverage < min_coverage (fraction of total weight represented by available
    # components), return NaN. This prevents sparse rows with one strong metric from
    # scoring higher than complete rows with mediocre metrics.
    w = pd.Series(weights, dtype=float)
    cols = [c for c in w.index if c in score_df.columns]
    if not cols:
        return pd.Series(np.nan, index=score_df.index, dtype=float)
    sub = score_df[cols]
    w = w[cols]
    total_weight = w.sum()
    mask = sub.notna()
    weight_sum = mask.mul(w, axis=1).sum(axis=1)
    coverage = weight_sum / total_weight
    weighted = sub.fillna(0).mul(w, axis=1).sum(axis=1)
    score = weighted / weight_sum.replace(0, np.nan)
    score = score.where(coverage >= min_coverage)
    return score

def compute_resampled_scores(sub_df, n_samples=None, seed=None):
    """Quantum Approach §Stage 1.1 — Monte Carlo resampling of POTENTIAL_WEIGHTS.
    For each of n_samples draws, sample (V,Q,G,S) weights uniformly from
    WEIGHT_BANDS, renormalize, compute the row-wise weighted composite over
    sub_df, and record the result. Returns (median, p05, p95, iqr,
    top_decile_pct) Series indexed like sub_df.
    `top_decile_pct` = fraction of draws (in %) in which the stock landed in
    the top decile of that draw — a robustness flag for weight sensitivity."""
    if n_samples is None:
        n_samples = N_RESAMPLES
    if seed is None:
        seed = RESAMPLE_SEED
    rng = np.random.default_rng(seed if seed else None)
    cols = ['valuation', 'quality', 'growth', 'sentiment']
    arr = sub_df[cols].to_numpy(dtype=float)
    n = arr.shape[0]
    samples = np.full((n_samples, n), np.nan, dtype=float)
    in_top = np.zeros(n, dtype=int)
    for it in range(n_samples):
        w = np.array([rng.uniform(WEIGHT_BANDS[c][0], WEIGHT_BANDS[c][1]) for c in cols])
        w = w / w.sum()
        valid = ~np.isnan(arr)
        w_row = np.where(valid, w[np.newaxis, :], 0.0)
        w_sum = w_row.sum(axis=1)
        contrib = np.where(valid, arr, 0.0) * w_row
        comp = np.divide(contrib.sum(axis=1), w_sum,
                         out=np.full(n, np.nan), where=(w_sum > 0.5))
        samples[it] = comp
        valid_comp = ~np.isnan(comp)
        if valid_comp.any():
            cutoff = np.nanpercentile(comp, 90)
            top_mask = valid_comp & (comp >= cutoff)
            in_top += top_mask.astype(int)
    median = np.nanmedian(samples, axis=0)
    p05 = np.nanpercentile(samples, 5, axis=0)
    p95 = np.nanpercentile(samples, 95, axis=0)
    p25 = np.nanpercentile(samples, 25, axis=0)
    p75 = np.nanpercentile(samples, 75, axis=0)
    iqr = p75 - p25
    top_pct = in_top / n_samples * 100.0
    idx = sub_df.index
    return (
        pd.Series(median, index=idx, name='potential_median'),
        pd.Series(p05, index=idx, name='potential_p05'),
        pd.Series(p95, index=idx, name='potential_p95'),
        pd.Series(iqr, index=idx, name='potential_iqr'),
        pd.Series(top_pct, index=idx, name='top_decile_pct'),
    )


def compute_impact_haircut(df):
    """Quantum Approach §Stage 1.3 — square-root market-impact law.
    impact_bps ≈ Y * sigma_daily * sqrt(Q / ADV) * 10000
    where sigma_daily = realized_vol / sqrt(252) (realized_vol annualized),
    Q = TARGET_POSITION_USD, ADV = avg_dollar_volume_30d.
    Reference: Tóth et al., Phys. Rev. X (2011); Almgren et al. (2005).
    Returns a Series in basis points; NaN if inputs missing."""
    vol_ann = pd.to_numeric(df.get('realized_vol'), errors='coerce')
    adv = pd.to_numeric(df.get('avg_dollar_volume_30d'), errors='coerce')
    sigma_daily = vol_ann / np.sqrt(252.0)
    q_over_adv = TARGET_POSITION_USD / adv.where(adv > 0, np.nan)
    haircut = IMPACT_Y * sigma_daily * np.sqrt(q_over_adv)
    haircut_bps = haircut * 10000.0
    return haircut_bps.where(haircut_bps.notna() & np.isfinite(haircut_bps))


def compute_potential_scores(df, verbose=True):
    # carrying magnitude — not a pure rank.
    print('  Computing potential scores...')
    df = df.copy()
    # Coerce all numeric columns to float64 — None/object dtype breaks arithmetic.
    numeric_cols = [c for c in df.columns if c not in ('ticker', 'company', 'sector', 'industry', 'sector_group', 'flags', 'n_yrs_history', 'stale_fundamentals', 'stale_last_pub_date', 'last_filing_date', 'filing_age_days', 'liquidity_tier')]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    sg = df['sector_group'].fillna('')
    mkt = df['market_cap'].astype(float)
    ev = df['enterprise_value'].astype(float)
    # DQ propagation: when the compute_snapshot share cross-check flagged
    # market_cap as corrupt (~17x wrong shares -> mkt_cap and EV both off
    # by the same factor), the display ratios (pe/pb/ps/pfcf/ev_ebitda +
    # TTM) are already NULLed at row build. But the scorer below recomputes
    # yields from raw mkt_cap / EV, so bad cap would silently pin
    # earnings/fcf/sales/ebitda yields high (17x cheap). Mask mkt/EV to
    # NaN for xcheck-failed rows so ey/fy/sy/eby become NaN too, and the
    # existing weighted-avg coverage floor (min_coverage=0.5) drops
    # valuation_score to NaN. book_yield already NaN via pb NULL upstream.
    # Non-xcheck rows are bit-identical.
    _xcheck_mask = pd.to_numeric(df.get('dq_share_xcheck_failed', 0),
                                 errors='coerce').fillna(0).astype(bool)
    if _xcheck_mask.any():
        mkt = mkt.where(~_xcheck_mask)
        ev = ev.where(~_xcheck_mask)

    # ---- VALUATION components (yield form — higher = cheaper = better). ----
    # earnings_yield = NI_ttm / market_cap. Force zero if NI<=0 (had data, just bad).
    ni = df['net_income_ttm']
    ey = (ni / mkt).where(mkt > 0)
    ey_fz = ni.notna() & (ni <= 0) & ~_xcheck_mask
    # fcf_yield = FCF_ttm / market_cap. Force zero on negative FCF.
    fcf = df['fcf_ttm']
    fy = (fcf / mkt).where(mkt > 0)
    fy_fz = fcf.notna() & (fcf <= 0) & ~_xcheck_mask
    # sales_yield = revenue_ttm / market_cap. Always positive when data exists.
    rev = df['revenue_ttm']
    sy = (rev / mkt).where(mkt > 0)
    # book_yield = 1/PB (only computed when book>0 upstream, so no force-zero needed).
    by = (1.0 / df['pb']).where(df['pb'].notna() & (df['pb'] > 0))
    # ebitda_yield = ebitda_ttm / EV. Force zero on negative ebitda. EV negative is
    # extremely rare (net-cash company with no debt) — treat as "missing" not "bad."
    ebt = df['ebitda_ttm']
    eby = (ebt / ev).where((ev > 0))
    eby_fz = ebt.notna() & (ebt <= 0) & (ev > 0) & ~_xcheck_mask
    val_scores = pd.DataFrame({
        'earnings_yield': _rank_within(ey, sg, ey_fz),
        'fcf_yield':      _rank_within(fy, sg, fy_fz),
        'sales_yield':    _rank_within(sy, sg),
        'book_yield':     _rank_within(by, sg),
        'ebitda_yield':   _rank_within(eby, sg, eby_fz),
    })
    # Apply sector-specific weights row by row.
    valuation = pd.Series(np.nan, index=df.index, dtype=float)
    valuation_all = pd.Series(np.nan, index=df.index, dtype=float)
    for sector_grp, idx in df.groupby('sector_group').groups.items():
        weights = SECTOR_VALUATION_WEIGHTS.get(sector_grp, DEFAULT_VALUATION_WEIGHTS)
        valuation.loc[idx] = _weighted_avg(val_scores.loc[idx], weights, min_coverage=0.5)
        valuation_all.loc[idx] = _weighted_avg(val_scores.loc[idx], weights, min_coverage=0.0)
    n_dropped = int(valuation_all.notna().sum() - valuation.notna().sum())
    if n_dropped > 0 and verbose:
        print(f'    Dropped {n_dropped} stocks from valuation_score (insufficient component coverage)')

    # ---- QUALITY components. ----
    q_roic = _rank_within(df['roic_3y_med'], sg)
    q_roe  = _rank_within(df['roe_3y_med'], sg)
    q_opm  = _rank_within(df['operating_margin_3y_med'], sg)
    q_fcfm = _rank_within(df['fcf_margin_3y_med'], sg)
    # low_leverage = 1/(1 + debt_equity). Cap d/e at 5 to avoid extreme outliers.
    de_capped = df['debt_equity'].clip(upper=5.0)
    low_lev = 1.0 / (1.0 + de_capped)
    q_lev = _rank_within(low_lev, sg)
    q_cur = _rank_within(df['current_ratio'].clip(upper=10.0), sg)
    # stability = 1/CV (inverse coefficient of variation). Floor CV to avoid div-by-zero.
    cv = df['revenue_cv_3y'].clip(lower=0.001)
    q_stab = _rank_within(1.0 / cv, sg)
    q_df = pd.DataFrame({
        'roic_3y_med': q_roic, 'roe_3y_med': q_roe,
        'operating_margin_3y_med': q_opm, 'fcf_margin_3y_med': q_fcfm,
        'low_leverage': q_lev, 'current_ratio': q_cur, 'stability': q_stab,
    })
    quality = _weighted_avg(q_df, QUALITY_WEIGHTS, min_coverage=0.5)
    quality_all = _weighted_avg(q_df, QUALITY_WEIGHTS, min_coverage=0.0)
    n_dropped = int(quality_all.notna().sum() - quality.notna().sum())
    if n_dropped > 0 and verbose:
        print(f'    Dropped {n_dropped} stocks from quality_score (insufficient component coverage)')

    # Mask trend metrics when history is thin, unknown, or fundamentals stale.
    trend_cols = ['revenue_trend_5y', 'ebitda_trend_5y', 'fcf_trend_5y']
    n_yrs = pd.to_numeric(df['n_yrs_history'], errors='coerce')
    stale = df['stale_fundamentals'].fillna(False).astype(bool)
    mask_to_clear = (n_yrs < 4) | n_yrs.isna() | stale
    for c in trend_cols:
        df.loc[mask_to_clear, c] = np.nan
    n_thin = ((n_yrs < 4) & ~stale).sum()
    n_unknown = n_yrs.isna().sum()
    n_stale = (stale & (n_yrs >= 4)).sum()
    if verbose:
        print(f'    Masked trend metrics: {n_thin} thin history (<4yr), '
              f'{n_unknown} unknown history, {n_stale} stale filings')

    # ---- GROWTH components. ----
    g_df = pd.DataFrame({
        'revenue_growth_3yr':   _rank_within(df['revenue_growth_3yr'], sg),
        'revenue_trend_5y':     _rank_within(df['revenue_trend_5y'], sg),
        'fcf_trend_5y':         _rank_within(df['fcf_trend_5y'], sg),
        'ebitda_trend_5y':      _rank_within(df['ebitda_trend_5y'], sg),
        'revenue_growth_yoy_q': _rank_within(df.get(
            'revenue_growth_yoy_q', pd.Series(np.nan, index=df.index)), sg),
    })
    # Phase 1.2: reduced coverage threshold so periods with short fundamental
    # history (early SimFin coverage) still compute a growth_score from
    # whatever subset is available rather than dropping the column entirely.
    growth = _weighted_avg(g_df, GROWTH_WEIGHTS, min_coverage=0.25)
    growth_all = _weighted_avg(g_df, GROWTH_WEIGHTS, min_coverage=0.0)
    n_dropped = int(growth_all.notna().sum() - growth.notna().sum())
    if n_dropped > 0 and verbose:
        print(f'    Dropped {n_dropped} stocks from growth_score (insufficient component coverage)')

    # ---- SENTIMENT components. ----
    # distance_from_52w_high: more negative = better → invert sign so higher is better.
    s_dist = _rank_within(-df['distance_from_52w_high'], sg)
    # return_12m_minus_1m: contrarian inverts (underperformance scores higher).
    momo_signal = df['return_12m_minus_1m']
    if CONTRARIAN_MODE:
        momo_signal = -momo_signal
    s_momo = _rank_within(momo_signal, sg)
    # short_float: heavy shorting INVERTED → low short_float scores higher.
    s_short = _rank_within(-df['short_float'], sg)
    # insider_own: higher = better.
    s_ins = _rank_within(df['insider_own'], sg)
    s_df = pd.DataFrame({
        'distance_from_52w_high': s_dist, 'return_12m_minus_1m': s_momo,
        'short_float': s_short, 'insider_own': s_ins,
    })
    sentiment = _weighted_avg(s_df, SENTIMENT_WEIGHTS, min_coverage=0.5)
    sentiment_all = _weighted_avg(s_df, SENTIMENT_WEIGHTS, min_coverage=0.0)
    n_dropped = int(sentiment_all.notna().sum() - sentiment.notna().sum())
    if n_dropped > 0 and verbose:
        print(f'    Dropped {n_dropped} stocks from sentiment_score (insufficient component coverage)')
    if verbose:
        n_with_own = df['insider_own'].notna().sum()
        pct = n_with_own / len(df) * 100
        print(f'    Ownership coverage: {n_with_own}/{len(df)} ({pct:.0f}%)  '
              f'(USE_FMP_OWNERSHIP path; 0% is expected default)')

    # ---- Combine into final POTENTIAL with sector-specific weights (§5.4). ----
    sub_df = pd.DataFrame({
        'valuation': valuation, 'quality': quality,
        'growth': growth, 'sentiment': sentiment,
    })
    raw_potential = pd.Series(np.nan, index=df.index, dtype=float)
    raw_potential_all = pd.Series(np.nan, index=df.index, dtype=float)
    for sector_grp, idx in df.groupby('sector_group').groups.items():
        weights = SECTOR_POTENTIAL_WEIGHTS.get(sector_grp, POTENTIAL_WEIGHTS)
        raw_potential.loc[idx]     = _weighted_avg(sub_df.loc[idx], weights, min_coverage=0.75)
        raw_potential_all.loc[idx] = _weighted_avg(sub_df.loc[idx], weights, min_coverage=0.0)
    n_dropped = int(raw_potential_all.notna().sum() - raw_potential.notna().sum())
    if n_dropped > 0 and verbose:
        print(f'    Dropped {n_dropped} stocks from potential_score (insufficient sub-score coverage)')
    final = raw_potential.copy()

    # ---- §Phase 2: hard-gate value traps via Piotroski F-score + Altman Z. ----
    # Within VALUE bucket (valuation_score > 70), exclude F<5 or Z<1.81.
    pf = pd.to_numeric(df.get('piotroski_f'), errors='coerce')
    az = pd.to_numeric(df.get('altman_z'), errors='coerce')
    value_bucket = valuation > 70
    bad_quality = (pf.notna() & (pf < 5)) | (az.notna() & (az < 1.81))
    trap_mask = (value_bucket & bad_quality).fillna(False)
    n_traps = int(trap_mask.sum())
    if n_traps and verbose:
        print(f'    §Phase 2 quality gate: nulled {n_traps} value-trap stocks '
              f'(value>70 with F<5 or Z<1.81)')
    final.loc[trap_mask] = np.nan

    # ---- §5.2: killer-KPI hard exclusions per sector. ----
    n_killed = 0
    for sg_name, rules in KILLER_KPI.items():
        sector_idx = df.index[df['sector_group'] == sg_name]
        if len(sector_idx) == 0:
            continue
        for col, op, threshold in rules:
            if col not in df.columns:
                continue
            vals = pd.to_numeric(df.loc[sector_idx, col], errors='coerce')
            if op == '>':
                kill_mask = vals > threshold
            elif op == '<':
                kill_mask = vals < threshold
            else:
                continue
            kill_idx = sector_idx[kill_mask.fillna(False)]
            n_killed += len(kill_idx)
            final.loc[kill_idx] = np.nan
    if n_killed and verbose:
        print(f'    §5.2 killer-KPI: nulled {n_killed} stocks failing sector-deal-breakers')

    # ---- §5.3: regime-conditional sector tilt overlay. ----
    regime = classify_regime_simple()
    tilts = REGIME_SECTOR_TILTS.get(regime, {})
    sg_series = df['sector_group'].fillna('')
    tilt_vec = sg_series.map(tilts).fillna(0).astype(float)
    final = (final + tilt_vec).clip(lower=0, upper=100)
    if verbose:
        active_tilts = {k: v for k, v in tilts.items() if v != 0}
        print(f'    §5.3 regime={regime} tilts applied: {active_tilts}')

    # ---- Quantum Approach §Stage 1.1: resampled scoring ("superposition"). ----
    # Resample (V,Q,G,S) weights uniformly within WEIGHT_BANDS over N_RESAMPLES
    # draws, recompute the composite each time, and report median / 5th-95th
    # percentile band / IQR / fraction in top decile. Stocks with high
    # top_decile_pct are robust to weight uncertainty; wide IQR = "in
    # superposition" (don't take the trade, or take smaller size).
    resamp_median, resamp_p05, resamp_p95, resamp_iqr, top_dec_pct = \
        compute_resampled_scores(sub_df, n_samples=N_RESAMPLES, seed=RESAMPLE_SEED)
    robust = (top_dec_pct >= ROBUST_TOP_DECILE_THRESHOLD * 100).astype(int)
    if verbose:
        n_robust = int(robust.sum())
        n_uncert = int(((top_dec_pct >= 50)
                        & (top_dec_pct < ROBUST_TOP_DECILE_THRESHOLD * 100)).sum())
        thr_pct = ROBUST_TOP_DECILE_THRESHOLD * 100
        print(f'    §Stage1.1 resampled scoring: {N_RESAMPLES} draws, '
              f'{n_robust} robust top-decile (>={thr_pct:.0f}%), '
              f'{n_uncert} uncertain (50-{thr_pct:.0f}%)')

    # ---- Quantum Approach §Stage 1.3: sqrt-law impact haircut ("observer"). ----
    # Subtract bps cost (converted to points) from final score so size-adjusted
    # alpha on thinly-traded names is penalized before ranking. Capped at
    # IMPACT_CAP_POINTS so a single illiquid microcap cannot zero the score.
    if IMPACT_ENABLED:
        impact_bps = compute_impact_haircut(df)
        impact_points = (impact_bps / 10.0).clip(upper=IMPACT_CAP_POINTS)
        final = (final - impact_points.fillna(0)).clip(lower=0, upper=100)
        if verbose:
            n_haircut = int(impact_bps.notna().sum())
            avg_bps = float(impact_bps.mean()) if n_haircut else 0.0
            max_bps = float(impact_bps.max()) if n_haircut else 0.0
            print(f'    §Stage1.3 impact haircut: {n_haircut} stocks, '
                  f'avg={avg_bps:.0f}bps, max={max_bps:.0f}bps '
                  f'(Q=${TARGET_POSITION_USD:,.0f}, Y={IMPACT_Y})')
    else:
        impact_bps = pd.Series(np.nan, index=df.index, dtype=float)
        impact_points = pd.Series(np.nan, index=df.index, dtype=float)

    # ---- Quantum Approach §Stage 2.6: crowding penalty via factor-ETF overlap ----
    # Stocks held in the top-N of multiple factor ETFs are crowded; OOS crash
    # probability is materially higher per Lee (2025). Apply linear penalty in
    # (count - 1) so being held by 1 ETF is neutral but doubling up costs.
    if CROWDING_ENABLED:
        _etf_holdings = load_factor_etf_holdings()
        crowd_count, crowd_flag = compute_crowding(df, _etf_holdings)
        if _etf_holdings:
            excess = (crowd_count.fillna(0) - 1).clip(lower=0)
            crowd_penalty = (excess * CROWDING_PENALTY_POINTS)
            final = (final - crowd_penalty).clip(lower=0, upper=100)
            if verbose:
                n_crowded = int((crowd_flag.fillna(0).astype(int) == 1).sum())
                print(f'    §Stage2.6 crowding: {n_crowded} stocks held by '
                      f'>={CROWDING_THRESHOLD} of {len(_etf_holdings)} factor ETFs '
                      f'(penalty -{CROWDING_PENALTY_POINTS}pts/extra ETF)')
        else:
            if verbose:
                print('    §Stage2.6 crowding: data/factor_etf_holdings.json missing '
                      '— run scripts/fetch_factor_etf_holdings.py to enable')
    else:
        crowd_count = pd.Series(np.nan, index=df.index, dtype=float)
        crowd_flag = pd.Series(pd.NA, index=df.index, dtype='Int64')

    out = df.copy()
    out['valuation_score'] = valuation.round(2)
    out['quality_score']   = quality.round(2)
    out['growth_score']    = growth.round(2)
    out['sentiment_score'] = sentiment.round(2)
    out['potential_score'] = final.round(2)
    out['regime'] = regime
    # §Stage 1.1 resampled-scoring outputs
    out['potential_median']    = resamp_median.round(2)
    out['potential_p05']       = resamp_p05.round(2)
    out['potential_p95']       = resamp_p95.round(2)
    out['potential_iqr']       = resamp_iqr.round(2)
    out['top_decile_pct']      = top_dec_pct.round(1)
    out['robust_pick']         = robust.astype('Int64')
    # §Stage 1.3 impact-haircut outputs
    out['impact_haircut_bps']    = impact_bps.round(1)
    out['impact_haircut_points'] = impact_points.round(2)
    # §Stage 2.6 crowding outputs
    out['crowding_count'] = crowd_count.round(0)
    out['crowded'] = crowd_flag

    # Drop scoring for sectors with insufficient population for ranking.
    # Below ~30 stocks, sector-relative percentiles become noise.
    MIN_SECTOR_POPULATION = 30
    sector_counts = df.groupby('sector_group').size()
    small_sectors = sector_counts[sector_counts < MIN_SECTOR_POPULATION].index.tolist()
    small_mask = df['sector_group'].isin(small_sectors) if small_sectors else pd.Series(False, index=df.index)
    if small_sectors:
        out.loc[small_mask, 'valuation_score'] = np.nan
        out.loc[small_mask, 'quality_score'] = np.nan
        out.loc[small_mask, 'growth_score'] = np.nan
        out.loc[small_mask, 'sentiment_score'] = np.nan
        out.loc[small_mask, 'potential_score'] = np.nan
        out.loc[small_mask, 'potential_median'] = np.nan
        out.loc[small_mask, 'potential_p05'] = np.nan
        out.loc[small_mask, 'potential_p95'] = np.nan
        out.loc[small_mask, 'potential_iqr'] = np.nan
        out.loc[small_mask, 'top_decile_pct'] = np.nan
        out.loc[small_mask, 'robust_pick'] = pd.NA
        if verbose:
            print(f'    Dropped scoring for sectors with <{MIN_SECTOR_POPULATION} '
                  f'stocks: {small_sectors} ({small_mask.sum()} stocks)')

    # ---- Phase 4: mispricing pattern flags. ----
    # For each flag every required input must be non-null; missing data should
    # not produce a flag. We build the masks with explicit notna() checks.
    v, q = valuation, quality
    r12 = df['return_12m']
    r6  = df['return_6m']
    rev_trend = df['revenue_trend_5y']
    dist = df['distance_from_52w_high']
    beta = df['beta']
    # Sector-relative percentiles: a -5% drawdown in utilities is extreme;
    # in biotech it's normal. Rank within sector so thresholds are comparable.
    dist_pct = pd.Series(np.nan, index=df.index, dtype=float)
    dist_mask = dist.notna()
    dist_pct.loc[dist_mask] = (
        dist.loc[dist_mask].groupby(df.loc[dist_mask, 'sector_group'])
            .rank(pct=True, method='average') * 100.0
    )
    r12_pct = pd.Series(np.nan, index=df.index, dtype=float)
    r12_mask = r12.notna()
    r12_pct.loc[r12_mask] = (
        r12.loc[r12_mask].groupby(df.loc[r12_mask, 'sector_group'])
            .rank(pct=True, method='average') * 100.0
    )
    # Sector-relative volatility percentile (lower = quieter).
    # Used as fallback when beta is NaN.
    vol = df['realized_vol']
    vol_pct = pd.Series(np.nan, index=df.index, dtype=float)
    vol_mask = vol.notna()
    vol_pct.loc[vol_mask] = (
        vol.loc[vol_mask].groupby(df.loc[vol_mask, 'sector_group'])
            .rank(pct=True, method='average') * 100.0
    )
    # Low-vol indicator: True if beta < 1.0, OR (beta NaN AND vol_pct < 40)
    # vol_pct < 40 means "in the bottom 40% of sector by realized vol"
    low_vol = (
        (beta < 1.0).fillna(False) |
        (beta.isna() & (vol_pct < 40).fillna(False))
    )
    def _and(*conditions):
        # AND across boolean Series, treating NaN as False.
        m = conditions[0].fillna(False)
        for c in conditions[1:]:
            m = m & c.fillna(False)
        return m
    # Per-flag sector eligibility based on backtested per-sector performance.
    # Sectors where the flag has demonstrated >= 50% hit rate AND positive alpha
    # across multiple periods. Sectors not listed: flag does NOT fire there.
    FLAG_SECTOR_ELIGIBILITY = {
        'MOMENTUM_VALUE': ['energy', 'industrials', 'consumer_staples',
                           'tech_hardware', 'non_bank_financial', 'reits'],
        'DEEP_VALUE': ['energy', 'industrials', 'non_bank_financial'],
        'QUIET_COMPOUNDER': None,  # all sectors (data too thin to restrict yet)
        'FALLEN_ANGEL': None,  # data too thin per-sector
        'AVOID_VALUE_TRAP': None,  # never fires anyway
        'OVEREXTENDED': ['reits'],  # only meaningful in REITs based on data
    }

    flag_masks = {
        # Cheap AND good — bread-and-butter value setup.
        'DEEP_VALUE':       _and(v.notna(), q.notna(), v > 80, q > 60),
        # Good company beaten down hard and now cheap — mean-reversion thesis.
        # r12_pct < 15: bottom 15% of sector by 12m return (sector-relative crash).
        'FALLEN_ANGEL':     _and(q.notna(), v.notna(), q > 70, r12_pct < 15, v > 60),
        # QUIET_COMPOUNDER: high quality, growing, low volatility.
        # "Low volatility" preferentially uses beta (cap-weighted market proxy),
        # but falls back to bottom-40% sector-relative realized volatility when
        # beta is NaN. This lets the flag fire in early backtest periods where
        # 60 days of post-cutoff returns aren't available.
        'QUIET_COMPOUNDER': _and(q.notna(), q > 80, rev_trend > 0.05,
                                 low_vol.astype(bool).rename(None)),
        # Cheap AND price already turning up — value with momentum confirmation.
        'MOMENTUM_VALUE':   _and(v.notna(), r6 > 0.10, v > 70),
        # Looks cheap but business is rotting — explicit negative warning.
        'AVOID_VALUE_TRAP': _and(v.notna(), q.notna(), v > 80, q < 30, rev_trend < 0),
        # Expensive AND near 52w high — fragile setup, drawdown risk elevated.
        # dist_pct > 85: top 15% of sector by closeness to high (sector-relative).
        'OVEREXTENDED':     _and(v.notna(), v < 20, dist_pct > 85),
    }
    # Vectorized flag assembly: build one Series per flag (empty string or
    # flag_name+','), then concatenate — no Python-level string ops per row.
    # Flag order in output follows flag_masks dict insertion order.
    for flag_name, mask in flag_masks.items():
        eligible_sectors = FLAG_SECTOR_ELIGIBILITY.get(flag_name)
        if eligible_sectors is not None:
            sector_eligible = df['sector_group'].isin(eligible_sectors).fillna(False)
            flag_masks[flag_name] = mask & sector_eligible
    flag_pieces = []
    for name, mask in flag_masks.items():
        mask = mask.fillna(False)
        flag_pieces.append(mask.map({True: name + ',', False: ''}))
    concat = flag_pieces[0]
    for fp in flag_pieces[1:]:
        concat = concat + fp
    out['flags'] = concat.str.rstrip(',').replace('', None)
    # §Stage 2.6 — append CROWDED tag for high-overlap names.
    if CROWDING_ENABLED:
        cw_mask = out['crowded'].fillna(0).astype(int) == 1
        if cw_mask.any():
            existing = out.loc[cw_mask, 'flags'].fillna('')
            appended = existing.where(existing == '', existing + ',') + 'CROWDED'
            out.loc[cw_mask, 'flags'] = appended
    if small_mask.any():
        out.loc[small_mask, 'flags'] = None
        out.loc[small_mask, 'crowding_count'] = np.nan
        out.loc[small_mask, 'crowded'] = pd.NA
    # Optional dev check: verify byte-for-byte match with old method
    # _old = pd.Series([''] * len(out), index=out.index, dtype=object)
    # for _name, _mask in flag_masks.items():
    #   _mask = _mask.fillna(False)
    #   _old = _old.where(~_mask, _old + ',' + _name)
    # _old_flags = _old.str.lstrip(',').replace('', None)
    # assert (_old_flags.fillna('') == out['flags'].fillna('')).all()

    # Filing freshness (already computed in compute_snapshot via ref_dt).
    fd = out['filing_age_days']
    if verbose and fd.notna().any():
        stale = (fd > 540).sum()
        ancient = (fd > 720).sum()
        print(f'    Filing freshness: {stale} stocks >18mo stale, {ancient} >24mo ancient')

    n_scored = out['potential_score'].notna().sum()
    n_flagged = out['flags'].notna().sum()
    if verbose:
        print(f'    Scored {n_scored}/{len(out)} stocks across {sg.nunique()} sector groups')
        print(f'    Flagged {n_flagged} stocks with mispricing patterns')
        for name in flag_masks:
            c = (out['flags'].fillna('').str.contains(name)).sum()
            print(f'      {name:18s} {c}')

    if verbose:
        print_correlations(out)

    return out

def save_cache(df):
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB))
    conn.executescript(CACHE_SCHEMA)
    df.to_sql('stocks', conn, if_exists='replace', index=False)
    conn.execute("INSERT OR REPLACE INTO cache_meta VALUES ('last_updated', ?)",
                 (datetime.now().isoformat(),))
    conn.execute("INSERT OR REPLACE INTO cache_meta VALUES ('schema_version', ?)",
                 (str(CACHE_SCHEMA_VERSION),))
    conn.commit()
    conn.close()
    print(f'  Cached {len(df)} stocks to {CACHE_DB}')
    print('  Stocks per sector_group:')
    for sg, n in df.groupby('sector_group').size().sort_values(ascending=False).items():
        print(f'    {sg or "(none)":20s}  {n:>4d}')

def load_cache():
    if not CACHE_DB.exists():
        return None
    conn = sqlite3.connect(str(CACHE_DB))
    try:
        meta = pd.read_sql("SELECT key, value FROM cache_meta", conn)
    except Exception:
        conn.close()
        return None
    if meta.empty:
        conn.close()
        return None
    meta_d = dict(zip(meta['key'], meta['value']))
    # Schema-version check. Missing or mismatched = stale, force rebuild.
    cached_ver = meta_d.get('schema_version')
    if cached_ver is None or str(cached_ver) != str(CACHE_SCHEMA_VERSION):
        print(f'  Cache schema_version={cached_ver!r} != current {CACHE_SCHEMA_VERSION}, rebuilding...')
        conn.close()
        return None
    last_upd_s = meta_d.get('last_updated')
    if not last_upd_s:
        conn.close()
        return None
    last_upd = datetime.fromisoformat(last_upd_s)
    age = datetime.now() - last_upd
    if age > timedelta(hours=24):
        print(f'  Cache is {int(age.total_seconds()/3600)}h old (>24h), refreshing...')
        conn.close()
        return None
    # Quantum §Stage 2 — invalidate cache if calibration / holdings refreshed.
    # Without this, decay weights or new crowding flags do not propagate until
    # the next 24h refresh; users running calibration mid-day stay stale.
    config_files = [
        DATA_DIR / 'factor_decay.json',
        DATA_DIR / 'factor_etf_holdings.json',
    ]
    last_upd_ts = last_upd.timestamp()
    for cf in config_files:
        if cf.exists() and cf.stat().st_mtime > last_upd_ts:
            print(f'  Cache stale — {cf.name} updated after last cache write, rebuilding...')
            conn.close()
            return None
    df = pd.read_sql("SELECT * FROM stocks", conn)
    conn.close()
    print(f'  Loaded {len(df)} stocks from cache (updated {last_upd.strftime("%Y-%m-%d %H:%M")})')
    return df

def _persist_flags_to_cache(df, db_path):
    """Write updated `flags` column back to the stocks table without touching
    schema or other columns. Silent no-op if DB is missing."""
    db_p = Path(str(db_path))
    if not db_p.exists() or 'flags' not in df.columns or 'ticker' not in df.columns:
        return
    try:
        conn = sqlite3.connect(str(db_p))
        payload = [
            (None if (f is None or (isinstance(f, float) and pd.isna(f))) else str(f), t)
            for t, f in zip(df['ticker'].tolist(), df['flags'].tolist())
        ]
        conn.executemany("UPDATE stocks SET flags=? WHERE ticker=?", payload)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'  Warning: could not persist flags to cache ({e})')


def _load_fmp_listing_oracle():
    """Load the FMP listing-status cache produced by
    ``scripts/fetch_fmp_listing_status.py``.  Returns
    ``(active_set, delisted_set, quote_probes, fetched_at)`` or
    ``(None, None, None, None)`` if the cache is missing or unreadable
    (heuristic-only fallback).
    """
    try:
        import json
        if not FMP_LISTING_STATUS_PATH.exists():
            return None, None, None, None
        data = json.loads(FMP_LISTING_STATUS_PATH.read_text())
        active = {str(s).upper() for s in data.get('active_symbols', []) if s}
        delisted = {str(s).upper() for s in data.get('delisted_symbols', []) if s}
        probes = {str(k).upper(): v for k, v in (data.get('quote_probes') or {}).items()}
        return active, delisted, probes, data.get('fetched_at')
    except Exception as e:
        print(f'  FMP oracle: failed to load {FMP_LISTING_STATUS_PATH.name} ({e}); '
              f'using heuristic only', file=sys.stderr)
        return None, None, None, None


def _oracle_status(ticker, active, delisted, probes, now_ts, max_age_sec):
    """Return one of 'ACTIVE' | 'DELISTED' | 'UNKNOWN' from the FMP oracle.

    Priority:
      1. On the FMP delisted list  -> DELISTED (authoritative).
      2. Fresh quote (ts within max_age)  -> ACTIVE.
      3. On the active symbol list but no fresh quote  -> UNKNOWN
         (oracle covers the symbol but cannot confirm liveness -> defer
         to heuristic).
      4. Not covered anywhere  -> UNKNOWN.

    Tries both dot and dash forms of the symbol to accommodate share-
    class formatting differences (e.g. BRK.B vs BRK-B).
    """
    if active is None:
        return 'UNKNOWN'
    variants = {ticker.upper()}
    if '.' in ticker:
        variants.add(ticker.upper().replace('.', '-'))
    if '-' in ticker:
        variants.add(ticker.upper().replace('-', '.'))
    for v in variants:
        if v in delisted:
            return 'DELISTED'
    for v in variants:
        rec = probes.get(v)
        if rec and rec.get('ts') is not None:
            try:
                age = now_ts - float(rec['ts'])
            except (TypeError, ValueError):
                age = None
            if age is not None and 0 <= age <= max_age_sec:
                return 'ACTIVE'
    return 'UNKNOWN'


def compute_liveness_and_flag(df, db_path):
    """Post-scoring liveness gate driven by (a) the FMP listing oracle when
    available and (b) prices_live (yfinance) plus SimFin filing_age as a
    heuristic fallback.  Additive, freeze-safe: does NOT touch scoring;
    only appends DELISTED / UNPRICED to the flags column and writes audit
    CSVs.  See docs/price_layer.md.

    Priority for a ticker that has no recent prices_live entry:
      1. FMP oracle delisted list  -> DELISTED (authoritative).
      2. FMP oracle active (fresh quote < 180d)  -> UNPRICED
         (live but this environment's yfinance cannot resolve it).
      3. Oracle UNKNOWN  -> heuristic: filing_age > 365 -> DELISTED,
         else -> UNPRICED.

    LIVE     : ticker's last_px within 10 calendar days of prices_live max.
    DELISTED : oracle-DELISTED OR (no/stale prices_live AND
               (oracle-UNKNOWN AND filing_age > 365)).
    UNPRICED : oracle-ACTIVE without prices_live OR (no/stale prices_live
               AND oracle-UNKNOWN AND recent filing).
    UNKNOWN  : coverage < 80% and ticker absent (refresh incomplete).
    STALE    : gap 10-30 days (no flag, no exclusion).
    Empty/missing prices_live -> loud stderr WARNING and skip.
    """
    if 'flags' not in df.columns:
        df['flags'] = None
    # Strip pre-existing DELISTED and UNPRICED so re-running is idempotent.
    _LIVENESS_LABELS = {'DELISTED', 'UNPRICED'}
    def _strip_liveness(s):
        if s is None:
            return None
        try:
            if pd.isna(s):
                return None
        except (TypeError, ValueError):
            pass
        parts = [p for p in str(s).split(',') if p and p not in _LIVENESS_LABELS]
        return ','.join(parts) if parts else None
    df['flags'] = df['flags'].map(_strip_liveness)

    # Loud-skip guard: when prices_live is missing or empty, the liveness
    # gate cannot mark any ticker DELISTED, so downstream delisting
    # protection is effectively OFF. Print a prominent WARNING to stderr
    # (not just stdout) so an operator cannot miss it and mistakenly rely
    # on a gate that is silently no-op. Refuse to no-op quietly.
    _LOUD = ('!!! LIVENESS GATE INACTIVE: prices_live {reason} '
             '- DELISTED flag NOT applied, delisting protection is OFF. '
             'Run: python scripts/refreshprice.py --top 0 --period 2y')
    db_p = Path(str(db_path))
    if not db_p.exists():
        msg = _LOUD.format(reason='cache DB not found')
        print(msg, file=sys.stderr)
        print(f'  {msg}')
        return df
    try:
        conn = sqlite3.connect(str(db_p))
        pl = pd.read_sql_query(
            "SELECT ticker, MAX(date) AS last_px FROM prices_live GROUP BY ticker",
            conn,
        )
        conn.close()
    except Exception as e:
        msg = _LOUD.format(reason=f'unavailable ({e})')
        print(msg, file=sys.stderr)
        print(f'  {msg}')
        return df
    if pl is None or pl.empty:
        msg = _LOUD.format(reason='empty')
        print(msg, file=sys.stderr)
        print(f'  {msg}')
        return df

    pl['last_px'] = pd.to_datetime(pl['last_px'], errors='coerce')
    pl = pl.dropna(subset=['last_px'])
    if pl.empty:
        msg = _LOUD.format(reason='has no parseable dates')
        print(msg, file=sys.stderr)
        print(f'  {msg}')
        return df

    table_max = pl['last_px'].max()
    scored_tickers = set(df.loc[df['potential_score'].notna(), 'ticker'].tolist())
    n_scored = len(scored_tickers)
    if n_scored == 0:
        coverage = 0.0
    else:
        coverage = len(scored_tickers & set(pl['ticker'].tolist())) / n_scored

    pl_map = dict(zip(pl['ticker'], pl['last_px']))
    coverage_ok = coverage >= 0.80

    # Filing-age is a WEAK discriminator on its own -- the SimFin freeze
    # makes stale filings unreliable evidence of delisting.  Use the FMP
    # oracle when available; fall back to the filing-age heuristic only
    # for tickers FMP does not cover.
    FILING_AGE_DELIST_MIN = 365
    age_map = dict(zip(df['ticker'], df.get('filing_age_days', pd.Series(dtype='float'))))

    active_set, delisted_set, quote_probes, oracle_fetched_at = _load_fmp_listing_oracle()
    oracle_available = active_set is not None
    if not oracle_available:
        print('  FMP oracle: cache missing at '
              f'{FMP_LISTING_STATUS_PATH.name}; using filing-age heuristic only. '
              'Run: python scripts/fetch_fmp_listing_status.py', file=sys.stderr)
    now_ts_epoch = datetime.now().timestamp()
    max_age_sec = FMP_QUOTE_ACTIVE_MAX_AGE_DAYS * 86400.0
    oracle_counts = {'ACTIVE': 0, 'DELISTED': 0, 'UNKNOWN': 0}

    def _has_stale_filing(t):
        age = age_map.get(t)
        if age is None:
            return True
        try:
            if pd.isna(age):
                return True
        except (TypeError, ValueError):
            pass
        try:
            return float(age) > FILING_AGE_DELIST_MIN
        except (TypeError, ValueError):
            return True

    def _oracle_lookup(t):
        if not oracle_available:
            return 'UNKNOWN'
        st = _oracle_status(t, active_set, delisted_set, quote_probes,
                            now_ts_epoch, max_age_sec)
        oracle_counts[st] = oracle_counts.get(st, 0) + 1
        return st

    def _resolve_no_price(t):
        """Ticker has no usable prices_live data. Decide DELISTED vs UNPRICED."""
        oracle_st = _oracle_lookup(t)
        if oracle_st == 'DELISTED':
            return 'DELISTED'
        if oracle_st == 'ACTIVE':
            # Oracle confirms trading; yfinance simply cannot resolve it here.
            return 'UNPRICED'
        # UNKNOWN: fall back to the filing-age heuristic.
        return 'DELISTED' if _has_stale_filing(t) else 'UNPRICED'

    def _classify(t):
        lp = pl_map.get(t)
        if lp is None:
            if not coverage_ok:
                return 'UNKNOWN'
            return _resolve_no_price(t)
        gap_days = (table_max - lp).days
        if gap_days <= 10:
            return 'LIVE'
        if gap_days > 30:
            return _resolve_no_price(t)
        return 'STALE'

    df['live_status'] = df['ticker'].map(_classify)
    # last_px_date as ISO string (safe for CSV / display).
    df['last_px_date'] = df['ticker'].map(
        lambda t: pl_map[t].date().isoformat() if t in pl_map else None
    )

    counts = df['live_status'].value_counts().to_dict()
    n_live = int(counts.get('LIVE', 0))
    n_del = int(counts.get('DELISTED', 0))
    n_unk = int(counts.get('UNKNOWN', 0))
    n_stale = int(counts.get('STALE', 0))
    n_unpriced = int(counts.get('UNPRICED', 0))

    # Coverage-floor visibility. 80% is the activation threshold for
    # "absent-as-DELISTED" semantics (kept unchanged); the block below only
    # adds loud visibility around it. Flag logic itself is untouched.
    if coverage < 0.80:
        floor_msg = (
            f'!!! LIVENESS COVERAGE BELOW FLOOR ({coverage*100:.1f}% < 80%): '
            f'absent tickers NOT treated as delisted - zombie protection '
            f'degraded. Run: python scripts/refreshprice.py --top 0 --period 2y'
        )
        print(floor_msg, file=sys.stderr)
        print(f'  {floor_msg}')
    elif coverage < 0.85:
        marginal_msg = (
            f'!! LIVENESS COVERAGE MARGINAL ({coverage*100:.1f}%): near the '
            f'80% floor - a weaker refresh will disable absent-as-delisted '
            f'semantics. Consider a fuller refresh: '
            f'python scripts/refreshprice.py --top 0 --period 2y'
        )
        print(marginal_msg, file=sys.stderr)
        print(f'  {marginal_msg}')

    if oracle_available:
        oracle_stamp = oracle_fetched_at or '?'
        print(f'  FMP oracle: fetched_at={oracle_stamp}  '
              f'active-quote-fresh={oracle_counts["ACTIVE"]}  '
              f'delisted-authoritative={oracle_counts["DELISTED"]}  '
              f'unknown-defer-to-heuristic={oracle_counts["UNKNOWN"]}')
    print(f'  Liveness: {n_live} live / {n_del} delisted (oracle+heuristic) / '
          f'{n_unpriced} unpriced (active, no local price) / '
          f'{n_unk} unknown (+{n_stale} stale-gap) '
          f'(prices_live coverage {coverage*100:.0f}%, table max {table_max.date()})')
    if oracle_available and n_unpriced > 10:
        print(f'  Note: {n_unpriced} UNPRICED names are FMP-confirmed active but '
              f'absent from prices_live. Consider a secondary price source '
              f'(e.g. FMP quote refresh, NYSE short-interest CSV) so these live '
              f'names can enter portfolios.')

    def _append_flag(mask, label):
        if not mask.any():
            return
        existing = df.loc[mask, 'flags'].fillna('')
        appended = existing.where(existing == '', existing + ',') + label
        df.loc[mask, 'flags'] = appended

    del_mask = df['live_status'] == 'DELISTED'
    unpriced_mask = df['live_status'] == 'UNPRICED'
    _append_flag(del_mask, 'DELISTED')
    _append_flag(unpriced_mask, 'UNPRICED')

    # Audit CSV for downstream reconciliation - one file each, so operators
    # can review the vendor-blind-spot list separately.
    try:
        audit_dir = DATA_DIR / 'audit'
        audit_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d')
        cols = ['ticker', 'last_px_date', 'filing_age_days', 'potential_score']
        for mask, label, fname in (
            (del_mask, 'DELISTED', f'delisted_exclusions_{stamp}.csv'),
            (unpriced_mask, 'UNPRICED', f'unpriced_review_{stamp}.csv'),
        ):
            if not mask.any():
                continue
            audit_path = audit_dir / fname
            audit_df = df.loc[mask, [c for c in cols if c in df.columns]].copy()
            audit_df = audit_df.sort_values('potential_score', ascending=False, na_position='last')
            audit_df.to_csv(audit_path, index=False)
            print(f'  Wrote {label} audit: {audit_path.name} ({int(mask.sum())} names)')
    except Exception as e:
        print(f'  Warning: failed to write liveness audit CSVs ({e})')

    return df


QUERY_FIELDS = {
    'pe': 'pe', 'p/e': 'pe', 'price': 'price', 'market cap': 'market_cap',
    'marketcap': 'market_cap', 'pb': 'pb', 'p/b': 'pb', 'ps': 'ps', 'p/s': 'ps',
    'pfcf': 'pfcf', 'p/fcf': 'pfcf', 'ev/ebitda': 'ev_ebitda', 'ev_ebitda': 'ev_ebitda',
    'revenue': 'revenue', 'net income': 'net_income', 'net_income': 'net_income',
    'fcf': 'fcf', 'ebitda': 'ebitda', 'roe': 'roe', 'roa': 'roa', 'roic': 'roic',
    'debt/equity': 'debt_equity', 'debt_equity': 'debt_equity', 'd/e': 'debt_equity',
    'current ratio': 'current_ratio', 'current_ratio': 'current_ratio',
    'dividend yield': 'dividend_yield', 'dividend': 'dividend_yield',
    'payout ratio': 'payout_ratio', 'payout_ratio': 'payout_ratio',
    'revenue growth': 'revenue_growth_3yr', 'revenue_growth_3yr': 'revenue_growth_3yr',
    'growth_3yr': 'revenue_growth_3yr',
    'beta': 'beta', 'enterprise value': 'enterprise_value',
    'ev': 'enterprise_value', 'gross margin': 'gross_margin', 'gross_margin': 'gross_margin',
    'operating margin': 'operating_margin', 'operating_margin': 'operating_margin',
    'net margin': 'net_margin', 'net_margin': 'net_margin',
    # Phase 3 sub-scores + final
    'potential': 'potential_score', 'potential_score': 'potential_score', 'pot': 'potential_score',
    'valuation': 'valuation_score', 'valuation_score': 'valuation_score',
    'quality': 'quality_score', 'quality_score': 'quality_score',
    'growth': 'growth_score', 'growth_score': 'growth_score',
    'sentiment': 'sentiment_score', 'sentiment_score': 'sentiment_score',
    # Phase 2 TTM ratios + momentum + ownership
    'pe_ttm': 'pe_ttm', 'ps_ttm': 'ps_ttm', 'pfcf_ttm': 'pfcf_ttm', 'ev_ebitda_ttm': 'ev_ebitda_ttm',
    'revenue_ttm': 'revenue_ttm', 'net_income_ttm': 'net_income_ttm',
    'fcf_ttm': 'fcf_ttm', 'ebitda_ttm': 'ebitda_ttm',
    'return_1m': 'return_1m', 'r1m': 'return_1m',
    'return_3m': 'return_3m', 'r3m': 'return_3m',
    'return_6m': 'return_6m', 'r6m': 'return_6m',
    'return_12m': 'return_12m', 'r12m': 'return_12m',
    'filing_age_days': 'filing_age_days', 'filing age': 'filing_age_days',
    'return_12m_minus_1m': 'return_12m_minus_1m', 'momentum': 'return_12m_minus_1m',
    'distance_from_52w_high': 'distance_from_52w_high', 'dist_52w': 'distance_from_52w_high',
    'insider_own': 'insider_own', 'insider': 'insider_own',
    'inst_own': 'inst_own', 'institutional': 'inst_own',
    'short_float': 'short_float', 'short': 'short_float',
    # Multi-year medians
    'roic_3y': 'roic_3y_med', 'roic_3y_med': 'roic_3y_med',
    'roe_3y': 'roe_3y_med', 'roe_3y_med': 'roe_3y_med',
    'roic_5y': 'roic_5y_med', 'roe_5y': 'roe_5y_med',
    'operating_margin_3y': 'operating_margin_3y_med',
    'fcf_margin_3y': 'fcf_margin_3y_med',
    'revenue_cv_3y': 'revenue_cv_3y', 'revenue_trend_5y': 'revenue_trend_5y',
    'fcf_trend_5y': 'fcf_trend_5y', 'ebitda_trend_5y': 'ebitda_trend_5y',
    '52w_high': 'high_52w', 'high_52w': 'high_52w',
    '52w_low': 'low_52w', 'low_52w': 'low_52w',
    # §Phase 2 quality gates
    'piotroski_f': 'piotroski_f', 'piotroski': 'piotroski_f', 'f_score': 'piotroski_f',
    'altman_z': 'altman_z', 'altman': 'altman_z', 'z_score': 'altman_z',
    'regime': 'regime',
}

FLAG_NAMES = ['DEEP_VALUE', 'FALLEN_ANGEL', 'QUIET_COMPOUNDER',
              'MOMENTUM_VALUE', 'AVOID_VALUE_TRAP', 'OVEREXTENDED',
              'DELISTED']

SECTOR_KEYWORDS = {
    'tech': 'tech_software', 'technology': 'tech_software', 'software': 'tech_software',
    'semiconductor': 'tech_hardware', 'hardware': 'tech_hardware', 'chip': 'tech_hardware',
    'bank': 'banks', 'financial': 'banks',
    'healthcare': 'healthcare', 'pharma': 'healthcare', 'biotech': 'healthcare',
    'medical': 'healthcare', 'drug': 'healthcare',
    'consumer disc': 'consumer_disc', 'consumer discretionary': 'consumer_disc',
    'retail': 'consumer_disc', 'auto': 'consumer_disc', 'restaurant': 'consumer_disc',
    'consumer staple': 'consumer_staples', 'consumer defensive': 'consumer_staples',
    'food': 'consumer_staples', 'beverage': 'consumer_staples',
    'energy': 'energy', 'oil': 'energy', 'gas': 'energy',
    'industrial': 'industrials', 'manufacturing': 'industrials',
    'aerospace': 'industrials', 'defense': 'industrials',
    'reit': 'reits', 'real estate': 'reits',
    'utility': 'utilities', 'utilities': 'utilities', 'electric': 'utilities',
}

def parse_query(text):
    t = text.lower().strip()
    sectors = set()
    for kw, sg in SECTOR_KEYWORDS.items():
        if kw in t:
            sectors.add(sg)
    # Flag detection: any FLAG_NAMES substring match (case-insensitive).
    flag_filters = [f for f in FLAG_NAMES if f.lower() in t]
    # Default sort: potential_score descending (best opportunity first). Override
    # only via explicit "sort by X" or specific aliases.
    sort_field = 'potential_score'
    sort_label = 'Potential'
    if 'sorted by' in t or 'sort by' in t:
        m = re.search(r'(?:sorted|sort)\s+by\s+([\w/]+(?:[ _][\w/]+)?)', t)
        if m:
            raw = m.group(1).strip().lower()
            # Prefer exact alias match; fall back to substring match.
            matched = QUERY_FIELDS.get(raw)
            if not matched:
                for name, field in QUERY_FIELDS.items():
                    if name in raw:
                        matched = field
                        break
            if matched:
                sort_field = matched
                sort_label = raw.title()
    # Special words: undervalued/cheap → ascending P/E etc; expensive → descending.
    if 'undervalued' in t or 'cheap' in t:
        sort_field = 'valuation_score'
        sort_label = 'Valuation'
    elif 'expensive' in t or 'overvalued' in t:
        sort_field = 'valuation_score'
        sort_label = 'Valuation'
    # Default direction: descending (biggest/best first). All current scoring
    # fields use the "higher = better" convention.
    sort_desc = True
    flip_tokens = ('reverse', 'desc', 'lowest', 'smallest')
    if any(k in t for k in flip_tokens):
        sort_desc = not sort_desc
    # 'expensive' on valuation_score means high valuation_score = cheap, so flip to low first.
    if 'expensive' in t or 'overvalued' in t:
        sort_desc = False  # ascending: low valuation score (= expensive) first
    # Liveness override: default is to hide DELISTED tickers from ranked output.
    # `include delisted` (or `with delisted`) turns the gate off for this query.
    include_delisted = bool(
        re.search(r'\binclude\s+delisted\b', t)
        or re.search(r'\bwith\s+delisted\b', t)
        or re.search(r'\bshow\s+delisted\b', t)
    )
    # If user explicitly filters `DELISTED` via the flag_filters path (e.g.
    # "DELISTED in energy"), they clearly want to see them.
    if 'DELISTED' in flag_filters:
        include_delisted = True
    filters = []
    if re.search(r'\bfresh\b', t):
        filters.append(('filing_age_days', '<', 365))
    if re.search(r'\bliquid\b', t):
        filters.append(('__tier_filter__', 'in', 'liquid'))
    if re.search(r'\btradeable\b', t):
        filters.append(('__tier_filter__', '=', 'tradeable'))
    pat = r'(\w+(?:\s*/\s*\w+)?)\s*(<=|>=|<|>|=)\s*(-?[\d,.]+[kKmMbB]?)'
    for m in re.finditer(pat, t):
        raw_field = m.group(1).lower().strip().replace(' ', '_')
        op = m.group(2)
        val_str = m.group(3).lower().replace(',', '')
        multiplier = 1
        if val_str.endswith('b'):
            multiplier = 1_000_000_000
        elif val_str.endswith('m'):
            multiplier = 1_000_000
        elif val_str.endswith('k'):
            multiplier = 1_000
        try:
            val = float(val_str.rstrip('kmb')) * multiplier
        except ValueError:
            continue
        field = QUERY_FIELDS.get(raw_field)
        if field is None:
            # Try original form with spaces (multi-word keys).
            field = QUERY_FIELDS.get(raw_field.replace('_', ' '))
        if field is not None:
            filters.append((field, op, val))
    limit = 20
    lm = re.search(r'\b(\d+)\s*(?:results?|rows?|stocks?|companies?)\b', t)
    if lm:
        limit = int(lm.group(1))
    elif 'top' in t:
        lm = re.search(r'top\s*(\d+)', t)
        if lm:
            limit = int(lm.group(1))
    return sectors, filters, flag_filters, sort_field, sort_label, sort_desc, limit, include_delisted

def execute_query(df, sectors, filters, flag_filters, sort_field, sort_label,
                  sort_desc, limit, include_delisted=False):
    result = df.copy()
    if sectors:
        result = result[result['sector_group'].isin(sectors)]
    # Default liveness gate: hide DELISTED unless caller opted in.
    if (not include_delisted) and 'flags' in result.columns:
        mask = result['flags'].fillna('').str.contains('DELISTED')
        result = result[~mask]
    # Apply flag filters: row matches if ALL requested flags are present in its flags column.
    if flag_filters and 'flags' in result.columns:
        for fname in flag_filters:
            result = result[result['flags'].fillna('').str.contains(fname)]
    for field, op, val in filters:
        if field == '__tier_filter__':
            if val == 'liquid':
                result = result[result['liquidity_tier'].isin(['large', 'mid'])]
            elif val == 'tradeable':
                result = result[result['liquidity_tier'] == 'large']
            continue
        if field not in result.columns:
            continue
        col = pd.to_numeric(result[field], errors='coerce')
        if op == '>':
            result = result[col > val]
        elif op == '<':
            result = result[col < val]
        elif op == '>=':
            result = result[col >= val]
        elif op == '<=':
            result = result[col <= val]
        elif op == '=':
            result = result[(col - val).abs() < 1e-6]
    if sort_field not in result.columns:
        sort_field = 'potential_score'
    result = result.sort_values(sort_field, ascending=not sort_desc, na_position='last')
    return result.head(limit)

def fmt_num(v, is_pct=False, is_dollar=False, is_ratio=False):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 'N/A'
    if pd.isna(v):
        return 'N/A'
    if not isinstance(v, (int, float, np.integer, np.floating)):
        return str(v)
    if abs(v) >= 1e12:
        return f'{v/1e12:.2f}T'
    if is_pct:
        return f'{v*100:.1f}%'
    if is_dollar:
        if abs(v) >= 1e9:
            return f'${v/1e9:.1f}B'
        elif abs(v) >= 1e6:
            return f'${v/1e6:.1f}M'
        else:
            return f'${v:,.0f}'
    if is_ratio:
        return f'{v:.1f}x'
    if isinstance(v, float):
        if abs(v) >= 1e9:
            return f'{v/1e9:.2f}B'
        elif abs(v) >= 1e6:
            return f'{v/1e6:.1f}M'
        elif abs(v) >= 1000:
            return f'{v:,.0f}'
        elif abs(v) < 0.01:
            return f'{v:.4f}'
        elif abs(v) < 1:
            return f'{v:.3f}'
        elif v == round(v, 0):
            return f'{v:,.0f}'
        else:
            return f'{v:,.2f}'
    return str(v)

COLUMNS_DISPLAY = [
    ('Ticker', 'ticker', str),
    ('Company', 'company', str),
    ('Sector', 'sector_group', str),
    ('POT', 'potential_score', 'num'),  # 1-100, sector-percentile-ranked
    ('VAL', 'valuation_score', 'num'),
    ('QLT', 'quality_score', 'num'),
    ('GRW', 'growth_score', 'num'),
    ('SNT', 'sentiment_score', 'num'),
    ('Price', 'price', 'price'),
    ('Mkt Cap', 'market_cap', 'dollar'),
    ('P/E TTM', 'pe_ttm', 'ratio'),
    ('P/B', 'pb', 'ratio'),
    ('EV/EBITDA', 'ev_ebitda_ttm', 'ratio'),
    ('ROIC 3Y', 'roic_3y_med', 'pct'),
    ('Rev Tr 5Y', 'revenue_trend_5y', 'pct'),
    ('Ret 12M', 'return_12m', 'pct'),
    ('Dist52w', 'distance_from_52w_high', 'pct'),
    ('Flags', 'flags', str),
]

def print_table(df, title='', sort_field='potential_score', sort_label='Potential', limit=20):
    cols = [c for c in COLUMNS_DISPLAY if c[1] in df.columns]
    width = 80 + 10 * len(cols)

    def _fmt_sample(v, typ):
        if typ == str:
            return str(v) if v is not None and not pd.isna(v) else ''
        return fmt_num(v, is_pct=(typ=='pct'), is_dollar=(typ=='dollar'), is_ratio=(typ=='ratio'))

    header_str = '  '
    col_widths = []
    for (name, field, typ) in cols:
        vals = df[field].dropna().head(20).tolist() if field in df.columns else []
        sample = [_fmt_sample(v, typ) for v in vals[:5]] + [name]
        max_w = max(len(str(s)) for s in sample)
        max_w = max(max_w, len(name), 8)
        max_w = min(max_w, 28)
        col_widths.append(max_w)
        h = name
        if field == sort_field:
            h = f'{h}v'
        header_str += f'{h:>{max_w}}  '
    print(f'  {"-" * (sum(col_widths) + 2 * len(col_widths) - 2)}')
    print(header_str)
    print(f'  {"-" * (sum(col_widths) + 2 * len(col_widths) - 2)}')
    count = 0
    for idx, row in df.iterrows():
        line = '  '
        for (name, field, typ), w in zip(cols, col_widths):
            val = row.get(field)
            if typ == 'pct':
                s = fmt_num(val, is_pct=True)
            elif typ == 'dollar':
                s = fmt_num(val, is_dollar=True)
            elif typ == 'ratio':
                s = fmt_num(val, is_ratio=True)
            elif typ == 'price':
                s = fmt_num(val, is_dollar=True)
            elif typ == str:
                s = str(val) if val else ''
            else:
                s = fmt_num(val)
            line += f'{s:>{w}}  '
        print(line)
        count += 1
        if count >= limit:
            break
    print(f'  {"-" * (sum(col_widths) + 2 * len(col_widths) - 2)}')
    print(f'  {count} results (sorted by {sort_label})')
    print()

def export_csv(df, filepath):
    os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
    df.to_csv(filepath, index=False)
    print(f'  Exported {len(df)} stocks to {filepath}')

def _pct_rank_in_sector(df, sector_group, field, value, higher_better=True):
    """Returns integer 0-100 percentile rank within sector_group, using
    pandas average-tie method to match _rank_within used in scoring.
    When higher_better=False, negates both value and column so lower raw
    values receive higher percentiles.

    NOTE: adds `value` to the population then ranks the combined series,
    which biases ranks slightly low for small sectors (n < 50). For most
    queries this is invisible."""
    if value is None or pd.isna(value):
        return None
    sub = pd.to_numeric(df[df['sector_group'] == sector_group][field], errors='coerce').dropna()
    if len(sub) == 0:
        return None
    if not higher_better:
        sub = -sub
        value = -value
    all_vals = pd.concat([sub, pd.Series([value])], ignore_index=True)
    ranks = all_vals.rank(pct=True, method='average') * 100.0
    return int(round(ranks.iloc[-1]))

# Per-sub-score component definitions for the `why` command. Each entry:
# (column_name, display_label, format_kind, higher_is_better).
# higher_is_better=False means the rank is computed on -value (so deeper drawdown
# scores higher, matching the model). Used by print_why.
WHY_COMPONENTS = {
    'VALUATION': [
        # earnings/fcf/sales/book yields aren't stored as columns — derive from
        # market_cap and TTM data when displaying.
        ('pe_ttm',          'P/E TTM',         'ratio', False),
        ('pb',              'P/B',             'ratio', False),
        ('ps_ttm',          'P/S TTM',         'ratio', False),
        ('pfcf_ttm',        'P/FCF TTM',       'ratio', False),
        ('ev_ebitda_ttm',   'EV/EBITDA TTM',   'ratio', False),
    ],
    'QUALITY': [
        ('roic_3y_med',             'ROIC 3y med',         'pct',   True),
        ('roe_3y_med',              'ROE 3y med',          'pct',   True),
        ('operating_margin_3y_med', 'Op Margin 3y med',    'pct',   True),
        ('fcf_margin_3y_med',       'FCF Margin 3y med',   'pct',   True),
        ('debt_equity',             'Debt/Equity',         'ratio', False),
        ('current_ratio',           'Current Ratio',       'ratio', True),
        ('revenue_cv_3y',           'Revenue CV 3y',       'num',   False),
        ('piotroski_f',             'Piotroski F (0-9)',   'num',   True),
        ('altman_z',                'Altman Z (>2.6 safe)','num',   True),
    ],
    'GROWTH': [
        ('revenue_growth_3yr', 'Rev Growth 3yr',  'pct', True),
        ('revenue_trend_5y',   'Rev Trend 5y',    'pct', True),
        ('fcf_trend_5y',       'FCF Trend 5y',    'pct', True),
        ('ebitda_trend_5y',    'EBITDA Trend 5y', 'pct', True),
    ],
    'SENTIMENT': [
        ('distance_from_52w_high', 'Dist from 52w High', 'pct', False),
        ('return_12m_minus_1m',    '12m-1m Momentum',    'pct', False),  # contrarian: inverted
        ('short_float',            'Short Float',        'pct', False),
        ('insider_own',            'Insider Own',        'pct', True),
    ],
}

def _mutual_information_pair(x, y, n_bins=12):
    """Plug-in MI estimator via 2D histogram. No sklearn dependency.
    Returns MI in nats. Works on continuous data; binned via equal-frequency.
    For Quantum Approach §Stage 2.4 — captures non-linear codependence that
    Pearson misses. Reference: Calsaverini & Vicente, EPL 88, 18001 (2009)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 30:
        return np.nan
    # Equal-frequency bin edges from quantiles (more robust than equal-width).
    try:
        xq = np.quantile(x, np.linspace(0, 1, n_bins + 1))
        yq = np.quantile(y, np.linspace(0, 1, n_bins + 1))
        # Force monotone bin edges
        xq = np.unique(xq); yq = np.unique(yq)
        if len(xq) < 3 or len(yq) < 3:
            return np.nan
        H, _, _ = np.histogram2d(x, y, bins=(xq, yq))
        Pxy = H / H.sum()
        Px = Pxy.sum(axis=1, keepdims=True)
        Py = Pxy.sum(axis=0, keepdims=True)
        # Sum where Pxy > 0
        nz = Pxy > 0
        with np.errstate(divide='ignore', invalid='ignore'):
            logterm = np.log(Pxy[nz] / (Px @ Py)[nz])
        mi = float((Pxy[nz] * logterm).sum())
        return mi if np.isfinite(mi) else np.nan
    except Exception:
        return np.nan


def _normalized_mi(x, y, n_bins=12):
    """MI normalized to [0,1] via min(H(X), H(Y)). Comparable to |Pearson|."""
    mi = _mutual_information_pair(x, y, n_bins=n_bins)
    if not np.isfinite(mi):
        return np.nan
    x = np.asarray(x, dtype=float)
    mask = np.isfinite(x)
    if mask.sum() < 30:
        return np.nan
    try:
        xq = np.quantile(x[mask], np.linspace(0, 1, n_bins + 1))
        xq = np.unique(xq)
        if len(xq) < 3:
            return np.nan
        hx, _ = np.histogram(x[mask], bins=xq)
        px = hx / hx.sum()
        px = px[px > 0]
        h = -float((px * np.log(px)).sum())
        return mi / h if h > 0 else np.nan
    except Exception:
        return np.nan


def print_correlations(df):
    sub_score_cols = ['valuation_score', 'quality_score',
                      'growth_score', 'sentiment_score']
    available = [c for c in sub_score_cols if c in df.columns]
    if len(available) < 2:
        print('  Not enough sub-score columns to compute correlations.')
        return
    corr = df[available].corr()
    print()
    print('  Sub-score correlation matrix (Pearson):')
    print(corr.round(2).to_string())
    for c1, c2 in itertools.combinations(available, 2):
        r = corr.loc[c1, c2]
        if abs(r) > 0.6:
            direction = 'positively' if r > 0 else 'negatively'
            print(f'    WARNING: {c1} and {c2} are {direction} '
                  f'correlated (r={r:.2f}). Effective weight is '
                  f'concentrated.')
    # Quantum Approach §Stage 2.4 — mutual information diagnostic.
    print()
    print('  Sub-score mutual-information matrix (normalized MI, 0-1):')
    short = {'valuation_score': 'V', 'quality_score': 'Q',
             'growth_score': 'G', 'sentiment_score': 'S'}
    hdr = '          ' + '   '.join(f'{short[c]:>5s}' for c in available)
    print(hdr)
    mi_mat = {}
    for c1 in available:
        row = '   ' + f'{short[c1]:>6s}  '
        for c2 in available:
            if c1 == c2:
                v = 1.00
            else:
                v = _normalized_mi(df[c1].values, df[c2].values)
            mi_mat[(c1, c2)] = v
            row += f'  {v:>5.2f}' if np.isfinite(v) else '    N/A'
        print(row)
    # Flag where nMI >> |Pearson| => non-linear coupling missed by Pearson.
    print('  Non-linear coupling flags (|nMI| - |Pearson| > 0.15):')
    flagged = False
    for c1, c2 in itertools.combinations(available, 2):
        nmi = mi_mat.get((c1, c2), np.nan)
        rho = abs(corr.loc[c1, c2])
        if np.isfinite(nmi) and (nmi - rho) > 0.15:
            print(f'    {short[c1]}-{short[c2]}: nMI={nmi:.2f}  |rho|={rho:.2f}  '
                  f'gap={nmi - rho:+.2f}  -> non-linear codependence')
            flagged = True
    if not flagged:
        print('    (none — Pearson captures available codependence)')

    print('  Per-sector highest |correlation|:')
    for sg in sorted(df['sector_group'].dropna().unique()):
        sub = df[df['sector_group'] == sg]
        if len(sub) >= 30:
            sg_corr = sub[available].corr()
            vals = sg_corr.where(
                np.triu(np.ones_like(sg_corr, dtype=bool), k=1)
            ).stack()
            if len(vals):
                top = vals.abs().idxmax()
                print(f'    {sg:18s}  {top[0]:18s} vs {top[1]:18s}'
                      f'  = {sg_corr.loc[top[0], top[1]]:+.2f}')


def print_why(df, ticker):
    ticker = ticker.upper()
    r = df[df['ticker'] == ticker]
    if r.empty:
        print(f'  Ticker {ticker} not found in cache.')
        return
    r = r.iloc[0]
    sg = r['sector_group']
    n_sector = (df['sector_group'] == sg).sum()
    print()
    print(f'  {"="*72}')
    print(f'  WHY {ticker} - {r.get("company", "")}')
    print(f'  {"="*72}')
    flags_str_top = r.get('flags')
    if flags_str_top and not pd.isna(flags_str_top) and 'DELISTED' in str(flags_str_top):
        lpd = r.get('last_px_date') if 'last_px_date' in r.index else None
        lpd_txt = f' (last live price {lpd})' if lpd else ''
        print(f'  !! DELISTED — no fresh price data{lpd_txt}. This ticker is '
              f'excluded from ranked output and all portfolios. See '
              f'data/audit/delisted_exclusions_*.csv.')
    print(f'  Sector: {sg or "(unmapped)"}  ({n_sector} stocks in sector)')
    pot = r.get('potential_score')
    if pot is not None and not pd.isna(pot):
        # potential_score is a weighted blend of four sector-relative sub-scores.
        print(f'  POTENTIAL: {pot:.1f}/100  (weighted sub-score blend, after haircut)')
    else:
        print(f'  POTENTIAL: N/A')
    # Quantum §Stage 1.1: resampled distribution under weight uncertainty.
    pm = r.get('potential_median'); p05 = r.get('potential_p05')
    p95 = r.get('potential_p95'); piqr = r.get('potential_iqr')
    tdp = r.get('top_decile_pct'); rob = r.get('robust_pick')
    if pm is not None and not pd.isna(pm):
        rob_tag = ' ROBUST' if (rob == 1 or rob == '1') else ''
        print(f'  Resampled ({N_RESAMPLES} draws): median={pm:.1f}  '
              f'p05={p05:.1f}  p95={p95:.1f}  IQR={piqr:.1f}  '
              f'in_top_decile={tdp:.0f}%{rob_tag}')
    # Quantum §Stage 1.3: sqrt-law impact haircut.
    ihb = r.get('impact_haircut_bps'); ihp = r.get('impact_haircut_points')
    if ihb is not None and not pd.isna(ihb):
        print(f'  Impact haircut: {ihb:.0f} bps  (-{ihp:.1f} points on potential)')
    # Quantum §Stage 2.6: factor-ETF crowding.
    cc = r.get('crowding_count'); cf = r.get('crowded')
    if cc is not None and not pd.isna(cc):
        tag = ' CROWDED' if (cf == 1 or cf == '1') else ''
        print(f'  Factor-ETF overlap: held by {int(cc)} ETF(s){tag}')
    fd = r.get('filing_age_days')
    if fd is not None and not pd.isna(fd):
        fd_int = int(round(fd))
        if fd_int > 720:
            print(f'  Last filing {fd_int} days ago (>24mo — FUNDAMENTALS STALE)')
        elif fd_int > 540:
            print(f'  Last filing {fd_int} days ago (>18mo — fundamentals stale)')
        else:
            print(f'  Last filing {fd_int} days ago')
    tier = r.get('liquidity_tier')
    dv = r.get('avg_dollar_volume_30d')
    if tier is not None and not pd.isna(tier):
        dv_str = f'${dv:,.0f}' if (dv is not None and not pd.isna(dv)) else '?'
        print(f'  Liquidity: {tier} (30d median daily vol {dv_str})')
    print()
    print(f'  Sub-scores (each rank-percentiled within sector, 0-100):')
    for sub_name, weight_key in (('VALUATION', 'valuation'), ('QUALITY', 'quality'),
                                  ('GROWTH', 'growth'), ('SENTIMENT', 'sentiment')):
        score_col = f'{sub_name.lower()}_score'
        v = r.get(score_col)
        w = POTENTIAL_WEIGHTS[weight_key]
        v_str = f'{v:>5.1f}' if v is not None and not pd.isna(v) else '  N/A'
        print(f'    {sub_name:10s}  {v_str}    weight={w:.0%}')
    print()
    # Per-component breakdown.
    for sub_name, components in WHY_COMPONENTS.items():
        print(f'  --- {sub_name} components ---')
        for col, label, kind, higher_better in components:
            val = r.get(col)
            if val is None or pd.isna(val):
                print(f'    {label:24s}  {"N/A":>10s}')
                continue
            val_f = float(val)
            rk = _pct_rank_in_sector(df, sg, col, val_f, higher_better=higher_better)
            star = '  *' if (rk is not None and rk >= 90) else ('  -' if (rk is not None and rk <= 10) else '')
            if kind == 'pct':
                v_disp = f'{val_f*100:>8.2f}%'
            elif kind == 'ratio':
                v_disp = f'{val_f:>8.2f}x'
            else:
                v_disp = f'{val_f:>9.3f}'
            rk_str = f'{rk:>3d}' if rk is not None else ' --'
            print(f'    {label:24s}  {v_disp}   rank {rk_str}{star}')
        print()
    ins = r.get('insider_own')
    sf = r.get('short_float')
    if (ins is None or pd.isna(ins)) and (sf is None or pd.isna(sf)):
        print('  (Ownership data unavailable — enable USE_FMP_OWNERSHIP=1 to populate)')
    flags = r.get('flags')
    print(f'  Flags: {flags if (flags and not pd.isna(flags)) else "(none)"}')
    n_yrs = r.get('n_yrs_history', 0)
    stale = r.get('stale_fundamentals', 0)
    last_pub = r.get('stale_last_pub_date')
    if stale or pd.isna(n_yrs) or n_yrs < 4:
        yrs_str = str(int(n_yrs)) if (n_yrs is not None and not pd.isna(n_yrs)) else '?'
        pub_str = str(last_pub) if (last_pub is not None and not pd.isna(last_pub)) else 'unknown'
        print(f'  WARNING: Limited fundamental history ({yrs_str} years, '
              f'last filing {pub_str}). Growth and quality sub-scores are less reliable.')
    # Consistency: verify _pct_rank_in_sector matches _rank_within method.
    _check_col = 'roic_3y_med'
    _check_val = r.get(_check_col)
    if _check_val is not None and not pd.isna(_check_val) and sg:
        _rk1 = _pct_rank_in_sector(df, sg, _check_col, _check_val)
        _sub = pd.to_numeric(df[df['sector_group'] == sg][_check_col], errors='coerce').dropna()
        _all = pd.concat([_sub, pd.Series([_check_val])], ignore_index=True)
        _rk2 = int(round((_all.rank(pct=True, method='average') * 100.0).iloc[-1]))
        if abs(_rk1 - _rk2) > 1:
            print(f'  WARNING: rank mismatch for {_check_col}: {_rk1} vs {_rk2}')
        assert _rk1 is None or 0 <= _rk1 <= 100, f'rank out of bounds: {_rk1}'
    # Also verify the inverted (lower-is-better) path, e.g. P/B.
    _check_col2 = 'pb'
    _check_val2 = r.get(_check_col2)
    if _check_val2 is not None and not pd.isna(_check_val2) and sg:
        _rk3 = _pct_rank_in_sector(df, sg, _check_col2, _check_val2, higher_better=False)
        _sub2 = pd.to_numeric(df[df['sector_group'] == sg][_check_col2], errors='coerce').dropna()
        _neg_sub = -_sub2
        _neg_val = -_check_val2
        _all2 = pd.concat([_neg_sub, pd.Series([_neg_val])], ignore_index=True)
        _rk4 = int(round((_all2.rank(pct=True, method='average') * 100.0).iloc[-1]))
        if abs(_rk3 - _rk4) > 1:
            print(f'  WARNING: inverted rank mismatch for {_check_col2}: {_rk3} vs {_rk4}')
        assert _rk3 is None or 0 <= _rk3 <= 100, f'rank out of bounds: {_rk3}'
    print()

def print_compare(df, tickers):
    tickers = [t.upper() for t in tickers]
    rows = []
    for t in tickers:
        rr = df[df['ticker'] == t]
        if rr.empty:
            print(f'  {t}: not found, skipping.')
            continue
        rows.append(rr.iloc[0])
    if not rows:
        return
    fields = [
        ('Sector',          'sector_group',         'str'),
        ('Price',           'price',                'dollar'),
        ('Mkt Cap',         'market_cap',           'dollar'),
        ('POTENTIAL',       'potential_score',      'num'),
        ('  Median',        'potential_median',     'num'),
        ('  IQR',           'potential_iqr',        'num'),
        ('  Top-decile %',  'top_decile_pct',       'num'),
        ('  Impact bps',    'impact_haircut_bps',   'num'),
        ('  Valuation',     'valuation_score',      'num'),
        ('  Quality',       'quality_score',        'num'),
        ('  Growth',        'growth_score',         'num'),
        ('  Sentiment',     'sentiment_score',      'num'),
        ('P/E TTM',         'pe_ttm',               'ratio'),
        ('P/B',             'pb',                   'ratio'),
        ('EV/EBITDA TTM',   'ev_ebitda_ttm',        'ratio'),
        ('ROIC 3y med',     'roic_3y_med',          'pct'),
        ('Op Margin 3y',    'operating_margin_3y_med', 'pct'),
        ('Rev Trend 5y',    'revenue_trend_5y',     'pct'),
        ('FCF Trend 5y',    'fcf_trend_5y',         'pct'),
        ('Return 12m',      'return_12m',           'pct'),
        ('Dist 52w High',   'distance_from_52w_high', 'pct'),
        ('Beta',            'beta',                 'num'),
        ('Filing Age',      'filing_age_days',      'days'),
        ('Flags',           'flags',                'str'),
    ]
    label_w = max(len(lbl) for lbl, _, _ in fields)
    col_w = max(12, max(len(r['ticker']) for r in rows) + 2)
    header = f'  {"":{label_w}s}  ' + '  '.join(f'{r["ticker"]:>{col_w}}' for r in rows)
    print()
    print(header)
    print(f'  {"-" * (label_w + (col_w + 2) * len(rows))}')
    for label, col, kind in fields:
        line = f'  {label:{label_w}s}  '
        cells = []
        for r in rows:
            v = r.get(col)
            if kind == 'str':
                cells.append(str(v) if v is not None and not pd.isna(v) else '')
            elif kind == 'pct':
                cells.append(fmt_num(v, is_pct=True) if v is not None else 'N/A')
            elif kind == 'ratio':
                cells.append(fmt_num(v, is_ratio=True) if v is not None else 'N/A')
            elif kind == 'dollar':
                cells.append(fmt_num(v, is_dollar=True) if v is not None else 'N/A')
            elif kind == 'days':
                cells.append(f'{int(v)}d' if (v is not None and not pd.isna(v)) else 'N/A')
            else:
                cells.append(fmt_num(v) if v is not None else 'N/A')
        line += '  '.join(f'{c:>{col_w}}' for c in cells)
        print(line)
    print()

# ============================================================================
# Unified v2 scorer (merged from former market_screener.v2.py). Quantum
# Approach Stage 1+2 wiring lives below: regime-probabilistic overlay,
# GICS sub-industry ranks, sector-specific metric tables, orthogonalization,
# quality gates (Piotroski/Altman/Beneish), and v2 composite scorer.
# ============================================================================
# ============================================================================
# Factor orthogonalization
# ============================================================================

def orthogonalize_factor_scores(valuation, quality, growth, sentiment, min_n=50):
    """Regress each sub-score on the other three; return orthogonalized series.
    Residual represents the 'pure' signal after removing common-factor overlap.
    Keeps original mean/std so scores remain on the same scale.
    """
    names = ['valuation_score', 'quality_score', 'growth_score', 'sentiment_score']
    scores = (valuation, quality, growth, sentiment)
    df = pd.concat({n: s for n, s in zip(names, scores)}, axis=1)

    result = {}
    for target, predictors in [
        ('valuation_score', ['quality_score', 'growth_score', 'sentiment_score']),
        ('quality_score', ['valuation_score', 'growth_score', 'sentiment_score']),
        ('growth_score', ['valuation_score', 'quality_score', 'sentiment_score']),
        ('sentiment_score', ['valuation_score', 'quality_score', 'growth_score']),
    ]:
        valid = df[[target] + predictors].dropna()
        if len(valid) < min_n:
            result[target] = df[target]
            continue

        y = valid[target].values
        X = np.column_stack([np.ones(len(valid)), valid[predictors].values])
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        residual = y - X @ beta

        orig_mean = df[target].mean()
        orig_std = df[target].std()
        res_std = np.std(residual)
        if res_std > 0:
            residual = residual / res_std * orig_std
        residual = residual + orig_mean - residual.mean()

        out = pd.Series(np.nan, index=df.index, dtype=float)
        out.loc[valid.index] = residual
        result[target] = out

    return (result['valuation_score'], result['quality_score'],
            result['growth_score'], result['sentiment_score'])


# ============================================================================
# GICS mapping: ticker -> GICS sub-industry code
# ============================================================================

def load_gics_for_tickers(companies_df):
    """Map SimFin IndustryId -> GICS code for each ticker."""
    companies = companies_df.reset_index()
    companies['IndustryId'] = pd.to_numeric(companies['IndustryId'], errors='coerce')

    ind_map = {}
    ind_csv = pd.read_csv(str(SIMFIN_DIR / 'industries.csv'), sep=';')
    for _, r in ind_csv.iterrows():
        ind_map[r['IndustryId']] = r['Industry']

    gics_codes = {}
    for _, comp in companies.iterrows():
        ticker = comp['Ticker']
        ind_id = comp.get('IndustryId')
        if ind_id is None or pd.isna(ind_id):
            continue
        ind_id = int(ind_id)
        gics_code = GICS_MAP.get(str(ind_id))
        if gics_code:
            gics_codes[ticker] = gics_code
        else:
            industry_name = ind_map.get(ind_id, '')
            gics_codes[ticker] = industry_name

    return gics_codes

def get_rank_group(gics_code, ticker_gics):
    """Determine ranking group for a ticker based on GICS code.
    Returns: (rank_key, group_label, level)
    Levels: 'sub_industry' (8-digit), 'industry' (6-digit), 'sector' (2-digit), 'fallback'
    """
    if not gics_code or len(gics_code) < 2:
        return ('FALLBACK', 'Fallback', 'fallback')

    code_str = str(gics_code).replace(' ', '')

    if len(code_str) >= 8:
        sub = code_str[:8]
        sub_count = sum(1 for c in ticker_gics.values() if str(c or '').startswith(sub))
        if sub_count >= RANK_GROUP_MIN:
            return (sub, sub, 'sub_industry')
        parent = code_str[:6]
        par_count = sum(1 for c in ticker_gics.values() if str(c or '').startswith(parent))
        if par_count >= RANK_GROUP_MIN:
            return (parent, parent, 'industry')
        sector = code_str[:2]
        return (sector, sector, 'sector')

    if len(code_str) >= 6:
        parent = code_str[:6]
        par_count = sum(1 for c in ticker_gics.values() if str(c or '').startswith(parent))
        if par_count >= RANK_GROUP_MIN:
            return (parent, parent, 'industry')
        sector = code_str[:2]
        return (sector, sector, 'sector')

    if len(code_str) >= 2:
        sec = code_str[:2]
        return (sec, sec, 'sector')

    return ('FALLBACK', 'Fallback', 'fallback')

# ============================================================================
# Sub-industry ranking (replaces _rank_within)
# ============================================================================

def _rank_within_subindustry(values, rank_keys, force_zero=None):
    """Percentile-rank values within GICS-determined peer groups.
    rank_keys: Series mapping index -> (rank_key, label, level) tuple.

    Falls back from sub-industry -> industry -> sector -> global.
    """
    if not isinstance(values, pd.Series):
        return pd.Series(np.nan, index=rank_keys.index, dtype=float)
    out = pd.Series(np.nan, index=values.index, dtype=float)
    if force_zero is None:
        force_zero = pd.Series(False, index=values.index)
    if not isinstance(force_zero, pd.Series):
        force_zero = pd.Series(False, index=values.index)

    rankable = values.notna() & ~force_zero
    if not rankable.any():
        return out

    rank_groups = rank_keys[rankable].apply(lambda x: x[0] if isinstance(x, tuple) else x)
    ranked = values.where(rankable).groupby(rank_groups).rank(pct=True, method='average') * 100.0
    out.loc[rankable] = ranked.loc[rankable]
    out.loc[force_zero.fillna(False)] = 0.0
    return out

# ============================================================================
# Sector-specific metric computations (new derived columns for compute_snapshot)
# ============================================================================

def compute_sector_metrics(df, balance=None, cashflow=None):
    """Add sector-specific derived columns to the DataFrame."""
    df = df.copy()
    mkt = df['market_cap'].astype(float)
    ni = df['net_income_ttm'].fillna(df.get('net_income', np.nan)).fillna(0).astype(float)
    equity = df.get('total_equity', pd.Series(np.nan, index=df.index))
    tot_assets = df.get('total_assets', pd.Series(np.nan, index=df.index))
    revenue = df['revenue_ttm'].fillna(df.get('revenue', np.nan)).fillna(0).astype(float)
    fcf = df['fcf_ttm'].fillna(df.get('fcf', np.nan)).fillna(0).astype(float)
    ebitda = df['ebitda_ttm'].fillna(df.get('ebitda', np.nan)).fillna(0).astype(float)
    ev = df.get('enterprise_value', pd.Series(0, index=df.index)).astype(float)

    # P/TBV (Tangible Book Value) - banks
    if balance is not None:
        bal = balance.reset_index().sort_values(['Ticker', 'Fiscal Year'])
        bal_latest = bal.groupby('Ticker').last()
        tbv = bal_latest.get('Total Equity', pd.Series(0)).fillna(0).astype(float)
        goodwill = bal_latest.get('Goodwill', pd.Series(0)).fillna(0).astype(float)
        intangibles = bal_latest.get('Intangible Assets', pd.Series(0)).fillna(0).astype(float)
        tbv_series = (tbv - goodwill - intangibles).clip(lower=0)
        df['ptbv'] = (mkt / tbv_series).where(tbv_series > 0)
        df['tbv_per_share'] = tbv_series / df.get('shares_outstanding', pd.Series(1, index=df.index)).clip(lower=1)
        df['tbv_growth_5y'] = None

        # CET1 approximation: equity / risk-weighted assets (use equity/assets as proxy)
        df['cet1_ratio'] = (equity / tot_assets).where(tot_assets > 0)
    else:
        df['ptbv'] = np.nan
        df['tbv_per_share'] = np.nan
        df['tbv_growth_5y'] = np.nan
        df['cet1_ratio'] = np.nan

    # ROTCE (Return on Tangible Common Equity) - banks
    # Uses net_income / tangible_common_equity
    if 'ptbv' in df.columns and df['ptbv'].notna().any():
        df['rotce'] = (ni / (equity.fillna(0) * 0.95)).where(equity > 0)
    df['nim'] = np.nan
    df['efficiency_ratio_inv'] = np.nan
    df['npl_coverage'] = np.nan

    # REITs: P/FFO, P/AFFO
    depr = df.get('depreciation_amortization', pd.Series(0, index=df.index)).astype(float)
    ffo = ni + depr
    df['p_ffo'] = (mkt / ffo).where(ffo > 0)
    df['p_affo'] = (mkt / (ffo * 0.9)).where(ffo > 0)
    affo_payout = df.get('dividends_paid', pd.Series(0, index=df.index)).astype(float).abs()
    df['affo_payout_ratio_inv'] = 1.0 - (affo_payout / ffo.abs().clip(lower=1)).clip(upper=0.95)
    df['affo_payout_ratio'] = (affo_payout / ffo.abs().clip(lower=1)).clip(upper=1.0)
    df['same_store_noi_growth'] = df.get('revenue_growth_3yr', np.nan)
    bvps = df.get('book_value_per_share', pd.Series(1, index=df.index)).fillna(1).clip(lower=1)
    df['premium_to_nav'] = (df.get('price', 0) / bvps).clip(upper=3.0)
    df['occupancy'] = np.nan
    df['walt'] = np.nan
    df['ffo_revision_90d'] = df.get('eps_revision_90d', np.nan)

    # Energy: EV/EBITDAX, ROCE
    rd = df.get('rnd_expense', pd.Series(0, index=df.index)).astype(float)
    ebitdax = ebitda + rd
    df['ev_ebitdax'] = (ev / ebitdax).where(ebitdax > 0)
    df['roce_3y'] = df.get('roic_3y_med', np.nan)
    df['production_growth'] = df.get('revenue_growth_3yr', np.nan)
    df['reserve_life'] = np.nan
    df['breakeven_wti'] = np.nan
    df['cash_margin_per_boe'] = np.nan

    # SaaS: Rule of 40, EV/Sales, NRR, ARR growth
    fcf_margin = (fcf / revenue).where(revenue > 0)
    rev_growth = df.get('revenue_growth_3yr', pd.Series(0, index=df.index))
    df['rule_of_40'] = (rev_growth * 100) + (fcf_margin * 100)
    df['ev_sales'] = (ev / revenue).where(revenue > 0)
    df['nrr'] = np.nan
    df['arr_growth'] = df.get('revenue_growth_3yr', np.nan)
    df['organic_revenue_growth'] = df.get('revenue_growth_3yr', np.nan)

    # Insurance: Combined ratio, BV growth
    df['combined_ratio_inv'] = np.nan
    df['book_value_growth'] = df.get('revenue_growth_3yr', np.nan)
    df['aum_growth'] = df.get('revenue_growth_3yr', np.nan)

    # General: Backlog metrics, debt/EBITDA inverse
    df['backlog_revenue'] = np.nan
    df['backlog_growth'] = np.nan
    debt = df.get('total_debt', df.get('debt', pd.Series(0, index=df.index))).astype(float)
    df['debt_ebitda'] = (debt / ebitda.abs().clip(lower=1)).where(ebitda.abs() > 0)
    df['debt_ebitda_inv'] = 1.0 / df['debt_ebitda'].clip(lower=0.01, upper=100)
    df['book_to_bill'] = np.nan

    # Capital markets
    df['loan_growth'] = df.get('revenue_growth_3yr', np.nan)

    # Pipeline / biotech
    df['cash_runway'] = np.nan
    df['pipeline_progress'] = np.nan
    df['rnd_efficiency'] = np.nan
    df['pipeline_npv'] = np.nan

    # Utilities
    df['allowed_roe'] = np.nan
    df['rate_base_growth'] = df.get('revenue_growth_3yr', np.nan)
    df['capacity_growth'] = np.nan
    df['eps_growth_5y'] = df.get('revenue_growth_3yr', np.nan)

    # Retail
    df['sss_growth'] = df.get('revenue_growth_3yr', np.nan)
    df['inventory_turns'] = np.nan
    df['organic_growth'] = df.get('revenue_growth_3yr', np.nan)

    # Tech
    df['services_mix_growth'] = np.nan
    df['ev_mix_growth'] = np.nan
    df['subscriber_growth'] = df.get('revenue_growth_3yr', np.nan)
    df['content_roi'] = np.nan
    df['user_growth'] = df.get('revenue_growth_3yr', np.nan)
    df['dau_mau'] = np.nan
    df['sub_net_adds'] = np.nan

    # Utilities yield proxy
    div_yield = df.get('dividend_yield', pd.Series(0, index=df.index))
    df['dividend_yield_inv'] = (1.0 / div_yield.clip(lower=0.001)).where(div_yield > 0)

    # Stability (low leverage proxy - already in v1 but add as named column)
    de = df.get('debt_equity', pd.Series(0, index=df.index)).clip(upper=5.0)
    df['low_leverage'] = 1.0 / (1.0 + de)

    # Stock-based comp as % of revenue
    sbc = df.get('stock_based_comp', pd.Series(0, index=df.index)).astype(float)
    df['sbc_pct_revenue'] = (sbc / revenue).where(revenue > 0)

    return df

# ============================================================================
# Negative-earnings handling
# ============================================================================

def handle_negative_earnings(df):
    """For tickers with negative net income, use EV/Sales and Rule-of-40
    instead of P/E-based valuation. Rank in a separate 'early_stage' bucket.
    """
    df = df.copy()
    ni = df['net_income_ttm'].fillna(df.get('net_income', np.nan)).fillna(0).astype(float)
    negative_ni = ni.notna() & (ni <= 0)
    df.loc[negative_ni, 'valuation_override'] = 'ev_sales_rule_of_40'
    df.loc[~negative_ni, 'valuation_override'] = 'standard'
    return df

# ============================================================================
# Quality Gates: Piotroski F-score, Altman Z-score, Beneish M-score
# ============================================================================

def compute_piotroski_fscore(row):
    """Compute 9-point Piotroski F-score from financial data.
    Returns integer 0-9. Higher = better fundamental strength.

    Tests (Piotroski 2000, JAR):
    1. Positive net income (ROA)
    2. Positive operating cash flow
    3. Rising ROA (YoY)
    4. Cash flow from ops > net income (accruals quality)
    5. Lower leverage (long-term debt ratio fell YoY)
    6. Higher current ratio (YoY)
    7. No share dilution (gross shares outstanding)
    8. Higher gross margin (YoY)
    9. Higher asset turnover (YoY)
    """
    score = 0
    try:
        ni = float(row.get('net_income', 0) or 0)
        ocf = float(row.get('operating_cash_flow', 0) or 0)
        tot_assets = float(row.get('total_assets', 0) or 0)
        lt_debt = float(row.get('long_term_debt', 0) or 0)
        cur_assets = float(row.get('current_assets', 0) or 0)
        cur_liab = float(row.get('current_liabilities', 0) or 0)
        revenue = float(row.get('revenue', 0) or 0)
        gross_profit = float(row.get('gross_profit', 0) or 0)
        shares_out = float(row.get('shares_outstanding', 0) or 0)

        ocf_prev = float(row.get('ocf_prev_year', 0) or 0)
        ni_prev = float(row.get('ni_prev_year', 0) or 0)
        lt_debt_prev = float(row.get('lt_debt_prev_year', 0) or 0)
        cur_ratio_prev = float(row.get('cur_ratio_prev_year', 0) or 0)
        gm_prev = float(row.get('gm_prev_year', 0) or 0)
        turnover_prev = float(row.get('turnover_prev_year', 0) or 0)
        shares_prev = float(row.get('shares_prev_year', 0) or 0)
        tot_assets_prev = float(row.get('tot_assets_prev_year', 0) or 0)

        # 1. Positive NI
        if ni > 0: score += 1

        # 2. Positive OCF
        if ocf > 0: score += 1

        # 3. Rising ROA
        roa = ni / tot_assets if tot_assets > 0 else 0
        roa_prev = ni_prev / tot_assets_prev if tot_assets_prev > 0 else 0
        if roa > roa_prev: score += 1

        # 4. CFO > NI (accruals)
        if ocf > ni: score += 1

        # 5. Lower leverage
        lever = lt_debt / tot_assets if tot_assets > 0 else 0
        lever_prev = lt_debt_prev / tot_assets_prev if tot_assets_prev > 0 else 0
        if lever < lever_prev: score += 1

        # 6. Higher current ratio
        cur_ratio = cur_assets / cur_liab if cur_liab > 0 else 0
        if cur_ratio > cur_ratio_prev: score += 1

        # 7. No dilution
        if shares_out <= shares_prev or shares_prev == 0: score += 1

        # 8. Higher gross margin
        gm = gross_profit / revenue if revenue > 0 else 0
        if gm > gm_prev: score += 1

        # 9. Higher asset turnover
        turnover = revenue / tot_assets if tot_assets > 0 else 0
        if turnover > turnover_prev: score += 1

    except (ValueError, TypeError, ZeroDivisionError):
        pass

    return score


def compute_altman_zscore(row):
    """Compute Altman Z-score (manufacturing) or Z-score for non-manufacturing.
    Returns Z value. Z > 2.99 = safe, Z < 1.81 = distress.

    Z = 1.2A + 1.4B + 3.3C + 0.6D + 1.0E
    Where: A = WC/TA, B = RE/TA, C = EBIT/TA, D = MVE/TL, E = Sales/TA

    For non-manufacturers (Z''): Z'' = 6.56A + 3.26B + 6.72C + 1.05D
    """
    try:
        wc = float(row.get('working_capital', 0) or 0)
        ta = float(row.get('total_assets', 0) or 0)
        re = float(row.get('retained_earnings', 0) or 0)
        ebit = float(row.get('operating_income', 0) or 0)
        mve = float(row.get('market_cap', 0) or 0)
        tl = float(row.get('total_liabilities', 0) or 0)
        sales = float(row.get('revenue', 0) or 0)

        if ta <= 0:
            return None

        a = wc / ta
        b = re / ta
        c = ebit / ta
        d = mve / tl if tl > 0 else 0
        e = sales / ta

        z = 1.2 * a + 1.4 * b + 3.3 * c + 0.6 * d + 1.0 * e
        return z
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def compute_beneish_mscore(row):
    """Compute Beneish M-score for earnings manipulation detection.
    M > -2.22 suggests possible manipulation.
    Uses 8 variables: DSRI, GMI, AQI, SGI, DEPI, SGAI, LVGI, TATA.
    """
    try:
        recv = float(row.get('receivables', 0) or 0)
        rev = float(row.get('revenue', 0) or 0)
        recv_prev = float(row.get('recv_prev_year', 0) or 0)
        rev_prev = float(row.get('rev_prev_year', 0) or 0)
        cogs = float(row.get('cogs', 0) or 0)
        cogs_prev = float(row.get('cogs_prev_year', 0) or 0)
        ta = float(row.get('total_assets', 0) or 0)
        ta_prev = float(row.get('tot_assets_prev_year', 0) or 0)
        ppe = float(row.get('ppe', 0) or 0)
        ppe_prev = float(row.get('ppe_prev_year', 0) or 0)
        depr = float(row.get('depreciation', 0) or 0)
        depr_prev = float(row.get('depr_prev_year', 0) or 0)
        sgna = float(row.get('sga_expense', 0) or 0)
        sgna_prev = float(row.get('sga_prev_year', 0) or 0)
        lt_debt = float(row.get('long_term_debt', 0) or 0)
        lt_debt_prev = float(row.get('lt_debt_prev_year', 0) or 0)
        cur_assets = float(row.get('current_assets', 0) or 0)
        cur_liab = float(row.get('current_liabilities', 0) or 0)
        cash = float(row.get('cash', 0) or 0)
        cl_prev = float(row.get('cl_prev_year', 0) or 0)
        cash_prev = float(row.get('cash_prev_year', 0) or 0)
        ni = float(row.get('net_income', 0) or 0)
        ocf = float(row.get('operating_cash_flow', 0) or 0)

        if rev_prev <= 0 or ta_prev <= 0:
            return None

        dsri = (recv / rev) / (recv_prev / rev_prev) if rev > 0 and rev_prev > 0 else 1.0
        gmi = (cogs_prev / rev_prev) / (cogs / rev) if rev > 0 and cogs > 0 and rev_prev > 0 else 1.0
        aqi = (1 - (cur_assets + ppe + cash) / ta) / (1 - (cur_assets + cash + ppe_prev) / ta_prev) if ta > 0 else 1.0
        sgi = rev / rev_prev if rev_prev > 0 else 1.0
        depi = (depr_prev / (ppe_prev + depr_prev)) / (depr / (ppe + depr)) if (ppe + depr) > 0 else 1.0
        sgai = (sgna / rev) / (sgna_prev / rev_prev) if rev > 0 and rev_prev > 0 else 1.0
        lvgi = ((lt_debt + cl_prev) / ta) / ((lt_debt_prev + cl_prev) / ta_prev) if ta > 0 and ta_prev > 0 else 1.0
        tata = (ni - ocf) / ta if ta > 0 else 0

        m_score = (
            -4.840 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi
            + 0.892 * sgi + 0.115 * depi - 0.172 * sgai
            + 4.679 * tata - 0.327 * lvgi
        )
        return m_score
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def apply_quality_gates(df):
    """Apply quality gate filters to the DataFrame."""
    df = df.copy()

    df['piotroski_fscore'] = df.apply(compute_piotroski_fscore, axis=1)
    df['altman_zscore'] = df.apply(compute_altman_zscore, axis=1)
    df['beneish_mscore'] = df.apply(compute_beneish_mscore, axis=1)

    df['quality_gate'] = 'pass'
    low_f = df['piotroski_fscore'].notna() & (df['piotroski_fscore'] < 5)
    df.loc[low_f, 'quality_gate'] = 'fail_piotroski'
    low_z = df['altman_zscore'].notna() & (df['altman_zscore'] < 1.81)
    df.loc[low_z, 'quality_gate'] = 'fail_altman'
    high_m = df['beneish_mscore'].notna() & (df['beneish_mscore'] > -2.22)
    df.loc[high_m, 'quality_gate'] = 'fail_beneish'

    return df


def apply_hard_excludes(df):
    """Apply sector-specific hard exclusion rules from config.
    Hard-excluded stocks get their potential_score set to NaN.
    """
    df = df.copy()
    gics_codes_df = df.get('gics_code', pd.Series(None, index=df.index))
    exclude_mask = pd.Series(False, index=df.index)

    for idx in df.index:
        gics = gics_codes_df.get(idx)
        if not gics:
            continue

        cfg_entry = None
        gics_str = str(gics)
        if gics_str in GICS_LOOKUP:
            cfg_entry = GICS_LOOKUP[gics_str]
        elif len(gics_str) >= 6 and gics_str[:6] in GICS_LOOKUP:
            cfg_entry = GICS_LOOKUP[gics_str[:6]]

        if cfg_entry is None:
            continue

        hard_ex = cfg_entry.get('hard_exclude', {})
        if not hard_ex:
            continue

        for field, rule in hard_ex.items():
            if field not in df.columns:
                continue
            val = df.loc[idx, field]
            if val is None or pd.isna(val):
                continue
            try:
                val_f = float(val)
                op = rule.get('op', '>')
                threshold = rule.get('value', 0)
                if op == '>':
                    if val_f > threshold:
                        exclude_mask[idx] = True
                elif op == '<':
                    if val_f < threshold:
                        exclude_mask[idx] = True
            except (ValueError, TypeError):
                continue

    df['hard_excluded'] = exclude_mask
    return df

# ============================================================================
# Regime classifier (Layer 4)
# ============================================================================

def classify_regime(ism=None, curve_10y2y=None, core_cpi_yoy=None, hy_oas=None):
    """Classify current macro regime — probabilistic (smooth transitions).
    Returns: (dominant_label, prob_dict) where prob_dict sums to 1.
    """
    regimes = {
        'R1': REGIME_CFG.get('regimes', {}).get('R1', {}),
        'R2': REGIME_CFG.get('regimes', {}).get('R2', {}),
        'R3': REGIME_CFG.get('regimes', {}).get('R3', {}),
        'R4': REGIME_CFG.get('regimes', {}).get('R4', {}),
        'R5': REGIME_CFG.get('regimes', {}).get('R5', {}),
    }

    ism = ism or 52.7
    curve = curve_10y2y or 0.58
    cpi = core_cpi_yoy or 0.028
    oas = hy_oas or 350

    # Normalise inputs to [0, 1]
    ism_n = np.clip((ism - 30) / 40, 0, 1)
    cpi_n = np.clip(cpi / 0.10, 0, 1)
    curve_n = np.clip((curve + 2) / 4, 0, 1)
    oas_n = np.clip(oas / 1000, 0, 1)
    state = np.array([ism_n, cpi_n, curve_n, oas_n])

    # Centroids estimated from YAML thresholds
    centroids = {
        'R1': (0.62, 0.20, 0.60, 0.25),
        'R2': (0.62, 0.40, 0.60, 0.35),
        'R3': (0.50, 0.30, 0.30, 0.40),
        'R4': (0.30, 0.20, 0.50, 0.70),
        'R5': (0.40, 0.25, 0.70, 0.30),
    }

    dists = {k: np.sqrt(np.sum((state - np.array(v)) ** 2))
             for k, v in centroids.items()}
    temp = 0.3
    dist_arr = np.array(list(dists.values()))
    probs = np.exp(-dist_arr / temp) / np.sum(np.exp(-dist_arr / temp))
    prob_dict = dict(zip(centroids.keys(), np.round(probs, decimals=3).tolist()))

    dominant = max(prob_dict, key=prob_dict.get)

    return dominant, prob_dict


def apply_probabilistic_overlay(df, prob_dict):
    """Apply regime-overlay blending proportional to regime probabilities.

    For each ticker, contribution from each regime = weight * regime_config.
    Factor-modulation weights are blended as probability-weighted averages
    (dot product: probs × {V: ..., } across all regimes).
    Sector tilts are accumulated with probability-weighted magnitudes.
    """
    df = df.copy()
    all_regimes = REGIME_CFG.get('regimes', {})

    # Blended factor weights
    v_w = 0.0
    q_w = 0.0
    g_w = 0.0
    s_w = 0.0
    gics_codes = df.get('gics_code', pd.Series(None, index=df.index))
    tilt_vector = pd.Series(0.0, index=df.index)

    for label, prob in prob_dict.items():
        if prob <= 1e-6:
            continue
        cfg = all_regimes.get(label, {})
        fm = cfg.get('factor_modulation', {})
        v_w += prob * fm.get('V', 0.30)
        q_w += prob * fm.get('Q', 0.25)
        g_w += prob * fm.get('G', 0.25)
        s_w += prob * fm.get('S', 0.15)

        st = cfg.get('sector_tilts', {})
        tilt_mag = prob * cfg.get('tilt_magnitude', 5) / 100.0
        for ow in st.get('overweight', []):
            mask = gics_codes.astype(str).str.startswith(str(ow), na=False)
            tilt_vector += mask.astype(float) * tilt_mag
        for uw in st.get('underweight', []):
            mask = gics_codes.astype(str).str.startswith(str(uw), na=False)
            tilt_vector -= mask.astype(float) * tilt_mag

    dominant = max(prob_dict, key=prob_dict.get)
    df['regime_tilt'] = tilt_vector
    df['regime_label'] = dominant
    df['regime_v_weight'] = np.round(v_w, 4)
    df['regime_q_weight'] = np.round(q_w, 4)
    df['regime_g_weight'] = np.round(g_w, 4)
    df['regime_s_weight'] = np.round(s_w, 4)

    return df

# ============================================================================
# Core scoring engine (replaces compute_potential_scores)
# ============================================================================



def compute_potential_scores_v2(df, gics_map=None, verbose=True):
    """V2 scoring: sub-industry percentile ranks with sector-specific metrics and weights.
    Implements the 5-layer architecture:
    Layer 1: Universe (pre-filtered)
    Layer 2: GICS sub-industry classification
    Layer 3: Composite score (sector-specific weights, sub-industry ranks)
    Layer 4: Regime overlay (applied later)
    Layer 5: Risk screens (applied later)
    """
    if verbose:
        print('  Computing v2 potential scores (sub-industry ranked)...')
    df = df.copy()

    numeric_cols = [c for c in df.columns if c not in (
        'ticker', 'company', 'sector', 'industry', 'sector_group', 'flags',
        'n_yrs_history', 'stale_fundamentals', 'stale_last_pub_date',
        'last_filing_date', 'filing_age_days', 'liquidity_tier',
        'gics_code', 'valuation_override', 'quality_gate', 'hard_excluded',
        'regime_tilt', 'regime_label', 'regime_v_weight', 'regime_q_weight',
        'regime_g_weight', 'regime_s_weight',
    )]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')

    # Layer 2: Build ranking groups from GICS codes
    gics_codes = {}
    ticker_list = df['ticker'].tolist() if 'ticker' in df.columns else []
    if gics_map:
        gics_codes = {t: gics_map.get(t) for t in ticker_list}

    rank_keys = pd.Series(index=df.index, dtype=object)
    for i, row in df.iterrows():
        ticker = row.get('ticker', '')
        gics = gics_codes.get(ticker, row.get('gics_code', ''))
        rank_keys[i] = get_rank_group(gics, gics_codes)
    df['rank_key'] = rank_keys.apply(lambda x: x[0] if isinstance(x, tuple) else 'FALLBACK')

    sg = df['rank_key']
    mkt = df['market_cap'].astype(float)
    ev = df['enterprise_value'].astype(float)
    # DQ propagation (same as v1 path): xcheck-failed rows -> mask cap
    # so cap-derived yields become NaN. See compute_potential_scores.
    _xcheck_mask = pd.to_numeric(df.get('dq_share_xcheck_failed', 0),
                                 errors='coerce').fillna(0).astype(bool)
    if _xcheck_mask.any():
        mkt = mkt.where(~_xcheck_mask)
        ev = ev.where(~_xcheck_mask)

    # ---- VALUATION components (yield form) ----
    ni = df['net_income_ttm'].fillna(df.get('net_income', np.nan)).fillna(0).astype(float)
    fcf = df['fcf_ttm'].fillna(df.get('fcf', np.nan)).fillna(0).astype(float)
    rev = df['revenue_ttm'].fillna(df.get('revenue', np.nan)).fillna(0).astype(float)
    ebit = df['ebitda_ttm'].fillna(df.get('ebitda', np.nan)).fillna(0).astype(float)
    pb = df.get('pb', pd.Series(np.nan, index=df.index)).astype(float)

    ey = (ni / mkt).where(mkt > 0)
    ey_fz = ni.notna() & (ni <= 0) & ~_xcheck_mask
    fy = (fcf / mkt).where(mkt > 0)
    fy_fz = fcf.notna() & (fcf <= 0) & ~_xcheck_mask
    sy = (rev / mkt).where(mkt > 0)
    by = (1.0 / pb).where(pb.notna() & (pb > 0))
    eby = (ebit / ev).where(ev > 0)
    eby_fz = ebit.notna() & (ebit <= 0) & (ev > 0) & ~_xcheck_mask

    # Sector-specific valuation metrics
    ptbv = df.get('ptbv', pd.Series(np.nan, index=df.index))
    ptbv_yield = (1.0 / ptbv).where(ptbv.notna() & (ptbv > 0))
    p_ffo_inv = df.get('p_ffo', pd.Series(np.nan, index=df.index))
    p_ffo_yield = (1.0 / p_ffo_inv).where(p_ffo_inv.notna() & (p_ffo_inv > 0))
    ev_sales_inv = df.get('ev_sales', pd.Series(np.nan, index=df.index))
    ev_sales_yield = (1.0 / ev_sales_inv).where(ev_sales_inv.notna() & (ev_sales_inv > 0))

    # Separate early-stage (negative earnings) from standard
    neg_mask = df.get('valuation_override', pd.Series('standard', index=df.index)) == 'ev_sales_rule_of_40'
    standard_mask = ~neg_mask

    val_scores = pd.DataFrame(index=df.index)
    val_scores['earnings_yield'] = _rank_within_subindustry(ey, rank_keys, ey_fz)
    val_scores['fcf_yield'] = _rank_within_subindustry(fy, rank_keys, fy_fz)
    val_scores['sales_yield'] = _rank_within_subindustry(sy, rank_keys)
    val_scores['book_yield'] = _rank_within_subindustry(by, rank_keys)
    val_scores['ebitda_yield'] = _rank_within_subindustry(eby, rank_keys, eby_fz)

    if ptbv_yield.notna().any():
        val_scores['ptbv_yield'] = _rank_within_subindustry(ptbv_yield, rank_keys)
    if p_ffo_yield.notna().any():
        val_scores['p_ffo_yield'] = _rank_within_subindustry(p_ffo_yield, rank_keys)
    if ev_sales_yield.notna().any():
        val_scores['ev_sales_yield'] = _rank_within_subindustry(ev_sales_yield, rank_keys)

    val_scores['rotce'] = _rank_within_subindustry(df.get('rotce', np.nan), rank_keys)
    val_scores['rule_of_40'] = _rank_within_subindustry(df.get('rule_of_40', np.nan), rank_keys)
    for col in ['cet1_ratio', 'efficiency_ratio_inv', 'affo_payout_ratio_inv',
                'combined_ratio_inv', 'debt_ebitda_inv', 'low_leverage', 'dividend_yield_inv',
                'ev_ebitdax']:
        if col in df.columns and df[col].notna().any():
            val_scores[col] = _rank_within_subindustry(df[col], rank_keys)

    # ---- QUALITY components ----
    q_cols = {
        'roic_3y_med': df.get('roic_3y_med', np.nan),
        'roe_3y_med': df.get('roe_3y_med', np.nan),
        'operating_margin_3y_med': df.get('operating_margin_3y_med', np.nan),
        'fcf_margin_3y_med': df.get('fcf_margin_3y_med', np.nan),
        'gross_margin': df.get('gross_margin', np.nan),
        'low_leverage': df.get('low_leverage', np.nan),
        'current_ratio': df.get('current_ratio', np.nan),
        'stability': 1.0 / df.get('revenue_cv_3y', pd.Series(np.nan, index=df.index)).clip(lower=0.001),
        'debt_ebitda_inv': df.get('debt_ebitda_inv', np.nan),
        'backlog_revenue': df.get('backlog_revenue', np.nan),
    }
    q_df = pd.DataFrame(index=df.index)
    for col_name, col_data in q_cols.items():
        if isinstance(col_data, pd.Series) and col_data.notna().any():
            q_df[col_name] = _rank_within_subindustry(pd.to_numeric(col_data, errors='coerce'), rank_keys)

    # ---- GROWTH components ----
    trend_cols = ['revenue_trend_5y', 'ebitda_trend_5y', 'fcf_trend_5y']
    n_yrs = pd.to_numeric(df.get('n_yrs_history', 0), errors='coerce')
    stale = df.get('stale_fundamentals', pd.Series(False)).fillna(False).astype(bool)
    mask_to_clear = (n_yrs < 4) | n_yrs.isna() | stale
    for c in trend_cols:
        if c in df.columns:
            df.loc[mask_to_clear, c] = np.nan

    g_df = pd.DataFrame(index=df.index)
    growth_fields = {
        'revenue_growth_3yr': df.get('revenue_growth_3yr', np.nan),
        'revenue_trend_5y': df.get('revenue_trend_5y', np.nan),
        'fcf_trend_5y': df.get('fcf_trend_5y', np.nan),
        'ebitda_trend_5y': df.get('ebitda_trend_5y', np.nan),
        # Phase 1.2 (revisited): quarterly YoY revenue growth (fills early
        # backtest periods where annual-derived growth is NaN).
        'revenue_growth_yoy_q': df.get('revenue_growth_yoy_q', np.nan),
        'production_growth': df.get('production_growth', np.nan),
        'organic_growth': df.get('organic_growth', np.nan),
        'same_store_noi_growth': df.get('same_store_noi_growth', np.nan),
        'book_value_growth': df.get('book_value_growth', np.nan),
        'arr_growth': df.get('arr_growth', np.nan),
        'tbv_growth_5y': df.get('tbv_growth_5y', np.nan),
    }
    for col_name, col_data in growth_fields.items():
        if isinstance(col_data, pd.Series) and col_data.notna().any():
            g_df[col_name] = _rank_within_subindustry(pd.to_numeric(col_data, errors='coerce'), rank_keys)

    # ---- SENTIMENT components ----
    s_df = pd.DataFrame(index=df.index)
    momo = df.get('return_12m_minus_1m', df.get('return_12m', np.nan))
    contrarian = SCONFIG.get('scoring', {}).get('contrarian_mode', False)
    if contrarian:
        momo = -momo if isinstance(momo, pd.Series) else momo

    sentiment_fields = {
        'distance_from_52w_high': -df.get('distance_from_52w_high', np.nan) if df.get('distance_from_52w_high') is not None else np.nan,
        'return_12m_minus_1m': momo,
        'short_float': -df.get('short_float', np.nan) if df.get('short_float') is not None else np.nan,
        'insider_own': df.get('insider_own', np.nan),
        'eps_revision_90d': df.get('eps_revision_90d', np.nan),
        'return_6m': df.get('return_6m', np.nan),
        'ffo_revision_90d': df.get('ffo_revision_90d', np.nan),
    }
    for col_name, col_data in sentiment_fields.items():
        if isinstance(col_data, pd.Series) and col_data.notna().any():
            s_df[col_name] = _rank_within_subindustry(pd.to_numeric(col_data, errors='coerce'), rank_keys)

    # ---- Sector-weighted combination ----
    sectors_cfg = SECTOR_METRICS.get('sectors', {})
    default_weights = SECTOR_METRICS.get('defaults', {}).get('weights',
        {'V': 0.30, 'Q': 0.30, 'G': 0.25, 'S': 0.15})

    def _resolve_cfg(gics_code):
        gs = str(gics_code).replace(' ', '')
        for ln in (8, 6):
            key = gs[:ln]
            if key in sectors_cfg:
                return sectors_cfg[key]
        return None

    def _resolve_metric_names(names):
        out = []
        for n in names:
            col = METRIC_NAME_MAP.get(n, n)
            out.append(col)
        return out

    all_ranked = {}
    for df_score in (val_scores, q_df, g_df, s_df):
        for c in df_score.columns:
            all_ranked[c] = df_score[c]

    tickers_arr = df['ticker'].tolist() if 'ticker' in df.columns else list(df.index)
    from collections import defaultdict
    cfg_groups = defaultdict(list)
    for i, t in enumerate(tickers_arr):
        g = gics_codes.get(t, '')
        cfg = _resolve_cfg(g)
        if cfg is not None:
            gs = str(g).replace(' ', '')
            cfg_groups[gs[:8]].append((i, cfg))

    universal_val = _weighted_avg(val_scores, {
        'earnings_yield': 0.15, 'fcf_yield': 0.20, 'sales_yield': 0.10,
        'book_yield': 0.10, 'ebitda_yield': 0.15, 'ptbv_yield': 0.10,
        'p_ffo_yield': 0.05, 'ev_sales_yield': 0.05, 'rule_of_40': 0.05,
        'rotce': 0.02, 'cet1_ratio': 0.01, 'ev_ebitdax': 0.02,
    }, min_coverage=0.3)
    qw_flat = {c: 1.0 / len(q_df.columns) for c in q_df.columns} if len(q_df.columns) > 0 else {}
    universal_qual = _weighted_avg(q_df, qw_flat, min_coverage=0.4)
    gw_flat = {c: 1.0 / len(g_df.columns) for c in g_df.columns} if len(g_df.columns) > 0 else {'revenue_growth_3yr': 1.0}
    # Phase 1.2: lowered to 0.25 so early-period growth is computed from any
    # 1-component subset rather than dropped entirely.
    universal_grw = _weighted_avg(g_df, gw_flat, min_coverage=0.25)
    sw_flat = {c: 1.0 / len(s_df.columns) for c in s_df.columns} if len(s_df.columns) > 0 else {}
    universal_sent = _weighted_avg(s_df, sw_flat, min_coverage=0.3)

    sector_val = pd.Series(np.nan, index=df.index)
    sector_qual = pd.Series(np.nan, index=df.index)
    sector_grw = pd.Series(np.nan, index=df.index)
    sector_sent = pd.Series(np.nan, index=df.index)
    sector_ws = {}

    for key, members in cfg_groups.items():
        cfg = members[0][1]
        idxs = [m[0] for m in members]
        ws = cfg.get('weights', default_weights)

        v_cols = _resolve_metric_names(cfg.get('valuation', []))
        v_avail = [c for c in v_cols if c in all_ranked]
        if v_avail:
            sector_val.iloc[idxs] = pd.DataFrame(
                {c: all_ranked[c].iloc[idxs] for c in v_avail}).mean(axis=1)

        q_cols = _resolve_metric_names(cfg.get('quality', []))
        q_avail = [c for c in q_cols if c in all_ranked]
        if q_avail:
            sector_qual.iloc[idxs] = pd.DataFrame(
                {c: all_ranked[c].iloc[idxs] for c in q_avail}).mean(axis=1)

        g_cols = _resolve_metric_names(cfg.get('growth', []))
        g_avail = [c for c in g_cols if c in all_ranked]
        if g_avail:
            sector_grw.iloc[idxs] = pd.DataFrame(
                {c: all_ranked[c].iloc[idxs] for c in g_avail}).mean(axis=1)

        s_cols = _resolve_metric_names(cfg.get('sentiment', []))
        s_avail = [c for c in s_cols if c in all_ranked]
        if s_avail:
            sector_sent.iloc[idxs] = pd.DataFrame(
                {c: all_ranked[c].iloc[idxs] for c in s_avail}).mean(axis=1)

        for idx in idxs:
            sector_ws[idx] = ws

    valuation = sector_val.where(sector_val.notna(), universal_val)
    quality = sector_qual.where(sector_qual.notna(), universal_qual)
    growth = sector_grw.where(sector_grw.notna(), universal_grw)
    sentiment = sector_sent.where(sector_sent.notna(), universal_sent)

    # Layer 3b: Factor orthogonalization (remove common-factor overlap)
    if SCONFIG.get('scoring', {}).get('orthogonalize', False):
        valuation, quality, growth, sentiment = orthogonalize_factor_scores(
            valuation, quality, growth, sentiment)
        if verbose:
            print('    Orthogonalized V/Q/G/S scores')

    use_regime_weights = 'regime_v_weight' in df.columns
    n = len(df)
    # Start every row with sector-specific weights (or global default if no
    # sector cfg). Quantum §Stage 2.4 MI reweight requires sector weights flow
    # through to backtest — earlier path let regime overlay overwrite sector
    # entirely; now regime acts as a multiplicative tilt on top of sector.
    v_w = np.full(n, default_weights['V'], dtype=float)
    q_w = np.full(n, default_weights['Q'], dtype=float)
    g_w = np.full(n, default_weights['G'], dtype=float)
    s_w = np.full(n, default_weights['S'], dtype=float)
    for idx, ws in sector_ws.items():
        v_w[idx] = ws.get('V', default_weights['V'])
        q_w[idx] = ws.get('Q', default_weights['Q'])
        g_w[idx] = ws.get('G', default_weights['G'])
        s_w[idx] = ws.get('S', default_weights['S'])

    if use_regime_weights:
        # Treat regime weights as a relative tilt vs. default; apply to sector
        # base per row, then renormalize per row to preserve sum-to-original.
        rv = df['regime_v_weight'].values.astype(float)
        rq = df['regime_q_weight'].values.astype(float)
        rg = df['regime_g_weight'].values.astype(float)
        rs = df['regime_s_weight'].values.astype(float)
        dV = max(1e-9, float(default_weights['V']))
        dQ = max(1e-9, float(default_weights['Q']))
        dG = max(1e-9, float(default_weights['G']))
        dS = max(1e-9, float(default_weights['S']))
        tilt_v = rv / dV
        tilt_q = rq / dQ
        tilt_g = rg / dG
        tilt_s = rs / dS
        orig_sum = v_w + q_w + g_w + s_w
        v_t = v_w * tilt_v
        q_t = q_w * tilt_q
        g_t = g_w * tilt_g
        s_t = s_w * tilt_s
        new_sum = v_t + q_t + g_t + s_t
        scale = np.where(new_sum > 0, orig_sum / new_sum, 1.0)
        v_w, q_w, g_w, s_w = (v_t * scale, q_t * scale, g_t * scale, s_t * scale)

    # Quantum §Stage 2.5 — apply hyperbolic-decay multipliers to per-row factor
    # weights, then renormalize each row to its original sum. Effect: factors
    # with high lambda (fast IC decay) are downweighted in the v2 composite,
    # matching the global POTENTIAL_WEIGHTS adjustment applied at module load.
    if getattr(ms, 'DECAY_ENABLED', False) and getattr(ms, 'FACTOR_DECAY_CFG', None):
        factors_cfg = FACTOR_DECAY_CFG.get('factors', {})
        horizon = float(getattr(ms, 'DECAY_HORIZON_PERIODS', 1.0))

        def _decay_mult(name):
            info = factors_cfg.get(name, {})
            lam = info.get('lambda')
            if lam is None or not np.isfinite(lam):
                return 1.0
            return 1.0 / (1.0 + max(0.0, float(lam)) * horizon)
        mv, mq, mg, msent = (_decay_mult('valuation'), _decay_mult('quality'),
                             _decay_mult('growth'), _decay_mult('sentiment'))
        orig_sum = v_w + q_w + g_w + s_w
        v_w_r = v_w * mv
        q_w_r = q_w * mq
        g_w_r = g_w * mg
        s_w_r = s_w * msent
        new_sum = v_w_r + q_w_r + g_w_r + s_w_r
        scale = np.where(new_sum > 0, orig_sum / new_sum, 1.0)
        v_w, q_w, g_w, s_w = (v_w_r * scale, q_w_r * scale,
                              g_w_r * scale, s_w_r * scale)
        if verbose:
            print(f'    §Stage2.5 v2 decay mults: V={mv:.3f} Q={mq:.3f} '
                  f'G={mg:.3f} S={msent:.3f}')

    w_arr = np.column_stack([v_w, q_w, g_w, s_w])
    score_df = pd.DataFrame({
        'valuation_score': valuation,
        'quality_score': quality,
        'growth_score': growth,
        'sentiment_score': sentiment,
    })
    valid = score_df.notna().values
    weighted = (score_df.fillna(0).values * w_arr).sum(axis=1)
    total_w_arr = (w_arr * valid).sum(axis=1)
    potential = pd.Series(
        np.where(total_w_arr > 0, weighted / total_w_arr, np.nan),
        index=df.index)

    # ---- Quantum §Stage 1.1: resampled scoring (Monte Carlo over WEIGHT_BANDS) ----
    sub_df_qm = pd.DataFrame({
        'valuation': valuation, 'quality': quality,
        'growth': growth, 'sentiment': sentiment,
    })
    try:
        resamp_median, resamp_p05, resamp_p95, resamp_iqr, top_dec_pct = \
            compute_resampled_scores(sub_df_qm,
                                        n_samples=N_RESAMPLES,
                                        seed=RESAMPLE_SEED)
        robust = (top_dec_pct >= ROBUST_TOP_DECILE_THRESHOLD * 100).astype(int)
        if verbose:
            n_robust = int(robust.sum())
            print(f'    §Stage1.1 v2 resampled: {N_RESAMPLES} draws, '
                  f'{n_robust} robust top-decile')
    except Exception as _exc:
        resamp_median = pd.Series(np.nan, index=df.index)
        resamp_p05 = resamp_median.copy()
        resamp_p95 = resamp_median.copy()
        resamp_iqr = resamp_median.copy()
        top_dec_pct = resamp_median.copy()
        robust = pd.Series(pd.NA, index=df.index, dtype='Int64')
        if verbose:
            print(f'    §Stage1.1 v2 resampled skipped: {_exc}')

    # ---- Quantum §Stage 1.3: sqrt-law impact haircut ----
    if getattr(ms, 'IMPACT_ENABLED', False):
        impact_bps = compute_impact_haircut(df)
        impact_points = (impact_bps / 10.0).clip(upper=IMPACT_CAP_POINTS)
        potential = (potential - impact_points.fillna(0)).clip(lower=0, upper=100)
        if verbose:
            n_hc = int(impact_bps.notna().sum())
            avg = float(impact_bps.mean()) if n_hc else 0.0
            max_bps = float(impact_bps.max()) if n_hc else 0.0
            print(f'    §Stage1.3 v2 impact haircut: {n_hc} stocks '
                  f'avg={avg:.0f}bps max={max_bps:.0f}bps')
    else:
        impact_bps = pd.Series(np.nan, index=df.index, dtype=float)
        impact_points = pd.Series(np.nan, index=df.index, dtype=float)

    # ---- Quantum §Stage 2.6: factor-ETF crowding penalty ----
    if getattr(ms, 'CROWDING_ENABLED', False):
        _etfs = load_factor_etf_holdings()
        crowd_count, crowd_flag = compute_crowding(df, _etfs)
        if _etfs:
            excess = (crowd_count.fillna(0) - 1).clip(lower=0)
            crowd_penalty = excess * CROWDING_PENALTY_POINTS
            potential = (potential - crowd_penalty).clip(lower=0, upper=100)
            if verbose:
                n_cw = int((crowd_flag.fillna(0).astype(int) == 1).sum())
                print(f'    §Stage2.6 v2 crowding: {n_cw} stocks held by '
                      f'>={CROWDING_THRESHOLD} of {len(_etfs)} factor ETFs')
        elif verbose:
            print('    §Stage2.6 v2 crowding: data/factor_etf_holdings.json missing')
    else:
        crowd_count = pd.Series(np.nan, index=df.index, dtype=float)
        crowd_flag = pd.Series(pd.NA, index=df.index, dtype='Int64')

    out = df.copy()
    out['valuation_score'] = valuation.round(2)
    out['quality_score'] = quality.round(2)
    out['growth_score'] = growth.round(2)
    out['sentiment_score'] = sentiment.round(2)
    out['potential_score'] = potential.round(2)
    out['potential_median']    = resamp_median.round(2)
    out['potential_p05']       = resamp_p05.round(2)
    out['potential_p95']       = resamp_p95.round(2)
    out['potential_iqr']       = resamp_iqr.round(2)
    out['top_decile_pct']      = top_dec_pct.round(1)
    out['robust_pick']         = robust
    out['impact_haircut_bps']    = impact_bps.round(1)
    out['impact_haircut_points'] = impact_points.round(2)
    out['crowding_count'] = crowd_count.round(0)
    out['crowded']        = crowd_flag

    small_sectors = df.groupby('rank_key').size()
    small_sectors = small_sectors[small_sectors < MIN_SECTOR_POP].index.tolist()
    small_mask = df['rank_key'].isin(small_sectors) if small_sectors else pd.Series(False, index=df.index)
    if small_mask.any():
        out.loc[small_mask, 'valuation_score'] = np.nan
        out.loc[small_mask, 'quality_score'] = np.nan
        out.loc[small_mask, 'growth_score'] = np.nan
        out.loc[small_mask, 'sentiment_score'] = np.nan
        out.loc[small_mask, 'potential_score'] = np.nan
        if verbose:
            print(f'    Dropped {small_mask.sum()} stocks in small rank groups (<{MIN_SECTOR_POP})')

    if verbose:
        n_scored = out['potential_score'].notna().sum()
        print(f'    Scored {n_scored}/{len(out)} stocks')

    return out


# ============================================================================
# Risk-adjusted portfolio sizing (Layer 5)
# ============================================================================

def apply_risk_sizing(df, vol_weight=0.5, liq_weight=0.5):
    """Adjust potential_score by volatility and liquidity.

    Formula:
      adjusted_score = score * (1/vol_norm)^{vol_weight} * liq_mult^{liq_weight}

    Vol is normalized within-universe (inverse-vol means high-vol → penalty).
    Liquidity multiplier: large=1.0, mid=0.8, small=0.5, micro=0.2.
    Both weights sum to 1.0 (default: equal blend).
    """
    df = df.copy()
    score_col = 'potential_score'
    out_col = 'risk_adjusted_score'

    if score_col not in df.columns:
        df[out_col] = np.nan
        return df

    liq_map = {'large': 1.0, 'mid': 0.8, 'small': 0.5, 'micro': 0.2}
    liq_mult = df.get('liquidity_tier', pd.Series('micro', index=df.index)).map(liq_map).fillna(0.2)

    vol = df.get('realized_vol', pd.Series(np.nan, index=df.index)).copy()
    vol_inv = 1.0 / vol
    vol_inv = vol_inv / vol_inv.quantile(0.95)  # cap at 95th percentile to avoid outlier dominance
    vol_inv = vol_inv.clip(upper=3.0, lower=0.3)

    factor = (vol_inv ** vol_weight) * (liq_mult ** liq_weight)
    df[out_col] = (df[score_col] * factor).round(2)

    return df


# ============================================================================
# Main pipeline
# ============================================================================






def interactive_loop(df):
    print(f'\n  Stock Universe: {len(df)} tickers (Market Cap > ${MIN_MARKET_CAP:,})')
    print(f'  Sectors available: {", ".join(sorted(SECTOR_LABEL.values()))}')
    print()
    print(f'  Type a query or "help" for examples, "quit" to exit.')
    while True:
        try:
            q = input('  > ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q in ('quit', 'exit', 'q'):
            break
        if q == 'help':
            print('''  Examples:
    (default sort is potential_score descending)
    tech top 20
    healthcare with quality > 80 and valuation > 70
    fresh in tech             — filings within 1 year only
    liquid in tech            — large or mid liquidity tier only
    tradeable in tech         — large liquidity tier only
    include delisted top 50   — restore DELISTED names (hidden by default)
    DELISTED                  — show only DELISTED names
    FALLEN_ANGEL in tech
    DEEP_VALUE energy top 50
    banks with pe_ttm < 15 sorted by dividend
    consumer staples with dividend_yield > 0.02
    industrials sorted by growth
    undervalued tech (sorts by valuation_score desc)
    why AAPL                 — full breakdown of one ticker
    compare AAPL MSFT NVDA   — side-by-side sub-scores
    export                   — write csv snapshot
    correlations             — sub-score correlation matrix''')
            continue
        if q == 'correlations':
            print_correlations(df)
            continue
        if q.startswith('export'):
            path = DATA_DIR / f'stocks_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
            export_csv(df, str(path))
            continue
        # `why TICKER`
        m = re.match(r'^why\s+([A-Za-z][\w.-]*)\s*$', q, re.IGNORECASE)
        if m:
            print_why(df, m.group(1))
            continue
        # `compare T1 T2 [T3 ...]`
        m = re.match(r'^compare\s+(.+)$', q, re.IGNORECASE)
        if m:
            tks = re.split(r'[\s,]+', m.group(1).strip())
            tks = [t for t in tks if t]
            if len(tks) < 2:
                print('  Compare needs 2+ tickers, e.g.: compare AAPL MSFT NVDA')
                continue
            print_compare(df, tks)
            continue
        sectors, filters, flag_filters, sf_, sl_, sd_, lim, incl_del = parse_query(q)
        result = execute_query(df, sectors, filters, flag_filters, sf_, sl_, sd_, lim,
                               include_delisted=incl_del)
        if result.empty:
            print('  No results match your query.')
            continue
        title = q[:60] + '...' if len(q) > 60 else q
        print_table(result, title=title, sort_field=sf_, sort_label=sl_, limit=lim)

def main():
    print()
    print('  ==========================================')
    print('        Stock Market Screener')
    print('  ==========================================')
    _regime = classify_regime_simple()
    _tilts = REGIME_SECTOR_TILTS.get(_regime, {})
    print(f'  Macro regime: {_regime}  '
          f'(ISM={REGIME_DEFAULTS["ISM_PMI"]}, '
          f'core-CPI={REGIME_DEFAULTS["YOY_CORE_CPI"]}%, '
          f'10Y-2Y={REGIME_DEFAULTS["YIELD_10Y_2Y"]}bps, '
          f'HY-OAS={REGIME_DEFAULTS["HY_OAS"]}bps)')
    if _tilts:
        print(f'  Sector tilts: ' + ', '.join(f'{k}{v:+d}' for k, v in _tilts.items()))
    print()
    cached = load_cache()
    if cached is not None:
        df = cached
    else:
        print('  [First run or cache expired - fetching data...]')
        print()
        t10y = fetch_t10y()
        print()
        (companies, industries, income, balance, cashflow,
         income_q, cashflow_q, sp) = load_simfin_data()
        print()
        # LIVE ANNUAL FUNDAMENTALS SOURCE (Option B / Option C from Study A).
        # DEFAULT: FMP annual (physical-period-keyed merge with SimFin fallback).
        # ROLLBACK: USE_FMP_FUNDAMENTALS=0 (or "off"/"false") forces SimFin-only.
        # Legacy USE_FMP_FUNDAMENTALS=1 also accepted (same as default).
        # Semantics:
        #   - ONLY the annual frames swap; income_q + cashflow_q (feeding
        #     compute_ttm and compute_quarterly_yoy_growth) stay SimFin.
        #   - Merge rule: keyed on (Ticker, Report Date) with 10-day tolerance;
        #     FMP preferred per physical period, SimFin fallback only where FMP
        #     lacks that physical period. Fiscal Year re-labeled to year of
        #     Report Date (year-of-END). See src/fmp_mapping.py:
        #     load_annual_with_fallback for the full contract.
        #   - Mixed-source-history tickers are logged, not silenced. Under the
        #     physical-period key this should be near 0.
        #   - Backtest is NOT routed through here (scripts/backtest.*.py does
        #     not call this gate); the backtest firewall stays intact.
        _fmp_env = os.environ.get('USE_FMP_FUNDAMENTALS', '').strip().lower()
        use_fmp_fund = _fmp_env not in ('0', 'off', 'false', 'no')
        if use_fmp_fund:
            from src.fmp_mapping import load_annual_with_fallback
            print()
            print('  ' + '=' * 70)
            print('  FUNDAMENTALS SOURCE: FMP annual (physical-period keyed) + SimFin quarterly/TTM')
            print('  ' + '=' * 70)
            income, balance, cashflow = load_annual_with_fallback(
                income, balance, cashflow,
            )
            print()
        else:
            print()
            print('  ' + '=' * 70)
            print('  FUNDAMENTALS SOURCE: SimFin (all) [USE_FMP_FUNDAMENTALS=0]')
            print('  ' + '=' * 70)
            print()
        sp_meta = sp.sort_index().groupby(level=0).last()
        sp_meta.columns = [f'sp_{c}' for c in sp_meta.columns]
        betas, vols = compute_betas(sp, sp_meta)
        hi52w, lo52w = compute_52w(sp)
        momo = compute_momentum(sp)
        liq = compute_liquidity(sp)
        del sp
        print()
        hist = compute_history_metrics(income, balance, cashflow)
        ttm = compute_ttm(income_q, cashflow_q)
        rev_yoy_q = compute_quarterly_yoy_growth(income_q)
        del income_q, cashflow_q
        print()
        # Ownership fields (insider_own, inst_own, short_float) are opt-in via
        # USE_FMP_OWNERSHIP=1, which reads scripts/refresh_ownership_fmp.py's
        # ownership_live table. Default off → empty dict → sentiment renormalizes
        # to the price pair (see module docstring + SENTIMENT_WEIGHTS comment).
        use_fmp_own = os.environ.get('USE_FMP_OWNERSHIP', '').lower() in ('1', 'true', 'yes')
        if use_fmp_own:
            caps_series = (sp_meta['sp_Close'] * sp_meta['sp_Shares Outstanding'])
            candidate_tickers = caps_series[caps_series >= MIN_MARKET_CAP].index.tolist()
            ownership = load_ownership_live(candidate_tickers)
            print(f'  FMP ownership_live: {len(ownership)}/{len(candidate_tickers)} tickers gained ownership data')
        else:
            ownership = {}
        print()
        # DQ guard on live path only when FMP fundamentals are in play.
        # USE_FMP_FUNDAMENTALS=0 (SF-only rollback) → guard OFF → bit-identical
        # to pre-DQ-guard behavior. Every other value (default unset, '1',
        # 'on', 'true', etc.) enables the guard.
        _dq_env = os.environ.get('USE_FMP_FUNDAMENTALS', '').strip().lower()
        _dq_live = _dq_env not in ('0', 'off', 'false')
        df = compute_snapshot(companies, industries, income, balance, cashflow,
                              sp_meta, betas, vols, t10y, hi52w, lo52w,
                              hist=hist, ttm=ttm, momo=momo, ownership=ownership,
                              liquidity=liq, rev_yoy_q=rev_yoy_q,
                              dq_guard=_dq_live)
        if len(df) > 0:
            df = compute_potential_scores(df)
            df = df.sort_values('potential_score', ascending=False, na_position='last')
            save_cache(df)
        else:
            print('  Error: no stocks could be processed.')
            sys.exit(1)
    # Post-scoring liveness gate (report-only; scoring untouched).
    df = compute_liveness_and_flag(df, CACHE_DB)
    # Persist just the flags column back to the cache so downstream tools
    # (scripts/generate_html_report.py) see the DELISTED tag without needing
    # to duplicate the liveness computation.
    _persist_flags_to_cache(df, CACHE_DB)
    # --no-repl / --pipeline-only skips the REPL for orchestrated runs
    # (scripts/run_daily.py). Scoring / gate logic untouched — this is a
    # single guard around the terminal-only input loop.
    no_repl = (
        '--no-repl' in sys.argv
        or '--pipeline-only' in sys.argv
        or os.environ.get('SCREENER_NO_REPL', '').strip().lower() in ('1', 'true', 'yes')
    )
    if no_repl:
        # Emit a single machine-readable status line so scripts/run_daily.py
        # can build the health log from THIS run's numbers directly rather
        # than scraping fragile human-readable prints. Print-only surface;
        # no scoring or gate change. Counts derived from df.flags after
        # compute_liveness_and_flag has run.
        import json as _json
        try:
            _flags = df.get('flags')
            def _cnt(sub):
                if _flags is None:
                    return 0
                return int(_flags.fillna('').astype(str).str.contains(sub, na=False).sum())
            n_universe = len(df)
            n_del = _cnt('DELISTED')
            n_unpriced = _cnt('UNPRICED')
            n_unknown = _cnt('UNKNOWN')
            n_live = n_universe - n_del - n_unpriced - n_unknown
            # Coverage: fresh-priced tickers as fraction of the TRADEABLE
            # universe (excluding oracle-DELISTED zombies). Reason: yfinance
            # gaps are dominantly delisted/halted tickers whose FMP quote
            # returns years-old timestamps; counting them against the
            # denominator masks how well the pipeline is covering the
            # actually-tradeable set. Formula:
            #   coverage = live / (universe - delisted)
            # Live-by-definition means the ticker has a fresh (within 10
            # calendar days of table_max) prices_live row.
            _cov = None
            _n_tradeable = n_universe - n_del
            if _n_tradeable > 0:
                _cov = n_live / _n_tradeable * 100
            # Non-USD + mixed-source + shares-DQ come from fmp_mapping.
            _non_usd = 0
            _mixed = 0
            _dq_shares = 0
            try:
                from src import fmp_mapping as _fm
                _non_usd = int(_fm.LAST_LOAD_STATS.get('non_usd_count', 0) or 0)
                _mixed = int(_fm.LAST_LOAD_STATS.get('mixed_source_tickers', 0) or 0)
                _dq_shares = int(_fm.LAST_LOAD_STATS.get('dq_shares_nulled', 0) or 0)
            except Exception:
                pass
            # Derived-DQ count comes from compute_snapshot's module attr,
            # populated in the cache-rebuild path. On cache-hit no snapshot ran
            # this invocation → attr stays 0 (the CSV log from the prior run
            # is still the source of truth for offender detail).
            _dq_derived = int(LAST_DQ_DERIVED_COUNT)
            # Read env at emit time so this works for both cache-hit and
            # cache-rebuild paths (use_fmp_fund is scoped to the rebuild
            # branch above and may be undefined on cache-hit).
            _fmp_env = os.environ.get('USE_FMP_FUNDAMENTALS', '').strip().lower()
            _src = 'SimFin' if _fmp_env in ('0','off','false','no') else 'FMP'
            _payload = {
                "source": _src,
                "universe": int(n_universe),
                "live": int(n_live),
                "delisted": int(n_del),
                "unpriced": int(n_unpriced),
                "unknown": int(n_unknown),
                "coverage_pct": round(_cov, 1) if _cov is not None else None,
                "non_usd_fallback": _non_usd,
                "mixed_source": _mixed,
                "dq_nulled_derived": _dq_derived,
                "dq_nulled_shares": _dq_shares,
                "regime": _regime,
                "cache_updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            print("HEALTH_JSON=" + _json.dumps(_payload))
        except Exception as _e:
            print(f"HEALTH_JSON_ERROR={_e!r}")
    else:
        interactive_loop(df)
    print('  Done.')

if __name__ == '__main__':
    main()
