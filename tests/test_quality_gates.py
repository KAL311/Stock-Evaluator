"""Unit tests for the quality-gate primitives: Piotroski F-score and
Altman Z-score. Hand-computed fixtures — if any assertion here breaks,
the gate math itself changed, which under frozen-model discipline
requires a new validation cycle, not a test edit.
"""

from __future__ import annotations

import pytest


def _piotroski_perfect_row():
    """All 9 tests should pass for this row."""
    return {
        # current year
        "net_income": 100.0,           # 1: NI > 0
        "operating_cash_flow": 150.0,  # 2: OCF > 0; 4: OCF > NI
        "total_assets": 1000.0,
        "long_term_debt": 100.0,       # 5: 0.10 leverage
        "current_assets": 300.0,
        "current_liabilities": 100.0,  # 6: current ratio 3.0
        "revenue": 800.0,
        "gross_profit": 400.0,         # 8: GM 0.50
        "shares_outstanding": 90.0,    # 7: 90 <= 100 (buyback)
        # prior year
        "ni_prev_year": 50.0,
        "tot_assets_prev_year": 1000.0,   # 3: ROA 0.10 > 0.05
        "ocf_prev_year": 40.0,
        "lt_debt_prev_year": 200.0,       # 5: 0.20 -> 0.10 fell
        "cur_ratio_prev_year": 2.0,       # 6: 3.0 > 2.0
        "gm_prev_year": 0.40,             # 8: 0.50 > 0.40
        "turnover_prev_year": 0.70,       # 9: 0.80 > 0.70
        "shares_prev_year": 100.0,
    }


def test_piotroski_perfect_nine(ms):
    assert ms.compute_piotroski_fscore(_piotroski_perfect_row()) == 9


def test_piotroski_empty_row_scores_one(ms):
    # All-zero row: only test 7 passes (shares_prev == 0 -> no-dilution
    # credit by construction). Documents current behaviour so any future
    # change to the zero-shares convention is caught deliberately.
    assert ms.compute_piotroski_fscore({}) == 1


def test_piotroski_single_failures(ms):
    row = _piotroski_perfect_row()
    row["net_income"] = -10.0
    # Failing NI>0 also flips ROA-rising (neg vs pos prior) and can flip
    # accruals (OCF>NI still true). Recompute expectations precisely:
    # 1 fails; 3: roa=-0.01 < 0.05 fails; others hold -> 7.
    assert ms.compute_piotroski_fscore(row) == 7

    row = _piotroski_perfect_row()
    row["shares_outstanding"] = 120.0  # dilution -> lose test 7 only
    assert ms.compute_piotroski_fscore(row) == 8

    row = _piotroski_perfect_row()
    row["long_term_debt"] = 300.0  # leverage rose -> lose test 5 only
    assert ms.compute_piotroski_fscore(row) == 8


def test_altman_z_hand_computed(ms):
    row = {
        "working_capital": 200.0,
        "total_assets": 1000.0,
        "retained_earnings": 300.0,
        "operating_income": 150.0,
        "market_cap": 1200.0,
        "total_liabilities": 500.0,
        "revenue": 1100.0,
    }
    # Z = 1.2(.2) + 1.4(.3) + 3.3(.15) + 0.6(2.4) + 1.0(1.1) = 3.695
    z = ms.compute_altman_zscore(row)
    assert z == pytest.approx(3.695, abs=1e-9)
    assert z > 2.99  # safe zone


def test_altman_z_distress_zone(ms):
    row = {
        "working_capital": -100.0,
        "total_assets": 1000.0,
        "retained_earnings": -200.0,
        "operating_income": -50.0,
        "market_cap": 100.0,
        "total_liabilities": 900.0,
        "revenue": 400.0,
    }
    z = ms.compute_altman_zscore(row)
    # 1.2(-.1)+1.4(-.2)+3.3(-.05)+0.6(1/9)+1.0(.4) = -0.0983...
    assert z == pytest.approx(-0.09833333, abs=1e-6)
    assert z < 1.81  # distress zone


def test_altman_z_no_assets_returns_none(ms):
    assert ms.compute_altman_zscore({"total_assets": 0.0}) is None
    assert ms.compute_altman_zscore({}) is None


def test_altman_z_zero_liabilities_drops_d_term(ms):
    row = {
        "working_capital": 200.0,
        "total_assets": 1000.0,
        "retained_earnings": 300.0,
        "operating_income": 150.0,
        "market_cap": 1200.0,
        "total_liabilities": 0.0,   # D term -> 0, no ZeroDivision
        "revenue": 1100.0,
    }
    z = ms.compute_altman_zscore(row)
    assert z == pytest.approx(3.695 - 0.6 * 2.4, abs=1e-9)
