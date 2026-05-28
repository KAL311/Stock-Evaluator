#!/usr/bin/env python3
"""Quantum Approach §Stage 2.6 — fetch top holdings of factor-replication
ETFs (MTUM/VLUE/QUAL/SIZE) and write data/factor_etf_holdings.json.

Sources (iShares public AJAX JSON endpoints; may change format):
  MTUM - iShares MSCI USA Momentum Factor ETF
  VLUE - iShares MSCI USA Value Factor ETF
  QUAL - iShares MSCI USA Quality Factor ETF
  SIZE - iShares MSCI USA Size Factor ETF

Falls back to manual seed list if HTTP fetch fails so the screener still has
a deterministic input. Edit MANUAL_SEED below to override or extend.

Usage:
  python scripts/fetch_factor_etf_holdings.py             # try fetch, write JSON
  python scripts/fetch_factor_etf_holdings.py --manual    # use seed only
"""
import argparse
import json
import ssl
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / 'data' / 'factor_etf_holdings.json'

# iShares JSON endpoints — known to drift. If fetch returns empty/invalid,
# replace these URLs with the current ones from each ETF's "Holdings" tab.
ETF_URLS = {
    'MTUM': 'https://www.ishares.com/us/products/239713/'
            'ishares-msci-usa-momentum-factor-etf/1467271812596.ajax'
            '?fileType=json&fileName=MTUM_holdings&dataType=fund',
    'VLUE': 'https://www.ishares.com/us/products/239668/'
            'ishares-msci-usa-value-factor-etf/1467271812596.ajax'
            '?fileType=json&fileName=VLUE_holdings&dataType=fund',
    'QUAL': 'https://www.ishares.com/us/products/239671/'
            'ishares-msci-usa-quality-factor-etf/1467271812596.ajax'
            '?fileType=json&fileName=QUAL_holdings&dataType=fund',
    'SIZE': 'https://www.ishares.com/us/products/239716/'
            'ishares-msci-usa-size-factor-etf/1467271812596.ajax'
            '?fileType=json&fileName=SIZE_holdings&dataType=fund',
}

# Fallback if every fetch fails — keeps the pipeline runnable.
# Curated top-30 holdings as of late-2024 / early-2026 snapshots from iShares
# Holdings tab. These factor ETFs have persistent tilts; full refresh quarterly
# via the iShares website (Holdings -> "Download Detailed Holdings PDF/Excel"),
# then paste tickers below. Replace any list to refresh.
MANUAL_SEED = {
    # MTUM — iShares MSCI USA Momentum Factor: large-cap momentum names
    'MTUM': [
        'NVDA', 'AVGO', 'JPM', 'AAPL', 'META', 'COST', 'WMT', 'LLY', 'V', 'MA',
        'NFLX', 'HD', 'BAC', 'ORCL', 'ADBE', 'CVX', 'MRK', 'ABBV', 'KO', 'PEP',
        'MCD', 'CRM', 'CSCO', 'TMO', 'ABT', 'LIN', 'INTU', 'BLK', 'GS', 'PGR',
    ],
    # VLUE — iShares MSCI USA Value Factor: cheap large/mid caps
    'VLUE': [
        'IBM', 'INTC', 'CSCO', 'T', 'VZ', 'F', 'GM', 'CMCSA', 'KHC', 'WBA',
        'BAC', 'C', 'GS', 'KR', 'MO', 'BMY', 'GILD', 'WFC', 'CVS', 'PFE',
        'BK', 'PRU', 'MET', 'TRV', 'ALL', 'PSX', 'VLO', 'MPC', 'KMI', 'OXY',
    ],
    # QUAL — iShares MSCI USA Quality Factor: high-ROE, low-leverage mega-caps
    'QUAL': [
        'AAPL', 'MSFT', 'NVDA', 'META', 'V', 'MA', 'COST', 'JPM', 'NFLX', 'LLY',
        'ORCL', 'WMT', 'JNJ', 'PG', 'HD', 'UNH', 'XOM', 'AVGO', 'ABBV', 'CVX',
        'ADBE', 'MRK', 'PEP', 'KO', 'CSCO', 'ACN', 'TMO', 'LIN', 'NKE', 'CMG',
    ],
    # SIZE — iShares MSCI USA Size Factor: smaller-cap tilt (US small/mid)
    'SIZE': [
        'AMD', 'AMZN', 'TSLA', 'GOOG', 'GOOGL', 'BRK.B', 'XOM', 'JNJ', 'UNH',
        'PG', 'MA', 'V', 'JPM', 'HD', 'BAC', 'CVX', 'PFE', 'KO', 'PEP',
        'WMT', 'MRK', 'TMO', 'COST', 'ABBV', 'AVGO', 'DIS', 'ABT', 'WFC', 'CSCO', 'INTC',
    ],
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; FactorETFCrowdingFetcher/1.0)',
    'Accept': 'application/json,text/plain,*/*',
}
TOP_N = 100


def _parse_ishares_payload(payload):
    """iShares 'aaData' format: each row a list, ticker at index 0 in most schemas.
    Tolerant parser: scan rows looking for an A-Z 1-5 char ticker."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get('aaData') or payload.get('data') or []
    out = []
    for r in rows:
        if not isinstance(r, list) or not r:
            continue
        cand = r[0]
        # Sometimes ticker is nested in HTML; strip tags.
        if isinstance(cand, str):
            sym = cand.strip()
            # Strip HTML if present
            if '<' in sym:
                import re
                sym = re.sub(r'<[^>]+>', '', sym).strip()
            if 1 <= len(sym) <= 6 and sym.isalpha():
                out.append(sym.upper())
    return out


def fetch_one(etf, url, timeout=15):
    ctx = ssl.create_default_context()
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read().decode('utf-8', errors='ignore')
        # iShares sometimes wraps JSON in BOM or trailing whitespace
        raw = raw.strip().lstrip('﻿')
        data = json.loads(raw)
        tickers = _parse_ishares_payload(data)
        if not tickers:
            return None, 'parser found no tickers'
        return tickers[:TOP_N], None
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manual', action='store_true',
                    help='Skip HTTP fetch; use MANUAL_SEED only')
    ap.add_argument('--out', default=str(OUT_PATH))
    args = ap.parse_args()

    out_etfs = {}
    now = datetime.now().isoformat(timespec='seconds')

    for etf, url in ETF_URLS.items():
        if args.manual:
            holdings = MANUAL_SEED.get(etf, [])
            note = 'manual seed (--manual)'
        else:
            holdings, err = fetch_one(etf, url)
            if not holdings:
                holdings = MANUAL_SEED.get(etf, [])
                note = f'fetch failed ({err}); fallback to MANUAL_SEED'
            else:
                note = f'fetched {len(holdings)} from iShares'
        print(f'  {etf}: {len(holdings)} holdings -- {note}')
        out_etfs[etf] = {
            'top_holdings': holdings,
            'n_holdings': len(holdings),
            'fetched_at': now,
            'note': note,
        }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        'updated_at': now,
        'top_n_target': TOP_N,
        'etfs': out_etfs,
    }, indent=2))
    print(f'\nWrote {out_path}')
    populated = sum(1 for v in out_etfs.values() if v['n_holdings'] > 0)
    if populated == 0:
        print('  WARNING: every ETF empty. Populate MANUAL_SEED in this script'
              ' or use the iShares Holdings tab to refresh URLs.')
        sys.exit(1)


if __name__ == '__main__':
    main()
