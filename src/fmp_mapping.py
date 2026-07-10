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
    # Extended for LIVE screener source-swap (compute_snapshot reads).
    # SimFin sign convention preserved: R&D negative (expense), Interest Expense
    # negative, Income Tax negative for tax expense. See map_income() for the
    # per-field mapping and the sign-check spot verification against AAPL FY2024.
    "Research & Development",
    "Interest Expense, Net",
    "Pretax Income (Loss)",
    "Income Tax (Expense) Benefit, Net",
    "Shares (Diluted)",
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
    # Extended for LIVE screener source-swap.
    "Dividends Paid",
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

    # Extended fields — see AAPL FY2024 sign spot check in the module docstring.
    # SimFin R&D is signed negative (expense); FMP positive → negate.
    if "researchAndDevelopmentExpenses" in df.columns:
        out["Research & Development"] = -pd.to_numeric(
            df["researchAndDevelopmentExpenses"], errors="coerce"
        )
    else:
        out["Research & Development"] = pd.NA
    # SimFin Interest Expense, Net is signed negative for expense. FMP exposes
    # interestExpense (positive) and interestIncome (positive) and their net via
    # netInterestIncome (positive when income > expense). To match SimFin's Net
    # convention: interestIncome - interestExpense == netInterestIncome; SimFin
    # signs the RESULT as negative when it is an expense, so we negate.
    if "netInterestIncome" in df.columns:
        out["Interest Expense, Net"] = -pd.to_numeric(
            df["netInterestIncome"], errors="coerce"
        )
    elif "interestExpense" in df.columns:
        # Fallback: gross interest expense as a negative number.
        out["Interest Expense, Net"] = -pd.to_numeric(
            df["interestExpense"], errors="coerce"
        )
    else:
        out["Interest Expense, Net"] = pd.NA
    # Pretax Income: sign matches
    out["Pretax Income (Loss)"] = pd.to_numeric(df.get("incomeBeforeTax"), errors="coerce")
    # SimFin Income Tax Expense signed negative; FMP incomeTaxExpense positive → negate.
    out["Income Tax (Expense) Benefit, Net"] = -pd.to_numeric(
        df.get("incomeTaxExpense"), errors="coerce"
    )
    # Diluted shares outstanding
    out["Shares (Diluted)"] = pd.to_numeric(
        df.get("weightedAverageShsOutDil"), errors="coerce"
    )

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
    # SimFin "Dividends Paid" is negative (outflow); FMP netDividendsPaid is
    # also negative on AAPL FY2024. Prefer netDividendsPaid, fall back to
    # commonDividendsPaid; both should already be signed as outflows.
    if "netDividendsPaid" in df.columns:
        out["Dividends Paid"] = pd.to_numeric(df["netDividendsPaid"], errors="coerce")
    elif "commonDividendsPaid" in df.columns:
        out["Dividends Paid"] = pd.to_numeric(df["commonDividendsPaid"], errors="coerce")
    else:
        out["Dividends Paid"] = pd.NA

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


# ---------- LIVE screener source-swap loader (Option B, gated) ----------


def _to_multiindex_via_publish(df: pd.DataFrame) -> pd.DataFrame:
    """Convert a flat FMP-mapped frame to MultiIndex(Ticker, Report Date).
    'Report Date' is derived from 'Publish Date' if present, else FY-end proxy.
    compute_snapshot's `income.sort_index(level=1)` requires this shape."""
    d = df.copy()
    if "Report Date" not in d.columns:
        if "Publish Date" in d.columns:
            d["Report Date"] = pd.to_datetime(d["Publish Date"], errors="coerce")
        else:
            d["Report Date"] = pd.to_datetime(
                d["Fiscal Year"].astype(str) + "-12-31", errors="coerce"
            )
    return d.set_index(["Ticker", "Report Date"]).sort_index()


