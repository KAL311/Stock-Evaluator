# Stock Evaluator test suite

Two layers, deliberately:

1. **Unit tests** (`test_scoring_helpers.py`, `test_quality_gates.py`) —
   pure-function math with hand-computed fixtures. No data, no golden
   master required. Green on a fresh clone in seconds.
2. **Golden-master regression** (`test_golden_master.py`,
   `test_dq_propagation.py`) — re-runs the *production* scorer
   (`compute_potential_scores`, the v1 path that `main()`/`run_daily`
   actually call) on a frozen copy of the real stocks table and requires
   exact agreement with a certified snapshot. This is the automated
   version of the manual cross-checking you've been doing against agent
   "all acceptance met" claims.

## Setup

```
pip install pytest
```

(and add `pytest` to the `requirements.txt` you're about to create —
gap #5 from the status review.)

## First-time: build the golden master

Do this **only on a verified build** — i.e. after
`scripts/verify_dq_v3.py` passes, per `docs/post_dq_v3_verification.md`:

```
python tests/build_golden_master.py
git add tests/ && git commit -m "Golden master: freeze verified scorer state"
```

This freezes: the stocks table (`fixtures/frozen_stocks.pkl`), copies of
`factor_decay.json` + `factor_etf_holdings.json`, and expected outputs
for ~60 sentinel tickers plus full-universe distribution stats
(`fixtures/golden_scores.json`), stamped with git HEAD and the
determinism knobs (seed=42, N_RESAMPLES=1000, DECAY_ENABLED=1).

## Running

```
pytest tests/ -v
```

Before the golden master exists, the regression tests skip with an
instructive message; the unit tests run regardless.

## Interpreting failures

| Symptom | Meaning | Response |
|---|---|---|
| Unit test fails | The gate/helper math itself changed | Frozen-model violation unless intentional; if intentional, new validation cycle, then update the fixture with a note |
| Golden sentinel diverges | An edit was not score-neutral | Investigate the diff; the failing tickers/columns are printed |
| Golden test **skips** citing calibration drift | `factor_decay.json` or ETF holdings were refreshed | Legitimate. Verify the current build, then re-freeze |
| Divergence right after a pandas/numpy upgrade | Dependency drift changed float behaviour | Pin the dependency; do **not** re-freeze to paper over it |

## Re-freeze policy (the important part)

The golden master is the executable definition of "the model I
validated." Re-freezing is allowed exactly when you would re-tag
`phase5-frozen`: after a deliberate, verified change. Re-freezing to
make a red test green without understanding why it went red defeats the
entire mechanism — it is the automated equivalent of nudging a
pre-registered threshold after seeing the result.

## Notes on module import

`src/market_screener.py` exits at import without `SIMFIN_API_KEY` and
applies factor-decay to weights at import time. `conftest.py` pins a
dummy key and the determinism env vars *before* import — don't import
the module in new test files directly; use the `ms` fixture.
