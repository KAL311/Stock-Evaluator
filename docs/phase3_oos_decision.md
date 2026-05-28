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

## REALIZED RESULT (run-id: oos_2024_locked)

Frozen commit: 6aed4a335aff397c07ae0885eccc558697d398f9
Run date: 2026-05-27

```
TRAINING PERIODS (4 periods, forwards 2022 & 2023; both 2024 forwards held out):
  Mean top_alpha:     +5.25%   (net +5.05%)
  Mean IC:            +0.1467  (std 0.0574, 4/4 periods IC > 0)
  Alpha hit rate:     3/4

OUT-OF-SAMPLE (2023-12-31 -> 2024-12-31, held out):
  Stocks:             1930
  Top decile mean:    +11.89%
  Universe mean:       +3.43%
  Bottom decile:       +1.13%
  TOP-DECILE ALPHA:    +8.46%   <-- primary metric
  Top-alpha net:       +7.77%
  OOS IC:             +0.1666   <-- confirming metric
  OOS spread:         +10.76%   (net +11.76%)

VERDICT (per pre-registered rules):
  top_alpha = +8.46% falls in band: > +6%  ("Model generalizes; edge is real")
  OOS IC = +0.1666 is CONFIRMING: inside the training 1-std band
    [+0.089, +0.204] (training mean +0.1467, std 0.0574) -> strongly confirming.
  top_alpha_net = +7.77% > 0 and gross alpha > +3% -> survives costs.
  Action triggered: PROCEED TO PHASE 4.
```

### Caveats (recorded, not rationalizations)

- **Single OOS year.** 2024 is one data point. The strong result must still be
  read against the Phase 4.1 subperiod consistency check before any capital
  decision.
- **Suspicion check (>+12% alpha) NOT triggered.** OOS alpha +8.46% and
  top-decile absolute return +11.89% are both below the +12% "be suspicious of a
  concentrated AI-capex bet" threshold, so the mandatory sector-concentration
  cross-check was not required.
- **Concentration cross-check unavailable from this run.** The audit save loop
  only persists scored CSVs for training periods (held-out periods are excluded),
  so the OOS top-decile sector mix was not written to disk. NOT re-running to
  obtain it (run-once discipline). A future report-only diagnostic should persist
  `oos_scored` to compute this without a re-run.
- **OOS top-decile (+11.89%) trailed the in-sample 2023->2024 (+14.99%) and the
  full-year in-sample (+7.66%) figures** noted in Phase 1 — i.e. the held-out
  result is in a sane range, neither collapsing (<+2%) nor implausibly high.