def load_annual_with_fallback(
    simfin_income: pd.DataFrame,
    simfin_balance: pd.DataFrame,
    simfin_cashflow: pd.DataFrame,
    fmp_dir: Path | None = None,
    max_fiscal_year: int = 2024,
    verbose: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Live-screener source-swap loader for Option B.

    Returns three DataFrames with the SAME shape and MultiIndex as
    simfin.load_income/balance/cashflow, so downstream code
    (compute_history_metrics + compute_snapshot) runs unmodified.

    Merge rule (per-cell fallback, FMP-preferred):
      - Rows keyed by (Ticker, Fiscal Year).
      - Where FMP has (ticker, FY): overwrite the mapper-covered columns with
        FMP values; other SimFin columns (S&M, non-op income, restated date,
        etc.) stay from SimFin because compute_snapshot occasionally reads them.
      - Rows where FMP has (ticker, FY) but SimFin does NOT: appended from the
        FMP frame; other-SimFin columns stay NaN → compute_snapshot's
        `.get(col, 0) or 0` defaults them to 0. These are the 22% of pairs
        Study A flagged as FMP-only.
      - Rows where SimFin has (ticker, FY) but FMP does NOT: kept verbatim
        from SimFin. These are the ~2% SF-only pairs (NANO, USX, ...).

    The function also logs mixed-source-history: tickers whose last-5-FY
    window under `max_fiscal_year` contains BOTH FMP and SimFin sources.
    Study A found this affects ~19% of the sample; the operator can inspect
    the log and decide whether to promote to "FMP-only, no fallback" later.
    """
    fmp_i = map_income(None if fmp_dir is None else fmp_dir / "fundamentals_income.csv")
    fmp_b = map_balance(None if fmp_dir is None else fmp_dir / "fundamentals_balance.csv")
    fmp_c = map_cashflow(None if fmp_dir is None else fmp_dir / "fundamentals_cashflow.csv")

    def _blend(
        sf: pd.DataFrame,
        fmp: pd.DataFrame,
        mapper_cols: list[str],
    ) -> pd.DataFrame:
        sf_flat = sf.reset_index()
        # Keys align on (Ticker, Fiscal Year). FMP FY was cast to Int64 by the
        # mapper; SimFin FY is int64. Coerce both to a common integer dtype
        # for consistent set membership.
        sf_flat["Fiscal Year"] = pd.to_numeric(
            sf_flat["Fiscal Year"], errors="coerce"
        ).astype("Int64")
        fmp2 = fmp.copy()
        fmp2["Fiscal Year"] = pd.to_numeric(
            fmp2["Fiscal Year"], errors="coerce"
        ).astype("Int64")

        # Overwrite mapper-covered columns on SF rows where FMP has the same key.
        fmp_indexed = fmp2.set_index(["Ticker", "Fiscal Year"])
        sf_flat = sf_flat.set_index(["Ticker", "Fiscal Year"])
        overlap_idx = sf_flat.index.intersection(fmp_indexed.index)
        for col in mapper_cols:
            if col in fmp_indexed.columns and col in sf_flat.columns:
                sf_flat.loc[overlap_idx, col] = fmp_indexed.loc[overlap_idx, col]

        # Append FMP-only rows. Preserve mapper columns from FMP; other SimFin
        # columns stay NaN. Report Date derived from Publish Date.
        fmp_only_idx = fmp_indexed.index.difference(sf_flat.index)
        if len(fmp_only_idx) > 0:
            fmp_only = fmp_indexed.loc[fmp_only_idx].reset_index()
            if "Publish Date" in fmp_only.columns:
                fmp_only["Report Date"] = pd.to_datetime(
                    fmp_only["Publish Date"], errors="coerce"
                )
            else:
                fmp_only["Report Date"] = pd.to_datetime(
                    fmp_only["Fiscal Year"].astype(str) + "-12-31", errors="coerce"
                )
            # Union columns with sf_flat
            sf_reset = sf_flat.reset_index()
            all_cols = list(dict.fromkeys(list(sf_reset.columns) + list(fmp_only.columns)))
            for c in all_cols:
                if c not in sf_reset.columns:
                    sf_reset[c] = pd.NA
                if c not in fmp_only.columns:
                    fmp_only[c] = pd.NA
            combined = pd.concat([sf_reset[all_cols], fmp_only[all_cols]], ignore_index=True)
        else:
            combined = sf_flat.reset_index()

        combined = combined.dropna(subset=["Ticker", "Fiscal Year", "Report Date"])
        return combined.set_index(["Ticker", "Report Date"]).sort_index()

    merged_i = _blend(simfin_income, fmp_i, INCOME_COLS)
    merged_b = _blend(simfin_balance, fmp_b, BALANCE_COLS)
    merged_c = _blend(simfin_cashflow, fmp_c, CASHFLOW_COLS)

    # ---- Mixed-source-history log ----
    # For each ticker, look at the last 5 FYs <= max_fiscal_year; note which
    # source served each (Ticker, FY). Print the count that mix.
    fmp_i_keys = set(zip(fmp_i["Ticker"], pd.to_numeric(fmp_i["Fiscal Year"], errors="coerce").astype("Int64")))
    mixed_tickers: list[str] = []
    sample_mixed_detail: list[tuple[str, list[str]]] = []
    for t, g in merged_i.reset_index().groupby("Ticker"):
        g = g[g["Fiscal Year"] <= max_fiscal_year].sort_values("Fiscal Year")
        fys = list(g["Fiscal Year"].dropna().astype(int).tail(5))
        if not fys:
            continue
        srcs = ["FMP" if (t, y) in fmp_i_keys else "SF" for y in fys]
        if len(set(srcs)) > 1:
            mixed_tickers.append(t)
            if len(sample_mixed_detail) < 10:
                sample_mixed_detail.append((t, srcs))
    if verbose:
        n_total = merged_i.index.get_level_values(0).nunique()
        print(
            f"  FMP fundamentals: merged {len(merged_i)} income, {len(merged_b)} balance, "
            f"{len(merged_c)} cashflow rows"
        )
        print(
            f"  FMP fundamentals: mixed-source-history tickers "
            f"{len(mixed_tickers)}/{n_total} "
            f"({len(mixed_tickers) / max(1, n_total) * 100:.1f}%). "
            f"Piotroski f_liquidity may be corrupted on these."
        )
        for t, srcs in sample_mixed_detail:
            print(f"    e.g. {t}: {srcs}")

    return merged_i, merged_b, merged_c


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
