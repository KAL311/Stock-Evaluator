# Phase 5 OOS Decision Record (2025)

**Frozen commit:** _(to be recorded at commit-and-tag time; tagged `phase5-frozen` after this doc lands)_
**Prior frozen tag:** `phase3-frozen` (6aed4a335aff397c07ae0885eccc558697d398f9), with the Phase 4 diagnostic and the Phase 5 evaluation-layer amendment stacked on top.
**Date rules written:** 2026-07-07
**OOS period:** 2025 full year (cutoff 2024-12-31 → forward 2025-12-31)
**Training periods:** the 5 periods with forward year ≤ 2024 (forwards 2022-06-30, 2022-12-31, 2023-06-30, 2023-12-31, 2024-06-30, 2024-12-31 — the 2024-06-30 H2 window and the 2024-12-31 full-year window BOTH participated in the earlier Phase 3/4 walk, so they now count as training for the 2025 OOS test).
**Primary metric:** top_alpha 2025 (top decile mean − universe mean), gross.
**Secondary metrics:** OOS IC (Spearman rank), top_alpha_net, quarterly subperiod robustness.

> **Holdout note (decided before run):** PERIODS now contains a single 2025
> entry (cutoff 2024-12-31 → forward 2025-12-31). `--oos-reserve 2025`
> matches only this period via the forward-year rule fixed in Phase 3, so
> the training summary is strictly pre-2025. The Phase 5 evaluation-layer
> amendment (see below) supplies the forward-price data.

## Decision rules (written before results are known)

| OOS top_alpha (2025) | Interpretation | Action |
|---|---|---|
| > +4% | Edge persists | Eligible for FULL SIZE **iff** the 4.1-style quarterly subperiod check shows ≥ 3/4 positive quarters. Otherwise stay half. |
| +1% to +4% | Edge real but weak | Stay HALF SIZE. No upsize. |
| −2% to +1% | Edge unclear, possibly noise | Stay half. Mandatory subperiod + sector-concentration review before any 2026 OOS test. |
| < −2% | Model degraded | Cut to quarter size or exit. Root-cause investigation before any further deployment. |

The upper band is intentionally stricter than Phase 3's +6% (which was pre-registered under the assumption that Phase 3 itself was the one-and-only OOS gate before sizing). Phase 4 already sized down to half based on the concentration and HRP diagnostics, so the 2025 result now decides whether we are allowed to size back UP, not whether we deploy at all.

## Confirming evidence (secondary)

- **OOS IC must be positive** and within ~1 standard deviation of the
  now-5-period training-period mean IC. The Phase 3 training set (4
  periods) had mean IC ≈ +0.147, std ≈ 0.057. After absorbing the 2024
  period as training, the 5-period mean will be roughly ≈ +0.15 with a
  similar dispersion; the exact recomputed mean and std are the reference
  band. OOS IC < 0 is disconfirming regardless of the alpha number.
- **top_alpha_net (after costs) must remain positive** if the gross alpha
  clears +3%.
- **Quarterly decomposition** via `scripts/analyze_oos.py`: ≥ 3/4 positive
  quarters is the “consistent edge” bar; lumpy alpha (1–2 quarters driving
  the year) forces a downsize regardless of the headline band.
- **Sector concentration** (report-only): top-2 sector share > 50 % of the
  top decile triggers the same warning as the Phase 3 doc — treat sector-
  neutral alpha as the real edge, not the headline.

## Allowed changes during freeze

### 1. Evaluation-layer amendment: yfinance forward prices past 2025-06-03

**What.** `scripts/backtest.v2.py` now branches on
`forward_str > SIMFIN_PRICE_CEILING` (`'2025-06-03'`; see
`docs/price_layer.md`). For such periods the cutoff price still comes
from SimFin (frozen scoring invariant), but the forward price is drawn
from the `prices_live` table in `data/stock_cache.db` — the same
yfinance-fed table the HTML interface uses. Yahoo's `Close` is auto-
adjusted (split + dividend), matching SimFin's `Adj. Close` semantics.
`scripts/analyze_oos.py` has the same fallback for quarterly
decomposition after the ceiling.

**Why.** SimFin's free-tier daily prices stopped advancing at
2025-06-03. Without a fallback, H2-2025 forward returns are simply
unavailable, and the 2025 OOS revisit mandated by
`docs/phase4_diagnostics.md` cannot happen.

**Scope.** The change is confined to (i) `PERIODS`, (ii) the
forward-return block in `run_one_period`, (iii) the source-consistency
splice check described next, (iv) OOS persistence verification, and
(v) the analyze_oos price loader. No scoring function, weight, regime
overlay, decay term, quality gate, or hard-exclude was touched. The
diff is verified byte-identical against the prior run on the 2024
holdout (see the Phase-4 tag).

**Splice-check exclusion rule.** yfinance back-adjusts a ticker's
entire history the moment it splits, while the SimFin cutoff price is
adjusted only through 2025-06-03. To prevent cross-source
discontinuities from contaminating the OOS number:

> For every ticker with both a SimFin `Adj. Close` and a
> `prices_live` close in the 10-day window `SIMFIN_PRICE_CEILING ± 5d`,
> compute `ratio = yfinance_last / simfin_last`. Any ticker with
> `|ratio − 1.0| > 0.05` is EXCLUDED from the forward universe. The
> excluded count is printed and recorded.

The existing forward-return sanity filter (`between(-0.95, 5.0)`) is
kept — the splice check runs in addition, not instead.

### 2. Delist-proxy semantics under the yfinance path

The Shumway 1997 `-0.30` delist proxy logic in the forward-return
block is unchanged. Under the yfinance path, "priced" means "present in
`prices_live` within 10 days of the forward date"; tickers with no
`prices_live` price and no SimFin trade near forward-end still receive
the proxy return. This is the same semantic that has run in every
period since Phase 2.2 — it just now reads a different source for the
"priced" set on 2025.

## Commitment (verbatim spirit of Phase 3)

If the 2025 OOS result disappoints (lands in the bottom two bands), I
will **NOT** re-tune the configuration, change the state variables, alter
the splice-check threshold, refresh SimFin, or re-run the backtest to
get a different number. That would convert this OOS test into another
in-sample fit. The honest responses to a disappointing OOS are:

  (a) accept that the model is weaker than the Phase 3/4 numbers
      suggested, and act on the band's prescribed sizing, or
  (b) wait for genuinely new data (a fresh 2026 forward window) to
      provide another OOS year.

The known limitation: 2025 is a SINGLE additional OOS year on top of
2024. Even a strong result is one more data point, and it must be read
together with the OOS IC, the quarterly subperiod robustness, and the
sector-concentration cross-check before any change in position size.

## REALIZED RESULT (run-id: oos_2025_locked)

_(this section is appended after the locked run; do not edit before the run)_
