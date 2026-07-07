#!/usr/bin/env python3
"""Phase 4.0/4.1: post-hoc diagnostics on the held-out OOS scored frame.
Report-only. Does not re-run the backtest or touch scoring.

Usage:
  python scripts/analyze_oos.py --csv data/audit/scored_OOS_<label>_oos_2024_locked.csv
"""
import argparse
import sqlite3
from pathlib import Path
import pandas as pd
import numpy as np

# SimFin free-tier daily prices stop advancing past this date. Quarters ending
# after this ceiling load prices from prices_live (yfinance) in
# data/stock_cache.db. See docs/price_layer.md.
SIMFIN_PRICE_CEILING = pd.Timestamp('2025-06-03')


def quarterly_decomposition(df, cutoff='2023-12-31', forward='2024-12-31'):
    """Decompose OOS top-decile alpha into quarterly subperiods.
    Loads SimFin prices read-only; does not touch scoring."""
    import os
    import simfin as sf
    ROOT = Path(__file__).resolve().parent.parent
    sf.set_api_key(os.environ.get('SIMFIN_API_KEY', 'placeholder'))
    sf.set_data_dir(str(ROOT / 'data' / 'simfin'))
    sp = sf.load_shareprices(variant='daily', market='us')

    top = df[df['decile'] == 10]
    top_tickers = set(top['ticker'].astype(str))
    all_tickers = set(df['ticker'].astype(str))

    cutoff_ts = pd.Timestamp(cutoff)
    forward_ts = pd.Timestamp(forward)
    # Start strictly after cutoff so the first iteration is not a self-comparison
    # (cutoff is itself a quarter-end, which would yield a spurious +0.00% quarter).
    q_ends = pd.date_range(cutoff_ts + pd.Timedelta(days=1), forward_ts, freq='QE')

    sp_dates = sp.index.get_level_values('Date')

    _live_conn_holder = {'conn': None, 'announced': False}

    def _prices_live_asof(ts):
        if _live_conn_holder['conn'] is None:
            db_path = ROOT / 'data' / 'stock_cache.db'
            _live_conn_holder['conn'] = sqlite3.connect(str(db_path))
        if not _live_conn_holder['announced']:
            print()
            print('*' * 64)
            print('  ANALYZE_OOS forward-price source: yfinance (prices_live)')
            print(f'  Quarter end > SIMFIN_PRICE_CEILING '
                  f'({SIMFIN_PRICE_CEILING.date()}) triggers fallback.')
            print('  See docs/price_layer.md.')
            print('*' * 64)
            _live_conn_holder['announced'] = True
        lo = (ts - pd.Timedelta(days=10)).strftime('%Y-%m-%d')
        hi = ts.strftime('%Y-%m-%d')
        df = pd.read_sql_query(
            "SELECT ticker, date, close FROM prices_live "
            f"WHERE date > '{lo}' AND date <= '{hi}' ORDER BY ticker, date",
            _live_conn_holder['conn'],
        )
        if df.empty:
            return pd.Series(dtype=float)
        return (df.sort_values(['ticker', 'date'])
                  .groupby('ticker')['close'].last())

    def price_asof(ts):
        if ts > SIMFIN_PRICE_CEILING:
            return _prices_live_asof(ts)
        window = sp[(sp_dates > ts - pd.Timedelta(days=10)) & (sp_dates <= ts)]
        if window.empty:
            return pd.Series(dtype=float)
        return window.sort_index().groupby(level=0).last()['Adj. Close']

    print('\n' + '=' * 60)
    print('OOS QUARTERLY ALPHA DECOMPOSITION')
    print('=' * 60)
    print(f'  {"Quarter":12s} {"top_ret":>8s} {"univ_ret":>9s} '
          f'{"alpha":>8s} {"n_top":>6s}')
    prev = price_asof(cutoff_ts)
    q_alphas = []
    for q in q_ends:
        cur = price_asof(q)
        common = list(set(prev.index) & set(cur.index))
        if not common:
            prev = cur
            continue
        ret = (cur.loc[common] / prev.loc[common] - 1)
        ret = ret[ret.between(-0.9, 2.0)]
        top_ret = ret[[t for t in ret.index if t in top_tickers]]
        univ_ret = ret[[t for t in ret.index if t in all_tickers]]
        if len(top_ret) >= 5 and len(univ_ret) >= 30:
            a = top_ret.mean() - univ_ret.mean()
            q_alphas.append(a)
            qlabel = f'{q.year}-Q{(q.month - 1) // 3 + 1}'
            print(f'  {qlabel:12s} {top_ret.mean():>+7.2%} '
                  f'{univ_ret.mean():>+8.2%} {a:>+7.2%} {len(top_ret):>6d}')
        prev = cur

    if q_alphas:
        arr = np.array(q_alphas)
        print(f'\n  Quarterly alpha mean:  {arr.mean():+.2%}')
        print(f'  Quarterly alpha std:   {arr.std():.2%}')
        print(f'  Positive quarters:     {(arr > 0).sum()}/{len(arr)}')
        if arr.sum() != 0:
            best_share = arr.max() / arr.sum() if arr.sum() > 0 else np.nan
            print(f'  Best quarter share:    {best_share:.0%} of total alpha')
        print(f'\n  ROBUSTNESS VERDICT:')
        if (arr > 0).sum() >= 3 and arr.std() < abs(arr.mean()) * 1.5:
            print(f'    STEADY: alpha positive in {(arr>0).sum()}/4 quarters, low')
            print(f'    dispersion. Edge is consistent; size with normal confidence.')
        elif (arr > 0).sum() >= 3:
            print(f'    MODERATELY STEADY: positive in {(arr>0).sum()}/4 quarters but')
            print(f'    high dispersion. Real but variable; size modestly.')
        else:
            print(f'    LUMPY: alpha positive in only {(arr>0).sum()}/4 quarters.')
            print(f'    The annual number is driven by 1-2 quarters. Size DOWN')
            print(f'    significantly; this edge has high timing risk.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', required=True, help='Path to persisted OOS scored CSV')
    ap.add_argument('--no-quarterly', action='store_true',
                    help='Skip the SimFin quarterly decomposition (4.1).')
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    print(f'Loaded {len(df)} OOS-scored rows from {args.csv}\n')

    # Recompute deciles defensively in case the column is missing
    if 'decile' not in df.columns or df['decile'].isna().all():
        df['decile'] = pd.qcut(df['potential_score'], 10,
                               labels=False, duplicates='drop') + 1

    top = df[df['decile'] == 10]
    univ_ret = df['fwd_return'].mean()
    top_ret = top['fwd_return'].mean()
    print(f'OOS top-decile mean:  {top_ret:+.2%}')
    print(f'OOS universe mean:    {univ_ret:+.2%}')
    print(f'OOS top-alpha:        {top_ret - univ_ret:+.2%}')
    print(f'OOS top-decile n:     {len(top)}\n')

    # ---- 4.0 Sector concentration ----
    print('=' * 60)
    print('OOS TOP-DECILE SECTOR CONCENTRATION')
    print('=' * 60)
    if 'sector_group' in df.columns:
        univ_sec = df['sector_group'].value_counts(normalize=True) * 100
        top_sec = top['sector_group'].value_counts(normalize=True) * 100
        top_sec_n = top['sector_group'].value_counts()
        all_secs = sorted(set(univ_sec.index) | set(top_sec.index))
        print(f'  {"sector":20s} {"univ_%":>7s} {"top_%":>7s} '
              f'{"top_n":>6s} {"tilt_pp":>8s}')
        for sec in sorted(all_secs, key=lambda s: -top_sec.get(s, 0)):
            up = univ_sec.get(sec, 0)
            tp = top_sec.get(sec, 0)
            tn = int(top_sec_n.get(sec, 0))
            print(f'  {str(sec):20s} {up:>6.1f}% {tp:>6.1f}% '
                  f'{tn:>6d} {tp - up:>+7.1f}pp')
        top2_share = top_sec.nlargest(2).sum()
        top3_share = top_sec.nlargest(3).sum()
        print(f'\n  Top-2 sector share of top decile: {top2_share:.1f}%')
        print(f'  Top-3 sector share of top decile: {top3_share:.1f}%')
        # Per-sector contribution to alpha
        print(f'\n  Per-sector alpha contribution (top-decile names only):')
        for sec in sorted(all_secs, key=lambda s: -top_sec.get(s, 0))[:6]:
            sub = top[top['sector_group'] == sec]
            if len(sub) >= 3:
                contrib = (sub['fwd_return'].mean() - univ_ret) * (len(sub) / len(top))
                print(f'    {str(sec):20s} ret={sub["fwd_return"].mean():>+7.2%}  '
                      f'weight={len(sub)/len(top):>5.1%}  '
                      f'alpha_contrib={contrib:>+6.2%}')

        # VERDICT
        print(f'\n  CONCENTRATION VERDICT:')
        if top2_share > 50:
            print(f'    WARNING: top-2 sectors are {top2_share:.0f}% of top decile.')
            print(f'    The OOS edge is substantially a sector bet. Size DOWN and')
            print(f'    treat sector-neutral alpha as the real edge, not headline.')
        elif top2_share > 40:
            print(f'    CAUTION: top-2 sectors are {top2_share:.0f}% of top decile.')
            print(f'    Moderately concentrated; check sector-neutral alpha holds.')
        else:
            print(f'    OK: top-2 sectors are {top2_share:.0f}% of top decile.')
            print(f'    Edge is reasonably broad across sectors.')
    else:
        print('  No sector_group column in OOS CSV.')

    # ---- 4.1 Subperiod robustness ----
    if not args.no_quarterly:
        quarterly_decomposition(df)


if __name__ == '__main__':
    main()
