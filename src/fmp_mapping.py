"""FMP → SimFin field mapper for the parity study (Study A).

Purpose
-------
Convert the FMP normalized long-format CSVs written by
scripts/fetch_fundamentals_fmp.py into pandas DataFrames whose columns and
dtypes match what src/market_screener.py:compute_history_metrics_full expects
from simfin.load_income / load_balance / load_cashflow.

Design rule
-----------
This module is imported ONLY by the parity harness (scripts/parity_study_a.py).
market_screener.py is NEVER edited or importing this. That guarantees the
frozen scoring path runs the same code on both SimFin and FMP inputs — the
ONLY substitution is the fundamentals DataFrames.

Field mapping (verified against AAPL FY2024 SimFin vs FMP)
----------------------------------------------------------
Income:
    Revenue                 <- revenue                       (0.00% delta)
    Gross Profit            <- grossProfit                   (0.00%)
    Operating Income (Loss) <- operatingIncome               (0.00%)
    Net Income (Common)     <- netIncome                     (0.00%; preferred
                                                              dividend deduction
                                                              omitted — SimFin
                                                              approximates the
                                                              same on typical
                                                              tickers; note as
                                                              known drift for
                                                              preferred-heavy
                                                              tickers, but the
                                                              scoring path itself
                                                              uses Net Income
                                                              (Common) verbatim)
    Cost of Revenue         <- -costOfRevenue                (SimFin negative
                                                              convention; unused
                                                              in metric math but
                                                              preserved)
    Publish Date            <- filingDate  (pd.Timestamp)    (exact match)

Balance:
    Total Equity            <- totalEquity or
                               totalStockholdersEquity        (0.00%)
    Total Assets            <- totalAssets                    (0.00%)
    Short Term Debt         <- shortTermDebt                  (classification
                                                              differs vs SimFin;
                                                              scoring uses sum
                                                              STD+LTD which
                                                              matches exactly on
                                                              AAPL)
    Long Term Debt          <- longTermDebt
    Cash, Cash Equivalents & Short Term Investments
                            <- cashAndShortTermInvestments    (0.00%)
    Total Current Assets    <- totalCurrentAssets             (0.00%)
    Total Current Liabilities
                            <- totalCurrentLiabilities        (~6.6% delta on
                                                              AAPL — FMP includes
                                                              broader accrued/
                                                              deferred items than
                                                              SimFin. Expect
                                                              cr_yr / Altman WC
                                                              drift.)

Cashflow:
    Net Cash from Operating Activities
                            <- netCashProvidedByOperatingActivities   (0.00%)
    Change in Fixed Assets & Intangibles
                            <- capitalExpenditure             (SIGN MATCHES
                                                              SimFin — both
                                                              negative. FCF math
                                                              fcf = ocf + capex
                                                              produces identical
                                                              value on AAPL FY24:
                                                              108.807B on both)
    Depreciation & Amortization
                            <- depreciationAndAmortization    (0.00%)

Piotroski F-score / Altman Z-score
----------------------------------
Both are computed inside aggregate_history_metrics from fields already covered
above (ni_yr, ocf_yr, ta_yr, debt_yr, cr_yr, sh_yr proxy=eq_yr, gm, rev_yr,
op_inc_yr, eq_yr, cur_a_yr, cur_l_yr). No additional FMP fields needed. There
is NO known parity gap for F or Z on the FMP path.
"""

from __future__ import annotations

from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
FMP_DIR = REPO_ROOT / "data" / "fmp"


# ---------- SimFin target column lists (must match compute_history_metrics_full) ----------

INCOME_COLS = [
    "Ticker",
    "Fiscal Year",
    "Revenue",
    "Gross Profit",
    "Operating Income (Loss)",
    "Net Income (Common)",
    "Cost of Revenue",
    "Publish Date",
]

BALANCE_COLS = [
    "Ticker",
    "Fiscal Year",
    "Total Equity",
    "Total Assets",
    "Short Term Debt",
    "Long Term Debt",
    "Cash, Cash Equivalents & Short Term Investments",
    "Total Current Assets",
    "Total Current Liabilities",
]

CASHFLOW_COLS = [
    "Ticker",
    "Fiscal Year",
    "Net Cash from Operating Activities",
    "Change in Fixed Assets & Intangibles",
    "Depreciation & Amortization",
]


# ---------- Field-level mappers ----------


def _to_int_fy(s: pd.Series) -> pd.Series:
    # FMP returns fiscalYear as string. SimFin FY is int64.
    return pd.to_numeric(s, errors="coerce").astype("Int64")


def _prefer(row_col_a: pd.Series, row_col_b: pd.Series) -> pd.Series:
    # Use column A where non-null, else fall back to B.
    return row_col_a.where(row_col_a.notna(), row_col_b)


