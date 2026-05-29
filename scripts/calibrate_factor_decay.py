#!/usr/bin/env python3
"""Quantum Approach §Stage 2.5 — calibrate hyperbolic factor decay.

Reads a backtest audit JSON (produced by `backtest.v2.py --audit-dir DIR`),
extracts per-period factor ICs for V/Q/G/S, fits Lee (2025) hyperbolic decay
alpha(t) = K / (1 + lambda*t), and writes the resulting (K, lambda, R^2)
per factor to data/factor_decay.json. The screener loads that file at
startup and downweights high-lambda factors via apply_decay_to_weights().

Usage:
  python scripts/calibrate_factor_decay.py --audit data/audit/audit_<run>.json
  python scripts/calibrate_factor_decay.py --audit-dir data/audit       # picks latest

Reference: Chorok Lee, arXiv:2512.11913 (2025); see Quantum Approach.md §5.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

# Import market_screener for fit_hyperbolic_decay only. SimFin key not needed
# at calibration time but the module enforces it; set a placeholder.
import os
os.environ.setdefault('SIMFIN_API_KEY', 'CALIBRATION_PLACEHOLDER')
os.environ.setdefault('DISABLE_FINVIZ', '1')
os.environ.setdefault('DECAY_ENABLED', '0')  # avoid recursive load
import market_screener as ms


FACTOR_KEY = {
    'valuation_score': 'valuation',
    'quality_score':   'quality',
    'growth_score':    'growth',
    'sentiment_score': 'sentiment',
}


def latest_audit(audit_dir):
    p = Path(audit_dir)
    cands = sorted(p.glob('audit_*.json'), key=lambda x: x.stat().st_mtime)
    if not cands:
        sys.exit(f'No audit_*.json in {audit_dir}')
    return cands[-1]


def load_audit_factor_ics(audit_path, exclude_period=None):
    """Return dict {factor_key: [(t, ic), ...]} from audit JSON."""
    data = json.loads(Path(audit_path).read_text())
    results = data.get('results', [])
    if exclude_period:
        results = [r for r in results
                   if exclude_period not in str(r.get('label', ''))]
        print(f'  Excluded periods containing {exclude_period!r}; '
              f'{len(results)} remain')
    out = {v: [] for v in FACTOR_KEY.values()}
    for t_idx, period in enumerate(results):
        fic = period.get('factor_ics', {}) or {}
        if not fic:
            continue
        for col, key in FACTOR_KEY.items():
            v = fic.get(col)
            if v is None:
                continue
            try:
                v = float(v)
            except Exception:
                continue
            out[key].append((float(t_idx), v))
    return out, len(results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--audit', help='Path to audit_<run>.json')
    ap.add_argument('--audit-dir', default=str(ROOT / 'data' / 'audit'),
                    help='Directory to scan for latest audit JSON')
    ap.add_argument('--out', default=str(ROOT / 'data' / 'factor_decay.json'),
                    help='Output path for decay calibration')
    ap.add_argument('--exclude-period', help='Exclude periods whose label '
                    'contains this string (for OOS-clean calibration).')
    args = ap.parse_args()

    audit_path = Path(args.audit) if args.audit else latest_audit(args.audit_dir)
    print(f'Reading audit: {audit_path}')

    fic_series, n_periods = load_audit_factor_ics(audit_path, args.exclude_period)
    print(f'  {n_periods} periods')

    factors_out = {}
    for key, series in fic_series.items():
        if len(series) < 3:
            print(f'  {key:11s}: {len(series)} pts -- skipping (need >=3)')
            factors_out[key] = {'K': None, 'lambda': None, 'r2': None,
                                'n_obs': len(series),
                                'note': 'insufficient observations'}
            continue
        t_arr = [s[0] for s in series]
        ic_arr = [s[1] for s in series]
        K, lam, r2 = ms.fit_hyperbolic_decay(ic_arr, t_arr)
        factors_out[key] = {
            'K': K if K is not None and (K == K) else None,
            'lambda': lam if lam is not None and (lam == lam) else None,
            'r2': r2 if r2 is not None and (r2 == r2) else None,
            'n_obs': len(series),
            'mean_ic': float(sum(ic_arr) / len(ic_arr)),
        }
        print(f'  {key:11s}: K={K:.4f}  lambda={lam:.4f}  R^2={r2:.3f}  '
              f'n={len(series)}  mean_IC={factors_out[key]["mean_ic"]:+.4f}')

    out = {
        'calibrated_at': datetime.now().isoformat(timespec='seconds'),
        'source_audit': str(audit_path),
        'n_periods': n_periods,
        'factors': factors_out,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f'\nWrote {out_path}')
    print('  Next run of market_screener will auto-load decay-adjusted weights.')


if __name__ == '__main__':
    main()
