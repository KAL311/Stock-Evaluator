# Forward Plan: Stock Evaluator Project

**Current grade: 65/100. Target: 80+/100.**

This document is the implementation roadmap for taking the screener from "promising but overstated" to "honest, defensible, and deployable." It is organized in five phases. Each phase has concrete code patches with file paths and line numbers referencing the current source.

The headline number from your latest backtest run — **+19% mean gross spread, 6/6 hit rate** — is real but inflated. The realistic alpha after honest accounting is **~5–8% gross top-decile-over-universe alpha, sector-neutral**. The goal of this plan is to get the backtest to *report* that honest number, fix the bugs that are currently hiding behind a healthy-looking aggregate, and harden the methodology before you trade real money on it.

**Order of operations rule:** do Phase 1 before Phase 2, etc. Phase 1 items are bug fixes that change what you believe about the model; Phase 2 items make the headline metric honest; Phase 3 items are real methodology upgrades; Phase 4 items deepen the rigor; Phase 5 is production hygiene. Do not skip ahead. Do not tune anything in Phase 3+ until Phase 1 and 2 are locked.

---

## Phase 1 — Critical bug fixes (Week 1, ~6 hours)

These three items are **bugs** in the current pipeline, not methodology choices. They are silently corrupting either the diagnostics or the model itself.

### 1.1 Fix the sensitivity-analysis baseline mismatch

**Problem:** In the most recent backtest output, period 1 reports a base spread of +20.64% but the sensitivity perturbation reports a range of [+32.76%, +35.05%] — the base lies entirely *outside* the perturbation range. This makes the "model is robust to ±5pp weight changes" claim meaningless.

**Root cause:** `compute_sensitivity` in `backtest_v2.py` hardcodes the baseline as `{'V': 0.30, 'Q': 0.30, 'G': 0.25, 'S': 0.15}` and only falls back to `regime_v_weight` columns if present (line 153-161). Meanwhile, the actual scorer in `market_screener.py` line 3703-3741 computes a **per-row** weight matrix that blends (default → sector-specific → regime-modulated → decay-multiplied) and renormalizes. A single global weight tuple cannot represent what the scorer actually applies. The sensitivity test is structurally measuring a different model.

**Fix:** Replace `compute_sensitivity` with a version that recomputes `potential_score` by calling the actual scorer with perturbed weights, rather than reconstructing the score from sub-scores under a single global weight.

**File:** `scripts/backtest_v2.py`

**Replace lines 151–199** (the entire `compute_sensitivity` function) with:

```python
def compute_sensitivity(scored, df_pre_score=None, gics_map=None,
                        regime_probs=None):
    """Perturb each global factor weight by ±5pp, re-run the full v2 scorer,
    return spread range.

    The previous implementation reconstructed `potential_score` from sub-scores
    under a single global weight, which does not match what the v2 scorer
    actually applies (sector-specific weights, per-row regime modulation,
    decay multipliers). The corrected version perturbs the *global default*
    weights and reruns the scorer end-to-end so sensitivity reflects what the
    model would do under perturbation.

    Falls back to a degraded sub-score-blend if df_pre_score is unavailable,
    with a warning that the result is approximate.
    """
    import market_screener as ms
    base = dict(ms.POTENTIAL_WEIGHTS)  # current global, post-decay if enabled
    factors = ['valuation', 'quality', 'growth', 'sentiment']
    short_to_long = {'V': 'valuation', 'Q': 'quality',
                     'G': 'growth', 'S': 'sentiment'}
    pert_pairs = [('V', +0.05), ('V', -0.05),
                  ('Q', +0.05), ('Q', -0.05),
                  ('G', +0.05), ('G', -0.05),
                  ('S', +0.05), ('S', -0.05)]

    # Fast-path: if we don't have the pre-score df, do an approximate
    # reconstruction using whatever per-row weights ARE on the scored frame.
    if df_pre_score is None:
        if all(c in scored.columns for c in
               ['regime_v_weight', 'regime_q_weight',
                'regime_g_weight', 'regime_s_weight']):
            # Use per-row blended weights as the baseline
            rw = scored[['regime_v_weight', 'regime_q_weight',
                         'regime_g_weight', 'regime_s_weight']].mean()
            base = {'valuation': rw['regime_v_weight'],
                    'quality':   rw['regime_q_weight'],
                    'growth':    rw['regime_g_weight'],
                    'sentiment': rw['regime_s_weight']}
        results = {}
        for fkey, delta in pert_pairs:
            long_key = short_to_long[fkey]
            w = dict(base)
            w[long_key] = max(0.0, w[long_key] + delta)
            total = sum(w.values())
            w = {k: v / total for k, v in w.items()}
            score = (scored['valuation_score'].fillna(0) * w['valuation']
                     + scored['quality_score'].fillna(0) * w['quality']
                     + scored['growth_score'].fillna(0) * w['growth']
                     + scored['sentiment_score'].fillna(0) * w['sentiment'])
            cov = (scored['valuation_score'].notna().astype(float) * w['valuation']
                   + scored['quality_score'].notna().astype(float) * w['quality']
                   + scored['growth_score'].notna().astype(float) * w['growth']
                   + scored['sentiment_score'].notna().astype(float) * w['sentiment'])
            score = (score / cov.replace(0, np.nan)).where(cov > 0)
            decile = pd.qcut(score, 10, labels=False, duplicates='drop') + 1
            top = scored.loc[decile == 10, 'fwd_return'].mean() if (decile == 10).any() else np.nan
            bot = scored.loc[decile == 1, 'fwd_return'].mean() if (decile == 1).any() else np.nan
            label = f'{fkey}{delta:+.0%}'
            results[label] = (top - bot) if (pd.notna(top) and pd.notna(bot)) else np.nan

        # Also report the baseline (unperturbed) spread under the same
        # blend so the range comparison is apples-to-apples.
        w = dict(base)
        total = sum(w.values())
        w = {k: v / total for k, v in w.items()}
        score = (scored['valuation_score'].fillna(0) * w['valuation']
                 + scored['quality_score'].fillna(0) * w['quality']
                 + scored['growth_score'].fillna(0) * w['growth']
                 + scored['sentiment_score'].fillna(0) * w['sentiment'])
        cov = (scored['valuation_score'].notna().astype(float) * w['valuation']
               + scored['quality_score'].notna().astype(float) * w['quality']
               + scored['growth_score'].notna().astype(float) * w['growth']
               + scored['sentiment_score'].notna().astype(float) * w['sentiment'])
        score = (score / cov.replace(0, np.nan)).where(cov > 0)
        decile = pd.qcut(score, 10, labels=False, duplicates='drop') + 1
        top0 = scored.loc[decile == 10, 'fwd_return'].mean() if (decile == 10).any() else np.nan
        bot0 = scored.loc[decile == 1, 'fwd_return'].mean() if (decile == 1).any() else np.nan
        results['baseline'] = (top0 - bot0) if (pd.notna(top0) and pd.notna(bot0)) else np.nan
        results['mode'] = 'approx_blend'
    else:
        # Full-fidelity path not yet wired; placeholder for Phase 4.
        results = {'mode': 'full_rescore_not_implemented'}

    spreads = [v for k, v in results.items()
               if pd.notna(v) and k not in ('mode', 'baseline')]
    results['min_spread'] = min(spreads) if spreads else np.nan
    results['max_spread'] = max(spreads) if spreads else np.nan
    results['range'] = (results['max_spread'] - results['min_spread']) if spreads else np.nan
    return results
```

