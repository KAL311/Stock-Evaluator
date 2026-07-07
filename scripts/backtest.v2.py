#!/usr/bin/env python3
"""Phase 7: Walk-forward backtest of v2 scoring engine (GICS-ranked, regime-adaptive).

Runs 6 semi-annual windows (2021-H2 through 2024 full-year), each using
point-in-time data and the full v2 pipeline:
  GICS mapping → sector metrics → quality gates → hard excludes → regime overlay → v2 scoring

Each period classifies its own macro regime from historical state variables so the
regime-adaptive weights and sector tilts are applied point-in-time.

WARNING: SimFin daily price data begins 2020-06-10; the 2020->2021 window is
omitted because too few stocks had the required 12 months of price history.
"""
import argparse, os, sys, sqlite3, time, json, hashlib
from datetime import datetime
from pathlib import Path
import pandas as pd
import numpy as np
import simfin as sf

# scipy is required for HRP linkage / quasi-diagonalization (Quantum §Stage 1.2).
# If unavailable, _hrp_weights falls back to inverse-variance weights with a
# warning so the pipeline still runs.
try:
    from scipy.cluster.hierarchy import linkage  # type: ignore
    from scipy.spatial.distance import squareform  # type: ignore
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
import market_screener as ms
# v2 scorer has been unified into market_screener.py (former v2 module retired).
# Keep `ms2` alias so existing backtest call sites (ms2.compute_potential_scores_v2,
# ms2.classify_regime, ms2.apply_probabilistic_overlay, etc.) continue to work.
ms2 = ms

# Free-tier SimFin us-shareprices-daily stopped advancing past this date. Forward-price
# EVALUATION for periods whose forward window extends past this ceiling falls back to
# yfinance (prices_live in data/stock_cache.db). Scoring at the cutoff date still uses
# SimFin exclusively — sources NEVER mix within scoring. See docs/price_layer.md and
# docs/phase5_oos_2025_decision.md (pre-registered "allowed change during freeze",
# same convention as the --oos-reserve fix in docs/phase3_oos_decision.md).
SIMFIN_PRICE_CEILING = '2025-06-03'

SURVIVORSHIP_WARNING = """
########################################################################
#                                                                      #
#   WARNING: SURVIVORSHIP BIAS                                         #
#                                                                      #
#   This backtest uses the CURRENT SimFin company universe + a         #
#   -30% delisted-ticker proxy (Shumway 1997 anchor) for tickers that  #
#   had filings before the cutoff but stopped trading >60 days before  #
#   the forward window end. This is a LOWER-BOUND correction, not a    #
#   full fix: actual delist returns vary widely (-100% bankruptcy to   #
#   +30% M&A premium). True PIT membership requires CRSP or Norgate.   #
#                                                                      #
#   With this correction the bottom-decile mean should DROP and the    #
#   top-decile alpha should be relatively unaffected. If top-alpha     #
#   drops significantly, the model was relying on bias for its edge.   #
#                                                                      #
########################################################################
"""

# Periods: (cutoff, forward, t10y, ism, curve_10y2y, core_cpi_yoy, hy_oas)
# Semi-annual windows to maximize test periods with available SimFin data.
# State variables are approximate period-start values from FRED/ISM/Haver.
# SimFin daily prices start 2020-06-10; pre-2021-H1 windows have insufficient history.
PERIODS = [
    ('2021-06-30', '2022-06-30', 0.0152, 60.0,  1.05, 0.054, 260),   # 2021 H2
    ('2021-12-31', '2022-12-31', 0.0152, 58.0,  0.75, 0.050, 300),   # 2022 full year
    ('2022-06-30', '2023-06-30', 0.0310, 53.0, -0.15, 0.081, 430),   # 2022 H2
    ('2022-12-31', '2023-12-31', 0.0388, 49.0, -0.60, 0.065, 450),   # 2023 full year
    ('2023-06-30', '2024-06-30', 0.0375, 46.0, -0.95, 0.030, 410),   # 2023 H2
    ('2023-12-31', '2024-12-31', 0.0388, 47.0, -0.40, 0.039, 380),   # 2024 full year
    # 2025 full year — pre-registered per docs/phase5_oos_2025_decision.md.
    # Forward year 2025 exceeds SIMFIN_PRICE_CEILING; forward prices sourced from
    # prices_live (yfinance). Values below are point-in-time as of 2024-12-31:
    #   t10y         FRED DGS10        = 4.58%           -> 0.0458
    #   ism          ISM Mfg PMI Dec-2024                = 49.3
    #   curve_10y2y  FRED T10Y2Y                        = +0.33
    #   core_cpi_yoy FRED CPILFESL YoY Dec-2024 (3.213%) -> 0.032
    #   hy_oas       FRED BAMLH0A0HYM2 = 2.92%           -> 292 bps
    ('2024-12-31', '2025-12-31', 0.0458, 49.3,  0.33, 0.032, 292),   # 2025 full year (OOS)
]


def filter_quarterly(df, cutoff):
    if 'Publish Date' in df.columns:
        return df[df['Publish Date'] <= cutoff]
    return df


def compute_ic(scored):
    """Spearman rank correlation between score and forward return."""
    valid = scored[['potential_score', 'fwd_return']].dropna()
    if len(valid) < 30:
        return np.nan
    return valid['potential_score'].rank().corr(valid['fwd_return'].rank())


def compute_factor_ics(scored):
    """Spearman rank IC for each factor sub-score (V, Q, G, S)."""
    factors = ['valuation_score', 'quality_score', 'growth_score', 'sentiment_score']
    ics = {}
    for f in factors:
        if f not in scored.columns:
            ics[f] = np.nan
            continue
        valid = scored[[f, 'fwd_return']].dropna()
        if len(valid) < 30:
            ics[f] = np.nan
        else:
            ics[f] = round(valid[f].rank().corr(valid['fwd_return'].rank()), 4)
    return ics


def compute_monthly_returns(sp_fwd_raw, sp_at_raw, cutoff):
    """Compute monthly return series per ticker during forward window.

    Returns DataFrame: columns=tickers, rows=month-end periods.
    """
    sp_fwd = sp_fwd_raw.reset_index()
    sp_fwd['month'] = sp_fwd['Date'].dt.to_period('M')
    monthly = sp_fwd.groupby(['Ticker', 'month'])['Adj. Close'].last()
    monthly_pivot = monthly.unstack('Ticker')

    start_prices = sp_at_raw.sort_index().groupby(level=0).last()['Adj. Close']
    start_period = pd.Timestamp(cutoff).to_period('M')
    start_row = pd.DataFrame([start_prices], index=[start_period])
    monthly_with_start = pd.concat([start_row, monthly_pivot])

    monthly_ret = monthly_with_start.pct_change().dropna(how='all')
    return monthly_ret


def compute_portfolio_drawdown(monthly_ret_series):
    """Compute max drawdown from a portfolio monthly return Series."""
    cum = (1 + monthly_ret_series.fillna(0)).cumprod()
    rolling_max = cum.cummax()
    dd = (cum / rolling_max - 1).min()
    return dd if dd < 0 else 0.0


def compute_portfolio_calmar(monthly_ret_series):
    """Compute Calmar ratio = annualized return / |max drawdown|."""
    n = len(monthly_ret_series)
    if n < 3:
        return np.nan
    cum_ret = (1 + monthly_ret_series.fillna(0)).prod() - 1
    n_years = n / 12.0
    ann_ret = (1 + cum_ret) ** (1 / n_years) - 1 if n_years > 0 else 0
    max_dd = compute_portfolio_drawdown(monthly_ret_series)
    return ann_ret / abs(max_dd) if max_dd < 0 else np.nan
    """Spearman rank IC for each factor sub-score (V, Q, G, S)."""
    factors = ['valuation_score', 'quality_score', 'growth_score', 'sentiment_score']
    ics = {}
    for f in factors:
        if f not in scored.columns:
            ics[f] = np.nan
            continue
        valid = scored[[f, 'fwd_return']].dropna()
        if len(valid) < 30:
            ics[f] = np.nan
        else:
            ics[f] = round(valid[f].rank().corr(valid['fwd_return'].rank()), 4)
    return ics


