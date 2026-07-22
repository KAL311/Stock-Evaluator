# Post-DQ-v3 Verification — pre-registered gates

**Purpose.** Commit `3d70654` ("DQ v3: propagate share-xcheck to scorer")
changes scorer *inputs* (masks `market_cap`/`enterprise_value` for
xcheck-failed rows so cap-derived yields go NaN instead of silently
inflating valuation). The 2026-07-19 go-live GO verdict describes commit
`8fb175e`, which predates this change. This checklist verifies the build
actually running is the build that was declared GO.

**Gates are written before the run.** PASS/FAIL criteria below are fixed
now; the run result does not get to renegotiate them.

**Automation.** `scripts/verify_dq_v3.py` executes Gates A–G against
`data/stock_cache.db` and exits non-zero on any hard-gate failure. The
manual steps here are the wrapper around it.

---

## Step 0 — Preconditions (before running anything)

- [ ] `git rev-parse --short HEAD` → `3d70654`
- [ ] `git status --porcelain` → empty (no uncommitted scorer edits)
- [ ] `CACHE_SCHEMA_VERSION` in `src/market_screener.py` = 14
- [ ] Confirm expected reference values on hand (from go-live doc):
      universe 2514, coverage ≈ 95.6–95.7%, dq_nulled_shares ≈ 283,
      dq_nulled_derived ≈ 150 (shares_xcheck ≈ 96), prior top-10 in
      `reports/.last_top10.json` (contains **MKC** — the name this fix
      targets).

## Step 1 — Force a fresh rebuild through the full pipeline

The cache must not be allowed to serve the pre-v3 snapshot.

```
python scripts/verify_dq_v3.py --clear-cache
python scripts/run_daily.py
```

(`--clear-cache` deletes only `cache_meta` rows, which is exactly what the
go-live run did; the stocks table rebuilds on next screener invocation.
Do not hand-type SQL with redirects in the shell — that is how the root
junk files were born.)

- [ ] `run_daily.py` exits 0
- [ ] Startup line shows `intended source: FMP`
- [ ] Fresh `HEALTH_JSON` emitted with `cache_updated_at` = today

## Step 2 — Automated gates

```
python scripts/verify_dq_v3.py
```

| Gate | Assertion | Type |
|---|---|---|
| **A — DQ v3 core** | Every row with `dq_share_xcheck_failed = 1` has `valuation_score` NULL. Zero exceptions. | **HARD** |
| **B — MKC-class exit** | MKC's `valuation_score` is NULL (was 99.52). Any xcheck-flagged ticker still in the potential top decile is listed for manual review (allowed only if Q/G/S carry it — expected rare). | **HARD** (MKC), report (others) |
| **C — Healthy anchors unmoved** | AAPL/MSFT/KO pe, pe_ttm, dividend_yield within loose sanity bands (the "non-xcheck rows bit-identical" claim, spot-checked). | **HARD** |
| **D — DQ volume stable** | Count of xcheck-flagged rows within 2x of the ~96 reference; total universe 2514 ± 50. A large jump means the guard's blast radius changed, not just propagated. | **HARD** |
| **E — Top-10 turnover** | Turnover vs `reports/.last_top10.json` ≤ 5 names. Entrants/leavers printed. MKC leaving is the expected, desired outcome — its presence in the new top-10 with a non-NULL valuation_score is an automatic FAIL. | **HARD** |
| **F — No implausible valuations** | max \|pe_ttm\| ≤ 1000, dividend_yield ≤ 50%, no NaN/Infinity leakage into numeric columns read back from the DB. | **HARD** |
| **G — Staples distribution** | consumer_staples potential_score: n ≥ 50, mean in [40, 60], std in [5, 20] (go-live reference: n=68, mean=51.0, std=10.5). MKC's new staples rank printed. | **SOFT** (warn) |

- [ ] All HARD gates PASS
- [ ] Gate G within band, or deviation explained in one sentence below

## Step 3 — Health line & dashboard

- [ ] Fresh `HEALTH_JSON`: universe arithmetic reconciles
      (live + delisted + unpriced + unknown = universe)
- [ ] `coverage_pct ≥ 90`
- [ ] No new WARN classes beyond the known `PRICE_FETCH_DEGRADED`
- [ ] `reports/latest.html` written; spot-open: banner absent/OK,
      Screener tab populated, MKC detail shows NULLed P/E ratios

## Step 4 — Record the verdict

- [ ] Append result to this doc under "Realized result" (below):
      gate table outcomes, new top-10, MKC before/after
      (potential, valuation_score), health line verbatim