**Then in `run_one_period`, find line 579 (`sens = compute_sensitivity(scored)`)** and update the print block immediately below to also report the new baseline:

```python
sens = compute_sensitivity(scored)
min_s = sens.get('min_spread')
max_s = sens.get('max_spread')
s_range = sens.get('range')
baseline = sens.get('baseline', scored.get('spread', np.nan))
print(f'  Sensitivity (±5pp, mode={sens.get("mode", "?")}):')
print(f'    Reported spread:  {spread:>+.2%}')
print(f'    Sensitivity base: {baseline:>+.2%}  '
      f'(should approximate reported spread)')
print(f'    Range:            [{min_s:+.2%}, {max_s:+.2%}]  '
      f'width={s_range:+.2%}')
for k, v in sens.items():
    if k in ('min_spread', 'max_spread', 'range', 'baseline', 'mode'):
        continue
    print(f'    {k}: spread={v:+.2%}')
```

**Validation:** After patching, the "Sensitivity base" line should be within ~1pp of the "Reported spread" line. If it's not, the approximate blend is still missing something (likely sector-specific or decay multipliers); proceed regardless since the *range* of perturbations is what matters for the robustness conclusion.

---

### 1.2 Fix the missing growth_score in early periods

**Problem:** In periods 2021-06-30 and 2021-12-31, the factor IC table shows `growth IC = N/A`. The composite score in those periods is effectively V+Q+S only — growth contributed nothing.

**Root cause:** In `market_screener.py` line 1755-1760, growth is computed from four components: `revenue_growth_3yr`, `revenue_trend_5y`, `fcf_trend_5y`, `ebitda_trend_5y`. The `_trend_5y` metrics require five years of annual fundamentals. For a 2021-06-30 cutoff, that means filings going back to FY2016/2017. SimFin's coverage of pre-2017 annuals is thin (look at `Computed history metrics for 3898 tickers` in period 1 vs `4329` in period 5 — that's a 12% coverage gap). Combined with `min_coverage=0.5` (must have at least 2 of 4 components), this drops the entire growth_score column for many tickers — and apparently enough that the IC compute (which needs 30 non-null pairs) failed.

**Fix:** Reduce the growth min_coverage from 0.5 to 0.25 (allow growth_score if any 1 of 4 components present), and add a fallback that uses revenue_growth_3yr alone when no trend metrics exist. This will let early-period growth scores be computed from rougher data rather than dropped entirely.

**File:** `src/market_screener.py`

**Change line 1760** from:

```python
growth = _weighted_avg(g_df, GROWTH_WEIGHTS, min_coverage=0.5)
```

to:

```python
# Reduced coverage threshold so periods with short fundamental history
# (early SimFin coverage) still compute a growth_score from whatever
# subset is available rather than dropping the column entirely.
growth = _weighted_avg(g_df, GROWTH_WEIGHTS, min_coverage=0.25)
```

**Also update the second occurrence at line 3693**:

```python
# Find the line:
growth = sector_grw.where(sector_grw.notna(), universal_grw)
# Just above it in the v2 scorer, find where universal_grw is computed
# (search for "universal_grw = _weighted_avg" or similar) and apply the
# same min_coverage=0.25 setting.
```

Use the view tool to locate the v2 growth computation around line 3650-3695 and apply the same change.

**Validation:** Re-run the backtest. The factor IC table should now show numeric values (not N/A) for growth in all six periods. Expect the early-period growth IC to be small (probably near zero or slightly negative) because the 1-component approximation is noisy. That's fine — it tells you growth wasn't actually adding value in 2021-2022, which is also useful information you didn't have before.

