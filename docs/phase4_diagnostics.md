# Phase 4 Diagnostics — OOS edge characterization (gates 4.0 + 4.1)

Scoring config FROZEN at `phase3-frozen`. Everything here is report-only.
Source frame: `data/audit/scored_OOS_2023_to_2024_oos_2024_locked.csv`
(regenerated after applying the Phase 3 persistence fix; see below).

## Pre-flight

- **Working tree:** dirty, but only with junk untracked files (heredoc
  fragments from a botched shell command: `'`, `0`, `{col_w}}'`, `2024`, …)
  plus `.claude/` and a data xlsx. None staged, none touch the model. Flagged,
  not deleted.
- **OOS scored CSV:** was MISSING. The Phase 3 persistence fix had not been
  applied — the held-out OOS frame (`oos_scored`) was excluded from
  `all_scored_list` and so never written.
- **Fix applied:** `scripts/backtest.v2.py` now persists the OOS frame as
  `scored_OOS_<label>_<run_id>.csv` after the period-write loop. Report-only;
  does not touch scoring.
- **Determinism check:** re-ran the locked command
  `python scripts/backtest.v2.py --oos-reserve 2024 --audit-dir data/audit --run-id oos_2024_locked`.
  Result reproduced IDENTICALLY: top-alpha +8.46%, top decile +11.89%,
  universe +3.43%, OOS IC +0.1666, n=1930. No non-determinism.

### Baseline caveat (not a failure)

`analyze_oos.py` computes universe mean as the raw `fwd_return` mean of the
persisted CSV (+4.45%), giving a script top-alpha of +7.44%. The backtest
headline universe (+3.43%) and alpha (+8.46%) differ because the backtest
applies the delisted/survivorship proxy and winsorization to the universe
baseline; the persisted CSV stores raw forward returns. Top-decile mean
(+11.89%) and n (1930) match exactly, so this is a baseline-definition
difference, NOT a model discrepancy. Concentration (count-based) and quarterly
(independent price recompute) verdicts are unaffected.

## 4.0 — Sector concentration (MANDATORY GATE)

Top-decile (n=193) sector mix:

| sector          | univ % | top % | top n | tilt   |
|-----------------|-------:|------:|------:|-------:|
| industrials     |  19.2  | 22.3  | 43    | +3.1pp |
| consumer_disc   |  15.8  | 20.7  | 40    | +5.0pp |
| tech_software   |  15.3  | 17.6  | 34    | +2.3pp |
| healthcare      |  21.5  | 10.4  | 20    | -11.1pp|
| tech_hardware   |   6.8  |  9.8  | 19    | +3.1pp |
| reits           |   7.3  |  6.7  | 13    | -0.5pp |

- **Top-2 sector share: 43.0%** → CAUTION band (40–50%).
- **Top-3 sector share: 60.6%.**
- Per-sector alpha contribution (top-decile names): industrials +2.84pp,
  tech_software +1.89pp, tech_hardware +1.73pp, consumer_disc +0.95pp;
  **healthcare DRAGS at -1.92pp** (-14.08% mean), reits -0.53pp.
- Largest single contributor = industrials at ~38% of total alpha,
  **below the 60% single-sector-fragility threshold.**

**Verdict 4.0: CAUTION (moderately concentrated, not a single-sector bet).**
Edge is spread across 4 positive-contributing sectors; lean on sector-neutral
alpha as the truer number, but it is not a one-sector wager.

## 4.1 — Subperiod robustness (MANDATORY GATE)

Quarterly decomposition of OOS top-decile alpha (price-based, read-only SimFin):

| Quarter | top_ret | univ_ret | alpha   | n_top |
|---------|--------:|---------:|--------:|------:|
| 2024-Q1 | +7.93%  | +4.41%   | +3.52%  | 179   |
| 2024-Q2 | -0.97%  | -3.78%   | +2.81%  | 178   |
| 2024-Q3 | +4.61%  | +7.93%   | -3.33%  | 175   |
| 2024-Q4 | +2.53%  | -0.05%   | +2.58%  | 175   |

- Quarterly alpha mean +1.39%, std 2.75%, **positive in 3/4 quarters**
  (Q3 2024 negative, -3.33%).
- Best-quarter share: 63% of total alpha (Q1).
- Dispersion is high (std 2.75% > 1.5× mean 1.39%).

**Verdict 4.1: MODERATELY STEADY.** Real but variable — positive in 3/4
quarters yet high dispersion and one clearly negative quarter.

## BINDING SIZING DECISION

```
PHASE 4 GATES
4.0 Concentration: top-2 sector share = 43.0%  -> CAUTION
    Single largest alpha contributor: industrials at ~38% of total alpha (OK, <60%)
4.1 Subperiod:     3/4 positive quarters, std 2.75%, best-quarter share 63%
    -> MODERATELY STEADY
SIZING IMPLICATION: HALF SIZE until more OOS (2025) data confirms steadiness.
```

