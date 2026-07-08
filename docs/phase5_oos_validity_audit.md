# Phase 5 OOS 2025 Validity Audit

**Audit date:** 2026-07-08
**Auditor commit at time of audit:** `954ddec` (post baff01a live-gate landing)
**Audited artifacts:** commit `7082880` (pre-reg), commit `738db4b` (realized).
**Result CSV:** `data/audit/scored_OOS_2024_to_2025_oos_2025_locked.csv` (2,109 rows).
**Audit type:** read-only forensic. No code, tags, or existing result artifacts were modified.

---

## Verdict — VALID

The recorded OOS 2025 result (`738db4b`) reflects a build that is
consistent with the currently-deployable model along every axis the
pre-registration constrained. Zombie contamination is present but its
direction is **conservative (deflationary)**, not inflationary; the
sizing decision is invariant to the correction.

The known caveats (35% `prices_live` coverage at run time, headline
`+17.02%` vs proxy-clean `+6.77%`) were **explicitly self-disclosed by
the operator in the realized-result section of the decision doc** at
commit time. No re-run is required.

---

## Evidence

### 1. Pre-registration conditions (7082880)

- **Primary metric bands** (verbatim):
  - `> +4%` → FULL SIZE eligible iff ≥ 3/4 positive quarters
  - `+1% to +4%` → HALF SIZE, no upsize
  - `−2% to +1%` → HALF SIZE + mandatory subperiod review
  - `< −2%` → quarter size or exit
- **Confirming metrics**: OOS IC positive & within ~1σ of 5-period
  training-mean IC (Phase 3 training ≈ +0.147 ± 0.057);
  `top_alpha_net > 0` if gross ≥ +3%; ≥ 3/4 positive quarters;
  sector-concentration top-2 > 50% triggers warning.
- **Model-state conditions declared**: (a) `PERIODS` 2025 row filled
  with 2024-12-31 PIT values `(0.0458, 49.3, 0.33, 0.032, 292)`
  (real, not placeholders); (b) forward-price fallback past
  `SIMFIN_PRICE_CEILING = '2025-06-03'` via `prices_live`;
  (c) splice-check exclusion `|ratio − 1.0| > 0.05` at ceiling ± 5d;
  (d) Shumway `-0.30` delist proxy retained.
- **No-retune commitment** (verbatim): "will NOT re-tune the
  configuration, change the state variables, alter the splice-check
  threshold, refresh SimFin, or re-run the backtest to get a different
  number."

### 2. Realized result (738db4b), verbatim from commit + doc

- Commit-message headline: `top-alpha gross +17.02%`, `2/4 positive
  quarters fails the ">3/4 positive" full-size gate -> STAY HALF SIZE`.
  `OOS IC +0.1620 confirming; block-bootstrap ROBUST (5th pctile
  +3.88%); sector concentration 42.4% CAUTION.`
- Doc REALIZED RESULT section (relevant excerpts, verbatim):
  - `OOS top-alpha: +6.77% <-- proxy-clean top-alpha`
  - `OOS top-decile n: 210`
  - `Top-alpha net = +7.28 %`
  - `Sector concentration — top-2 sectors 42.4 %`
  - Quarterly: Q1 +1.71%, Q2 +6.14%, Q3 −2.25%, Q4 −1.99%. **2/4**.
  - `Action triggered: STAY HALF SIZE.`

### 3. Model state at OOS run time (738db4b tree)

| Condition                       | State at 738db4b                       | Matches deployable? |
|---------------------------------|----------------------------------------|---------------------|
| FMP ownership adapter present   | **Absent** (`grep load_ownership_live` in 738db4b:src/market_screener.py → 0 matches; same for `USE_FMP_OWNERSHIP`) | **Yes** — matches the mandatory OOS rule (`USE_FMP_OWNERSHIP` MUST be UNSET, per docs/ownership_layer.md added later). Run was price-only sentiment. |
| Forward-price plumbing          | Present (`SIMFIN_PRICE_CEILING = '2025-06-03'`, `if forward_str > SIMFIN_PRICE_CEILING: ... SELECT ... FROM prices_live`, splice check at ±5d, hard ERROR if prices_live empty) | **Yes.** |
| `prices_live` populated at run  | **Partially — 35% coverage** (operator-disclosed in doc caveats). Ran without erroring because prices_live returned non-zero rows; the guard only trips on 0 rows. | **No** — 35% coverage is well below any healthy live deployment. Documented, not corrected. |
| Backtest survivorship path      | Backtest.v2.py has its own Shumway `-0.30` proxy for tickers `truly_delisted = period_universe − priced_set` (does NOT call `compute_liveness_and_flag`). | Independent of the live screener's liveness gate. Correct semantics. |

