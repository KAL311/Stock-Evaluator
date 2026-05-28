# Phase 3 OOS Decision Record

**Frozen commit:** 6aed4a335aff397c07ae0885eccc558697d398f9 (tag `phase3-frozen`)
**Date rules written:** 2026-05-27
**OOS period:** 2024 full year (cutoff 2023-12-31, forward 2024-12-31)
**Training periods:** the 4 periods with forward year <= 2023 (forwards 2022-06-30, 2022-12-31, 2023-06-30, 2023-12-31)
**Primary metric:** top_alpha (top decile mean - universe mean), gross
**Secondary metric:** OOS IC (Spearman rank), and top_alpha_net

> **Holdout note (decided before run):** PERIODS contains TWO periods whose
> forward window touches 2024: cutoff 2023-06-30 -> forward 2024-06-30 (H2
> model) and cutoff 2023-12-31 -> forward 2024-12-31 (full year). To avoid
> ANY part of 2024 leaking into training, BOTH are held out of the training
> summary. Only the full-year period (forward 2024-12-31) is reported as the
> single OOS result. This is stricter than reserving one period; it keeps the
> training set strictly pre-2024.

## Decision rules (written before results are known)

| OOS top_alpha (2024) | Interpretation | Action |
|---|---|---|
| > +6% | Model generalizes; edge is real | Proceed to Phase 4 |
| +2% to +6% | Edge real but small | Proceed to Phase 4; size positions modestly |
| -2% to +2% | Edge unclear, possibly noise | Run Phase 4.1 subperiod robustness BEFORE trusting; do not deploy |
| < -2% | Model overfit | Major rework; the model may not work |

## Confirming evidence (secondary)

- OOS IC should be positive and within ~1 std of the training-period mean IC
  (training mean IC ~ +0.16, std ~ 0.05; so OOS IC > +0.10 is confirming,
  OOS IC < 0 is disconfirming regardless of the alpha number).
- top_alpha_net (after costs) should remain positive if gross alpha > +3%.

## Commitment

If the OOS result disappoints (lands in the bottom two rows), I will NOT
re-tune the configuration and re-run. That would convert this OOS test into
another in-sample fit. The honest responses to a disappointing OOS are:
(a) accept the model is weaker than the in-sample numbers suggested, or
(b) wait for genuinely new data (2025) to provide another OOS year.

The known limitation: 2024 is a SINGLE out-of-sample year. Even a strong
result is one data point. It must be read together with the OOS IC and the
Phase 4.1 subperiod consistency check before any capital decision.

## Allowed change during freeze

Fixed `--oos-reserve` matching in `scripts/backtest.v2.py` to match the
FORWARD year (`p[1][:4]`) instead of a substring of the cutoff (`p[0]`).
Without this, `--oos-reserve 2024` matched nothing (no cutoff contains
"2024"; the full-year-2024 period has cutoff 2023-12-31). The fix also
holds out BOTH periods whose forward year is 2024 (forwards 2024-06-30 and
2024-12-31) from the training summary so no part of 2024 leaks into
training, and reports only the full-year period (forward 2024-12-31) as the
single OOS result. Also added a `top_alpha`/`top_alpha_net`/`Universe`
line to the OOS print block (report-only). None of this touches scoring
logic; the frozen config is unchanged.