def map_income(fmp_income_csv: Path | None = None) -> pd.DataFrame:
    """FMP income long-format CSV → SimFin-shaped income DataFrame."""
    src = fmp_income_csv or (FMP_DIR / "fundamentals_income.csv")
    df = pd.read_csv(src)

    out = pd.DataFrame()
    out["Ticker"] = df["ticker"].astype(str).str.upper()
    out["Fiscal Year"] = _to_int_fy(df["fiscalYear"])
    out["Revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    out["Gross Profit"] = pd.to_numeric(df["grossProfit"], errors="coerce")
    out["Operating Income (Loss)"] = pd.to_numeric(df["operatingIncome"], errors="coerce")
    # netIncomeCommon is sometimes null in FMP; fall back to netIncome (preferred
    # dividend deduction is negligible on most tickers).
    ni_common = pd.to_numeric(df.get("netIncomeCommon"), errors="coerce") if "netIncomeCommon" in df.columns else pd.Series([pd.NA] * len(df))
    ni_full = pd.to_numeric(df["netIncome"], errors="coerce")
    out["Net Income (Common)"] = _prefer(ni_common, ni_full)
    # SimFin signs Cost of Revenue negative; FMP signs it positive → negate.
    out["Cost of Revenue"] = -pd.to_numeric(df["costOfRevenue"], errors="coerce")
    # Publish Date: use FMP filingDate (PIT filing) as pd.Timestamp
    out["Publish Date"] = pd.to_datetime(df["filingDate"], errors="coerce")

    out = out.dropna(subset=["Ticker", "Fiscal Year"])
    out = out.reset_index(drop=True)
    return out[INCOME_COLS]


def map_balance(fmp_balance_csv: Path | None = None) -> pd.DataFrame:
    """FMP balance long-format CSV → SimFin-shaped balance DataFrame."""
    src = fmp_balance_csv or (FMP_DIR / "fundamentals_balance.csv")
    df = pd.read_csv(src)

    out = pd.DataFrame()
    out["Ticker"] = df["ticker"].astype(str).str.upper()
    out["Fiscal Year"] = _to_int_fy(df["fiscalYear"])
    # totalEquity ~ totalStockholdersEquity for firms without minority interest;
    # prefer totalStockholdersEquity to match SimFin's "Total Equity" (which is
    # common-shareholders' equity), fall back to totalEquity.
    tse = pd.to_numeric(df.get("totalStockholdersEquity"), errors="coerce") if "totalStockholdersEquity" in df.columns else pd.Series([pd.NA] * len(df))
    te = pd.to_numeric(df.get("totalEquity"), errors="coerce") if "totalEquity" in df.columns else pd.Series([pd.NA] * len(df))
    out["Total Equity"] = _prefer(tse, te)
    out["Total Assets"] = pd.to_numeric(df["totalAssets"], errors="coerce")
    out["Short Term Debt"] = pd.to_numeric(df["shortTermDebt"], errors="coerce")
    out["Long Term Debt"] = pd.to_numeric(df["longTermDebt"], errors="coerce")
    out["Cash, Cash Equivalents & Short Term Investments"] = pd.to_numeric(
        df["cashAndShortTermInvestments"], errors="coerce"
    )
    out["Total Current Assets"] = pd.to_numeric(df["totalCurrentAssets"], errors="coerce")
    out["Total Current Liabilities"] = pd.to_numeric(df["totalCurrentLiabilities"], errors="coerce")

    out = out.dropna(subset=["Ticker", "Fiscal Year"])
    out = out.reset_index(drop=True)
    return out[BALANCE_COLS]


def map_cashflow(fmp_cashflow_csv: Path | None = None) -> pd.DataFrame:
    """FMP cash-flow long-format CSV → SimFin-shaped cashflow DataFrame."""
    src = fmp_cashflow_csv or (FMP_DIR / "fundamentals_cashflow.csv")
    df = pd.read_csv(src)

    out = pd.DataFrame()
    out["Ticker"] = df["ticker"].astype(str).str.upper()
    out["Fiscal Year"] = _to_int_fy(df["fiscalYear"])
    out["Net Cash from Operating Activities"] = pd.to_numeric(
        df["netCashProvidedByOperatingActivities"], errors="coerce"
    )
    # capitalExpenditure: FMP negative, SimFin negative → direct assignment.
    # Confirmed on AAPL FY2024: SimFin -9.447B, FMP -9.447B, FCF identical
    # 108.807B via ocf + capex.
    out["Change in Fixed Assets & Intangibles"] = pd.to_numeric(
        df["capitalExpenditure"], errors="coerce"
    )
    out["Depreciation & Amortization"] = pd.to_numeric(
        df["depreciationAndAmortization"], errors="coerce"
    )

    out = out.dropna(subset=["Ticker", "Fiscal Year"])
    out = out.reset_index(drop=True)
    return out[CASHFLOW_COLS]


def load_fmp_as_simfin(
    income_csv: Path | None = None,
    balance_csv: Path | None = None,
    cashflow_csv: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return (income, balance, cashflow) DataFrames shaped exactly like the
    output of simfin.load_income/balance/cashflow for the scoring path."""
    return map_income(income_csv), map_balance(balance_csv), map_cashflow(cashflow_csv)


# ---------- Self-check ----------

if __name__ == "__main__":
    import sys

    inc = map_income()
    bal = map_balance()
    cf = map_cashflow()
    print(f"income:   {len(inc):>6} rows  {inc['Ticker'].nunique()} tickers  cols={list(inc.columns)}")
    print(f"balance:  {len(bal):>6} rows  {bal['Ticker'].nunique()} tickers  cols={list(bal.columns)}")
    print(f"cashflow: {len(cf):>6} rows  {cf['Ticker'].nunique()} tickers  cols={list(cf.columns)}")
    # AAPL FY2024 spot check
    for name, df in [("income", inc), ("balance", bal), ("cashflow", cf)]:
        aa = df[(df["Ticker"] == "AAPL") & (df["Fiscal Year"] == 2024)]
        print(f"\nAAPL FY2024 {name}:")
        print(aa.to_string(index=False))
    sys.exit(0)