The "delisting gate dormant → zombies in the pool" concern posed in the
audit prompt is a **false-alarm framing** for the *backtest*: the live
screener's `compute_liveness_and_flag` is not called by
`scripts/backtest.v2.py`. The backtest applies its own survivorship
correction. What can go wrong is not "the gate was dormant" but "prices_live was under-populated, so more tickers got the −0.30 proxy than
actually delisted." That is precisely the caveat the operator flagged.

### 4. Zombie-contamination check on the recorded CSV

Reproduction of the pre-registered top-decile aggregate from
`scored_OOS_2024_to_2025_oos_2025_locked.csv`:

| Quantity                                          | Value    |
|---------------------------------------------------|----------|
| Rows                                              | 2,109    |
| Universe mean `fwd_return`                        | +2.05%   |
| Top-decile n (`⌊N/10⌋`)                           | 210      |
| Top-decile mean `fwd_return`                      | +8.82%   |
| **Reproduced proxy-clean top-alpha**              | **+6.77%** |
| Universe `-0.30` proxy hits                       | 489      |
| Top-decile `-0.30` proxy hits                     | 27       |

(This reproduces the "proxy-clean top-alpha +6.77%" number in the
committed doc exactly.)

Cross-reference against the CURRENTLY-DELISTED set produced by the
now-active liveness gate against the current `stocks` table:

| Quantity                                                       | Value    |
|----------------------------------------------------------------|----------|
| Live-gate DELISTED count (current run)                         | 507      |
| Top-decile members currently flagged DELISTED                  | **26**   |
| Of those 26: hit `-0.30` proxy in CSV                          | 24       |
| Of those 26: real (non-proxy) `fwd_return` in CSV              | 2 (CTRA +6.65%, APLS −21.29%) |
| Recorded top-alpha EXCLUDING those 26                          | +12.01%  |
| **Delta**                                                      | **+5.24 pp (UPWARD)** |

**Interpretation of the delta.** The 5.24 pp is above the >1 pp
threshold in the audit prompt, but the direction is **upward**:
removing the "zombies" from the top decile makes the alpha larger, not
smaller. The 24 proxy-hits carried `-0.30` returns and were dragging
the top-decile mean DOWN; excluding them raises it. There is no
inflation of the headline by phantom-live returns. The two non-proxy
"zombies" (CTRA +6.65%, APLS −21.29%) contribute in opposite
directions and negligibly.

Applied to the pre-registered bands:
- Proxy-clean recorded: **+6.77%** → `> +4%` band.
- Excluding now-DELISTED from top decile: **+12.01%** → `> +4%` band.
- Headline gross recorded: **+17.02%** → `> +4%` band.

**All three land in the same band.** Band membership is robust to the
zombie correction. Sizing decision (STAY HALF, driven by 2/4 quarters
failing the ≥ 3/4 positive gate) is invariant.

### 5. Sizing / deployment decision (738db4b:docs/phase5_oos_2025_decision.md)

Recorded action: **STAY HALF SIZE**. This is a *provisional* recording
in the sense of the audit prompt: it does rest on the recorded result.
It is however robust to every correction identified in this audit:

- The alpha band membership is unchanged under all three views.
- The full-size gate (≥ 3/4 positive quarters) fails independently of
  alpha magnitude — 2/4 quarters were positive in the recorded output,
  and nothing this audit examined disturbs the quarterly decomposition
  (quarterly returns are recorded, not derived from headline alpha).

Therefore the STAY-HALF verdict does not require re-visitation.

---

## What would change under a re-run (informational only, out of scope)

If the operator were to re-run OOS 2025 today with prices_live at
80.6% coverage instead of 35%:

- Universe baseline would rise (fewer −0.30 proxy hits in the mean).
- Headline top-alpha would fall from +17.02% toward the +6.77% region.
- Proxy-clean number would be essentially unchanged (already excluded).
- Quarterly decomposition would likely shift, but the sign structure
  (Q2 driving, Q3/Q4 negative) is a market-regime observation, not a
  proxy artifact.

The no-retune commitment in the pre-registration explicitly forbids
this re-run. The pre-registration is upheld.

## Summary

- Pre-registration was written before the result was observed (7082880
  precedes 738db4b by 22 minutes, and the doc's REALIZED RESULT section
  was explicitly appended after the fact by the operator).
- Model at run time was price-only sentiment, matching the mandatory
  OOS validation rule for `USE_FMP_OWNERSHIP` established later.
- Forward-price plumbing was present; splice check ran (dropped 13/2008
  = 0.6%).
- The 35% `prices_live` coverage at run time inflates the *headline*
  alpha via a depressed universe baseline. The operator disclosed this
  and adopted the +6.77% proxy-clean number as the honest read.
