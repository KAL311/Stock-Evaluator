"""Golden-master regression test.

Re-runs the production scorer (compute_potential_scores, v1 path — the one
main()/run_daily actually invoke) on the frozen inputs and requires the
outputs to match the certified snapshot exactly (atol 1e-6).

If this test fails after an edit you believed was score-neutral, the edit
was not score-neutral. That is the entire point: it automates the
"agent said all-acceptance-met, reality disagreed" cross-check.

Legitimate reasons for divergence, and the correct response to each:
  - factor_decay.json / factor_etf_holdings.json recalibrated
        -> test SKIPS on sha mismatch; verify the new state, re-freeze.
  - deliberate model change under a new validation cycle
        -> re-freeze AFTER the change passes its own gates; note the
           freeze commit in the commit message.
  - pandas/numpy version change altering float behaviour
        -> that is the dependency-pinning gap biting; pin, don't re-freeze.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

ATOL = 1e-6


def _sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _check_calib_unchanged(golden):
    drift = []
    for name in ("factor_decay.json", "factor_etf_holdings.json"):
        live = _sha(REPO / "data" / name)
        frozen = golden["calib_sha256"].get(name)
        if live != frozen:
            drift.append(name)
    if drift:
        pytest.skip(
            f"Calibration drift since freeze: {drift}. Scores will "
            f"legitimately differ. Verify the current build, then re-run "
            f"`python tests/build_golden_master.py` to re-certify."
        )


def test_knobs_match_freeze(ms, golden):
    k = golden["knobs"]
    assert ms.RESAMPLE_SEED == k["RESAMPLE_SEED"], "seed drifted vs freeze"
    assert ms.N_RESAMPLES == k["N_RESAMPLES"], "n_resamples drifted vs freeze"
    assert ms.DECAY_ENABLED == k["DECAY_ENABLED"], "decay flag drifted"


def test_sentinel_scores_exact(ms, frozen_inputs, golden):
    import pandas as pd

    _check_calib_unchanged(golden)

    scored = ms.compute_potential_scores(frozen_inputs.copy(), verbose=False)
    scored = scored.set_index("ticker")

    mismatches = []
    for ticker, expected in golden["sentinels"].items():
        if ticker not in scored.index:
            mismatches.append(f"{ticker}: missing from output")
            continue
        row = scored.loc[ticker]
        for col, exp in expected.items():
            if col not in scored.columns:
                continue
            got = row[col]
            got_na = pd.isna(got)
            if exp is None:
                if not got_na:
                    mismatches.append(
                        f"{ticker}.{col}: expected NULL, got {got}")
            else:
                if got_na or abs(float(got) - exp) > ATOL:
                    mismatches.append(
                        f"{ticker}.{col}: expected {exp}, got "
                        f"{'NULL' if got_na else float(got)}")

    assert not mismatches, (
        f"{len(mismatches)} sentinel divergence(s) from golden master "
        f"(first 20):\n  " + "\n  ".join(mismatches[:20])
    )


def test_universe_distribution_exact(ms, frozen_inputs, golden):
    import pandas as pd

    _check_calib_unchanged(golden)

    scored = ms.compute_potential_scores(frozen_inputs.copy(), verbose=False)
    pot = pd.to_numeric(scored["potential_score"], errors="coerce")
    s = golden["stats"]

    assert len(scored) == s["n_rows"]
    assert int(pot.notna().sum()) == s["n_potential_nonnull"]
    assert abs(float(pot.mean()) - s["potential_mean"]) <= ATOL
    assert abs(float(pot.std()) - s["potential_std"]) <= ATOL
    for q, key in ((0.10, "potential_q10"), (0.50, "potential_q50"),
                   (0.90, "potential_q90")):
        assert abs(float(pot.quantile(q)) - s[key]) <= ATOL, key