def compute_sensitivity(scored, df_pre_score=None, gics_map=None,
                        regime_probs=None):
    """Perturb each global factor weight by +/-5pp, re-run v2 scorer.

    The previous implementation reconstructed potential_score from sub-scores
    under a single global weight, which does not match what the v2 scorer
    actually applies (sector-specific weights, per-row regime modulation,
    decay multipliers). The corrected version perturbs the global default
    weights and reruns the scorer end-to-end so sensitivity reflects what the
    model would do under perturbation. Falls back to a sub-score blend if
    df_pre_score is unavailable, with a flag that result is approximate.
    """
    base = dict(ms.POTENTIAL_WEIGHTS)  # current global, post-decay if enabled
    short_to_long = {'V': 'valuation', 'Q': 'quality',
                     'G': 'growth', 'S': 'sentiment'}
    pert_pairs = [('V', +0.05), ('V', -0.05),
                  ('Q', +0.05), ('Q', -0.05),
                  ('G', +0.05), ('G', -0.05),
                  ('S', +0.05), ('S', -0.05)]

    if df_pre_score is None:
        if all(c in scored.columns for c in
               ['regime_v_weight', 'regime_q_weight',
                'regime_g_weight', 'regime_s_weight']):
            rw = scored[['regime_v_weight', 'regime_q_weight',
                         'regime_g_weight', 'regime_s_weight']].mean()
            base = {'valuation': rw['regime_v_weight'],
                    'quality':   rw['regime_q_weight'],
                    'growth':    rw['regime_g_weight'],
                    'sentiment': rw['regime_s_weight']}
        results = {}
        for fkey, delta in pert_pairs:
            long_key = short_to_long[fkey]
            w = dict(base)
            w[long_key] = max(0.0, w[long_key] + delta)
            total = sum(w.values())
            w = {k: v / total for k, v in w.items()}
            score = (scored['valuation_score'].fillna(0) * w['valuation']
                     + scored['quality_score'].fillna(0) * w['quality']
                     + scored['growth_score'].fillna(0) * w['growth']
                     + scored['sentiment_score'].fillna(0) * w['sentiment'])
            cov = (scored['valuation_score'].notna().astype(float) * w['valuation']
                   + scored['quality_score'].notna().astype(float) * w['quality']
                   + scored['growth_score'].notna().astype(float) * w['growth']
                   + scored['sentiment_score'].notna().astype(float) * w['sentiment'])
            score = (score / cov.replace(0, np.nan)).where(cov > 0)
            try:
                decile = pd.qcut(score, 10, labels=False, duplicates='drop') + 1
            except Exception:
                decile = pd.Series(np.nan, index=score.index)
            top = scored.loc[decile == 10, 'fwd_return'].mean() if (decile == 10).any() else np.nan
            bot = scored.loc[decile == 1, 'fwd_return'].mean() if (decile == 1).any() else np.nan
            label = f'{fkey}{delta:+.0%}'
            results[label] = (top - bot) if (pd.notna(top) and pd.notna(bot)) else np.nan

        # Baseline (unperturbed) spread under same blend so range comparison
        # is apples-to-apples with the perturbed values.
        w = dict(base)
        total = sum(w.values())
        if total > 0:
            w = {k: v / total for k, v in w.items()}
        score = (scored['valuation_score'].fillna(0) * w['valuation']
                 + scored['quality_score'].fillna(0) * w['quality']
                 + scored['growth_score'].fillna(0) * w['growth']
                 + scored['sentiment_score'].fillna(0) * w['sentiment'])
        cov = (scored['valuation_score'].notna().astype(float) * w['valuation']
               + scored['quality_score'].notna().astype(float) * w['quality']
               + scored['growth_score'].notna().astype(float) * w['growth']
               + scored['sentiment_score'].notna().astype(float) * w['sentiment'])
        score = (score / cov.replace(0, np.nan)).where(cov > 0)
        try:
            decile = pd.qcut(score, 10, labels=False, duplicates='drop') + 1
        except Exception:
            decile = pd.Series(np.nan, index=score.index)
        top0 = scored.loc[decile == 10, 'fwd_return'].mean() if (decile == 10).any() else np.nan
        bot0 = scored.loc[decile == 1, 'fwd_return'].mean() if (decile == 1).any() else np.nan
        results['baseline'] = (top0 - bot0) if (pd.notna(top0) and pd.notna(bot0)) else np.nan
        results['mode'] = 'approx_blend'
    else:
        # Full-fidelity path not yet wired; placeholder for Phase 4.
        results = {'mode': 'full_rescore_not_implemented'}

    spreads = [v for k, v in results.items()
               if pd.notna(v) and k not in ('mode', 'baseline')]
    results['min_spread'] = min(spreads) if spreads else np.nan
    results['max_spread'] = max(spreads) if spreads else np.nan
    results['range'] = (results['max_spread'] - results['min_spread']) if spreads else np.nan
    return results


def _hrp_weights(returns_df):
    """Hierarchical Risk Parity (López de Prado, J. Portfolio Mgmt 42(4), 2016).
    returns_df: rows = trailing daily return dates, cols = tickers.
    Implementation: correlation-distance metric, single-linkage cluster,
    quasi-diagonalization, recursive bisection with inverse-variance allocation.
    Quantum Approach §Stage 1.2 — replaces Pearson-correlation portfolio
    construction with hierarchical-clustering-aware sizing. Returns Series of
    weights summing to 1, indexed by ticker. Falls back to inverse-variance
    weights if scipy unavailable or matrix degenerate."""
    if returns_df.shape[1] < 2:
        return pd.Series(1.0, index=returns_df.columns)
    cov = returns_df.cov()
    if not _HAS_SCIPY:
        ivp = 1.0 / np.diag(cov.values)
        ivp = np.where(np.isfinite(ivp), ivp, 0)
        if ivp.sum() <= 0:
            return pd.Series(1.0 / len(cov), index=cov.index)
        return pd.Series(ivp / ivp.sum(), index=cov.index)
    corr = returns_df.corr().fillna(0).clip(-0.999, 0.999)
    dist = np.sqrt(0.5 * (1.0 - corr))
    dist_vals = np.asarray(dist.values, dtype=float).copy()
    np.fill_diagonal(dist_vals, 0.0)
    # symmetrize to absorb tiny numerical asymmetry
    dist_vals = 0.5 * (dist_vals + dist_vals.T)
    try:
        link = linkage(squareform(dist_vals, checks=False), method='single')
    except Exception:
        ivp = 1.0 / np.diag(cov.values)
        ivp = np.where(np.isfinite(ivp), ivp, 0)
        if ivp.sum() <= 0:
            return pd.Series(1.0 / len(cov), index=cov.index)
        return pd.Series(ivp / ivp.sum(), index=cov.index)
    n = returns_df.shape[1]
    link_int = link.astype(int)

    def _expand(ix):
        if ix < n:
            return [ix]
        row = link_int[ix - n]
        return _expand(row[0]) + _expand(row[1])
    order = _expand(link_int[-1, 0]) + _expand(link_int[-1, 1])
    tickers_ordered = [returns_df.columns[i] for i in order]
    weights = pd.Series(1.0, index=tickers_ordered, dtype=float)

    def _cluster_var(c):
        sub = cov.loc[c, c].values
        d = np.diag(sub)
        d = np.where(d > 0, d, np.nan)
        ivp = 1.0 / d
        if not np.isfinite(ivp).any():
            return np.nan
        ivp = np.where(np.isfinite(ivp), ivp, 0)
        s = ivp.sum()
        if s <= 0:
            return np.nan
        ivp = ivp / s
        return float(ivp @ sub @ ivp)

    clusters = [tickers_ordered]
    while clusters:
        new_clusters = []
        for cl in clusters:
            if len(cl) < 2:
                continue
            mid = len(cl) // 2
            left, right = cl[:mid], cl[mid:]
            var_l = _cluster_var(left)
            var_r = _cluster_var(right)
            if not (np.isfinite(var_l) and np.isfinite(var_r)) or (var_l + var_r) <= 0:
                alpha = 0.5
            else:
                alpha = 1.0 - var_l / (var_l + var_r)
            weights.loc[left] *= alpha
            weights.loc[right] *= (1.0 - alpha)
            new_clusters.append(left)
            new_clusters.append(right)
        clusters = new_clusters
    s = weights.sum()
    if s <= 0:
        return pd.Series(1.0 / len(weights), index=weights.index)
    return (weights / s).reindex(returns_df.columns).fillna(0.0)