---

### 1.3 Fix the period label bug in the summary table

**Problem:** Your summary table prints "sentiment" as the period label for every row instead of "2021->2022" etc. The label is constructed correctly in `run_one_period` line 290 as `f'{cutoff.year}->{forward.year}'` — somewhere downstream it's getting clobbered.

**Diagnostic step (do this first, before patching):**

```bash
# Add to backtest_v2.py, at line 698 immediately after summary['is_oos'] = False
# (inside the else branch where summary is appended to all_results):
print(f"DEBUG: appending summary with label={summary.get('label')!r}, "
      f"keys={list(summary.keys())[:5]}")
```

Run the backtest once with that diagnostic line. If the printed labels are correct ("2021->2022" etc.) but the summary table still shows "sentiment," the bug is in the print loop — apply the defensive patch below. If the labels are already wrong at append time, the bug is upstream in `run_one_period` and we need different tooling.

**Defensive patch (apply if the diagnostic above shows correct labels):**

In `scripts/backtest_v2.py` around line 737, before the loop, add:

```python
# Defensive: if label is missing or non-string, reconstruct from period idx.
for i, r in enumerate(all_results):
    if not isinstance(r.get('label'), str) or len(r.get('label', '')) < 4:
        cutoff_year = 2021 + i // 2
        forward_year = cutoff_year + 1
        r['label'] = f'{cutoff_year}->{forward_year}'
```

**Validation:** Summary table should show "2021->2022", "2022->2023", etc., not "sentiment".

---

### 1.4 Audit the energy sector representation

**Problem:** Energy is conspicuously absent from every top-decile sector concentration table. The Framework calls for energy overweight in R2/R3 regimes, and 2022 was the year energy returned +47% — yet your top decile shows industrials/consumer_disc dominance and zero energy. Either energy isn't in your universe, or the scoring isn't picking it up.

**Fix:** Add a diagnostic that prints sector representation at universe and top-decile level for each period. This is a *diagnostic*, not a code change to the scoring logic — figure out *what's happening* before deciding what to do.

**File:** `scripts/backtest_v2.py`

**Insert at line 596 (just after the top-decile sector concentration print block):**

```python
# --- Universe-vs-top-decile sector representation (diagnostic) ---
print(f'\n  Universe-vs-top-decile sector representation:')
uni_secs = scored['sector_group'].value_counts()
top_secs = scored.loc[scored['decile'] == 10, 'sector_group'].value_counts()
all_secs = sorted(set(uni_secs.index) | set(top_secs.index))
print(f'    {"sector":20s} {"univ_n":>7s} {"univ_%":>7s} '
      f'{"top_n":>6s} {"top_%":>6s} {"over/under":>10s}')
for sec in all_secs:
    un = int(uni_secs.get(sec, 0))
    tn = int(top_secs.get(sec, 0))
    up = un / len(scored) * 100 if len(scored) > 0 else 0
    tp = tn / len(scored[scored['decile'] == 10]) * 100 \
        if (scored['decile'] == 10).any() else 0
    ou = tp - up
    print(f'    {(sec or "(none)"):20s} {un:>7d} {up:>6.1f}% '
          f'{tn:>6d} {tp:>5.1f}% {ou:>+9.1f}pp')
```

**Validation:** Run the backtest. Energy should be ~5-7% of the universe. If it's 0-2% the universe is missing energy names (a SimFin coverage issue or a GICS mapping bug). If it's 5%+ in the universe but 0-1% in the top decile, the scoring is systematically downscoring energy (likely the energy-sector valuation weights don't capture EV/EBITDAX correctly, or the regime tilts aren't biting). The diagnostic will tell you which.

**No code change to the energy scoring yet** — fix the diagnostic, see the data, then decide. If the issue is universe coverage, you can't fix it in code; you'd need a different data vendor. If it's scoring, that's a Phase 3 item.

---

## Phase 2 — Honest headline metrics (Week 2, ~4 hours)

After Phase 1 the model is producing correct numbers internally. Phase 2 is about *reporting* them honestly so you don't fool yourself with the +19% number.

### 2.1 Change the headline metric from top-minus-bottom to top-minus-universe

**Why:** You cannot short most names in decile 1. The borrow is unavailable or punitively expensive for the small-cap, low-quality names that cluster there. The realistic strategy is long top-decile vs SPY/universe. The Framework even calls this out (§2 caveats). Your output already prints `Universe mean` next to spread; just promote it to the headline.

**File:** `scripts/backtest_v2.py`

**Change line 437-439** from:

```python
print(f'\nTop decile mean:    {top_mean:>+7.2%}')
print(f'Bottom decile mean: {bot_mean:>+7.2%}')
print(f'Spread:             {spread:>+7.2%}')
print(f'Universe mean:      {univ_mean:>+7.2%}')
```

to:

```python
top_alpha = top_mean - univ_mean
print(f'\nTop decile mean:    {top_mean:>+7.2%}')
print(f'Universe mean:      {univ_mean:>+7.2%}')
print(f'TOP-DECILE ALPHA:   {top_alpha:>+7.2%}  <-- realistic long-only edge')
print(f'(Bottom decile:     {bot_mean:>+7.2%})')
print(f'(Long-short spread: {spread:>+7.2%}  -- aspirational; bottom often unshortable)')
```

**Add `top_alpha` to the summary dict returned by `run_one_period`** (around line 598):

