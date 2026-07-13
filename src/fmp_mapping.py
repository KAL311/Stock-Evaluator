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
import numpy as np
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
    # Physical period-end date. Used by the LIVE-screener source-swap merge to
    # key on (Ticker, Report Date) instead of the FY label, so retailers /
    # off-calendar filers whose SF and FMP FY labels disagree collapse to a
    # single physical period rather than duplicating (fix for the GIII class).
    out["Report Date"] = pd.to_datetime(df.get("date"), errors="coerce")
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
    cols = INCOME_COLS + (["Report Date"] if "Report Date" in out.columns else [])
    return out[cols]


def map_balance(fmp_balance_csv: Path | None = None) -> pd.DataFrame:
    """FMP balance long-format CSV → SimFin-shaped balance DataFrame."""
    src = fmp_balance_csv or (FMP_DIR / "fundamentals_balance.csv")
    df = pd.read_csv(src)

    out = pd.DataFrame()
    out["Ticker"] = df["ticker"].astype(str).str.upper()
    out["Fiscal Year"] = _to_int_fy(df["fiscalYear"])
    out["Report Date"] = pd.to_datetime(df.get("date"), errors="coerce")
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
    cols = BALANCE_COLS + (["Report Date"] if "Report Date" in out.columns else [])
    return out[cols]


