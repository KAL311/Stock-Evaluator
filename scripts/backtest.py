#!/usr/bin/env python3
"""Phase 6 validation: multi-period point-in-time backtest of potential_score.

Runs 4 non-overlapping 1-year windows (2020->2021 through 2023->2024), each
filtering all SimFin data to the period's cutoff date. Measures forward 1-year
decile returns per period. Top decile should consistently beat bottom decile if
the score has predictive power.

NOTE: SimFin daily price data begins 2020-06-10, so the 2019->2020 window is
omitted (zero price history before that date).

Also prints sector balance check and full breakdown of well-known tickers from the
CURRENT cache (not point-in-time).

============================================================================
WARNING TO FUTURE-SELF: DO NOT PASS FINVIZ DATA INTO THE BACKTEST.
============================================================================
The Finviz fields (insider_own, inst_own, short_float) used by the SENTIMENT
sub-score are scraped LIVE from finviz.com and reflect TODAY's ownership
structure, not the as-of-cutoff values. Passing them into compute_snapshot for
a historical backtest = look-ahead contamination: future ownership changes,
short-squeezes, etc., leak into the period's score.

Mitigation here: we explicitly pass `finviz={}` so compute_potential_scores
sees only price-based sentiment components (distance_from_52w_high and
return_12m_minus_1m), both of which are computed from sp data already filtered
to <= the period's cutoff date.

Downstream consequence: _weighted_avg in compute_potential_scores silently
rescales the SENTIMENT_WEIGHTS over the 2 available components (raising their
effective weight from 35%+35%=70% to 100%). That's intended for the backtest.
DO NOT "fix" the missing data by passing today's Finviz dict — that
reintroduces the look-ahead bias.

(The production screener in market_screener.py *does* pass live Finviz data;
that's fine for the live screen since "today" = "now" everywhere.)
============================================================================
"""
import argparse, os, sys, sqlite3, time
from pathlib import Path
import pandas as pd
import numpy as np
import simfin as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
import market_screener as ms  # noqa: E402

# T10Y rates from FRED series DGS10, end-of-year close.
# https://fred.stlouisfed.org/series/DGS10
# Verified 2025-XX-XX (backtest data range limits pre-2020 inclusion).
# If you add a period, look up the rate first.
#   2019-12-31: 1.92%  (untestable — price data starts 2020-06-10)
#   2024-12-31: 4.58%  (untestable — price data ends 2025-05-14, before forward date)
PERIODS = [
    ('2020-12-31', '2021-12-31', 0.0093),  # T10Y end-2020 (~6mo price hist, marginal)
    ('2021-12-31', '2022-12-31', 0.0152),  # T10Y end-2021
    ('2022-12-31', '2023-12-31', 0.0388),  # T10Y end-2022 (12/30 was last trading day)
    ('2023-12-31', '2024-12-31', 0.0388),  # T10Y end-2023 (12/29 was last trading day)
]

# §Phase 1.1 — point-in-time fundamentals lagged ≥45 days.
# 10-Qs file ~45 days after quarter end, 10-Ks ~60-90 days. Using as-of-cutoff
# directly is look-ahead: the data was not yet public. Lag by FILING_LAG_DAYS
# before applying any fundamentals to a backtest period.
FILING_LAG_DAYS = 45

def filter_quarterly(df, cutoff):
    if 'Publish Date' in df.columns:
        lagged_cutoff = cutoff - pd.Timedelta(days=FILING_LAG_DAYS)
        return df[df['Publish Date'] <= lagged_cutoff]
    return df

# §Phase 4.14 — transaction costs by liquidity tier (round-trip bps).
# Large/mid: 20 bps each side = 40 bps round trip. Small: 50 bps each side.
TXN_COST_BPS = {'large': 0.0040, 'mid': 0.0040, 'small': 0.0100, 'micro': 0.0200}

def apply_txn_costs(scored_df):
    """Subtract round-trip cost from fwd_return based on liquidity_tier."""
    if 'liquidity_tier' not in scored_df.columns:
        return scored_df
    tiers = scored_df['liquidity_tier'].fillna('small')
    costs = tiers.map(TXN_COST_BPS).fillna(0.0100)
    out = scored_df.copy()
    out['fwd_return_net'] = out['fwd_return'] - costs
    return out