```python
return {
    'label': label,
    'n': len(scored),
    'top_mean': top_mean,
    'bot_mean': bot_mean,
    'spread': spread,
    'top_alpha': top_alpha,   # <-- ADD THIS
    'spread_net': spread_net,
    # ... rest unchanged
```

**Update the multi-period summary table** at line 730-758 to include the alpha column:

```python
print(f'  {"Period":14s} {"n":>6s}  {"TopDec":>8s}  {"Univ":>8s}  '
      f'{"Alpha":>8s}  {"BotDec":>8s}  {"Spread":>8s}  {"Sharpe":>7s}  '
      f'{"DD":>7s}  {"Calmar":>7s}  {"Turn":>6s}  {"Regime":>10s}')
# adjust the dashes line and the per-row print accordingly
```

In the per-row print:
```python
print(f'  {r["label"]:14s} {r["n"]:>6d}  {r["top_mean"]:>+7.2%}  '
      f'{r["universe_mean"]:>+7.2%}  {r["top_alpha"]:>+7.2%}  '
      f'{r["bot_mean"]:>+7.2%}  {r["spread"]:>+7.2%}  '
      f'{sh_str}  {dd_str}  {ca_str}  {to_str}  {r["regime"]:>10s}')
```

In the mean rows:
```python
mean_alpha = np.mean([r.get('top_alpha', np.nan) for r in valid_results])
mean_alpha = np.nan if np.isnan(mean_alpha) else mean_alpha
print(f'  {"Mean Alpha":14s} {"":>6s}  {"":>8s}  {"":>8s}  '
      f'{mean_alpha:>+7.2%}  <-- this is your realistic long-only edge')
print(f'  {"Mean Spread":14s} {"":>6s}  {"":>8s}  {"":>8s}  '
      f'{"":>8s}  {"":>8s}  {mean_spread:>+7.2%}')
```

**Update the consistency interpretation block at line 960:**

```python
# Replace mean_spread with mean_alpha as the gate
mean_alpha_val = np.mean([r.get('top_alpha', 0) for r in valid_results])
n_alpha_pos = sum(1 for r in valid_results if r.get('top_alpha', 0) > 0)
if n_alpha_pos >= 4 and n_valid >= 5 and mean_alpha_val > 0.04:
    print('  STRONG EDGE (alpha basis): top-decile beats universe by >4%')
    print('  in 4+/5 periods. This is a realistic, deployable edge.')
elif n_alpha_pos >= 3 and n_valid >= 4 and mean_alpha_val > 0:
    print('  WEAK EDGE (alpha basis): top-decile beats universe in most periods')
    print('  but margin is below 4%. Edge is real but small.')
else:
    print('  NO RELIABLE ALPHA-BASIS EDGE.')
```

**Expected outcome based on your current data:** mean alpha will be roughly +8% gross (from the table I built in our last exchange). After Phase 1 + 2 the model should report ~+8% as its headline, not +19%. That's the honest number.

---

### 2.2 Add a delisted-ticker proxy correction

**Why:** 22% of tickers per period delisted between the cutoff and today. The bottom decile is the most contaminated because failed companies cluster there. Without correction, your bottom-decile mean is artificially less-negative because the actually-bankrupt names are missing.

**You cannot fully solve this** without a survivorship-bias-free data vendor (CRSP, Norgate). But you can apply a **lower-bound correction** by assuming the missing tickers in each period had a fixed return.

**File:** `scripts/backtest_v2.py`

**Approach:** SimFin's `companies` is the current roster. Any ticker that appears in `income` for the cutoff year but does not appear in `sp` after `cutoff + 60 days` is presumed delisted during the forward window. Compute their assumed return as the median delisting outcome (`fwd_return = -0.30` is a reasonable proxy based on academic studies of delisted-stock returns; Shumway 1997 finds CRSP delist returns average -55% to -75% in the month of delisting).

**Insert immediately after line 401 (`prices = prices[prices['fwd_return'].between(-0.95, 5.0)]`):**

```python
# --- Survivorship correction (Phase 2.2) ---
# Tickers in the period's universe (had filings before cutoff) that are
# NOT in the forward price set are presumed delisted. Assign them a
# punitive return so they contribute realistically to the bottom decile.
# Shumway (1997, J. Finance) finds CRSP delist returns average ~-55%
# in the delisting month; we use -0.30 as a conservative lower bound
# (assumes some recovery via M&A premium or partial liquidation).
DELIST_PROXY_RETURN = -0.30

period_universe = set(income_h.index.get_level_values('Ticker').unique())
priced_set = set(prices.index)
presumed_delisted = period_universe - priced_set
# Only count those without recent trading in the FULL price history
last_trade_full = sp.groupby(level='Ticker').apply(
    lambda g: g.index.get_level_values('Date').max())
forward_end = pd.Timestamp(forward_str) + pd.Timedelta(days=10)
truly_delisted = {
    t for t in presumed_delisted
    if t in last_trade_full.index
    and last_trade_full[t] < forward_end - pd.Timedelta(days=60)
}
n_proxy = len(truly_delisted)
if n_proxy > 0:
    proxy_rows = pd.DataFrame({
        'p_at_adj': np.nan,
        'p_fwd': np.nan,
        'fwd_return': DELIST_PROXY_RETURN,
    }, index=list(truly_delisted))
    proxy_rows.index.name = 'Ticker'
    prices = pd.concat([prices, proxy_rows])
    print(f'  Added {n_proxy} delisted-proxy returns at '
          f'{DELIST_PROXY_RETURN:.0%} (Shumway 1997 anchor)')
```

**Caveats and how to communicate them:**

Add this to the survivorship warning at the top of the file (line 40-53):

