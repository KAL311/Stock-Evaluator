import sys
from datetime import datetime
from finvizfinance.quote import finvizfinance

LABELS = {
    "Company": "Company", "Sector": "Sector", "Industry": "Industry",
    "Country": "Country", "Exchange": "Exchange", "Index": "Index",
    "P/E": "P/E Ratio", "Forward P/E": "Forward P/E",
    "EPS (ttm)": "EPS (TTM)", "EPS next Y": "EPS (Next Y)",
    "EPS next Q": "EPS (Next Q)", "EPS this Y": "EPS (This Y)",
    "EPS next 5Y": "EPS Growth (5Y)", "Sales": "Revenue",
    "Sales Q/Q": "Revenue Growth (Q/Q)", "EPS Q/Q": "EPS Growth (Q/Q)",
    "Market Cap": "Market Cap", "Income": "Net Income",
    "Short Float": "Short Float", "Dividend": "Dividend",
    "Dividend %": "Dividend Yield", "ROE": "ROE",
    "ROA": "ROA", "ROI": "ROI", "Gross Margin": "Gross Margin",
    "Oper. Margin": "Operating Margin", "Profit Margin": "Profit Margin",
    "P/B": "Price / Book", "P/S": "Price / Sales",
    "P/C": "Price / Cash", "Debt/Eq": "Debt / Equity",
    "LT Debt/Eq": "LT Debt / Equity", "ATR": "ATR",
    "Volatility W": "Volatility (Week)", "Volatility M": "Volatility (Month)",
    "Stoch RSI %D": "Stoch RSI", "RSI (14)": "RSI (14)",
    "SMA20": "SMA (20)", "SMA50": "SMA (50)", "SMA200": "SMA (200)",
    "Target Price": "Target Price", "Recom": "Recommendation",
    "Price": "Price", "Change": "Change",
    "Rel Volume": "Relative Volume", "Volume": "Volume",
    "Avg Volume": "Avg Volume", "Shs Outstand": "Shares Outstanding",
    "Shs Float": "Shares Float", "Insider Own": "Insider Ownership",
    "Insider Trans": "Insider Transactions", "Inst Own": "Institutional Ownership",
    "Inst Trans": "Institutional Transactions", "Short Ratio": "Short Ratio",
    "Short Interest": "Short Interest", "Perf Week": "Performance (Week)",
    "Perf Month": "Performance (Month)", "Perf Quarter": "Performance (Quarter)",
    "Perf Half Y": "Performance (6M)", "Perf Year": "Performance (YTD)",
    "Perf YTD": "Performance (YTD)", "Perf Dec": "Performance (Dec)",
    "Perf Year": "Performance (Year)",
    "52W High": "52W High", "52W Low": "52W Low",
    "Beta": "Beta", "High": "High (Day)", "Low": "Low (Day)",
}

DISPLAY_ORDER = [
    "Price", "Change",
    ("", "VALUATION"),
    "P/E", "Forward P/E", "P/S", "P/B", "P/C", "PEG",
    ("", "FINANCIALS"),
    "EPS (ttm)", "EPS next Y", "EPS next Q", "EPS this Y", "EPS next 5Y",
    "Sales", "Income", "Gross Margin", "Oper. Margin", "Profit Margin",
    ("", "LIQUIDITY"),
    "Debt/Eq", "LT Debt/Eq", "ROE", "ROA", "ROI",
    ("", "DIVIDENDS"),
    "Dividend", "Dividend %",
    ("", "TRADING"),
    "Volume", "Avg Volume", "Rel Volume", "Beta",
    "ATR", "Volatility W", "Volatility M",
    "RSI (14)", "Stoch RSI %D", "SMA20", "SMA50", "SMA200",
    "52W High", "52W Low",
    ("", "PERFORMANCE"),
    "Perf Week", "Perf Month", "Perf Quarter", "Perf Half Y", "Perf Year", "Perf YTD",
    ("", "OWNERSHIP"),
    "Insider Own", "Insider Trans", "Inst Own", "Inst Trans",
    "Short Float", "Short Ratio", "Short Interest",
    ("", "FORECAST"),
    "Target Price", "Recom",
]

_header_card = [
    "Company", "Sector", "Industry", "Country", "Exchange", "Index",
]


def fmt(val, label=""):
    if val is None or val == "-":
        return "N/A"
    val = str(val)
    if label == "Dividend %":
        val = val.replace("%", "")
        try:
            return f"{float(val):.2f}%"
        except ValueError:
            return val
    return val


def analyze_ticker(symbol):
    f = finvizfinance(symbol.upper())
    raw = f.ticker_fundament()

    if not raw or raw.get("Company", "-") == "-":
        print(f"[!] No data found for ticker: {symbol.upper()}")
        return

    print("=" * 62)
    name = raw.get("Company", symbol.upper())
    print(f"  {name} ({symbol.upper()})")
    parts = [raw.get(k, "") for k in _header_card[1:] if raw.get(k, "-") != "-"]
    print(f"  {' | '.join(parts)}")
    print("=" * 62)

    seen = set()
    for entry in DISPLAY_ORDER:
        if isinstance(entry, tuple):
            _, section = entry
            print(f"\n  {'-' * 58}")
            print(f"  {section:^58}")
            print(f"  {'-' * 58}")
            continue

        key = entry
        if key not in raw:
            continue
        val = raw[key]
        if val is None or val == "-":
            continue
        label = LABELS.get(key, key)
        display_val = fmt(val, key)
        print(f"  {label:25}: {display_val}")
        seen.add(key)

    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python stock_info.py <TICKER>")
        print("Example: python stock_info.py AAPL")
        sys.exit(1)

    analyze_ticker(sys.argv[1].upper())