def run_one_period(cutoff_str, forward_str, t10y, ism, curve_10y2y, core_cpi_yoy, hy_oas,
                   companies, industries, income, balance, cashflow,
                   income_q, cashflow_q, sp, *, full_hist_frame=None):
    cutoff = pd.Timestamp(cutoff_str)
    forward = pd.Timestamp(forward_str)
    label = f'{cutoff.year}->{forward.year}'

    print()
    print('=' * 70)
    print(f'Period: {label}  (T10Y={t10y:.4f}, ISM={ism:.0f}, CPI={core_cpi_yoy:.1%}, '
          f'curve={curve_10y2y:+.2f}, OAS={hy_oas:.0f})')
    print('=' * 70)

    # ----- Filter to point-in-time -----
    income_h   = filter_quarterly(income, cutoff)
    balance_h  = filter_quarterly(balance, cutoff)
    cashflow_h = filter_quarterly(cashflow, cutoff)
    income_q_h   = filter_quarterly(income_q, cutoff)
    cashflow_q_h = filter_quarterly(cashflow_q, cutoff)
    sp_dates = sp.index.get_level_values('Date')
    sp_at      = sp[sp_dates <= cutoff]
    sp_forward = sp[(sp_dates > cutoff) & (sp_dates <= forward + pd.Timedelta(days=10))]
    print(f'\nFiltering to as-of {cutoff.date()}...')
    print(f'  income annual:   {len(income_h):>8d}  (of {len(income):>8d})')
    print(f'  cashflow annual: {len(cashflow_h):>8d}  (of {len(cashflow):>8d})')
    print(f'  income quarterly: {len(income_q_h):>7d}  (of {len(income_q):>7d})')
    print(f'  sp daily (cutoff): {len(sp_at):>7d}  (of {len(sp):>7d})')

    n_dates = sp_at.index.get_level_values('Date').nunique()
    if n_dates < 60:
        print(f'\n  SKIP: only {n_dates} trading days before cutoff.')
        return {
            'label': label, 'n': 0, 'top_mean': None, 'bot_mean': None,
            'spread': None, 'universe_mean': None, 'survivor_pct': None,
            'regime': None, 'flags': {},
        }, None

    # ----- Build at-cutoff sp_meta -----
    sp_meta_at = sp_at.sort_index().groupby(level=0).last()
    sp_meta_at.columns = [f'sp_{c}' for c in sp_meta_at.columns]

    # ----- Reuse v1 helpers on filtered data -----
    print('\nComputing point-in-time inputs...')
    betas, vols = ms.compute_betas(sp_at, sp_meta_at)
    hi52w, lo52w = ms.compute_52w(sp_at)
    momo = ms.compute_momentum(sp_at)
    hist = ms.aggregate_history_metrics(full_hist_frame, max_fiscal_year=cutoff.year)
    ttm_p = ms.compute_ttm(income_q_h, cashflow_q_h)
    rev_yoy_q = ms.compute_quarterly_yoy_growth(income_q_h)
    liq = ms.compute_liquidity(sp_at)

    n_beta = len([b for b in betas.values() if b is not None])
    n_vol = len([v for v in vols.values() if v is not None])
    print(f'  Beta: {n_beta} tickers, Vol: {n_vol} tickers')

    latest_sp_at_date = sp_at.index.get_level_values('Date').max()
    gap = (cutoff - latest_sp_at_date).days
    if gap > 5:
        print(f'  WARNING: latest sp date {latest_sp_at_date.date()}, {gap}d before cutoff')

    print('\nBuilding v1 snapshot...')
    print('  Finviz disabled (no look-ahead).')
    df = ms.compute_snapshot(
        companies, industries, income_h, balance_h, cashflow_h,
        sp_meta_at, betas, vols, t10y, hi52w, lo52w,
        hist=hist, ttm=ttm_p, momo=momo, finviz={},
        liquidity=liq, reference_date=cutoff, rev_yoy_q=rev_yoy_q,
    )
    print(f'  Snapshot: {len(df)} stocks')

    # ----- V2 pipeline -----
    print('\nRunning v2 pipeline...')
    gics_map = ms2.load_gics_for_tickers(companies)
    df['gics_code'] = df['ticker'].map(gics_map)
    print(f'  GICS mapped: {df.gics_code.notna().sum()}/{len(df)}')

    df = ms2.compute_sector_metrics(df, balance_h, cashflow_h)
    df = ms2.handle_negative_earnings(df)
    df = ms2.apply_quality_gates(df)
    df = ms2.apply_hard_excludes(df)

    # Point-in-time regime classification (probabilistic, smooth transitions)
    regime_label, regime_probs = ms2.classify_regime(
        ism=ism, curve_10y2y=curve_10y2y,
        core_cpi_yoy=core_cpi_yoy, hy_oas=hy_oas,
    )
    probs_str = ', '.join(f'{k}={v:.3f}' for k, v in sorted(regime_probs.items(), key=lambda x: -x[1]))
    print(f'  Regime: {regime_label}  [{probs_str}]')

    df = ms2.apply_probabilistic_overlay(df, regime_probs)
    df = ms2.compute_potential_scores_v2(df, gics_map, verbose=False)
    df = ms2.apply_risk_sizing(df)
    n_scored = df['potential_score'].notna().sum()
    print(f'  Scored: {n_scored}/{len(df)}')

    # ----- Sentiment sanity (no Finviz leak) -----
    n_short = df.get('short_float', pd.Series([0])).notna().sum()
    n_insider = df.get('insider_own', pd.Series([0])).notna().sum()
    if n_short > 1 or n_insider > 1:
        sys.exit('ABORT: Finviz data leaked into backtest.')
    n_dist = df.get('distance_from_52w_high', pd.Series([0])).notna().sum()
    n_momo = df.get('return_12m_minus_1m', pd.Series([0])).notna().sum()
    print(f'  Price-sentiment: dist52w={n_dist}, momo12-1={n_momo}')
    if n_dist < 100 or n_momo < 100:
        print(f'  SKIP: insufficient price history.')
        return {
            'label': label, 'n': 0, 'top_mean': None, 'bot_mean': None,
            'spread': None, 'universe_mean': None, 'survivor_pct': None,
            'regime': regime_label, 'flags': {},
        }, None

    # ----- Forward returns -----
    # Cutoff price ALWAYS comes from SimFin (frozen scoring invariant).
    # Forward price comes from SimFin except when forward_str extends past
    # SIMFIN_PRICE_CEILING; those periods draw p_fwd from prices_live (yfinance,
    # data/stock_cache.db). yfinance close is auto-adjusted (split+dividend),
    # matching SimFin 'Adj. Close' semantics per docs/price_layer.md — no
    # rescaling. Forward-return EVALUATION is not scoring; sources never mix
    # within the scoring pipeline.
    price_at_adj = sp_at.sort_index().groupby(level=0).last()['Adj. Close'].rename('p_at_adj')

    if forward_str > SIMFIN_PRICE_CEILING:
        cutoff_universe = set(price_at_adj.index)
        db_path = ROOT / 'data' / 'stock_cache.db'
        window_lo = (pd.Timestamp(forward_str) - pd.Timedelta(days=10)).strftime('%Y-%m-%d')
        with sqlite3.connect(str(db_path)) as _conn:
            try:
                live_fwd = pd.read_sql_query(
                    "SELECT ticker, date, close FROM prices_live "
                    f"WHERE date > '{window_lo}' AND date <= '{forward_str}' "
                    "ORDER BY ticker, date",
                    _conn,
                )
            except Exception as e:
                sys.exit(
                    f'ERROR: prices_live table unavailable ({e}). '
                    f'Run scripts/refreshprice.py before running periods with forward > '
                    f'{SIMFIN_PRICE_CEILING} (see docs/price_layer.md).'
                )
        if live_fwd.empty:
            sys.exit(
                f'ERROR: prices_live returned zero rows in ({window_lo}, {forward_str}]. '
                f'Refresh prices_live before running this OOS period.'
            )
        # Last close within 10d at/before forward date per ticker.
        price_fwd_yf = (
            live_fwd.sort_values(['ticker', 'date'])
                    .groupby('ticker')['close']
                    .last()
                    .rename('p_fwd')
        )
        price_fwd_yf.index.name = 'Ticker'
        n_live = len(price_fwd_yf)
        print()
        print('*' * 72)
        print('  FORWARD-PRICE SOURCE: yfinance (prices_live)  <-- SimFin ceiling exceeded')
        print(f'  forward = {forward_str} > SIMFIN_PRICE_CEILING = {SIMFIN_PRICE_CEILING}')
        print(f'  cutoff price source: SimFin (unchanged)')
        print(f'  yfinance-priced tickers: {n_live} / {len(cutoff_universe)} in cutoff universe')
        print('  See docs/price_layer.md and docs/phase5_oos_2025_decision.md.')
        print('*' * 72)

        # --- Cross-source splice check ---
        # yfinance back-adjusts the entire series when a ticker splits between
        # 2024-12-31 and the fetch date, while the SimFin cutoff price is
        # adjusted only through SIMFIN_PRICE_CEILING. Detect gross mismatches
        # by comparing yfinance and SimFin closes on SIMFIN_PRICE_CEILING ± 5d
        # and EXCLUDE any ticker whose ratio deviates from 1.0 by > 5%.
        splice_ts = pd.Timestamp(SIMFIN_PRICE_CEILING)
        splice_lo = splice_ts - pd.Timedelta(days=5)
        splice_hi = splice_ts + pd.Timedelta(days=5)
        sp_full_dates = sp.index.get_level_values('Date')
        sp_splice = sp[(sp_full_dates >= splice_lo) & (sp_full_dates <= splice_hi)]
        n_excluded = 0
        n_compared = 0
        if len(sp_splice) > 0:
            sf_splice_last = (
                sp_splice.sort_index()
                         .groupby(level='Ticker')
                         .last()['Adj. Close']
            )
            with sqlite3.connect(str(db_path)) as _conn:
                yf_splice = pd.read_sql_query(
                    "SELECT ticker, date, close FROM prices_live "
                    f"WHERE date >= '{splice_lo.strftime('%Y-%m-%d')}' "
                    f"AND date <= '{splice_hi.strftime('%Y-%m-%d')}' "
                    "ORDER BY ticker, date",
                    _conn,
                )
            if not yf_splice.empty:
                yf_splice_last = (
                    yf_splice.sort_values(['ticker', 'date'])
                             .groupby('ticker')['close']
                             .last()
                )
                overlap = sf_splice_last.index.intersection(yf_splice_last.index)
                n_compared = len(overlap)
                if n_compared > 0:
                    ratio = yf_splice_last.loc[overlap] / sf_splice_last.loc[overlap]
                    bad = ratio[(ratio - 1.0).abs() > 0.05].index
                    drop = [t for t in bad if t in price_fwd_yf.index]
                    n_excluded = len(drop)
                    if drop:
                        price_fwd_yf = price_fwd_yf.drop(index=drop)
        print(f'  Splice check @ {SIMFIN_PRICE_CEILING} ±5d: '
              f'{n_compared} tickers compared, {n_excluded} excluded '
              f'(>5% cross-source ratio anomaly).')

        price_fwd = price_fwd_yf
    else:
        price_fwd = sp_forward.sort_index().groupby(level=0).last()['Adj. Close'].rename('p_fwd')

    prices = price_at_adj.to_frame().join(price_fwd, how='inner')
    prices['fwd_return'] = prices['p_fwd'] / prices['p_at_adj'] - 1
    prices = prices[prices['fwd_return'].between(-0.95, 5.0)]
    print(f'\nForward-return universe: {len(prices)} tickers')

    # --- Phase 2.2: Survivorship correction via delisted-ticker proxy ---
    # Tickers that had filings before the cutoff but did NOT trade within the
    # forward window (or stopped trading >60 days before forward end) are
    # presumed delisted during the period. Assign a punitive return.
    # Anchor: Shumway (1997, J. Finance) finds CRSP delist returns average
    # ~-55% to -75% in the delisting month. -0.30 is a conservative lower
    # bound (assumes some recovery via M&A premium or partial liquidation).
    DELIST_PROXY_RETURN = -0.30
    period_universe = set(income_h.index.get_level_values('Ticker').unique())
    priced_set = set(prices.index)
    presumed_missing = period_universe - priced_set
    last_trade_full = sp.groupby(level='Ticker').apply(
        lambda g: g.index.get_level_values('Date').max())
    forward_end_ts = pd.Timestamp(forward_str) + pd.Timedelta(days=10)
    delist_cutoff_ts = forward_end_ts - pd.Timedelta(days=60)
    truly_delisted = {
        t for t in presumed_missing
        if t in last_trade_full.index and last_trade_full[t] < delist_cutoff_ts
    }
    n_proxy = len(truly_delisted)
    if n_proxy > 0:
        proxy_rows = pd.DataFrame({
            'p_at_adj': np.nan,
            'p_fwd': np.nan,
            'fwd_return': DELIST_PROXY_RETURN,
        }, index=list(truly_delisted))
        proxy_rows.index.name = 'Ticker'
        prices = pd.concat([prices, proxy_rows])
        print(f'  Survivorship proxy: +{n_proxy} delisted tickers @ '
              f'{DELIST_PROXY_RETURN:+.0%} (Shumway 1997 anchor)')
        print(f'  Forward-return universe (corrected): {len(prices)} tickers')

    merged = df.merge(prices['fwd_return'], left_on='ticker', right_index=True, how='left')
    scored = merged.dropna(subset=['potential_score', 'fwd_return']).copy()
    print(f'With both score and forward return: {len(scored)}')

    # ----- Transaction costs: bid-ask floor + market impact (Phase 2.3) -----
    # Floor: 20bps large-cap, 50bps small-cap one-way (bid-ask spread proxy).
    large_thresh = 10e9
    scored['tc_bps'] = np.where(
        scored.get('market_cap', pd.Series(0, index=scored.index)).fillna(0) >= large_thresh,
        20, 50,
    )
    # Square-root market impact (Almgren-Chriss / Bouchaud-Toth):
    #   impact_bps ≈ Y * sigma_daily * 10000 * sqrt(Q / ADV)
    # Y=1 calibrated for US large caps (CFM and others).
    # Q = $1M target position size (retail scale).
    # ADV column = avg_dollar_volume_30d (market_screener.py).
    # realized_vol from screener is ANNUALIZED, so convert to daily via /sqrt(252).
    # Cap impact at 100bps to prevent blow-ups on ultra-thin names.
    TARGET_Q = 1_000_000.0
    IMPACT_Y_LOC = 1.0
    adv = pd.to_numeric(
        scored.get('avg_dollar_volume_30d', pd.Series(np.nan, index=scored.index)),
        errors='coerce',
    )
    realized_vol_ann = pd.to_numeric(
        scored.get('realized_vol', pd.Series(np.nan, index=scored.index)),
        errors='coerce',
    )
    sigma_daily = (realized_vol_ann / np.sqrt(252)).fillna(0.02)
    size_ratio = (TARGET_Q / adv).replace([np.inf, -np.inf], np.nan).fillna(0).clip(0, 1)
    impact_bps = IMPACT_Y_LOC * sigma_daily * 10000 * np.sqrt(size_ratio)
    impact_bps = impact_bps.clip(upper=100.0)
    scored['impact_bps'] = impact_bps
    scored['total_tc_bps'] = scored['tc_bps'] + scored['impact_bps']
    scored['fwd_return_net'] = scored['fwd_return'] - scored['total_tc_bps'] * 2 / 10000
    mean_floor = scored['tc_bps'].mean()
    mean_impact = float(impact_bps.mean())
    n_with_vol = int(realized_vol_ann.notna().sum())
    n_with_adv = int(adv.notna().sum())
    print(f'  Costs: floor={mean_floor:.1f}bps + impact={mean_impact:.1f}bps '
          f'(round-trip 2x in net return)')
    print(f'  realized_vol coverage: {n_with_vol}/{len(scored)}  '
          f'avg_dollar_volume_30d coverage: {n_with_adv}/{len(scored)}')
    if n_with_vol < 0.5 * len(scored):
        print(f'  WARNING: realized_vol coverage <50% — impact uses 2% daily fallback.')

    # ----- Decile analysis (gross) -----
    print()
    print('=' * 70)
    print(f'DECILE FORWARD RETURNS ({label}, regime={regime_label})')
    print('=' * 70)
    scored['decile'] = pd.qcut(scored['potential_score'], 10, labels=False, duplicates='drop') + 1
    agg = scored.groupby('decile').agg(
        n=('ticker', 'count'),
        mean_ret=('fwd_return', 'mean'),
        median_ret=('fwd_return', 'median'),
        std_ret=('fwd_return', 'std'),
        sharpe=('fwd_return', lambda s: s.mean() / s.std() if s.std() > 0 else np.nan),
        win_rate=('fwd_return', lambda s: (s > 0).mean()),
    ).round(4)
    print(agg)

    top_dec = scored[scored['decile'] == 10]['fwd_return']
    bot_dec = scored[scored['decile'] == 1]['fwd_return']
    top_mean = top_dec.mean()
    bot_mean = bot_dec.mean()
    spread = top_mean - bot_mean
    # Phase 2.2: univ_mean now uses the proxy-corrected forward-return universe
    # (includes delisted-ticker ghosts at DELIST_PROXY_RETURN). Top/bottom decile
    # means are NOT affected because proxy rows lack scores and never enter `scored`.
    univ_mean = prices['fwd_return'].mean()
    n_universe = len(prices)
    top_alpha = top_mean - univ_mean
    print(f'\nTop decile mean:    {top_mean:>+7.2%}')
    print(f'Universe mean:      {univ_mean:>+7.2%}  '
          f'(n={n_universe} incl. {n_proxy} delisted-proxy)')
    print(f'TOP-DECILE ALPHA:   {top_alpha:>+7.2%}  <-- realistic long-only edge')
    print(f'(Bottom decile:     {bot_mean:>+7.2%})')
    print(f'(Long-short spread: {spread:>+7.2%}  -- aspirational; bottom often unshortable)')

    # ----- Net-of-costs decile analysis -----
    scored['decile_net'] = pd.qcut(scored['potential_score'], 10, labels=False, duplicates='drop') + 1
    top_net = scored.loc[scored['decile_net'] == 10, 'fwd_return_net'].mean() if (scored['decile_net'] == 10).any() else np.nan
    bot_net = scored.loc[scored['decile_net'] == 1, 'fwd_return_net'].mean() if (scored['decile_net'] == 1).any() else np.nan
    spread_net = (top_net - bot_net) if (pd.notna(top_net) and pd.notna(bot_net)) else np.nan
    cost_drag_bps = (spread - spread_net) * 10000 if (pd.notna(spread) and pd.notna(spread_net)) else np.nan
    print(f'\nNet of costs:')
    print(f'  Top decile:     {top_net:>+7.2%}')
    print(f'  Bottom decile:  {bot_net:>+7.2%}')
    print(f'  Net spread:     {spread_net:>+7.2%}')
    print(f'  Cost drag:      {cost_drag_bps:>7.1f} bps' if pd.notna(cost_drag_bps) else '')

    # Phase 2.1: net-of-cost alpha (top decile vs universe).
    # univ_net uses scored['fwd_return_net'] (cost-burdened scored set) NOT
    # prices, because proxy rows have no cost. Proxy is for gross-universe
    # baseline only; cost-adjusted universe = cost-adjusted scored.
    univ_net = (scored['fwd_return_net']).mean() if 'fwd_return_net' in scored.columns else univ_mean
    top_alpha_net = top_net - univ_net if pd.notna(top_net) else np.nan
    print(f'  TOP-ALPHA NET:    {top_alpha_net:>+7.2%}')

    # ----- Monthly returns, drawdown & Calmar -----
    monthly_ret = compute_monthly_returns(sp_forward, sp_at, cutoff)
    drawdowns = {}
    calmars = {}
    for d in sorted(scored['decile'].dropna().unique()):
        d_int = int(d)
        tickers_d = scored.loc[scored['decile'] == d_int, 'ticker'].tolist()
        avail = [t for t in tickers_d if t in monthly_ret.columns]
        if len(avail) >= 5:
            port_ret = monthly_ret[avail].mean(axis=1)
            drawdowns[d_int] = round(compute_portfolio_drawdown(port_ret), 4)
            calmars[d_int] = round(compute_portfolio_calmar(port_ret), 4)
        else:
            drawdowns[d_int] = np.nan
            calmars[d_int] = np.nan
    if pd.notna(drawdowns.get(10, np.nan)):
        print(f'\n  Top-decile drawdown: {drawdowns[10]:.2%}')
    if pd.notna(calmars.get(10, np.nan)):
        print(f'  Top-decile Calmar:   {calmars[10]:.2f}')
    if pd.notna(drawdowns.get(1, np.nan)):
        print(f'  Bot-decile drawdown: {drawdowns[1]:.2%}')

    # ----- Quantum §Stage 1.2: HRP-weighted top decile (López de Prado, 2016) -----
    # Build trailing-daily-return covariance from sp_at (no look-ahead), feed
    # _hrp_weights → recursive-bisection inverse-variance weights, then apply
    # to the forward-period monthly_ret. Compare to equal-weight on same set.
    hrp_summary = {}
    top_dec_tickers = scored.loc[scored['decile'] == 10, 'ticker'].tolist()
    top_avail_monthly = [t for t in top_dec_tickers if t in monthly_ret.columns]
    if len(top_avail_monthly) >= 5:
        try:
            prices_pre = sp_at['Adj. Close'].unstack(level=0).sort_index().ffill()
            trailing_ret = prices_pre.pct_change().tail(252)
            avail_hrp = [t for t in top_avail_monthly if t in trailing_ret.columns]
            # require >=60% of the 252 daily returns non-null to compute cov
            req = max(60, int(0.60 * len(trailing_ret)))
            ret_for_hrp = trailing_ret[avail_hrp].dropna(axis=1, thresh=req)
            if ret_for_hrp.shape[1] >= 5:
                hrp_w = _hrp_weights(ret_for_hrp.fillna(0.0))
                hrp_w = hrp_w[hrp_w > 0]
                w_aligned = hrp_w.reindex([t for t in hrp_w.index if t in monthly_ret.columns])
                w_aligned = w_aligned / w_aligned.sum()
                hrp_port_ret = (monthly_ret[w_aligned.index] * w_aligned.values).sum(axis=1)
                hrp_fwd = (1 + hrp_port_ret.fillna(0)).prod() - 1
                hrp_dd = compute_portfolio_drawdown(hrp_port_ret)
                hrp_calmar = compute_portfolio_calmar(hrp_port_ret)
                ew_port_ret = monthly_ret[w_aligned.index].mean(axis=1)
                ew_fwd = (1 + ew_port_ret.fillna(0)).prod() - 1
                ew_dd = compute_portfolio_drawdown(ew_port_ret)
                vol_ew = float(ew_port_ret.std())
                vol_hrp = float(hrp_port_ret.std())
                vol_red_pct = ((vol_ew - vol_hrp) / vol_ew * 100.0) if vol_ew > 0 else 0.0
                max_w = float(w_aligned.max())
                top5_hrp_weight = float(w_aligned.nlargest(5).sum())
                ew_equiv = 5.0 / len(w_aligned)
                print(f'\n  HRP top-decile (Stage 1.2, n={len(w_aligned)}):  '
                      f'fwd={hrp_fwd:>+7.2%}  dd={hrp_dd:.2%}  Calmar={hrp_calmar:.2f}  '
                      f'max_w={max_w:.1%}')
                print(f'  HRP top-5 weight share: {top5_hrp_weight:.1%}  '
                      f'(equal-weight equivalent: {ew_equiv:.1%})')
                print(f'  Equal-weight (same names):       '
                      f'fwd={ew_fwd:>+7.2%}  dd={ew_dd:.2%}  '
                      f'vol_reduction={vol_red_pct:+.1f}%')
                hrp_summary = {
                    'hrp_fwd': hrp_fwd, 'hrp_dd': hrp_dd, 'hrp_calmar': hrp_calmar,
                    'ew_fwd': ew_fwd, 'ew_dd': ew_dd, 'vol_reduction_pct': vol_red_pct,
                    'n_hrp': len(w_aligned), 'max_weight': max_w,
                    'top5_weight': top5_hrp_weight, 'ew_equiv': ew_equiv,
                }
            else:
                print(f'\n  HRP top-decile: insufficient cov data '
                      f'({ret_for_hrp.shape[1]} tickers with {req}+ obs).')
        except Exception as exc:
            print(f'\n  HRP top-decile: error — {exc}')

    # ----- Survivorship -----
    tickers_period = set(income_h.index.get_level_values('Ticker').unique())
    last_trade = sp.groupby(level='Ticker').apply(
        lambda g: g.index.get_level_values('Date').max())
    latest_sp_date = sp.index.get_level_values('Date').max()
    alive_cutoff = latest_sp_date - pd.Timedelta(days=60)
    alive_tickers = set(last_trade[last_trade >= alive_cutoff].index)
    missing = tickers_period - alive_tickers
    pct_missing = (len(missing) / len(tickers_period) * 100) if tickers_period else 0
    print(f'\n  Survivorship: {len(missing)}/{len(tickers_period)} ({pct_missing:.1f}%) '
          f'delisted since {cutoff.year}')

    # ----- Sector-neutral spread -----
    sn_top_pieces = []
    sn_bot_pieces = []
    for sg in scored['sector_group'].dropna().unique():
        sub = scored[scored['sector_group'] == sg]
        if len(sub) < 30:
            print(f'    Skipping {sg} from sector-neutral (n={len(sub)} < 30)')
            continue
        n_decile = max(2, len(sub) // 10)
        sn_top_pieces.append(sub.nlargest(n_decile, 'potential_score'))
        sn_bot_pieces.append(sub.nsmallest(n_decile, 'potential_score'))
    if sn_top_pieces:
        sn_top = pd.concat(sn_top_pieces)
        sn_bot = pd.concat(sn_bot_pieces)
        sn_spread = sn_top.fwd_return.mean() - sn_bot.fwd_return.mean()
        print(f'\n  Sector-neutral: top={sn_top.fwd_return.mean():+.2%}, '
              f'bot={sn_bot.fwd_return.mean():+.2%}, spread={sn_spread:+.2%}, '
              f'alpha={sn_top.fwd_return.mean() - univ_mean:+.2%}')

    # ----- Information Coefficient -----
    ic = compute_ic(scored)
    print(f'\n  IC (Spearman rank): {ic:+.4f}')

    # ----- Factor-level ICs -----
    factor_ics = compute_factor_ics(scored)
    print('  Factor ICs:')
    for f, v in factor_ics.items():
        # Phase 1.3: use a distinct loop var so `label` (period label) is not
        # clobbered. Earlier code reassigned `label = f.replace(...)` here and
        # leaked 'sentiment' into the summary dict.
        f_lbl = f.replace('_score', '')
        print(f'    {f_lbl:10s}  IC={v:+.4f}' if pd.notna(v) else f'    {f_lbl:10s}  IC=N/A')

    # ----- Capacity constraint check -----
    n_capped = 0
    if 'dollar_volume' in scored.columns:
        top_dec = scored[scored['decile'] == 10]
        max_pos = round(0.10 * scored['market_cap'].median())
        for _, r in top_dec.iterrows():
            dv = r.get('dollar_volume', 0) or 0
            if dv > 0 and max_pos / dv > 0.10:
                n_capped += 1
    cap_str = f'{n_capped} capped' if n_capped > 0 else 'none capped'

    # ----- Sensitivity: +/-5pp weight perturbation -----
    sens = compute_sensitivity(scored)
    min_s = sens.get('min_spread')
    max_s = sens.get('max_spread')
    s_range = sens.get('range')
    baseline_s = sens.get('baseline', np.nan)
    mode_s = sens.get('mode', '?')
    print(f'  Sensitivity (+/-5pp, mode={mode_s}):')
    print(f'    Reported spread:  {spread:>+7.2%}')
    if pd.notna(baseline_s):
        print(f'    Sensitivity base: {baseline_s:>+7.2%}  '
              f'(should approximate reported spread)')
    if pd.notna(min_s) and pd.notna(max_s):
        print(f'    Range:            [{min_s:+.2%}, {max_s:+.2%}]  '
              f'width={s_range:+.2%}')
    for k, v in sens.items():
        if k in ('min_spread', 'max_spread', 'range', 'baseline', 'mode'):
            continue
        if pd.notna(v):
            print(f'    {k}: spread={v:+.2%}')

    # ----- Sector concentration in top decile -----
    top_decile = scored[scored['decile'] == 10]
    sec_conc = top_decile['sector_group'].value_counts()
    sec_conc_pct = sec_conc / len(top_decile) * 100
    print(f'\n  Top-decile sector concentration (n={len(top_decile)}):')
    for sg, n in sec_conc.head(5).items():
        print(f'    {sg or "(none)":20s} {n:>4d} ({sec_conc_pct[sg]:>5.1f}%)')

    # ----- Phase 1.4: universe-vs-top-decile sector representation -----
    # Surface sector tilts so we can see if e.g. energy is absent from the
    # universe (data coverage) vs absent only from the top decile (scoring).
    print(f'\n  Universe-vs-top-decile sector representation:')
    uni_secs = scored['sector_group'].value_counts(dropna=False)
    top_secs = scored.loc[scored['decile'] == 10, 'sector_group'].value_counts(dropna=False)
    all_secs = sorted(
        set(uni_secs.index) | set(top_secs.index),
        key=lambda x: (x is None or (isinstance(x, float) and pd.isna(x)), str(x))
    )
    n_top_dec = int((scored['decile'] == 10).sum())
    print(f'    {"sector":20s} {"univ_n":>7s} {"univ_%":>7s} '
          f'{"top_n":>6s} {"top_%":>6s} {"over/under":>10s}')
    for sec in all_secs:
        un = int(uni_secs.get(sec, 0))
        tn = int(top_secs.get(sec, 0))
        up = (un / len(scored) * 100) if len(scored) > 0 else 0.0
        tp = (tn / n_top_dec * 100) if n_top_dec > 0 else 0.0
        ou = tp - up
        sec_label = '(none)' if sec is None or (isinstance(sec, float) and pd.isna(sec)) else str(sec)
        print(f'    {sec_label:20s} {un:>7d} {up:>6.1f}% '
              f'{tn:>6d} {tp:>5.1f}% {ou:>+9.1f}pp')

    return {
        'label': label,
        'n': len(scored),
        'top_mean': top_mean,
        'bot_mean': bot_mean,
        'spread': spread,
        'top_alpha': top_alpha,
        'top_alpha_net': top_alpha_net,
        'spread_net': spread_net,
        'universe_mean': univ_mean,
        'n_proxy': n_proxy,
        'survivor_pct': pct_missing,
        'regime': regime_label,
        'ic': ic,
        'sensitivity': sens,
        'top_sharpe': top_dec.mean() / top_dec.std() if top_dec.std() > 0 else np.nan,
        'top_drawdown': drawdowns.get(10, np.nan),
        'top_calmar': calmars.get(10, np.nan),
        'factor_ics': factor_ics,
        'n_capped': n_capped,
        'sector_concentration': sec_conc.to_dict() if not sec_conc.empty else {},
        'top_tickers': set(scored.loc[scored['decile'] == 10, 'ticker'].tolist()),
        'hrp': hrp_summary,
    }, scored


def main():
    parser = argparse.ArgumentParser(
        description='Walk-forward backtest of v2 scoring engine across semi-annual windows.')
    parser.add_argument('--acknowledge-bias', action='store_true',
                        help='Skip survivorship-bias warning pause.')
    parser.add_argument('--oos-reserve',
                        help='Reserve a year (e.g. 2023) as final out-of-sample holdout.')
    parser.add_argument('--audit-dir',
                        help='Directory to save per-period scored CSVs and audit log.')
    parser.add_argument('--run-id', default=None,
                        help='Optional run identifier for audit log naming.')
    args = parser.parse_args()

    api_key = os.environ.get('SIMFIN_API_KEY')
    if not api_key:
        sys.exit('SIMFIN_API_KEY not set')
    sf.set_api_key(api_key)
    sf.set_data_dir(str(ROOT / 'data' / 'simfin'))

    print(SURVIVORSHIP_WARNING)
    if not args.acknowledge_bias:
        print('  (pausing 3s -- pass --acknowledge-bias to skip)\n')
        time.sleep(3)

    # ----- Load raw data ONCE -----
    print('\nLoading SimFin data...')
    data = ms.load_simfin_data()
    companies, industries, income, balance, cashflow, income_q, cashflow_q, sp = data
    full_hist_frame = ms.compute_history_metrics_full(income, balance, cashflow)
    print(f'  Full history: {len(full_hist_frame)} rows, '
          f'{full_hist_frame["Ticker"].nunique()} tickers.')
    print(f'  Running {len(PERIODS)} periods...\n')

    all_results = []
    all_scored_list = []
    last_scored = None
    prev_top_tickers = None
    oos_result = None
    oos_scored = None

    # Move OOS-reserved period(s) to end.
    # --oos-reserve YYYY holds out EVERY period whose FORWARD year == YYYY so
    # that no part of the OOS year leaks into training. (Matching p[0]/cutoff
    # would mismatch: the full-year-2024 period has cutoff 2023-12-31.) The
    # full-year period (latest forward date) is reported as the single OOS
    # result; any other same-forward-year period (e.g. an overlapping H2
    # window) is also excluded from training but not reported.
    periods = list(PERIODS)

    # Pre-flight: PERIODS may contain pre-registered entries with unfilled state
    # var placeholders (None). If the operator explicitly targets such a period
    # via --oos-reserve, ABORT loudly. Otherwise, silently drop unready periods
    # from the runlist so standard reproduction runs remain deterministic.
    _STATE_KEYS = ('t10y', 'ism', 'curve_10y2y', 'core_cpi_yoy', 'hy_oas')
    ready = []
    for p in periods:
        missing = [k for k, v in zip(_STATE_KEYS, p[2:]) if v is None]
        if missing:
            if args.oos_reserve and p[1][:4] == args.oos_reserve:
                sys.exit(
                    f'ERROR: --oos-reserve {args.oos_reserve} targets period '
                    f'{p[0]}->{p[1]} but state vars {missing} are None '
                    f'placeholders. Fill values from FRED/ISM/HY-OAS and '
                    f'pre-register in docs/phase5_oos_2025_decision.md before '
                    f'the locked run.'
                )
            print(f'  Note: skipping pre-registered period {p[0]}->{p[1]} '
                  f'(state vars unfilled: {missing}).')
            continue
        ready.append(p)
    periods = ready
    heldout_idxs = set()
    oos_report_idx = None
    if args.oos_reserve:
        matches = [i for i, p in enumerate(periods)
                   if p[1][:4] == args.oos_reserve]
        if not matches:
            sys.exit(f'ERROR: no period with forward year {args.oos_reserve!r}. '
                     f'Available forward years: {[p[1][:4] for p in periods]}')
        oos_report_period = max((periods[i] for i in matches), key=lambda p: p[1])
        keep = [p for i, p in enumerate(periods) if i not in matches]
        heldout = [periods[i] for i in matches]
        periods = keep + heldout
        heldout_idxs = set(range(len(keep), len(periods)))
        oos_report_idx = periods.index(oos_report_period)

    for idx, (cutoff_str, forward_str, t10y, ism, curve, cpi, oas) in enumerate(periods):
        summary, scored = run_one_period(
            cutoff_str, forward_str, t10y, ism, curve, cpi, oas, *data,
            full_hist_frame=full_hist_frame,
        )
        # Compute turnover vs previous period's top decile
        if prev_top_tickers is not None and summary['n'] > 0:
            curr_top = summary.get('top_tickers', set())
            if len(curr_top) > 0 and len(prev_top_tickers) > 0:
                overlap = curr_top & prev_top_tickers
                summary['turnover'] = 1.0 - len(overlap) / max(len(curr_top), len(prev_top_tickers))
            else:
                summary['turnover'] = np.nan
        else:
            summary['turnover'] = np.nan
        if summary['n'] > 0 and 'top_tickers' in summary:
            prev_top_tickers = summary['top_tickers']

        if args.oos_reserve and idx in heldout_idxs:
            # Held out from training entirely. Only the full-year period is
            # reported as OOS; other same-forward-year periods are excluded
            # silently (prevents 2024 leaking into training summary stats).
            summary['is_oos'] = (idx == oos_report_idx)
            if idx == oos_report_idx:
                oos_result = summary
                oos_scored = scored
        else:
            summary['is_oos'] = False
            if scored is not None:
                all_results.append(summary)
                all_scored_list.append(scored)
                last_scored = scored

    # ----- Detailed by-sector breakdown (most recent period) -----
    if last_scored is not None:
        print()
        print('=' * 70)
        print('TOP-VS-BOTTOM BY SECTOR (most recent period)')
        print('=' * 70)
        scored = last_scored
        for sg in sorted(scored['sector_group'].dropna().unique()):
            sub = scored[scored['sector_group'] == sg]
            if len(sub) < 20:
                continue
            try:
                sub_dec = pd.qcut(sub['potential_score'], 5, labels=False, duplicates='drop') + 1
            except ValueError:
                continue
            sub = sub.assign(qntl=sub_dec)
            top_s = sub[sub.qntl == sub.qntl.max()]['fwd_return']
            bot_s = sub[sub.qntl == sub.qntl.min()]['fwd_return']
            if len(top_s) > 0 and len(bot_s) > 0:
                print(f'  {sg:18s} n={len(sub):>4d}  top={top_s.mean():>+7.2%}  '
                      f'bot={bot_s.mean():>+7.2%}  spread={top_s.mean()-bot_s.mean():>+7.2%}')

    # ----- Summary table -----
    print()
    print()
    print('=' * 70)
    print('MULTI-PERIOD SUMMARY')
    print('=' * 70)
    # Phase 1.3 defensive: if label is missing, malformed, or accidentally a
    # factor name (legacy leak), reconstruct from period idx. Harmless if
    # labels already correct.
    _BAD_LABELS = {'valuation', 'quality', 'growth', 'sentiment'}
    for i, r in enumerate(all_results):
        lbl = r.get('label')
        if (not isinstance(lbl, str) or len(lbl) < 4 or '->' not in lbl
                or lbl.lower() in _BAD_LABELS):
            cutoff_year = 2021 + i // 2
            forward_year = cutoff_year + 1
            r['label'] = f'{cutoff_year}->{forward_year}'

    print(f'  {"Period":14s} {"n":>6s}  {"TopDec":>8s}  {"Univ":>8s}  '
          f'{"ALPHA":>8s}  {"AlphaNet":>9s}  {"BotDec":>8s}  {"Spread":>8s}  '
          f'{"Sharpe":>7s}  {"DD":>7s}  {"Calmar":>7s}  {"Turn":>6s}  '
          f'{"Regime":>10s}')
    print(f'  {"-"*14} {"-"*6}  {"-"*8}  {"-"*8}  {"-"*8}  {"-"*9}  '
          f'{"-"*8}  {"-"*8}  {"-"*7}  {"-"*7}  {"-"*7}  {"-"*6}  {"-"*10}')
    n_pos = 0
    n_valid = 0
    for r in all_results:
        if r['n'] == 0:
            print(f'  {r["label"]:14s} {"SKIPPED":>6s}')
            continue
        n_valid += 1
        pos = r['spread'] > 0
        if pos:
            n_pos += 1
        sh = r.get('top_sharpe', np.nan)
        dd = r.get('top_drawdown', np.nan)
        ca = r.get('top_calmar', np.nan)
        to = r.get('turnover', np.nan)
        ta = r.get('top_alpha', np.nan)
        tan = r.get('top_alpha_net', np.nan)
        ta_str = f'{ta:>+7.2%}'   if pd.notna(ta)  else ' ' * 8
        tan_str = f'{tan:>+8.2%}' if pd.notna(tan) else ' ' * 9
        sh_str = f'{sh:>+6.2f}'   if pd.notna(sh)  else ' ' * 7
        dd_str = f'{dd:>6.2%}'    if pd.notna(dd)  else ' ' * 7
        ca_str = f'{ca:>+6.2f}'   if pd.notna(ca)  else ' ' * 7
        to_str = f'{to:>5.1%}'    if pd.notna(to)  else ' ' * 6
        print(f'  {r["label"]:14s} {r["n"]:>6d}  {r["top_mean"]:>+7.2%}  '
              f'{r["universe_mean"]:>+7.2%}  {ta_str}  {tan_str}  '
              f'{r["bot_mean"]:>+7.2%}  {r["spread"]:>+7.2%}  '
              f'{sh_str}  {dd_str}  {ca_str}  {to_str}  {r["regime"]:>10s}')
    if n_valid > 0:
        valid_results = [r for r in all_results if r['n'] > 0]
        mean_spread = np.mean([r['spread'] for r in valid_results])
        mean_net = np.nanmean([r.get('spread_net', np.nan) for r in valid_results])
        mean_alpha = np.nanmean([r.get('top_alpha', np.nan) for r in valid_results])
        mean_alpha_net = np.nanmean([r.get('top_alpha_net', np.nan) for r in valid_results])
        n_alpha_pos = sum(1 for r in valid_results if r.get('top_alpha', 0) > 0)
        print(f'  {"-"*14}')
        print(f'  {"Mean Alpha":14s} {"":>6s}  {"":>8s}  {"":>8s}  '
              f'{mean_alpha:>+7.2%}  {mean_alpha_net:>+8.2%}  {"":>8s}  {"":>8s}'
              f'  <-- realistic long-only edge')
        print(f'  {"Mean Spread":14s} {"":>6s}  {"":>8s}  {"":>8s}  {"":>8s}  '
              f'{"":>9s}  {"":>8s}  {mean_spread:>+7.2%}'
              f'  <-- aspirational; bottom often unshortable')
        print(f'  {"Mean NetSpr":14s} {"":>6s}  {"":>8s}  {"":>8s}  {"":>8s}  '
              f'{"":>9s}  {"":>8s}  {mean_net:>+7.2%}')
        print(f'  Alpha Hit Rate:   {n_alpha_pos}/{n_valid} periods where top_alpha > 0')
        print(f'  Spread Hit Rate:  {n_pos}/{n_valid} periods where spread > 0')

    # ----- Quantum §Stage 1.2: HRP vs Equal-Weight summary -----
    hrp_rows = [r for r in all_results if r['n'] > 0 and r.get('hrp')]
    if hrp_rows:
        print()
        print('=' * 70)
        print('HRP TOP-DECILE vs EQUAL-WEIGHT (Quantum §Stage 1.2)')
        print('=' * 70)
        print(f'  {"Period":14s}  {"n":>4s}  {"HRP fwd":>9s}  {"EW fwd":>9s}  '
              f'{"HRP dd":>8s}  {"EW dd":>8s}  {"HRP Cal":>7s}  {"VolRed":>7s}  '
              f'{"MaxW":>6s}')
        hrp_fwd_all, ew_fwd_all, vol_red_all = [], [], []
        for r in hrp_rows:
            h = r['hrp']
            print(f'  {r["label"]:14s}  {h["n_hrp"]:>4d}  '
                  f'{h["hrp_fwd"]:>+8.2%}  {h["ew_fwd"]:>+8.2%}  '
                  f'{h["hrp_dd"]:>+7.2%}  {h["ew_dd"]:>+7.2%}  '
                  f'{h["hrp_calmar"]:>+6.2f}  '
                  f'{h["vol_reduction_pct"]:>+6.1f}%  {h["max_weight"]:>5.1%}')
            hrp_fwd_all.append(h['hrp_fwd'])
            ew_fwd_all.append(h['ew_fwd'])
            vol_red_all.append(h['vol_reduction_pct'])
        if hrp_fwd_all:
            print(f'  {"-"*14}')
            print(f'  {"Mean":14s}  {"":>4s}  '
                  f'{np.mean(hrp_fwd_all):>+8.2%}  {np.mean(ew_fwd_all):>+8.2%}  '
                  f'{"":>8s}  {"":>8s}  {"":>7s}  '
                  f'{np.mean(vol_red_all):>+6.1f}%')

    # ----- Regime-by-regime breakdown -----
    if n_valid > 0:
        print()
        print('=' * 70)
        print('REGIME BREAKDOWN')
        print('=' * 70)
        regime_groups = {}
        for r in all_results:
            if r['n'] == 0:
                continue
            rg = r.get('regime', 'unknown')
            if rg not in regime_groups:
                regime_groups[rg] = []
            regime_groups[rg].append(r)
        for rg, results in sorted(regime_groups.items()):
            spreads = [r['spread'] for r in results]
            mean_r = np.mean(spreads)
            n_periods = len(results)
            n_positive = sum(1 for s in spreads if s > 0)
            print(f'  {rg:10s}  n_periods={n_periods}  mean_spread={mean_r:>+7.2%}  '
                  f'hit_rate={n_positive}/{n_periods}')

    # ----- IC summary -----
    if n_valid > 0:
        print()
        print('=' * 70)
        print('INFORMATION COEFFICIENT BY PERIOD')
        print('=' * 70)
        ic_values = []
        for r in all_results:
            if r['n'] == 0 or r.get('ic') is None:
                continue
            ic_values.append(r['ic'])
            print(f'  {r["label"]:14s}  IC={r["ic"]:+.4f}')
        if ic_values:
            mean_ic = np.mean(ic_values)
            ic_std = np.std(ic_values)
            print(f'  {"---":14s}')
            print(f'  Mean IC:          {mean_ic:+.4f}')
            print(f'  Std IC:           {ic_std:.4f}')
            print(f'  IC/sqrt(N):       {mean_ic * np.sqrt(len(ic_values)):+.4f}  '
                  f'(t-stat proxy)')
            print(f'  IC > 0 periods:   {sum(1 for v in ic_values if v > 0)}/{len(ic_values)}')

    # ----- Factor IC summary -----
    if n_valid > 0:
        print()
        print('=' * 70)
        print('FACTOR IC BY PERIOD')
        print('=' * 70)
        factor_names = ['valuation_score', 'quality_score', 'growth_score', 'sentiment_score']
        header = f'  {"Period":14s}'
        for fn in factor_names:
            header += f'  {fn[:4]:>6s}'
        print(header)
        print(f'  {"-"*14}  {"-"*6}  {"-"*6}  {"-"*6}  {"-"*6}')
        factor_ic_cols = {fn: [] for fn in factor_names}
        for r in all_results:
            if r['n'] == 0:
                continue
            fic = r.get('factor_ics', {})
            line = f'  {r["label"]:14s}'
            for fn in factor_names:
                v = fic.get(fn, np.nan)
                line += f'  {v:>+6.4f}' if pd.notna(v) else f'  {"N/A":>6s}'
                if pd.notna(v):
                    factor_ic_cols[fn].append(v)
            print(line)
        print(f'  {"---":14s}  {"-"*6}  {"-"*6}  {"-"*6}  {"-"*6}')
        mean_line = f'  {"Mean":14s}'
        for fn in factor_names:
            vals = factor_ic_cols[fn]
            mean_line += f'  {np.mean(vals):>+6.4f}' if vals else '  {"N/A":>6s}'
        print(mean_line)

    # ----- Sensitivity summary -----
    if n_valid > 0:
        print()
        print('=' * 70)
        print('SENSITIVITY ANALYSIS (±5pp WEIGHT PERTURBATION)')
        print('=' * 70)
        print(f'  {"Period":14s} {"BaseSpread":>10s} {"MinSpread":>10s} '
              f'{"MaxSpread":>10s} {"Range":>10s}')
        print(f'  {"-"*14} {"-"*10} {"-"*10} {"-"*10} {"-"*10}')
        for r in all_results:
            if r['n'] == 0:
                continue
            sens = r.get('sensitivity', {})
            base = r['spread']
            mn = sens.get('min_spread', np.nan)
            mx = sens.get('max_spread', np.nan)
            rng = sens.get('range', np.nan)
            print(f'  {r["label"]:14s}  {base:>+9.2%}  {mn:>+9.2%}  '
                  f'{mx:>+9.2%}  {rng:>+9.2%}')

    # ----- OOS validation report -----
    # ----- Audit log -----
    if args.audit_dir:
        audit_path = Path(args.audit_dir)
        audit_path.mkdir(parents=True, exist_ok=True)
        run_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        run_id = args.run_id or run_ts
        safe_label = lambda lbl: lbl.replace('->', '_to_').replace(' ', '_')

        for r, s in zip(all_results, all_scored_list):
            if r['n'] == 0 or s is None:
                continue
            safe = safe_label(r['label'])
            period_file = audit_path / f'scored_{safe}_{run_id}.csv'
            cols = [c for c in ['ticker', 'sector_group', 'potential_score',
                'valuation_score', 'quality_score', 'growth_score', 'sentiment_score',
                'fwd_return', 'fwd_return_net', 'decile', 'regime_label']
                if c in s.columns]
            s[cols].to_csv(period_file, index=False)

        # Persist the held-out OOS scored frame (excluded from all_scored_list)
        # so post-hoc diagnostics can run without re-executing the backtest.
        if oos_result is not None and oos_scored is not None and oos_result['n'] > 0:
            safe = safe_label(oos_result['label'])
            oos_file = audit_path / f'scored_OOS_{safe}_{run_id}.csv'
            cols = [c for c in ['ticker', 'sector_group', 'potential_score',
                'valuation_score', 'quality_score', 'growth_score', 'sentiment_score',
                'fwd_return', 'fwd_return_net', 'decile', 'regime_label']
                if c in oos_scored.columns]
            oos_scored[cols].to_csv(oos_file, index=False)
            print(f'  OOS scored frame saved to {oos_file}')

        meta = {
            'run_id': run_id,
            'timestamp': run_ts,
            'n_periods': len(all_results),
            'oos_reserve': args.oos_reserve,
            'results': [{
                'label': r['label'],
                'n': r['n'],
                'spread': r['spread'],
                'spread_net': r.get('spread_net'),
                'top_alpha': r.get('top_alpha'),
                'top_alpha_net': r.get('top_alpha_net'),
                'universe_mean': r.get('universe_mean'),
                'n_proxy': r.get('n_proxy'),
                'ic': r.get('ic'),
                'regime': r.get('regime'),
                'factor_ics': r.get('factor_ics', {}),
                'hrp': r.get('hrp', {}),
            } for r in all_results],
        }
        with open(audit_path / f'audit_{run_id}.json', 'w') as f:
            json.dump(meta, f, indent=2, default=str)
        print(f'\n  Audit log saved to {audit_path}')

    if oos_result is not None and oos_result['n'] > 0:
        print()
        print('=' * 70)
        print('OUT-OF-SAMPLE VALIDATION')
        print('=' * 70)
        print(f'  Period:     {oos_result["label"]}')
        print(f'  Regime:     {oos_result["regime"]}')
        print(f'  Stocks:     {oos_result["n"]}')
        print(f'  Top decile: {oos_result["top_mean"]:>+7.2%}')
        print(f'  Bot decile: {oos_result["bot_mean"]:>+7.2%}')
        print(f'  Universe:   {oos_result["universe_mean"]:>+7.2%}')
        ta_oos = oos_result.get('top_alpha', np.nan)
        print(f'  TOP-ALPHA:  {ta_oos:>+7.2%}  <-- PRIMARY (top decile - universe)')
        tan_oos = oos_result.get('top_alpha_net', np.nan)
        if pd.notna(tan_oos):
            print(f'  Top-alpha net: {tan_oos:>+7.2%}')
        print(f'  Spread:     {oos_result["spread"]:>+7.2%}')
        sn_oos = oos_result.get('spread_net', np.nan)
        print(f'  Net spread: {sn_oos:>+7.2%}' if pd.notna(sn_oos) else '')
        ic_oos = oos_result.get('ic', np.nan)
        print(f'  IC:         {ic_oos:+.4f}')
        if oos_result['spread'] > 0:
            print(f'\n  PASS: OOS spread positive — model generalizes.')
        else:
            print(f'\n  FAIL: OOS spread negative — model overfit or regime change.')

    # ----- Consistency interpretation (alpha basis) -----
    print()
    print('=' * 70)
    print('CONSISTENCY INTERPRETATION (alpha basis)')
    print('=' * 70)
    if n_valid == 0:
        print('  No periods had sufficient data for meaningful analysis.')
    else:
        valid_results = [r for r in all_results if r['n'] > 0]
        mean_alpha_val = np.nanmean([r.get('top_alpha', 0) for r in valid_results])
        n_alpha_pos = sum(1 for r in valid_results if r.get('top_alpha', 0) > 0)
        if n_alpha_pos >= 5 and n_valid >= 6 and mean_alpha_val > 0.06:
            print('  STRONG EDGE: top-decile beats universe by >6% in 5+/6 periods.')
            print('  This is a deployable retail edge.')
        elif n_alpha_pos >= 4 and n_valid >= 5 and mean_alpha_val > 0.03:
            print('  REAL EDGE: top-decile beats universe in 4+/5 periods, mean >3%.')
            print('  Edge is real; size positions modestly until OOS confirms.')
        elif n_alpha_pos >= 3 and mean_alpha_val > 0:
            print('  WEAK EDGE: top-decile beats universe in most periods but small margin.')
            print('  Insufficient for deployment without further validation.')
        else:
            print('  NO RELIABLE ALPHA: top-decile fails to beat universe consistently.')
            print('  Fundamental rework needed before any deployment.')
    print()
    print('  NOTE: Earlier periods suffer more survivorship bias.')
    print('  Top-alpha is less biased than spread (bottom decile is most contaminated).')
    print('  Phase 2.2 delisted-ticker proxy partially corrects the universe baseline.')
    print(f'\n{"Done.":>72s}')


if __name__ == '__main__':
    main()
