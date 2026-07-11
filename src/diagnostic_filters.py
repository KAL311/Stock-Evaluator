"""Diagnostic query helpers for top-N audits and A/B verification.

The SGFY "#5 in rollback top 10" false alarm (commit 0c4d0f6) came from an
ad-hoc SQL query that sorted by `potential_score` without applying the
liveness flag filter. Production `print_table` and `execute_query` already
respect flags; this module exists only so future diagnostic / verification
scripts do the same.

Do NOT use this from the interactive screener. This is intended for
`scripts/ab_verify_fmp.py`, `scripts/parity_study_a.py`, and any other
ad-hoc audit query that ranks by potential_score.
"""

from __future__ import annotations

import pandas as pd

# Flags that should exclude a ticker from a top-N tradeable list. Matches the
# liveness gate in src/market_screener.py:compute_liveness_and_flag semantics.
EXCLUDE_FLAGS = ("DELISTED", "UNPRICED")


def tradeable_sql_filter() -> str:
    """WHERE-clause fragment (without the leading WHERE) for tradeable-only
    diagnostic queries against stock_cache.db `stocks` table.

    Example:
        query = f"SELECT ticker, potential_score FROM stocks " \\
                f"WHERE potential_score IS NOT NULL " \\
                f"AND ({tradeable_sql_filter()}) " \\
                f"ORDER BY potential_score DESC LIMIT 20"
    """
    conds = " AND ".join(f"flags NOT LIKE '%{f}%'" for f in EXCLUDE_FLAGS)
    return f"(flags IS NULL OR ({conds}))"


def is_tradeable_flag(flags_str: str | None) -> bool:
    """Is a single flags string tradeable (no DELISTED / UNPRICED)?"""
    if not flags_str:
        return True
    return not any(f in flags_str for f in EXCLUDE_FLAGS)


def filter_tradeable(df: pd.DataFrame, flags_col: str = "flags") -> pd.DataFrame:
    """Filter a DataFrame of scored rows to tradeable-only.

    Any row whose `flags_col` contains a DELISTED or UNPRICED substring is
    dropped. Rows with NaN/None/empty flags are KEPT (tradeable by default).
    """
    if flags_col not in df.columns:
        return df
    mask = df[flags_col].fillna("").apply(is_tradeable_flag)
    return df[mask]