- [ ] `git add docs/post_dq_v3_verification.md && git commit`

## Step 5 — Close out gaps #1 and #3 (sequenced here deliberately)

Only after Step 4 passes — so what gets pushed and scheduled is the
verified build:

- [ ] `git push origin master` (clears the 24-commit backlog)
- [ ] Reconcile the stale remote tag:
      `git push origin :refs/tags/phase5-frozen`
      then `git push origin phase5-frozen` (local tag → `0a5343a`)
- [ ] `git push origin phase3-frozen` if not already on origin
- [ ] Create the Task Scheduler entry per `docs/run_daily.md`
      (post-close daily trigger → `python scripts/run_daily.py`)
- [ ] Build the golden master while the verified state is current:
      `python tests/build_golden_master.py` then commit
      `tests/fixtures/` (see `tests/README.md`)

## Failure protocol

Any HARD gate failure → do **not** push, do **not** schedule. The scorer
mask is ~15 lines in two places (`compute_potential_scores` ~1813,
`compute_potential_scores_v2` ~3979); diff those against the intent in
the commit message, fix, re-run from Step 1. The failure and fix get
their own commit and a one-line addendum here.

---

## Realized result

**2026-07-22 — first run (commit `3d70654`): Gate A FAIL.**
19 xcheck-flagged rows with NI/FCF≤0 leaked `valuation_score=0.0` via the
force-zero override of the cap mask (`ey_fz`/`fy_fz` test the numerator
only, `_rank_within` force_zero overrides NaN→0.0). Deflationary (0.0 =
worst, not inflated), so no MKC-class inflation risk — but violates the
zero-exceptions gate. Initial agent diagnosis ("unpatched v2 path") was
wrong and disproven: the mask exists in both paths and production calls v1
only (`main()` line 4589). Fixed in **DQ v3.1** (`b18be20`) by ANDing all
three fz flags with `~_xcheck_mask` in both `compute_potential_scores`
(~1823/1827/1837) and `compute_potential_scores_v2` (~3993/3995/3999).

**2026-07-22 — re-run (commit `b18be20`): VERDICT PASS (0 warns).**

| Gate | Outcome |
|---|---|
| A — DQ v3 core | **PASS** — 96 xcheck rows, 0 with non-NULL valuation_score |
| B — MKC-class exit | **PASS** — MKC xcheck=1, valuation_score=NULL, potential=NULL |
| C — Healthy anchors | **PASS** — AAPL pe 28.16/dy 0.0049; MSFT 36.74/0.0064; KO 22.94/0.0291 |
| D — DQ volume stable | **PASS** — universe 2514; xcheck 96 (ceiling 192) |
| E — Top-10 turnover | **PASS** — turnover 1 (out YOU, in AAWW); MKC exited |
| F — No implausible vals | **PASS** — no \|pe_ttm\|>1000, no dy>50% |
| G — Staples dist (soft) | **PASS** — n=66 mean=51.00 std=10.10 (ref n=68/51.0/10.5) |

New top-10: `RIGL, CPRX, LRN, AAWW, PPC, APP, NUTX, IDCC, OTTR, JFIN`
(review-only, Q/G/S-carried xcheck names in top decile: BROS, STLA, SNDL —
all valuation_score NULL per Gate A, allowed).

**MKC before → after:** valuation_score 99.52 → NULL; potential (top-10) →
NULL (exited top-10). This fix's target outcome, realized.

Health line (verbatim):
```
HEALTH_JSON={"source": "FMP", "universe": 2514, "live": 2003, "delisted": 423, "unpriced": 88, "unknown": 0, "coverage_pct": 95.8, "non_usd_fallback": 19, "mixed_source": 16, "dq_nulled_derived": 149, "dq_nulled_shares": 283, "regime": "R3", "cache_updated_at": "2026-07-22T14:08:57"}
```
Universe arithmetic: 2003 + 423 + 88 + 0 = 2514 ✓. coverage 95.8% ≥ 90 ✓.
Only known WARN class present (`PRICE_FETCH_DEGRADED`). `reports/latest.html`
written (2026-07-22 14:09).

Addendum: 2026-07-22 run: Gate A FAIL — 19 xcheck rows with NI/FCF≤0 leaked
valuation_score=0.0 via force-zero override of the cap mask (deflationary,
no inflation risk). Agent misdiagnosed as unpatched v2 path; disproven —
production uses v1 only, both paths carried the mask. Fixed in DQ v3.1 by
ANDing fz flags with ~xcheck. Re-run: PASS (all HARD gates, 0 warns).