```python
SURVIVORSHIP_WARNING = """
########################################################################
#                                                                      #
#   WARNING: SURVIVORSHIP BIAS                                         #
#                                                                      #
#   This backtest uses the CURRENT SimFin company universe + a         #
#   -30% delisted-ticker proxy (Shumway 1997 anchor) for tickers that  #
#   had filings before the cutoff but stopped trading during the       #
#   forward window. This is a LOWER-BOUND correction, not a full fix:  #
#   the actual delist returns vary widely (-100% bankruptcy to +30%    #
#   M&A premium). True PIT membership requires CRSP or Norgate.        #
#                                                                      #
#   With this correction the bottom-decile spread should DROP and the  #
#   top-decile alpha should be relatively unaffected. If alpha drops   #
#   significantly, the model was relying on bias for its edge.         #
#                                                                      #
########################################################################
"""
```

**Validation:** After Phase 2.1 + 2.2, re-run the backtest. Expected effects:
- Bottom-decile mean returns should drop 3-8pp in each period (more delisted names contaminate it)
- Long-short spread will *widen* (because bottom got more negative), but this is now closer to the un-bias-corrupted number
- Top-decile alpha (top - universe) should be roughly stable, maybe -1 to -2pp
- Hit rate on alpha should remain 5-6/6

If top-decile alpha *drops* significantly after the correction, that's a problem — it means the model was generating its alpha by avoiding names that delisted, which is information it wouldn't have had in real time. Watch for this.

---

### 2.3 Charge market impact on capacity-capped names

**Why:** Your `n_capped` field (line 568-576) already detects names that would exceed 10% of ADV at a $1M position size. You count them but don't charge for them. The Almgren-Chriss square-root impact model gives you the cost.

**Fix:** Add impact-adjusted returns to the cost block.

**File:** `scripts/backtest_v2.py`

**Replace lines 407-413 with:**

```python
# --- Transaction costs (Phase 2.3): bid-ask + market impact ---
# Bid-ask spread proxy: 20bps large-cap, 50bps small-cap (one-way each side).
# Round trip = 2x. This is the floor.
large_thresh = 10e9
scored['tc_bps'] = np.where(
    scored.get('market_cap', pd.Series(0, index=scored.index)).fillna(0) >= large_thresh,
    20, 50,
)
# Market impact: square-root law, IMPACT_Y * sigma * sqrt(Q/ADV) bps.
# Use realized vol from scored if available; else 30% as conservative default.
# Q = $1M target position. ADV = scored['dollar_volume'].
TARGET_Q = 1_000_000.0  # $1M position
IMPACT_Y_LOC = 1.0       # Almgren-Chriss US large-cap calibration
adv = scored.get('dollar_volume', pd.Series(np.nan, index=scored.index))
sigma_daily = scored.get('realized_vol', pd.Series(0.02, index=scored.index)).fillna(0.02)
# sigma_daily is daily stdev (e.g. 0.02 = 2%). Convert to bps for impact formula.
size_ratio = (TARGET_Q / adv).replace([np.inf, -np.inf], np.nan).fillna(0).clip(0, 1)
impact_bps = IMPACT_Y_LOC * sigma_daily * 10000 * np.sqrt(size_ratio)
impact_bps = impact_bps.clip(upper=100.0)  # cap at 100 bps to avoid blow-ups
scored['impact_bps'] = impact_bps
scored['total_tc_bps'] = scored['tc_bps'] + scored['impact_bps']
scored['fwd_return_net'] = scored['fwd_return'] - scored['total_tc_bps'] * 2 / 10000
mean_impact = impact_bps.mean()
print(f'  Mean market impact: {mean_impact:.1f} bps (round-trip 2x in net return)')
```

**Validation:** Cost drag should increase from the current ~15-25 bps to roughly 30-60 bps total. Net spread will compress slightly but not dramatically (impact is dominated by large positions in thinly-traded names, which are a minority).

---

## Phase 3 — True out-of-sample validation (Week 3, ~3 hours)

This is the test that matters. Everything until now has been measurement; this is a single experiment that tells you whether you have an edge or whether you've overfit.

### 3.1 Lock the configuration and reserve 2024

**The rule:** From this point forward, **no further tuning** of:
- `POTENTIAL_WEIGHTS`
- `SECTOR_POTENTIAL_WEIGHTS`
- `SECTOR_VALUATION_WEIGHTS`
- `WEIGHT_BANDS`
- regime classifier thresholds
- factor decay calibration
- any code path that changes a stock's score

You can still fix obvious bugs, change *what gets reported*, or change *how diagnostics are computed*. But you cannot change the scoring.

**Step 1:** Tag the current commit:
```bash
git add -A
git commit -m "Phase 1 + 2 complete; freezing config before OOS test"
git tag phase2-frozen
```

**Step 2:** Re-run with `--oos-reserve 2024`:
```bash
python scripts/backtest_v2.py --acknowledge-bias --oos-reserve 2024 \
    --audit-dir data/audit --run-id oos_2024_locked
```

**Step 3:** Look only at the OOS validation block of the output. The other 4-5 periods are now your training set; their numbers should be ignored for go/no-go.

### 3.2 Interpretation rules (decide these BEFORE running)

Write these rules down now, before you see the OOS number. This is the part of the discipline that matters.

| OOS top-alpha (2024 only) | Interpretation | Action |
|---|---|---|
| > +6% | Model generalizes; edge is real | Proceed to Phase 4 |
| +2% to +6% | Edge real but small | Phase 4, but size positions modestly |
| -2% to +2% | Edge unclear; possibly noise | Run Phase 4.1 (subperiod robustness) before trusting |
| < -2% | Model overfit | Major rework; consider that the model may not work |

