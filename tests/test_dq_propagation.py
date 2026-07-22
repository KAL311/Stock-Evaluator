"""Behavioral tests for the DQ share-xcheck -> scorer propagation (DQ v3).

Strategy: take real frozen inputs, artificially flag a known-healthy
large-cap as xcheck-failed, re-score, and assert the mask actually bites.
This exercises the exact code path the go-live GO verdict depends on
(mkt/EV masked -> cap-derived yields NaN -> valuation cannot inflate),
using real column structure instead of a guessed synthetic frame.
"""

from __future__ import annotations

import pandas as pd
import pytest

PROBE_CANDIDATES = ["AAPL", "MSFT", "KO", "JPM", "COST"]


def _score(ms, df):
    return ms.compute_potential_scores(df.copy(), verbose=False).set_index("ticker")


@pytest.fixture(scope="module")
def baseline(ms, frozen_inputs):
    return _score(ms, frozen_inputs)


def _pick_probe(baseline):
    for t in PROBE_CANDIDATES:
        if t in baseline.index and pd.notna(baseline.loc[t, "valuation_score"]):
            return t
    pytest.skip("no healthy probe ticker with a valuation_score in fixture")


def test_flipping_xcheck_kills_valuation_inflation(ms, frozen_inputs, baseline):
    probe = _pick_probe(baseline)
    base_val = float(baseline.loc[probe, "valuation_score"])
    base_pot = float(baseline.loc[probe, "potential_score"])

    mutated = frozen_inputs.copy()
    mutated.loc[mutated["ticker"] == probe, "dq_share_xcheck_failed"] = 1
    rescored = _score(ms, mutated)

    new_val = rescored.loc[probe, "valuation_score"]
    new_pot = rescored.loc[probe, "potential_score"]

    # Core DQ v3 assertion: with market_cap/EV masked, valuation must not
    # survive at-or-above its unmasked level. NaN (coverage floor tripped)
    # is the expected outcome; a strict decrease is the weakest acceptable.
    assert pd.isna(new_val) or float(new_val) < base_val, (
        f"{probe}: valuation_score {base_val} -> {new_val} did not fall "
        f"after xcheck flag — the scorer mask is not biting"
    )
    # NOTE: masking valuation guarantees only that valuation falls/NaNs
    # (assertion above). It does NOT guarantee the *composite* falls: with
    # valuation NaN, the potential weight renormalizes over quality/growth/
    # sentiment, so a name whose valuation sat *below* its surviving pillars
    # (e.g. a strong-Q/G large-cap like AAPL) can see potential *rise*. That
    # is correct renormalization, not inflation — the DQ-v3 concern (corrupt
    # cap -> fake cheapness -> inflated valuation) is fully covered by the
    # valuation assertion. We assert only that the composite stays a valid,
    # bounded score and does not blow up.
    assert pd.isna(new_pot) or (0.0 <= float(new_pot) <= 100.0), (
        f"{probe}: potential_score out of range after masking ({new_pot})"
    )


def test_unflagged_rows_bit_identical_under_mutation(ms, frozen_inputs, baseline):
    """The commit claims non-xcheck rows are bit-identical. Verify: flagging
    one ticker must not move any OTHER ticker's absolute yield inputs —
    though sector-percentile ranks CAN shift by one slot when a peer's
    components go NaN. So assert on a ticker in a DIFFERENT sector_group
    than the probe, where ranks cannot be affected."""
    probe = _pick_probe(baseline)
    probe_group = frozen_inputs.loc[
        frozen_inputs["ticker"] == probe, "sector_group"].iloc[0]

    others = frozen_inputs[
        (frozen_inputs["sector_group"] != probe_group)
        & (pd.to_numeric(frozen_inputs.get("dq_share_xcheck_failed", 0),
                         errors="coerce").fillna(0) == 0)
    ]["ticker"].head(5).tolist()
    if not others:
        pytest.skip("no cross-sector comparison tickers available")

    mutated = frozen_inputs.copy()
    mutated.loc[mutated["ticker"] == probe, "dq_share_xcheck_failed"] = 1
    rescored = _score(ms, mutated)

    for t in others:
        for col in ("valuation_score", "quality_score", "growth_score",
                    "sentiment_score", "potential_score"):
            a, b = baseline.loc[t, col], rescored.loc[t, col]
            if pd.isna(a) and pd.isna(b):
                continue
            assert pd.notna(a) and pd.notna(b) and abs(float(a) - float(b)) < 1e-9, (
                f"{t}.{col} moved ({a} -> {b}) when only {probe} "
                f"(different sector_group) was flagged"
            )


def test_real_xcheck_rows_have_null_valuation(ms, frozen_inputs):
    """Gate A of the verification checklist, as a permanent regression test:
    every genuinely-flagged row in the fixture must score NULL valuation."""
    scored = _score(ms, frozen_inputs)
    merged = frozen_inputs.set_index("ticker").join(
        scored[["valuation_score"]], rsuffix="_scored")
    xmask = pd.to_numeric(
        merged.get("dq_share_xcheck_failed", 0), errors="coerce"
    ).fillna(0).astype(bool)
    if not xmask.any():
        pytest.skip("fixture contains no xcheck-flagged rows")
    offenders = merged.loc[
        xmask & merged["valuation_score_scored"].notna()].index.tolist()
    assert not offenders, (
        f"xcheck-flagged rows scored a valuation anyway: {offenders[:10]}"
    )