SURVIVORSHIP_WARNING = """
########################################################################
#                                                                      #
#   WARNING: SURVIVORSHIP BIAS                                         #
#                                                                      #
#   This backtest uses the CURRENT SimFin company universe. Companies  #
#   that delisted, went bankrupt, or were acquired between the cutoff  #
#   date for each period and today are ABSENT from this analysis.      #
#                                                                      #
#   Consequence: bottom-decile results are biased UPWARD because the   #
#   worst outcomes (zeros from bankruptcies, near-zeros from forced    #
#   mergers, etc.) are missing from the data. Top-decile results are   #
#   biased upward less.                                                #
#                                                                      #
#   Treat the top-vs-bottom spread shown below as an UPPER BOUND on    #
#   real-world performance. The true spread is smaller -- possibly      #
#   meaningfully smaller -- once delisted names are included.          #
#                                                                      #
#   NOTE: earlier periods suffer MORE survivorship bias because more   #
#   companies have delisted since. The 2020->2021 results below are   #
#   the LEAST reliable.                                                #
#                                                                      #
#   This bias cannot be fixed without point-in-time company data,      #
#   which SimFin's bulk feeds do not easily provide.                   #
#                                                                      #
########################################################################
"""

def run_one_period(cutoff_str, forward_str, t10y,
                   companies, industries, income, balance, cashflow,
                   income_q, cashflow_q, sp, *, full_hist_frame=None):
    cutoff = pd.Timestamp(cutoff_str)
    forward = pd.Timestamp(forward_str)
    label = f'{cutoff.year}->{forward.year}'

    print()
    print('=' * 70)
    print(f'Period: {label}  (T10Y = {t10y:.4f})')
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
    print(f'  income annual rows:   {len(income_h):>8d}  (of {len(income):>8d})')
    print(f'  cashflow annual rows: {len(cashflow_h):>8d}  (of {len(cashflow):>8d})')
    print(f'  income quarterly:     {len(income_q_h):>8d}  (of {len(income_q):>8d})')
    print(f'  sp daily (at cutoff): {len(sp_at):>8d}  (of {len(sp):>8d})')

    # Guard: if fewer than 60 trading days, skip (can't compute meaningful betas/52w).
    n_dates = sp_at.index.get_level_values('Date').nunique()
    if n_dates < 60:
        print(f'\n  SKIP: only {n_dates} trading days before cutoff — insufficient for point-in-time metrics.')
        return {
            'label': label, 'n': 0, 'top_mean': None, 'bot_mean': None,
            'spread': None, 'universe_mean': None, 'survivor_pct': None,
            'flags': {},
        }, None

    # ----- Build at-cutoff sp_meta -----
    sp_meta_at = sp_at.sort_index().groupby(level=0).last()
    sp_meta_at.columns = [f'sp_{c}' for c in sp_meta_at.columns]

    # ----- Reuse helpers on filtered data -----
    print('\nComputing point-in-time inputs...')
    betas, vols = ms.compute_betas(sp_at, sp_meta_at)
    hi52w, lo52w = ms.compute_52w(sp_at)
    momo = ms.compute_momentum(sp_at)
    hist = ms.aggregate_history_metrics(full_hist_frame, max_fiscal_year=cutoff.year)
    ttm_p = ms.compute_ttm(income_q_h, cashflow_q_h)
    liq = ms.compute_liquidity(sp_at)

    n_beta = len([b for b in betas.values() if b is not None])
    n_vol = len([v for v in vols.values() if v is not None])
    print(f'  Beta coverage: {n_beta} tickers, Vol coverage: {n_vol} tickers')

    # Verify sp_at latest date is close to cutoff (catches data-gap bugs).
    latest_sp_at_date = sp_at.index.get_level_values('Date').max()
    gap = (cutoff - latest_sp_at_date).days
    if gap > 5:
        print(f'  WARNING: latest sp_at date is {latest_sp_at_date.date()}, '
              f'{gap}d before cutoff {cutoff.date()}')

    print('\nBuilding snapshot + scoring...')
    print('  Finviz disabled (historical sentiment unreliable).')
    df = ms.compute_snapshot(
        companies, industries, income_h, balance_h, cashflow_h,
        sp_meta_at, betas, vols, t10y, hi52w, lo52w,
        hist=hist, ttm=ttm_p, momo=momo, finviz={},
        liquidity=liq, reference_date=cutoff,
    )
    df = ms.compute_potential_scores(df, verbose=False)
    n_scored = df['potential_score'].notna().sum()
    n_flagged = df['flags'].notna().sum()
    print(f'  Scored {n_scored}/{len(df)} stocks, {n_flagged} flagged')

    # ----- Sentiment-coverage sanity check -----
    n_short = df['short_float'].notna().sum()
    n_insider = df['insider_own'].notna().sum()
    if n_short > 0 or n_insider > 0:
        sys.exit('ABORT: Finviz data leaked into backtest — sentiment_score is contaminated.')
    # Confirm price-based sentiment components ARE populated (catches sp filtering bugs).
    n_dist = df['distance_from_52w_high'].notna().sum()
    n_momo = df['return_12m_minus_1m'].notna().sum()
    print(f'  Price-sentiment coverage: dist52w={n_dist}, momo12-1={n_momo}')
    if n_dist < 100 or n_momo < 100:
        print(f'  SKIP: only {n_dist} dist52w and {n_momo} momo values — '
              f'insufficient price history for this period.')
        return {
            'label': label, 'n': 0, 'top_mean': None, 'bot_mean': None,
            'spread': None, 'universe_mean': None, 'survivor_pct': None,
            'flags': {},
        }, None

    # Sub-score coverage summary.
    for col in ('valuation_score', 'quality_score', 'growth_score',
                'sentiment_score', 'potential_score'):
        n = df[col].notna().sum()
        pct = n / len(df) * 100
        print(f'    {col}: {n}/{len(df)} ({pct:.0f}%)')

    # ----- Forward returns -----
    price_at_adj = sp_at.sort_index().groupby(level=0).last()['Adj. Close'].rename('p_at_adj')
    price_fwd = sp_forward.sort_index().groupby(level=0).last()['Adj. Close'].rename('p_fwd')
    prices = price_at_adj.to_frame().join(price_fwd, how='inner')
    prices['fwd_return'] = prices['p_fwd'] / prices['p_at_adj'] - 1
    prices = prices[prices['fwd_return'].between(-0.95, 5.0)]
    print(f'\nForward-return universe: {len(prices)} tickers with both prices')

    merged = df.merge(prices['fwd_return'], left_on='ticker', right_index=True, how='left')
    scored = merged.dropna(subset=['potential_score', 'fwd_return']).copy()
    # §Phase 4.14 transaction costs (round-trip, by liquidity tier).
    scored = apply_txn_costs(scored)
    print(f'Stocks with both potential_score and fwd_return: {len(scored)}')

    # ----- Decile analysis -----
    print()
    print('=' * 70)
    print(f'DECILE FORWARD RETURNS ({label})')
    print('=' * 70)
    scored['decile'] = pd.qcut(scored['potential_score'], 10, labels=False, duplicates='drop') + 1
    agg = scored.groupby('decile').agg(
        n=('ticker', 'count'),
        mean_ret=('fwd_return', 'mean'),
        median_ret=('fwd_return', 'median'),
        win_rate=('fwd_return', lambda s: (s > 0).mean()),
    ).round(4)
    print(agg)

    top_dec = scored[scored['decile'] == 10]['fwd_return']
    bot_dec = scored[scored['decile'] == 1]['fwd_return']
    top_mean = top_dec.mean()
    bot_mean = bot_dec.mean()
    spread = top_mean - bot_mean
    univ_mean = scored['fwd_return'].mean()
    print(f'\nTop decile mean:    {top_mean:>+7.2%}')
    print(f'Bottom decile mean: {bot_mean:>+7.2%}')
    print(f'Spread:             {spread:>+7.2%}')
    print(f'Universe mean:      {univ_mean:>+7.2%}')
    hit = 'TOP DECILE BEATS BOTTOM' if spread > 0 else 'BOTTOM BEATS TOP'
    print(f'{hit}')
    # §Phase 4.14 — net-of-cost top-decile + Sharpe-ish per-stock IR.
    if 'fwd_return_net' in scored.columns:
        top_net = scored[scored['decile'] == 10]['fwd_return_net']
        bot_net = scored[scored['decile'] == 1]['fwd_return_net']
        spread_net = top_net.mean() - bot_net.mean()
        print(f'\nNet of txn costs (round-trip 40-200 bps by liquidity tier):')
        print(f'  Top decile net:    {top_net.mean():>+7.2%}')
        print(f'  Bottom decile net: {bot_net.mean():>+7.2%}')
        print(f'  Net spread:        {spread_net:>+7.2%}')
        top_std = top_net.std()
        if top_std and np.isfinite(top_std) and top_std > 0:
            ir = top_net.mean() / top_std
            print(f'  Top-decile IR:     {ir:>+6.3f}  (mean/stdev cross-section)')

    # ----- Survivorship-bias estimate -----
    tickers_period = set(income_h.index.get_level_values('Ticker').unique())
    last_trade = sp.groupby(level='Ticker').apply(lambda g: g.index.get_level_values('Date').max())
    latest_sp_date = sp.index.get_level_values('Date').max()
    alive_cutoff = latest_sp_date - pd.Timedelta(days=60)
    alive_tickers = set(last_trade[last_trade >= alive_cutoff].index)
    missing = tickers_period - alive_tickers
    n_missing = len(missing)
    pct_missing = (n_missing / len(tickers_period) * 100) if tickers_period else 0
    print(f'\n  Survivorship: {n_missing} of {len(tickers_period)} ({pct_missing:.1f}%) '
          f'companies with {cutoff.year} fundamentals are no longer trading (bias grows for older periods)')

    # ----- Flag-level performance -----
    flag_results = {}
    for flag in ms.FLAG_NAMES:
        flagged = scored[scored['flags'].fillna('').str.contains(flag)]
        n_flag = len(flagged)
        if n_flag >= 5:
            flag_mean = flagged['fwd_return'].mean()
            flag_results[flag] = {
                'n': n_flag,
                'mean_return': flag_mean,
                'vs_universe': flag_mean - univ_mean,
                'returns': flagged['fwd_return'].tolist(),
                'hit_rate': (flagged['fwd_return'] > 0).mean(),
            }

    # ----- Deep dive on MOMENTUM_VALUE (the only flag with consistent alpha) -----
    print()
    print(f'  --- MOMENTUM_VALUE deep dive ({label}) ---')
    mv = scored[scored['flags'].fillna('').str.contains('MOMENTUM_VALUE')]
    if len(mv) > 0:
        n = len(mv)
        mean_ret = mv['fwd_return'].mean()
        median_ret = mv['fwd_return'].median()
        hit_rate = (mv['fwd_return'] > 0).mean()
        universe_mean = scored['fwd_return'].mean()
        print(f'    n={n}, mean={mean_ret:+.2%}, median={median_ret:+.2%}, '
              f'hit_rate={hit_rate:.0%}, alpha_vs_univ={mean_ret - universe_mean:+.2%}')

        # Sector concentration
        sec_counts = mv.groupby('sector_group').agg(
            n=('ticker', 'count'),
            mean_ret=('fwd_return', 'mean'),
        ).sort_values('n', ascending=False)
        print(f'    Sector breakdown:')
        for sg, row in sec_counts.iterrows():
            pct = row['n'] / n * 100
            print(f'      {sg or "(none)":18s} n={int(row["n"]):>3d} ({pct:>4.1f}%)  '
                  f'mean_ret={row["mean_ret"]:+.2%}')

        # Top 5 and bottom 5 by forward return
        top5 = mv.nlargest(5, 'fwd_return')[['ticker', 'sector_group', 'fwd_return']]
        bot5 = mv.nsmallest(5, 'fwd_return')[['ticker', 'sector_group', 'fwd_return']]
        print(f'    Top 5: ' + ', '.join(f'{r.ticker}({r.fwd_return:+.0%})'
                                           for r in top5.itertuples()))
        print(f'    Bot 5: ' + ', '.join(f'{r.ticker}({r.fwd_return:+.0%})'
                                           for r in bot5.itertuples()))
    else:
        print(f'    No MOMENTUM_VALUE stocks in this period.')

    # Sector-neutral construction: top 10% per sector, combined
    sn_top_pieces = []
    sn_bot_pieces = []
    for sg in scored['sector_group'].dropna().unique():
        sub = scored[scored['sector_group'] == sg]
        if len(sub) < 20:
            continue
        n_decile = max(2, len(sub) // 10)
        sn_top_pieces.append(sub.nlargest(n_decile, 'potential_score'))
        sn_bot_pieces.append(sub.nsmallest(n_decile, 'potential_score'))

    if sn_top_pieces:
        sn_top = pd.concat(sn_top_pieces)
        sn_bot = pd.concat(sn_bot_pieces)
        print(f'\n  Sector-neutral top: n={len(sn_top)}, mean={sn_top.fwd_return.mean():+.2%}, '
              f'median={sn_top.fwd_return.median():+.2%}, hit_rate={(sn_top.fwd_return > 0).mean():.0%}')
        print(f'  Sector-neutral bot: n={len(sn_bot)}, mean={sn_bot.fwd_return.mean():+.2%}')
        print(f'  Sector-neutral spread: {sn_top.fwd_return.mean() - sn_bot.fwd_return.mean():+.2%}')
        print(f'  vs universe (+{univ_mean:.2%}): alpha={sn_top.fwd_return.mean() - univ_mean:+.2%}')

    return {
        'label': label,
        'n': len(scored),
        'top_mean': top_mean,
        'bot_mean': bot_mean,
        'spread': spread,
        'universe_mean': univ_mean,
        'survivor_pct': pct_missing,
        'flags': flag_results,
    }, scored

def main():
    parser = argparse.ArgumentParser(
        description='Multi-period point-in-time backtest of potential_score across 5 non-overlapping 1-year windows.')
    parser.add_argument('--acknowledge-bias', action='store_true',
                        help='Skip the 3-second pause forcing you to read the survivorship-bias warning.')
    args = parser.parse_args()

    api_key = os.environ.get('SIMFIN_API_KEY')
    if not api_key:
        sys.exit('SIMFIN_API_KEY not set')
    sf.set_api_key(api_key)
    sf.set_data_dir(str(ROOT / 'data' / 'simfin'))

    print(SURVIVORSHIP_WARNING)
    if not args.acknowledge_bias:
        print('  (pausing 3 seconds -- pass --acknowledge-bias to skip this pause)\n')
        time.sleep(3)

    # ----- Load raw SimFin data ONCE (shared across all periods) -----
    print('\nLoading SimFin data...')
    data = ms.load_simfin_data()
    companies, industries, income, balance, cashflow, income_q, cashflow_q, sp = data
    full_hist_frame = ms.compute_history_metrics_full(income, balance, cashflow)
    print(f'  Full history frame: {len(full_hist_frame)} rows across '
          f'{full_hist_frame["Ticker"].nunique()} tickers.')
    print(f'  Data loaded. Running {len(PERIODS)} periods...\n')

    all_results = []
    all_scored_list = []
    last_scored = None
    for idx, (cutoff_str, forward_str, t10y) in enumerate(PERIODS):
        summary, scored = run_one_period(
            cutoff_str, forward_str, t10y, *data,
            full_hist_frame=full_hist_frame,
        )
        all_results.append(summary)
        if scored is not None:
            all_scored_list.append(scored)
            last_scored = scored  # keep the last valid for detailed breakdown

    # ----- Detailed output for the most recent period -----
    print()
    print('=' * 70)
    print('DETAILED BREAKDOWN: MOST RECENT PERIOD ONLY')
    print('=' * 70)

    scored = last_scored

    # By-sector top-vs-bottom decile spread
    print()
    print('Top-vs-bottom-decile spread by sector:')
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
            print(f'  {sg:18s} n={len(sub):>4d}  top={top_s.mean():>+7.2%}  bot={bot_s.mean():>+7.2%}  spread={top_s.mean()-bot_s.mean():>+7.2%}')

    # ----- Sector balance (uses current cache) -----
    print()
    print('=' * 70)
    print('SECTOR BALANCE (current cache, stocks scoring > 80)')
    print('=' * 70)
    cache = pd.read_sql('SELECT * FROM stocks', sqlite3.connect(str(ROOT / 'data' / 'stock_cache.db')))
    counts = cache.groupby('sector_group').agg(
        total=('ticker', 'count'),
        above80=('potential_score', lambda s: (s > 80).sum()),
    )
    counts['pct'] = (counts['above80'] / counts['total'] * 100).round(1)
    print(counts.to_string())

    # ----- Spot-check 10 well-knowns from current cache -----
    print()
    print('=' * 70)
    print('SPOT-CHECK: 10 well-known tickers (current cache)')
    print('=' * 70)
    spot = ['AAPL', 'MSFT', 'NVDA', 'JPM', 'XOM', 'KO', 'JNJ', 'T', 'WMT', 'BAC']
    for t in spot:
        r = cache[cache['ticker'] == t]
        if r.empty:
            print(f'  {t}: not in cache')
            continue
        r = r.iloc[0]
        print(f'  {t:5s} sg={r["sector_group"] or "(none)":15s} '
              f'POT={r["potential_score"]:>5.1f} V={r["valuation_score"]:>5.1f} '
              f'Q={r["quality_score"]:>5.1f} G={r["growth_score"]:>5.1f} S={r["sentiment_score"]:>5.1f}  '
              f'flags={r["flags"] or ""}')

    # ----- Summary table across all periods -----
    print()
    print()
    print('=' * 70)
    print('MULTI-PERIOD SUMMARY')
    print('=' * 70)
    print(f'  {"Period":14s} {"n":>6s}  {"TopDec":>8s}  {"BotDec":>8s}  {"Spread":>8s}  {"Universe":>8s}  {"Svrv":>5s}')
    print(f'  {"-"*14} {"-"*6}  {"-"*8}  {"-"*8}  {"-"*8}  {"-"*8}  {"-"*5}')
    n_pos = 0
    n_valid = 0
    for r in all_results:
        if r['n'] == 0:  # skipped period (insufficient data)
            print(f'  {r["label"]:14s} {"SKIPPED":>6s}  {"":>8s}  {"":>8s}  {"":>8s}  {"":>8s}  {"":>5s}')
            continue
        n_valid += 1
        pos = r['spread'] > 0
        if pos:
            n_pos += 1
        print(f'  {r["label"]:14s} {r["n"]:>6d}  {r["top_mean"]:>+7.2%}  {r["bot_mean"]:>+7.2%}  {r["spread"]:>+7.2%}  {r["universe_mean"]:>+7.2%}  {r["survivor_pct"]:>4.1f}%')
    if n_valid > 0:
        valid_results = [r for r in all_results if r['n'] > 0]
        mean_spread = np.mean([r['spread'] for r in valid_results])
        print(f'  {"----":14s}')
        print(f'  {"Mean Spread":14s} {"":>6s}  {"":>8s}  {"":>8s}  {mean_spread:>+7.2%}')
        print(f'  Hit Rate: {n_pos}/{n_valid} periods where spread > 0')

    # ----- Flag performance table -----
    print()
    print()
    print('=' * 70)
    print('FLAG PERFORMANCE BY PERIOD')
    print('=' * 70)
    period_labels = [r['label'] for r in all_results if r['n'] > 0]
    header = f'  {"Flag":20s}'
    for pl in period_labels:
        header += f'  {pl:>22s}'
    header += f'  {"All Periods":>22s}'
    print(header)
    print(f'  {"-"*20}  ' + '  '.join(['-'*22] * (len(period_labels) + 1)))
    for flag in ms.FLAG_NAMES:
        line = f'  {flag:20s}'
        all_n = 0
        weighted_return = 0.0
        has_data = False
        for r in all_results:
            if r['n'] == 0:
                continue
            fd = r['flags'].get(flag)
            if fd:
                has_data = True
                n = fd['n']
                mr = fd['mean_return']
                line += f'  {n:>4d} {mr:>+7.2%}     '
                all_n += n
                weighted_return += n * mr
            else:
                line += f'  {"":>4s} {"":>7s}     '
        if has_data and all_n > 0:
            all_mean = weighted_return / all_n
            line += f'  {all_n:>4d} {all_mean:>+7.2%}     '
        else:
            line += f'  {"":>4s} {"":>7s}     '
        print(line)
        # Combined hit rate across all periods.
        all_flagged_returns = []
        for r in all_results:
            if r['n'] == 0:
                continue
            fd = r['flags'].get(flag)
            if fd and 'returns' in fd:
                all_flagged_returns.extend(fd['returns'])
        if all_flagged_returns:
            combined_hit = sum(1 for x in all_flagged_returns if x > 0) / len(all_flagged_returns)
            print(f'  {"":20s}  hit_rate={combined_hit:.0%}')

    # ----- FLAG x SECTOR PERFORMANCE (ALL PERIODS COMBINED) -----
    if all_scored_list:
        all_scored = pd.concat(all_scored_list, ignore_index=True)
        print()
        print()
        print('=' * 70)
        print('FLAG x SECTOR PERFORMANCE (ALL PERIODS COMBINED)')
        print('=' * 70)
        for flag in ['MOMENTUM_VALUE', 'DEEP_VALUE', 'QUIET_COMPOUNDER', 'OVEREXTENDED']:
            flagged = all_scored[all_scored['flags'].fillna('').str.contains(flag)]
            if len(flagged) < 5:
                continue
            print(f'\n  {flag}:')
            sec_perf = flagged.groupby('sector_group').agg(
                n=('ticker', 'count'),
                mean=('fwd_return', 'mean'),
                median=('fwd_return', 'median'),
                hit=('fwd_return', lambda s: (s > 0).mean()),
            ).sort_values('mean', ascending=False)
            for sg, row in sec_perf.iterrows():
                if row['n'] >= 5:
                    print(f'    {sg or "(none)":18s} n={int(row["n"]):>3d}  '
                          f'mean={row["mean"]:>+7.2%}  median={row["median"]:>+7.2%}  '
                          f'hit={row["hit"]:.0%}')

    print()
    print('=' * 70)
    print('COMBINED PORTFOLIO SIMULATION (annual rebalance, equal weight)')
    print('=' * 70)
    for flag in ['MOMENTUM_VALUE', 'DEEP_VALUE', 'QUIET_COMPOUNDER']:
        print(f'\n  {flag}:')
        period_returns = []
        period_ns = []
        for r in all_results:
            if r['n'] == 0:
                continue
            fd = r['flags'].get(flag)
            if fd:
                period_returns.append(fd['mean_return'])
                period_ns.append(fd['n'])
                print(f'    {r["label"]:14s}  n={fd["n"]:>3d}  return={fd["mean_return"]:>+7.2%}  '
                      f'universe={r["universe_mean"]:>+7.2%}  alpha={fd["mean_return"] - r["universe_mean"]:>+7.2%}')
        if period_returns:
            cumulative = 1.0
            for r in period_returns:
                cumulative *= (1 + r)
            annualized = cumulative ** (1.0 / len(period_returns)) - 1
            print(f'    {"---":14s}')
            print(f'    Cumulative return over {len(period_returns)} periods: {(cumulative - 1):>+7.2%}')
            print(f'    Annualized return:                              {annualized:>+7.2%}')

    print()
    print('=' * 70)
    print('CONSISTENCY INTERPRETATION')
    print('=' * 70)
    if n_valid == 0:
        print('  No periods had sufficient data for meaningful analysis.')
    elif n_pos >= 3 and n_valid >= 4 and mean_spread > 0.05:
        print('  STRONG EDGE: spread positive in 3+/4 periods with mean > 5%.')
        print('  The model shows persistent, year-over-year predictive power.')
        print('  Top-decile outperformance is unlikely to be random.')
    elif n_pos >= 2 and n_valid >= 3 and mean_spread > 0:
        print('  WEAK EDGE: spread positive in most periods with mean > 0.')
        print('  Some predictive signal exists, but year-to-year variance dominates.')
        print('  The model may work in certain market regimes but not others.')
    else:
        print('  NO RELIABLE EDGE: spread positive in 2 or fewer periods.')
        print('  Top-decile outperformance in any given year is largely luck.')
        print('  The model needs fundamental rework before deployment.')

    print()
    print('  NOTE: Earlier periods suffer more survivorship bias because more companies')
    print('  have delisted since. The spreads above are UPPER BOUNDS on real-world')
    print('  performance — the true spreads are smaller.')
    print(f'\n{"Done.":>72s}')

if __name__ == '__main__':
    main()
