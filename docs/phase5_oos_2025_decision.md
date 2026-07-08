# Phase 5 OOS Decision Record (2025)

> **Tag status (2026-07-08):** `phase5-frozen` tag removed from premature
> commit `7082880` on 2026-07-08; to be recreated on the commit that
> fills PERIODS state variables for the 2025 period, per Step 2.5
> pre-registration.  Remote tag on `origin` still points at `7082880` and
> requires operator confirmation to delete
> (`git push origin :refs/tags/phase5-frozen`) — see Prompt-6 audit log.

**Frozen commit:** `7082880` (tag `phase5-frozen`) — **now superseded, tag removed; see status note above**
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

Frozen commit: 7082880 (tag `phase5-frozen`)
Run date: 2026-07-07
Command:
    python scripts/backtest.v2.py --oos-reserve 2025 \
        --audit-dir data/audit --run-id oos_2025_locked --acknowledge-bias

### Backtest headline (from scripts/backtest.v2.py)

```
OUT-OF-SAMPLE VALIDATION
  Period:     2024->2025
  Regime:     R5
  Stocks:     2109 (scored)
  Top decile:  +8.82%
  Bot decile:  +1.82%
  Universe:    -8.20%    (n=4127 incl. 2118 delisted-proxy rows)
  TOP-ALPHA:  +17.02%    <-- PRIMARY metric, gross, per pre-reg
  Top-alpha net: +7.28%   (universe basis excludes proxy)
  Spread:      +7.00%
  Net spread:  +8.23%
  IC:         +0.1620
```

### Evaluation-layer amendment (Phase 5) evidence

Forward-price fallback fired as designed:

    FORWARD-PRICE SOURCE: yfinance (prices_live)
    forward = 2025-12-31 > SIMFIN_PRICE_CEILING = 2025-06-03
    yfinance-priced tickers: 2016 / 5805 in cutoff universe
    Splice check @ 2025-06-03 ±5d: 2008 tickers compared,
                                    13 excluded (>5% cross-source ratio anomaly).
    Survivorship proxy: +2118 delisted tickers @ -30% (Shumway 1997 anchor)

### Clean, no-proxy view (from scripts/analyze_oos.py)

The pre-registered primary metric uses the same convention as Phase 3
(top-decile mean minus the WHOLE universe mean, including the Shumway
delist-proxy rows). Under Phase 5, `prices_live` covered only
**2016 / 5805 ≈ 35 %** of the SimFin fundamentals universe, so 2118
tickers received the −30 % proxy. That drove the gross universe mean to
−8.20 % and mechanically inflated the headline top-alpha. The scored-
frame view (proxy rows never carry potential scores; they only affect the
universe baseline) is the fair comparison to Phase 3’s numbers:

    OOS top-decile mean:  +8.82%
    OOS universe mean:    +2.05%
    OOS top-alpha:        +6.77%     <-- proxy-clean top-alpha
    OOS top-decile n:     210

### Confirming metrics

- **OOS IC (Spearman) = +0.1620** — positive, within ~1 std of the Phase 3
  training mean IC (+0.147 ± 0.057) and within a similar band on the
  5-period training re-tally. **Confirming**.
- **Top-alpha net = +7.28 %** — > 0 with gross alpha well over +3 %.
  **Survives costs**.
- **Sector concentration** — top-2 sectors 42.4 % of the top decile
  (tech_software 21.4 %, consumer_disc 21.0 %). Below the 50 % warning
  threshold but flagged CAUTION.
- **Block-bootstrap** (`scripts/bootstrap_significance.py`, 1000 draws
  across 4 blocks including the 2025 OOS): pooled point top-alpha
  **+5.71 %**, 5th percentile **+3.88 %**, 95th **+7.12 %**,
  `fraction alpha > 0 = 100 %`. **ROBUST** — edge survives resampling.