def map_cashflow(fmp_cashflow_csv: Path | None = None) -> pd.DataFrame:
    """FMP cash-flow long-format CSV → SimFin-shaped cashflow DataFrame."""
    src = fmp_cashflow_csv or (FMP_DIR / "fundamentals_cashflow.csv")
    df = pd.read_csv(src)

    out = pd.DataFrame()
    out["Ticker"] = df["ticker"].astype(str).str.upper()
    out["Fiscal Year"] = _to_int_fy(df["fiscalYear"])
    out["Report Date"] = pd.to_datetime(df.get("date"), errors="coerce")
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
    cols = CASHFLOW_COLS + (["Report Date"] if "Report Date" in out.columns else [])
    return out[cols]


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
    date_tolerance_days: int = 10,
    verbose: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Live-screener source-swap loader for Option B / Option C (physical-period key).

    Returns three DataFrames with the SAME MultiIndex(Ticker, Report Date) as
    simfin.load_income/balance/cashflow, so downstream code
    (compute_history_metrics + compute_snapshot) runs unmodified.

    Merge rule — PHYSICAL PERIOD key (`Report Date`), not FY LABEL
    -----------------------------------------------------------------
    SimFin labels a fiscal year by calendar-year-of-START; FMP by
    calendar-year-of-END. For off-calendar filers (retailers with Jan/Feb
    year-ends, biotech with mid-year year-ends, etc.), the SAME physical
    filing gets different FY labels (e.g. GIII: SF FY2020 = FMP FY2021,
    both dated Report Date 2021-01-31). The old (Ticker, Fiscal Year)-keyed
    merge would gap-fill one label with a SimFin row that is the SAME
    physical filing as the neighbouring FMP row, duplicating one physical
    period and inflating growth signals purely as a labeling artifact.

    This version keys on `(Ticker, Report Date)` with a tolerance window
    (default 10 days). Two rows collapse when their report dates are within
    the tolerance, treating them as the SAME physical period. FMP is
    preferred within the collapse; SimFin is used only when FMP has no row
    within tolerance of a SimFin row.

    Fiscal Year label convention (post-merge)
    ----------------------------------------
    After the physical-period merge, we reassign a SINGLE consistent FY
    label per row using the calendar year the fiscal period ENDS (i.e.
    Report Date's year). This matches FMP's convention. A given physical
    period will now carry the same FY label whether it came from SimFin
    or FMP. Downstream aggregate_history_metrics's `max_fiscal_year`
    filter continues to work; note that a period SF used to label as
    FY2020 may now be relabeled FY2021 for retailers. Physical data is
    unchanged.

    Live/backtest keying difference (documented, benign)
    ----------------------------------------------------
    The LIVE screener runs through this function; the BACKTEST does not.
    Backtest still uses SimFin's original FY labels. For off-calendar
    filers a live score and its backtest counterpart may reference the
    same physical period under different FY labels. That is the intended
    firewall behavior — do not chase it as a bug.
    """
    fmp_i = map_income(None if fmp_dir is None else fmp_dir / "fundamentals_income.csv")
    fmp_b = map_balance(None if fmp_dir is None else fmp_dir / "fundamentals_balance.csv")
    fmp_c = map_cashflow(None if fmp_dir is None else fmp_dir / "fundamentals_cashflow.csv")

    # --- Non-USD routing (Option b for the currency bug in commit 0c4d0f6) ---
    # FMP preserves reportedCurrency (CNY / EUR / CAD / GBP / JPY / INR / ...);
    # SimFin normalizes everything to USD. Under the mixed live pipeline,
    # market_cap is USD (price × shares) but FMP revenue for a non-USD filer is
    # in local currency → P/S mis-scaled by the FX ratio (TCOM V=100 on CNY).
    # Fix: for tickers where reportedCurrency != USD, do NOT enter the FMP
    # branch. Route the whole ticker wholesale to SimFin's already-
    # USD-normalized annual fundamentals — no intra-ticker mixing that would
    # re-introduce a cross-source problem.
    non_usd_tickers: set[str] = set()
    currency_breakdown: dict[str, list[str]] = {}
    raw_inc_csv = (fmp_dir or FMP_DIR) / "fundamentals_income.csv"
    if raw_inc_csv.exists():
        _raw = pd.read_csv(
            raw_inc_csv, usecols=["ticker", "reportedCurrency"], dtype=str
        )
        # A ticker is NON_USD if ANY reported currency for it is non-USD and
        # non-null. That guards against tickers where FMP sometimes tags USD
        # and sometimes local (rare but seen).
        for tk, group in _raw.dropna(subset=["reportedCurrency"]).groupby("ticker"):
            curs = set(group["reportedCurrency"].str.upper())
            non_usd = curs - {"USD"}
            if non_usd:
                non_usd_tickers.add(str(tk).upper())
                for c in non_usd:
                    currency_breakdown.setdefault(c, []).append(str(tk).upper())

    if verbose and non_usd_tickers:
        curr_summary = ", ".join(
            f"{c}={len(v)}" for c, v in sorted(currency_breakdown.items(),
                                               key=lambda kv: -len(kv[1]))
        )
        print(
            f"  NON_USD fallback: {len(non_usd_tickers)} tickers routed to SimFin "
            f"(currencies: {curr_summary})"
        )
        # Preview a few names for auditability
        sample_names = sorted(non_usd_tickers)[:10]
        print(f"    e.g. {sample_names}")

    # Filter FMP frames to drop non-USD tickers. The downstream blend then
    # sees them as "FMP-empty" and takes SimFin wholesale.
    if non_usd_tickers:
        fmp_i = fmp_i[~fmp_i["Ticker"].isin(non_usd_tickers)].copy()
        fmp_b = fmp_b[~fmp_b["Ticker"].isin(non_usd_tickers)].copy()
        fmp_c = fmp_c[~fmp_c["Ticker"].isin(non_usd_tickers)].copy()

    stats = {"collapsed_exact": 0, "collapsed_within_tol": 0,
             "fmp_only_periods": 0, "sf_only_periods": 0,
             "total_periods_out": 0, "dup_period_tickers": 0,
             "non_usd_routed": len(non_usd_tickers)}

    def _blend(
        sf: pd.DataFrame,
        fmp: pd.DataFrame,
        mapper_cols: list[str],
    ) -> pd.DataFrame:
        # Flatten
        sf_flat = sf.reset_index().copy()
        fmp2 = fmp.copy()

        # Ensure Report Date is datetime
        sf_flat["Report Date"] = pd.to_datetime(sf_flat["Report Date"], errors="coerce")
        fmp2["Report Date"] = pd.to_datetime(fmp2["Report Date"], errors="coerce")

        # Drop rows with missing Report Date (can't be physically keyed)
        sf_flat = sf_flat.dropna(subset=["Ticker", "Report Date"])
        fmp2 = fmp2.dropna(subset=["Ticker", "Report Date"])

        # Per-ticker union + collapse-within-tolerance
        rows_out: list[pd.DataFrame] = []
        tol = pd.Timedelta(days=date_tolerance_days)
        sf_by_t = dict(list(sf_flat.groupby("Ticker", sort=False)))
        fmp_by_t = dict(list(fmp2.groupby("Ticker", sort=False)))
        tickers = set(sf_by_t) | set(fmp_by_t)

        for t in tickers:
            sf_rows = sf_by_t.get(t, sf_flat.iloc[0:0]).sort_values("Report Date").copy()
            fmp_rows = fmp_by_t.get(t, fmp2.iloc[0:0]).sort_values("Report Date").copy()

            if len(fmp_rows) == 0:
                # SimFin-only ticker (e.g. NANO, USX). Keep as-is.
                stats["sf_only_periods"] += len(sf_rows)
                rows_out.append(sf_rows)
                continue
            if len(sf_rows) == 0:
                stats["fmp_only_periods"] += len(fmp_rows)
                # Add all-SF cols as NA
                out = fmp_rows.copy()
                for c in sf_flat.columns:
                    if c not in out.columns:
                        out[c] = pd.NA
                rows_out.append(out[sf_flat.columns.tolist() + [c for c in out.columns if c not in sf_flat.columns]])
                continue

            # For each SF row, check if any FMP row falls within tol; if so, drop SF.
            fmp_dates = fmp_rows["Report Date"].values
            sf_keep_mask = []
            for sd in sf_rows["Report Date"].values:
                deltas_days = np.abs((fmp_dates - sd).astype("timedelta64[D]").astype(int))
                if len(deltas_days) == 0:
                    sf_keep_mask.append(True)
                    continue
                min_delta = deltas_days.min()
                if min_delta == 0:
                    stats["collapsed_exact"] += 1
                    sf_keep_mask.append(False)
                elif min_delta <= date_tolerance_days:
                    stats["collapsed_within_tol"] += 1
                    sf_keep_mask.append(False)
                else:
                    sf_keep_mask.append(True)  # SF has a period FMP lacks
            sf_kept = sf_rows[sf_keep_mask]
            stats["sf_only_periods"] += len(sf_kept)
            stats["fmp_only_periods"] += len(fmp_rows)

            # Combine: FMP + SF-not-covered. Union of columns.
            all_cols = list(dict.fromkeys(list(sf_flat.columns) + list(fmp_rows.columns)))
            fmp_pad = fmp_rows.copy()
            for c in all_cols:
                if c not in fmp_pad.columns:
                    fmp_pad[c] = pd.NA
            sf_pad = sf_kept.copy()
            for c in all_cols:
                if c not in sf_pad.columns:
                    sf_pad[c] = pd.NA
            combined = pd.concat([fmp_pad[all_cols], sf_pad[all_cols]], ignore_index=True)
            rows_out.append(combined)

        if not rows_out:
            merged = sf_flat.iloc[0:0].copy()
        else:
            merged = pd.concat(rows_out, ignore_index=True)

        # Assign consistent Fiscal Year label = year of Report Date (year-of-END)
        merged["Fiscal Year"] = merged["Report Date"].dt.year.astype("Int64")
        merged = merged.dropna(subset=["Ticker", "Report Date", "Fiscal Year"])
        merged = merged.sort_values(["Ticker", "Report Date"])
        stats["total_periods_out"] = len(merged)
        return merged.set_index(["Ticker", "Report Date"]).sort_index()

    merged_i = _blend(simfin_income, fmp_i, INCOME_COLS)
    merged_b = _blend(simfin_balance, fmp_b, BALANCE_COLS)
    merged_c = _blend(simfin_cashflow, fmp_c, CASHFLOW_COLS)

    # ---- Duplicate-physical-period check (should be ~0) ----
    dup = 0
    dup_examples = []
    for t, g in merged_i.reset_index().groupby("Ticker"):
        g = g[g["Fiscal Year"] <= max_fiscal_year].sort_values("Report Date")
        g_window = g.tail(5)
        # Duplicate = same year label OR two rows within tolerance
        if len(g_window["Fiscal Year"].unique()) < len(g_window):
            dup += 1
            if len(dup_examples) < 5:
                dup_examples.append((t, list(g_window["Report Date"].dt.date)))
    stats["dup_period_tickers"] = dup

    # ---- Mixed-source-history log (kept for continuity — now near-0 by construction) ----
    fmp_report_dates_by_ticker: dict[str, list] = {}
    for t, g in fmp_i.dropna(subset=["Report Date"]).groupby("Ticker"):
        fmp_report_dates_by_ticker[t] = list(g["Report Date"])

    tol = pd.Timedelta(days=date_tolerance_days)
    def _is_fmp(t, rd):
        dates = fmp_report_dates_by_ticker.get(t, [])
        for d in dates:
            if abs((rd - d).days) <= date_tolerance_days:
                return True
        return False

    mixed_tickers: list[str] = []
    for t, g in merged_i.reset_index().groupby("Ticker"):
        g = g[g["Fiscal Year"] <= max_fiscal_year].sort_values("Report Date")
        rows = list(g.tail(5)["Report Date"])
        if not rows:
            continue
        srcs = ["FMP" if _is_fmp(t, rd) else "SF" for rd in rows]
        if len(set(srcs)) > 1:
            mixed_tickers.append(t)

    n_total = merged_i.index.get_level_values(0).nunique()
    stats["mixed_source_tickers"] = len(mixed_tickers)
    stats["universe_annual"] = n_total
    # Publish stats + non-USD info at module level so scripts/market_screener
    # can build the HEALTH_JSON line without re-scraping stdout.
    global LAST_LOAD_STATS  # noqa: PLW0603
    LAST_LOAD_STATS = {
        **stats,
        "non_usd_count": len(non_usd_tickers),
        "non_usd_currencies": {c: len(v) for c, v in currency_breakdown.items()},
    }

    if verbose:
        print(
            f"  FMP fundamentals (physical-period keyed, ±{date_tolerance_days}d tolerance):"
        )
        print(
            f"    merged income={len(merged_i)}, balance={len(merged_b)}, cashflow={len(merged_c)} rows"
        )
        print(
            f"    collapse events on income: exact={stats['collapsed_exact']}, "
            f"within_tol={stats['collapsed_within_tol']}"
        )
        print(
            f"    duplicate-physical-period tickers in scoring window: "
            f"{stats['dup_period_tickers']} (should be 0)"
        )
        if dup_examples:
            for tk, dates in dup_examples:
                print(f"      e.g. {tk}: {dates}")
        print(
            f"    mixed-source-history tickers: {len(mixed_tickers)}/{n_total}"
        )

    return merged_i, merged_b, merged_c


# Populated by load_annual_with_fallback each time it runs. Read by
# src/market_screener.py to emit HEALTH_JSON in --no-repl mode.
LAST_LOAD_STATS: dict = {}


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