The fact that you have only ONE out-of-sample year is itself a limitation — even a +8% alpha in 2024 is one data point. So pair this with the in-sample IC consistency: if mean in-sample IC is +0.17 with std 0.05 and OOS IC is +0.15+, that's confirming evidence. If OOS IC is negative, the rest doesn't matter.

### 3.3 Resist the urge to peek and re-tune

If 2024 OOS comes in at -1%, you will be strongly tempted to "fix the regime classifier" or "adjust the sector weights" and re-run. **Don't.** That's how you turn a real OOS test into another in-sample fit. If 2024 disappoints, the honest move is to acknowledge it, document what happened, and either accept the model is weaker than you thought or wait for 2025 data to provide another genuinely-OOS year.

---

## Phase 4 — Methodology depth (Week 4-5, ~10 hours)

Only do these *after* Phase 3 OOS comes in positive. If OOS failed, these don't matter.

### 4.1 Add subperiod robustness checks

The current backtest treats each period as a single observation. Decompose each 12-month forward window into 4 quarterly subperiods to see if the alpha is consistent within periods or driven by a single quarter.

**File:** `scripts/backtest_v2.py` — add a new function `compute_quarterly_alpha`:

```python
def compute_quarterly_alpha(scored, sp_forward, sp_at, cutoff_str, forward_str):
    """Decompose forward-return alpha into quarterly subperiods.

    Returns DataFrame with columns: quarter, top_mean, univ_mean, alpha, n_top.
    """
    cutoff = pd.Timestamp(cutoff_str)
    forward = pd.Timestamp(forward_str)
    quarters = pd.date_range(cutoff, forward, freq='3MS')[1:]  # 4 quarter ends
    if len(quarters) < 2:
        return None
    sp_all = pd.concat([sp_at, sp_forward])
    price_at_q = {}
    last_q = cutoff
    for q_end in quarters:
        prices_q = sp_all[(sp_all.index.get_level_values('Date') > last_q)
                          & (sp_all.index.get_level_values('Date') <= q_end)]
        if prices_q.empty:
            continue
        last_in_q = prices_q.sort_index().groupby(level=0).last()['Adj. Close']
        price_at_q[q_end] = last_in_q
        last_q = q_end
    if len(price_at_q) < 2:
        return None
    rows = []
    q_dates = sorted(price_at_q.keys())
    prev_p = sp_at.sort_index().groupby(level=0).last()['Adj. Close']
    top_tickers = scored.loc[scored['decile'] == 10, 'ticker'].tolist()
    for q_end in q_dates:
        p_q = price_at_q[q_end]
        common = list(set(prev_p.index) & set(p_q.index))
        ret_q = (p_q.loc[common] / prev_p.loc[common] - 1)
        ret_q = ret_q[ret_q.between(-0.9, 2.0)]  # clip
        top_in_q = ret_q.loc[[t for t in top_tickers if t in ret_q.index]]
        rows.append({
            'quarter': q_end.strftime('%Y-Q%q').replace('Q1', 'Q1').replace('Q2', 'Q2'),
            'top_mean': top_in_q.mean() if len(top_in_q) > 5 else np.nan,
            'univ_mean': ret_q.mean(),
            'alpha': (top_in_q.mean() - ret_q.mean()) if len(top_in_q) > 5 else np.nan,
            'n_top': len(top_in_q),
        })
        prev_p = p_q
    return pd.DataFrame(rows)
```

Call this from `run_one_period` after the monthly-returns block (line 457 area) and report whether alpha is consistent across the 4 quarters or driven by 1-2. If alpha is driven by a single quarter (e.g. 1 quarter at +20% and 3 quarters at -2%), the model is timing-lucky, not edge-real.

### 4.2 Re-calibrate factor decay using out-of-sample IC

Your current decay calibration uses the same 6 periods that compute IC for evaluation. That's in-sample. After Phase 3, you have one genuine OOS year (2024). The calibration becomes more honest if you fit decay on periods 1-5 and validate the per-factor lambda predictions on period 6.

This is a small change to `scripts/calibrate_factor_decay.py`:

```python
# Add a CLI flag:
ap.add_argument('--exclude-period', help='Exclude period containing this year '
                'string from calibration (for OOS validation).')

# In load_audit_factor_ics, filter results:
if args.exclude_period:
    results = [r for r in results if args.exclude_period not in r.get('label', '')]
```

Then run:
```bash
python scripts/calibrate_factor_decay.py --audit data/audit/audit_oos_2024_locked.json \
    --exclude-period 2024 --out data/factor_decay_5period.json
```

Compare the lambdas to the current 6-period ones. If they're materially different, the decay calibration was overfitting to 2024.

### 4.3 Investigate the HRP underperformance

HRP is currently *losing* to equal-weight on raw return (mean +6.56% vs +8.12%) while winning on drawdown (+20% vol reduction). This is consistent with López de Prado's paper — HRP improves *risk-adjusted* returns by diversifying away concentration in flyers — but you should understand what's happening in your specific data.

**Diagnostic:** Add a print of the HRP weight concentration vs equal-weight in each period:

```python
# Inside the HRP block around line 510, add:
top5_hrp_weight = w_aligned.nlargest(5).sum()
print(f'  HRP top-5 weight share: {top5_hrp_weight:.1%}  '
      f'(equal-weight would be {5.0/len(w_aligned):.1%})')
```