- **Quarterly subperiod robustness** (2025 forward window, yfinance
  fallback active):

    | Quarter | top_ret | univ_ret | alpha  |
    |---------|--------:|---------:|-------:|
    | 2025-Q1 |  −7.30% |   −9.01% | +1.71% |
    | 2025-Q2 | +15.75% |   +9.62% | +6.14% |
    | 2025-Q3 |  +7.11% |   +9.37% | −2.25% |
    | 2025-Q4 |  −0.28% |   +1.72% | −1.99% |

    Positive quarters: **2 / 4**. Mean +0.90 %, std 3.41 %. Verdict:
    **LUMPY** — half the annual alpha lived in Q2, and Q3/Q4 were
    negative. This IS the subperiod gate referenced in the top-band
    action rule.

### Diagnostic-tool fix

`scripts/analyze_oos.py` previously hard-coded the Phase 3
`cutoff='2023-12-31', forward='2024-12-31'` defaults in
`quarterly_decomposition`. Run against the 2025 OOS CSV it would
decompose 2024 quarters against 2024 prices — silently wrong. The
docstring in the file made the assumption implicit, not explicit. A
minimal, evaluation-only patch adds `--cutoff` / `--forward` CLI args and
an inferrer that reads them from the `<cutoff_year>_to_<forward_year>`
segment of the CSV filename. This is a diagnostic-tool bug fix, not a
scoring change; the corrected quarterly table above is the one used for
the verdict.

### Verdict (per pre-registered bands)

- Primary metric: top-alpha gross (as recorded by the backtest headline,
  Phase 3 convention) = **+17.02 %**, which lands in the `> +4 %` band
  ("Edge persists"). The proxy-clean view (+6.77 %) also lands in the
  same band.
- Top-band action rule: **FULL SIZE only if ≥ 3 / 4 positive quarters**.
  Observed: **2 / 4 positive quarters** with LUMPY verdict → the full-
  size gate is **NOT** cleared.
- Confirming metric: OOS IC +0.1620 positive and within band →
  confirming.
- Cost-net survival: top-alpha net +7.28 %, spread net +8.23 % → OK.
- Sector concentration 42.4 % → CAUTION but not warning.

**Action triggered: STAY HALF SIZE.** The alpha number is in the top
band but its Q3–Q4 negative quarters and heavy dependence on Q2 fail the
consistency gate. Per the no-retune commitment, this is the final Phase 5
verdict; no rerun, no threshold nudge, no state-var adjustment.

### Caveats (recorded, not rationalizations)

- **prices_live coverage was 35 %, not > 90 %.** The 2118 delist-proxy
  rows outnumber the priced universe (2016). This is a data-availability
  artifact of yfinance vs SimFin fundamentals, not a real delisting wave.
  It heavily inflated the gross top-alpha via a downward-biased universe
  baseline. Practical implication: the +17.02 % headline is optically
  strong but the +6.77 % clean number is the honest read on the model's
  contribution over comparable stocks. Both remain in the top band.
- **Splice-check dropped 13 / 2008 tickers** (0.6 %) for > 5 % cross-
  source ratio anomaly at SIMFIN_PRICE_CEILING ± 5d. Very low
  splice contamination; source-mixing is well controlled.
- **Regime = R5.** Q1/Q4 negative alpha may reflect regime-mismatch tail
  risk more than model failure; not a rationalization, just a hypothesis
  the next OOS year can test.
- **Two OOS years now on the record** — 2024 (+8.46 % under original
  Phase 3 data, drifted to +9.07 % under refreshed SimFin) and 2025 as
  above. Signal is now more than one data point; sample is still small.
- **The suspicion check (> +12 % headline alpha) IS triggered by the
  gross +17.02 %.** The mandatory cross-check: sector-concentration is
  42.4 % top-2 (not a concentrated AI-capex bet by that threshold), and
  the proxy-clean view is +6.77 % — well below the suspicion band. The
  headline inflation source is understood (proxy math under low prices_live
  coverage) and documented, so the suspicion check does not translate into
  further action.