Per the pre-registered Phase 4.1 decision rule (binding, like the Phase 3
pre-registration): **variable → half size.** Deploy at half the intended
position size; revisit for full size after a second OOS year (2025) or if a
subperiod recheck shows steadier quarters. Not lumpy (would have forced
quarter-size), not steady (would have allowed full size).

## Status

Both mandatory gates passed and are interpreted. Project is sound and
deployable at HALF SIZE. Optional depth work (4.2-4.5) complete; results below. None changes the
HALF-SIZE sizing conclusion — they refine the production setup.

## 4.2 — HRP weight concentration (optional)

Standard 6-period backtest (run-id `phase4_diag`). HRP top-5 weight share vs
equal-weight equivalent (~2.7%):

| Period      | n   | HRP fwd | EW fwd  | HRP Cal | EW Cal | top-5 share |
|-------------|----:|--------:|--------:|--------:|-------:|------------:|
| 2021->2022  | 185 | -10.09% | -18.10% | -0.47   | -0.70  | 13.4%       |
| 2021->2022  | 201 | -10.57% | -15.29% | -0.72   | -0.86  | 18.9%       |
| 2022->2023  | 185 |  +0.05% | +20.22% | +0.75   | +1.88  | 98.3%       |
| 2022->2023  | 181 |  +0.00% | +19.68% | +0.46   | +2.02  | 87.5%       |
| 2023->2024  | 193 |  +5.55% | +17.27% | +1.35   | +1.47  | 57.0%       |
| 2023->2024  | 193 |  +5.28% | +13.05% | +3.79   | +2.31  | 67.9%       |
| **Mean**    |     | -1.63%  | +6.14%  |         |        | vol -63.6%  |

- HRP makes **large concentrated bets** — top-5 share 57-98% in 4/6 periods
  (vs EW 2.7%). It is NOT "doing nothing"; it is doing a lot, and what it does
  mostly hurts return.
- HRP cuts vol/drawdown (mean vol reduction +63.6%) but at a heavy return cost:
  mean fwd **-1.63% vs EW +6.14%**. In 2022->2023 HRP collapsed to a few names
  (72-98% top-5) and made ~0% while EW made ~+20%.
- HRP improves Calmar in only **3/6** periods (p1, p2, p6).

**Verdict 4.2: borderline (3/6). Default to EQUAL-WEIGHT for live sizing**
unless drawdown control becomes a hard priority. HRP's concentration is real
but return-destructive in this universe.

## 4.3 — OOS-clean decay refit (optional)

`--exclude-period` flag added to `calibrate_factor_decay.py`. Refit on the
locked audit (4 training periods, 2024 already held out) -> `factor_decay_train_only.json`.

| factor    | frozen λ | train-only λ | shift   |
|-----------|---------:|-------------:|--------:|
| valuation | 0.5611   | 8.7606       | 8.20    |
| quality   | 0.5870   | 0.6761       | 0.09    |
| growth    | 0.1003   | 374.44       | 374.34  |
| sentiment | 0.0000   | 0.0000       | 0.00    |

**Verdict 4.3: INCONCLUSIVE / confounded.** The comparison mixes (a) removing
2024 with (b) dropping from 6 fit periods to 4. The train-only fit is
numerically unstable at n=4 (growth λ=374 with mean_IC=nan is a degenerate
exponential fit). The huge shifts are small-sample noise, NOT clean evidence
the frozen decay overfit 2024. **Frozen `factor_decay.json` left untouched**
(train-only written to a separate file). A real OOS-clean decay diagnosis needs
more periods; not actionable now.

## 4.4 — Sector-neutral minimum size (optional)

Threshold raised 20 -> 30 with a skip message. Standard re-run produced
**0 skips** — no sector_group falls in the 20-29 band. The change is a no-op.
Note: the real small-decile noise the plan flagged (utilities ~n=51, energy
~n=68 giving within-sector deciles of 5-7 names) comes from sectors with n>=30,
so the 30 threshold does not address it. Fixing that would need a higher
threshold or a min-decile-size floor — scoring-adjacent, out of Phase 4 scope.

## 4.5 — Block-bootstrap significance (optional)

`scripts/bootstrap_significance.py`, blocks = whole periods, 1000 draws over
the 3 locked-run scored frames (2021->2022 +2.66%, 2022->2023 +5.53%,
OOS 2023->2024 +7.44%).

- Pooled point top-alpha: +5.15% (N=5747).
- Median +5.15%, **5th pct +3.54%, 95th pct +6.83%, 100% of draws > 0**.

**Verdict 4.5: ROBUST** — 5th percentile +3.54% > 0; edge survives resampling.
Caveat: only 3 blocks gives low bootstrap resolution, but all three periods are
individually positive, so the robustness is genuine if coarse.

## Production setup implications (summary)

- **Position size: HALF** (4.1 binding).
- **Weighting: equal-weight, not HRP** (4.2 borderline; HRP return-destructive).
- **Decay: keep frozen** (4.3 inconclusive; do not swap).
- **Significance: robust** (4.5), concentration moderate not fatal (4.0).
- Revisit full size + HRP reconsideration after a 2025 OOS year.