If HRP top-5 share is similar to equal-weight, HRP isn't doing much. If it's very different (e.g. HRP gives 50% to top 5 names while equal-weight gives ~3%), HRP is making big bets that don't pay off.

**Decision rule for production:** If HRP improves Calmar by >50% in 5/6 periods, use it for live sizing. Otherwise, use equal-weight on top decile. Right now, HRP improves Calmar in 4/6 periods. Keep it; it's right at the edge.

### 4.4 Validate sector neutrality is doing what you think

Your sector-neutral spread numbers (5.85%, 6.79%, 3.75%, 1.69%, 14.33%, 6.09%) are the most credible alpha numbers in the report. But they're computed by ranking *within* sector and taking deciles. With small sectors (utilities n=51, energy n=68), the "decile" within is 5-7 stocks — tiny. This makes the sector-neutral spread noisy.

**Fix:** Require minimum 30 stocks per sector for inclusion in sector-neutral; otherwise pool with related sectors.

In line 543, change:

```python
if len(sub) < 20:
    continue
```

to:

```python
if len(sub) < 30:
    # Skip undersized sectors from sector-neutral computation. Their
    # within-sector deciles would be <3 stocks and dominate noise.
    print(f'    Skipping {sg} from sector-neutral (n={len(sub)} < 30)')
    continue
```

---

## Phase 5 — Production hygiene (Week 6, ~6 hours)

Cosmetic but matter for credibility and re-running.

### 5.1 Clean up dead code

In `scripts/backtest_v2.py`, `compute_factor_ics` appears twice — once at line 83 (real) and once at line 136 inside the `compute_portfolio_calmar` function (dead code, unreachable because of the `return` on line 135).

**Fix:** Delete lines 136-148 (the orphaned duplicate).

### 5.2 Fix pandas warnings

The `PerformanceWarning: DataFrame is highly fragmented` at `market_screener.py:3485` is from inserting columns one at a time. Fix in `compute_potential_scores_v2` by collecting new columns into a dict and concat-assigning:

```python
# Instead of:
df['col1'] = ...
df['col2'] = ...
df['col3'] = ...

# Do:
new_cols = {'col1': ..., 'col2': ..., 'col3': ...}
df = pd.concat([df, pd.DataFrame(new_cols, index=df.index)], axis=1)
```

The `FutureWarning: The default fill_method='pad' in DataFrame.pct_change` at line 114 of `backtest_v2.py`:

```python
# Change:
monthly_ret = monthly_with_start.pct_change().dropna(how='all')
# To:
monthly_ret = monthly_with_start.pct_change(fill_method=None).dropna(how='all')
```

### 5.3 Add a backtest reproducibility manifest

After each backtest run, write a manifest of all inputs and configuration so the run can be reproduced. Add to the audit block (line 894 area):

```python
# Phase 5.3: reproducibility manifest
import hashlib
manifest = {
    'run_id': run_id,
    'timestamp': run_ts,
    'git_commit': os.popen('git rev-parse HEAD').read().strip(),
    'git_dirty': bool(os.popen('git status --porcelain').read().strip()),
    'POTENTIAL_WEIGHTS': dict(ms.POTENTIAL_WEIGHTS),
    'SECTOR_POTENTIAL_WEIGHTS_hash': hashlib.md5(
        str(sorted(ms.SECTOR_POTENTIAL_WEIGHTS.items())).encode()).hexdigest()[:8],
    'DECAY_ENABLED': ms.DECAY_ENABLED,
    'IMPACT_ENABLED': ms.IMPACT_ENABLED,
    'CROWDING_ENABLED': ms.CROWDING_ENABLED,
    'periods': [{'cutoff': p[0], 'forward': p[1]} for p in PERIODS],
    'oos_reserve': args.oos_reserve,
}
with open(audit_path / f'manifest_{run_id}.json', 'w') as f:
    json.dump(manifest, f, indent=2, default=str)
```

If `git_dirty` is True, you know the run isn't reproducible from a tagged commit. This will save you when you forget which version of the code produced which results.

### 5.4 Add a smoke test before each backtest

Catches the kind of bug Phase 1 found before you waste 30 minutes on a full backtest.

Create `scripts/smoke_test.py`:

```python
#!/usr/bin/env python3
"""Pre-backtest smoke test: validate the scoring pipeline on a tiny universe.
Fails loud if anything in the v2 path is broken."""
import os, sys
sys.path.insert(0, 'src')
os.environ.setdefault('DISABLE_FINVIZ', '1')
import market_screener as ms
import numpy as np

# 1. Module imports
print('[1/6] Module imports... OK')

# 2. Config loads
assert ms.POTENTIAL_WEIGHTS, 'POTENTIAL_WEIGHTS empty'
assert abs(sum(ms.POTENTIAL_WEIGHTS.values()) - 1.0) < 1e-6
print(f'[2/6] POTENTIAL_WEIGHTS = {ms.POTENTIAL_WEIGHTS}  OK')

# 3. Sector config sums to 1
for sec, w in ms.SECTOR_POTENTIAL_WEIGHTS.items():
    s = sum(w.values())
    assert abs(s - 1.0) < 1e-6, f'SECTOR_POTENTIAL_WEIGHTS[{sec}] sums to {s}'
print(f'[3/6] All {len(ms.SECTOR_POTENTIAL_WEIGHTS)} sector configs valid  OK')

# 4. Regime classifier returns valid output
label, probs = ms.classify_regime(ism=50, curve_10y2y=0, core_cpi_yoy=0.03, hy_oas=400)
assert label in ('R1', 'R2', 'R3', 'R4', 'R5'), f'bad regime: {label}'
assert abs(sum(probs.values()) - 1.0) < 1e-6, f'probs sum: {sum(probs.values())}'
print(f'[4/6] Regime classifier: {label}, probs sum 1.0  OK')

# 5. Decay file is loadable if present
decay = ms.load_factor_decay()
if decay:
    print(f'[5/6] factor_decay.json: {len(decay.get("factors", {}))} factors  OK')
else:
    print('[5/6] factor_decay.json missing (will use base weights)  OK')

# 6. Hyperbolic decay math is sane
K, lam, r2 = ms.fit_hyperbolic_decay([0.20, 0.15, 0.10, 0.08, 0.06])
assert lam > 0, f'expected positive lambda for decaying series, got {lam}'
assert 0 < K < 1, f'K out of bounds: {K}'
print(f'[6/6] fit_hyperbolic_decay: K={K:.3f}, lambda={lam:.3f}, R²={r2:.3f}  OK')

print('\nSmoke test passed. Safe to run backtest.')
```