- The 26 top-decile members currently flagged DELISTED were already
  either handled by the backtest's own Shumway proxy (24) or carry real
  small returns (2). Excluding them shifts the recorded alpha UPWARD
  (+5.24 pp), so there is no upward contamination to strip.
- Sizing decision (STAY HALF) is robust to all identified corrections.

**Verdict: VALID.** No re-run required. Sizing decision (STAY HALF)
stands on both the recorded and corrected views. The absence of the
`phase5-frozen` tag on the current HEAD is a separate operator step
(see Prompt-6 audit note in `docs/phase5_oos_2025_decision.md`).

## What this audit does NOT authorize

- Recreating the `phase5-frozen` tag — still awaiting Step 2.5.
- Modifying `docs/phase5_oos_2025_decision.md` — untouched.
- Re-running the OOS backtest — forbidden by the no-retune commitment
  and unnecessary per this verdict.
- Deleting or moving the 738db4b result artifacts — untouched.

---

## Frozen-tag placement decision

The `phase5-frozen` tag was deleted locally (Prompt 6 audit).  Remote
`origin` still holds `phase5-frozen -> 7082880` and must be unpublished
by the operator (`git push origin :refs/tags/phase5-frozen`).  Once the
remote is clean the tag can be recreated on the correct commit.  There
are two candidates and they now refer to materially different builds.

### Option A — "Frozen = the validated model"

- **Hash:** `7082880` — `pre-register 2025 OOS decision rules`.
- **Meaning:** the exact tree the (VALID) 2025 OOS result in `738db4b`
  was produced against.  Cleanest link between tag and OOS evidence.
- **Limitations at this hash:** live liveness gate silently dormant
  (no `prices_live`, no loud-skip guard); no FMP ownership adapter;
  no coverage-floor warnings; no UNPRICED/DELISTED distinction.  The
  gate's dormancy is immaterial to the backtest (`scripts/backtest.v2.py`
  uses its own Shumway `-0.30` proxy for survivorship and does not call
  `compute_liveness_and_flag`), but it does matter to the LIVE screener
  and portfolio-build path that would run against this tag.

### Option B — "Frozen = what deploys" *(RECOMMENDED)*

- **Hash:** the Prompt 7 commit landed by this task — the current
  post-Part-A HEAD.  Fill this in after the commit lands; example
  placeholder: `<POST_PART_A_HASH>`.
- **Meaning:** the tree that will run with capital going into 2026.
  Includes: live liveness gate + prices_live populated (baff01a);
  loud-skip + coverage-floor warnings (954ddec); UNPRICED/DELISTED
  label split (this commit).  FMP ownership adapter is present but
  default OFF per the OOS validation rule.
- **Assumption made explicit:** the data-layer additions between
  `7082880` and this HEAD are argued to be **validation-neutral**:
  the backtest is unaffected (uses its own survivorship path,
  `USE_FMP_OWNERSHIP` is off in the tree of `738db4b` and remains off
  by default), and the liveness gate affects only the live ranked-output
  / portfolio-build path, not the scored model's factor math.  If a
  future auditor rejects the validation-neutral claim, they must either
  re-run OOS at this hash under the no-retune commitment or move the
  tag back to Option A.

### Recommendation: Option B

The tag's job going into 2026 is to mark **what is running with
capital**.  Every safety layer added since `7082880` (live delisting
protection, coverage warnings, honest UNPRICED label) protects live
production without changing the validated factor math.  Tagging the
validated model at `7082880` would leave the operator with a frozen
reference that does not include those protections and would create
ambiguity every time someone asks "is what I'm deploying frozen?".
Option B collapses that ambiguity.

The validation-neutral claim rests on three concrete facts already
established elsewhere in this doc:

1. `USE_FMP_OWNERSHIP` is UNSET by default and MUST be unset for OOS
   per `docs/ownership_layer.md`.  The backtest tree in `738db4b` does
   not even contain the adapter.
2. `compute_liveness_and_flag` is called from the live screener only,
   never from `scripts/backtest.v2.py`.  Its activation cannot alter
   any recorded OOS number.
3. Coverage-floor and loud-skip guards are print-only.

If any of those three facts is invalidated later, revisit this
recommendation.

### Operator commands (do not execute here)

Do the remote cleanup first, then tag:

```
# 1) Unpublish the old remote tag
git push origin :refs/tags/phase5-frozen

# 2) Pick one — recommend Option B once the Prompt-7 commit lands
git tag phase5-frozen <POST_PART_A_HASH>     # Option B (recommended)
# git tag phase5-frozen 7082880              # Option A (validated model)

# 3) Publish the chosen tag
git push origin phase5-frozen
```

The agent will NOT run any of these; tag placement is the operator's
call and must land on a stable hash.
