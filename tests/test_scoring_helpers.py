"""Unit tests for the scoring primitives: _weighted_avg coverage floor,
_rank_within force-zero semantics, and apply_decay_to_weights invariants.

These are pure functions with no data dependencies — they run without a
golden master and should be the first tests green on any machine.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


# ---------------------------- _weighted_avg ---------------------------------

def test_weighted_avg_full_coverage(ms):
    df = pd.DataFrame({"a": [80.0, 20.0], "b": [40.0, 60.0]})
    out = ms._weighted_avg(df, {"a": 0.75, "b": 0.25})
    assert out.iloc[0] == pytest.approx(0.75 * 80 + 0.25 * 40)
    assert out.iloc[1] == pytest.approx(0.75 * 20 + 0.25 * 60)


def test_weighted_avg_renormalizes_over_available(ms):
    # Row 0 missing 'b' -> weights renormalize over 'a' alone -> score = a.
    df = pd.DataFrame({"a": [80.0], "b": [np.nan]})
    out = ms._weighted_avg(df, {"a": 0.6, "b": 0.4}, min_coverage=0.5)
    assert out.iloc[0] == pytest.approx(80.0)


def test_weighted_avg_coverage_floor_returns_nan(ms):
    # Only 'b' (weight 0.4) present, min_coverage=0.5 -> NaN. This is the
    # mechanism DQ v3 relies on to drop valuation_score when cap-derived
    # yields are masked: sparse-but-strong must not outscore complete rows.
    df = pd.DataFrame({"a": [np.nan], "b": [99.0]})
    out = ms._weighted_avg(df, {"a": 0.6, "b": 0.4}, min_coverage=0.5)
    assert pd.isna(out.iloc[0])


def test_weighted_avg_all_nan_row(ms):
    df = pd.DataFrame({"a": [np.nan], "b": [np.nan]})
    out = ms._weighted_avg(df, {"a": 0.5, "b": 0.5})
    assert pd.isna(out.iloc[0])


# ----------------------------- _rank_within ---------------------------------

def test_rank_within_percentiles_by_group(ms):
    vals = pd.Series([1.0, 2.0, 3.0, 10.0, 20.0])
    grp = pd.Series(["x", "x", "x", "y", "y"])
    out = ms._rank_within(vals, grp)
    # Within-group pct ranks * 100.
    assert out.iloc[0] == pytest.approx(100 / 3)
    assert out.iloc[2] == pytest.approx(100.0)
    assert out.iloc[3] == pytest.approx(50.0)
    assert out.iloc[4] == pytest.approx(100.0)


def test_rank_within_nan_stays_nan(ms):
    vals = pd.Series([1.0, np.nan, 3.0])
    grp = pd.Series(["x", "x", "x"])
    out = ms._rank_within(vals, grp)
    assert pd.isna(out.iloc[1])
    assert pd.notna(out.iloc[0]) and pd.notna(out.iloc[2])


def test_rank_within_force_zero_beats_data(ms):
    # "Had data but it's actively bad" (e.g. negative P/E) -> exactly 0,
    # never NaN, never ranked as infinitely cheap.
    vals = pd.Series([5.0, 9.0, 7.0])
    grp = pd.Series(["x", "x", "x"])
    fz = pd.Series([False, True, False])
    out = ms._rank_within(vals, grp, force_zero=fz)
    assert out.iloc[1] == 0.0
    # Remaining two rank against each other only.
    assert out.iloc[0] == pytest.approx(50.0)
    assert out.iloc[2] == pytest.approx(100.0)


# ------------------------- apply_decay_to_weights ----------------------------

def test_decay_preserves_total_weight(ms):
    base = {"valuation": 0.35, "quality": 0.30, "growth": 0.20, "sentiment": 0.15}
    cfg = {"factors": {
        "valuation": {"lambda": 0.5},
        "quality": {"lambda": 0.0},
        "growth": {"lambda": 2.0},
        "sentiment": {"lambda": 0.1},
    }}
    out = ms.apply_decay_to_weights(base, cfg, horizon=1.0)
    assert sum(out.values()) == pytest.approx(sum(base.values()))


def test_decay_downweights_high_lambda(ms):
    base = {"a": 0.5, "b": 0.5}
    cfg = {"factors": {"a": {"lambda": 5.0}, "b": {"lambda": 0.0}}}
    out = ms.apply_decay_to_weights(base, cfg, horizon=1.0)
    assert out["a"] < out["b"]
    # Hand-check: mult_a = 1/6, mult_b = 1 -> renormalized to sum 1.0.
    raw_a, raw_b = 0.5 / 6, 0.5
    assert out["a"] == pytest.approx(raw_a / (raw_a + raw_b))


def test_decay_noop_without_config(ms):
    base = {"a": 0.6, "b": 0.4}
    assert ms.apply_decay_to_weights(base, None) == base
    assert ms.apply_decay_to_weights(base, {}) == base
    # Missing lambda -> multiplier 1.0 -> unchanged after renorm.
    out = ms.apply_decay_to_weights(base, {"factors": {"a": {}}}, horizon=1.0)
    assert out["a"] == pytest.approx(0.6)
    assert out["b"] == pytest.approx(0.4)