Run it before every backtest:
```bash
python scripts/smoke_test.py && python scripts/backtest_v2.py --acknowledge-bias
```

---

## Phase 6 — Optional improvements (someday, low priority)

These are real Framework items, but their marginal value is small until Phase 1-5 are complete. Listed in priority order.

1. **Crowding flag (Stage 2.6).** You have the script; the calibration is straightforward. But your top decile is currently ~50% industrials + consumer_disc, neither of which are heavily ETF-replicated. Probably won't move alpha by more than 50bps.

2. **Mutual information for factor coupling.** Your healthcare V↔Q nMI=+0.70 finding was the foundation of the healthcare reweight. Extend the analysis to all sectors and document the MI matrix per period. Adds defensibility but probably not alpha.

3. **Multiple-testing correction.** White's reality check or Hansen's SPA. Your t-stat proxy (IC/sqrt(N)) is the wrong test for the data structure. A bootstrap-based test would be more honest. ~4 hours.

4. **Capacity-aware position sizing.** Your `n_capped` field tells you which top-decile names you couldn't realistically own at scale. A real portfolio constructor would cap position sizes at 10% of ADV and redistribute. ~6 hours.

5. **Alternative-data signals.** Insider buying ratio, short interest change, ETF flow data. Only after Phase 4 stabilizes and OOS is consistent. These are real signals but they're competing for the same alpha budget and the risk of overfitting goes up sharply.

---

## Summary checklist

Phase 1 (Week 1 — critical bug fixes):
- [ ] 1.1 Fix sensitivity baseline (compute_sensitivity rewrite)
- [ ] 1.2 Lower growth min_coverage to 0.25
- [ ] 1.3 Diagnose and patch period label bug
- [ ] 1.4 Add energy-sector representation diagnostic

Phase 2 (Week 2 — honest reporting):
- [ ] 2.1 Make `top-alpha` the headline metric
- [ ] 2.2 Add delisted-ticker proxy
- [ ] 2.3 Add square-root market impact to costs

Phase 3 (Week 3 — locked OOS test):
- [ ] 3.1 Tag commit, freeze config, run OOS 2024
- [ ] 3.2 Write down decision rules BEFORE seeing results
- [ ] 3.3 Don't re-tune on OOS failure

Phase 4 (Week 4-5 — methodology, only if OOS passes):
- [ ] 4.1 Quarterly subperiod robustness
- [ ] 4.2 OOS-aware decay calibration
- [ ] 4.3 HRP weight concentration diagnostic
- [ ] 4.4 Sector-neutral minimum size = 30

Phase 5 (Week 6 — hygiene):
- [ ] 5.1 Delete dead code in backtest_v2.py:136-148
- [ ] 5.2 Fix pandas warnings
- [ ] 5.3 Reproducibility manifest
- [ ] 5.4 Smoke test script

## Grade trajectory

| Milestone | Grade | What it would mean |
|---|---|---|
| Now | 65 | Real signal, overstated headline, several bugs |
| After Phase 1+2 | 70-72 | Honest reporting, bugs fixed, smaller-but-real headline number |
| After Phase 3 (OOS pass) | 78-82 | Genuine out-of-sample validation; you know what you have |
| After Phase 4 | 83-87 | Methodology rigor matches a mid-tier institutional quant pipeline |
| After Phase 5 | 87-90 | Production-ready for retail deployment |

Above 90 requires things that aren't worth doing for a one-person retail project: PIT data from CRSP/Norgate (~$5k/year), proper risk-model integration (Barra-style), tick-level execution simulation. Don't chase 90+; chase honest 80.

## What this plan is *not* doing

- **Not adding more factors.** The model already has V/Q/G/S. Adding momentum sub-signals, alt-data, etc. before Phase 3 OOS would be premature.
- **Not changing the regime classifier.** It's working (R2/R3 calls match the 2021-2024 reality).
- **Not chasing the +19% headline.** That number isn't real.
- **Not implementing the crowding flag.** Stage 2.6 is in the codebase but inactive; leave it until Phase 6.
- **Not migrating to point-in-time index membership data.** That's a $5k/year subscription decision, not a code patch.

## One closing note

The most important thing on this list, by far, is **Phase 3: lock the config and run 2024 OOS without peeking**. Everything else is supporting infrastructure. If Phase 3 OOS shows +5% top-alpha you have a real edge that's worth $5-50k/year of effort to maintain. If it shows -2% you know to stop spending time on this model and start over with a different approach. That's the experiment that earns the right to do everything else.
