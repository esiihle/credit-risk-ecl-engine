# Progress Log: Credit Risk ECL Engine

> **Purpose.** A living status tracker, updated at the end of each working
> session. Read this plus `docs/ROADMAP.md` and you know exactly where the
> project stands. Newest entry at the top.

**Current phase:** Phase 7 complete. Next: Phase 8, the SAS implementation
and the reconciliation harness.
**Overall status:** 🟢 Mart, roll rates, staging, a PD term structure 9x more
accurate per account than a pooled curve, LGD from resolved workouts, exposure
exact to the cent, **per account ECL reconciling to the answer key at
1.8e-11**, and 48 vintage cohorts with the MI to read them. **137 tests
green.**
**Cadence:** one commit per working day.

---

## Phase checklist
- [x] Phase 0: Scaffolding, config, synthetic book + answer key, warehouse, CI
- [x] Phase 1: Data mart
- [x] Phase 2: Delinquency and roll rates
- [x] Phase 3: IFRS 9 staging
- [x] Phase 4: PD term structure
- [x] Phase 5: LGD and EAD
- [x] Phase 6: ECL calculation
- [x] Phase 7: Vintage and portfolio MI
- [ ] Phase 8: SAS implementation and reconciliation
- [ ] Phase 9: Documentation and study guide

## Open decisions
- [x] **Product:** unsecured personal loans, fixed term, monthly instalments,
      no collateral, so LGD stays simple and the IFRS 9 mechanics stay in focus.
      Settled 23 Oct 2026.
- [x] **Data:** synthetic, with a known analytic ground truth. Settled 23 Oct.
- [x] **Engine:** DuckDB as the executable reference, SAS `PROC SQL` as the
      parallel implementation, reconciled in Phase 8. Settled 23 Oct.
- [ ] **Scorecard PD integration**, whether Phase 4 consumes the scorecard's
      exported PD directly or reimplements the scaling. Settle before Phase 4.

## How to run
```bash
pip install -r requirements.txt
pytest                                    # 137 tests
python scripts/run_pipeline.py --sample   # the committed sample book
python scripts/run_pipeline.py --status   # the build plan
python scripts/generate_book.py           # full book, ~12s
python scripts/generate_book.py --sample  # rebuild the committed sample
python scripts/generate_book.py --summary # describe the warehouse
```

---

## Session log

### 2026-11-08, Phase 7: vintage curves, and MI that invents a trend

First phase whose gates check for FALSE POSITIVES. Every earlier one asked
whether an estimate was accurate enough; this one asks whether the MI reports
something the book does not contain. An MI pack that manufactures a trend costs
a credit committee a policy change it did not need, more expensive than a
provision a few percent out.

The check is unusually sharp because the truth is exact: the generator draws
segment and origination month **independently**, so every cohort is the same
book and the true vintage effect is zero.

#### The result: the naive comparison invents 4pp a year
| | trend in 12-month default rate | correlation |
|---|---|---|
| naive, each cohort at its own latest age | **-4.03pp / year** | -0.853 |
| fair, every cohort at 12 months | **-0.09pp / year** | -0.099 |

**97.8% of the apparent improvement is artefact.** Four straight years of
apparent credit improvement at r=-0.85, the kind of number that gets a lending
policy loosened, against a flat truth. The 2021-04 cohort's naive rate sits
18.48pp above its 12-month rate, not because it lent badly but because it has
been watched 44 months longer.

Third appearance of one mechanism:

| phase | measure | bias comes from |
|---|---|---|
| 2 | roll rates | the **end** of an account's series |
| 5 | LGD | the **beginning** of a workout |
| 7 | vintage curves | the **end** of the observation window |

Every time the naive denominator included outcomes that had not happened yet,
and every time the direction was predictable from the mechanism before
measuring.

#### Monthly cohorts could not have seen a real effect anyway
Spread between cohorts: observed SD 0.01028 against 0.01084 expected from
binomial sampling alone, ratio **0.948**, chi-square 42.50 on 47 df,
**p = 0.659**. Nothing there, correctly.

Smallest difference detectable at 80% power:

| grouping | minimum detectable |
|---|---|
| monthly (~420 accounts) | **4.29pp** |
| quarterly | 2.48pp |
| annual | 1.24pp |

Against a 5.16% base rate, a monthly triangle cannot see anything smaller than
**83% of the base rate itself**. It is a picture of sampling noise with a trend
line through it. The right response to a noisy monthly chart is to group the
cohorts, not to explain the wiggles, which is the opposite of what the chart
invites.

#### Calendar time shows deterioration that is only maturity
Over 54 settled months: average age 3.0 -> 28.3 months, arrears 7.56% ->
15.00%, correlation +0.850 (Stage 2 share, +0.902). Arrears doubled; nothing
about lending changed.

The age-period-cohort problem in its simplest form: age + vintage = period
exactly, so the three effects are not separately identifiable without an
outside assumption. Here two are zero by construction, which is the only reason
the decomposition can be checked at all. On a real book it cannot be, and the
honest line to a committee is that calendar movement has at least two
explanations and the data will not choose between them.

#### The first charts in the repo
`scripts/make_charts.py` regenerates `docs/charts/` from the warehouse; each
image stamps its reporting date, book size and the command that produced it.
Two new tests keep the rule enforceable: every image a document references must
exist, and nothing may sit in `docs/charts/` that the script does not produce,
a hand-exported image looks identical to a generated one and is exactly the
figure that goes stale.

Three layout defects fixed in the making, all the same kind, something drawn
where nothing knew it was:
- **`fig.text` ignores constrained layout.** The title band drew straight
  through the axes: subtitle across the plot area, stamp through the x-axis
  label. Bands are now reserved via the layout engine's `rect`, sized from how
  many lines the subtitle wraps to.
- **A 100% stacked stage mix spent the panel on the part that does not move.**
  Stage 1 is 92-97% throughout, leaving Stages 2 and 3 a pixel high. Replaced
  with just the two stages under scrutiny on a scale that fits them.
- **The staging-cliff bars started at 0.9**, so a 1.00x bar looked substantial.
  Drawn from a baseline of 1.0 now, so bar length is the jump itself.

#### Next
Phase 8, the SAS `PROC SQL` implementation and the reconciliation harness: the
same ECL computed twice, in two languages, tied out to the cent.

---

### 2026-11-06, Phase 6: the provision, and an answer key with nothing behind its defaults

ECL is `PD x LGD x EAD`, discounted, everything the previous five phases built,
multiplied together. The arithmetic took an afternoon. Two of the three things
found along the way were in work already committed.

#### The result
| stage | accounts | exposure | ECL | coverage | true | error |
|---|---|---|---|---|---|---|
| 1, 12-month | 7,625 | 126,289,603 | 2,821,097 | 2.23% | 2.30% | -2.99% |
| 2, lifetime | 570 | 9,572,408 | 2,404,604 | 25.12% | 25.30% | -0.73% |
| 3, impaired | 61 | 939,053 | 611,250 | 65.09% | 68.24% | -4.62% |
| **portfolio** | **8,256** | **136,801,064** | **5,836,951** | **4.27%** | **4.36%** | **-2.25%** |

#### The bug: the key had no provision against its own defaulted book
The key recorded **all 61 credit-impaired accounts at a lifetime PD of 1.00 and
an ECL of exactly 0.00**, R939k of defaulted exposure, nothing behind it.

One line. ECL was built from `np.diff(curve)`, the probability of defaulting in
each **future** month. Default is absorbing, so for an account already in
bucket 4 the curve is 1.0 at t=0 and stays there, every forward increment is
zero, and the sum is zero. The formula was right about what it computes and was
being asked a question it does not answer: an impaired account's default is not
a future event to weight by its probability, it has happened, and the loss is
`LGD x EAD`.

**Second time the key has been valid for only part of the book**, the first was
the same function computing every account's PD as though it were performing
(31 Oct). Same shape both times: a formula correct for the majority case applied
to a population it did not describe, and both times the invalid part was where
the numbers are largest. Had the engine been built to agree with the key, it
would have shipped provisioning nothing against its defaults.

#### One tolerance was answering two questions, and neither well
`max_ecl_relative_error: 0.02`, set in Phase 0. The engine missed it at -2.25%
with arithmetic reconciling to 1.8e-11 and both inputs inside their own gates.

The threshold could not be met by correct code. ECL is a product of three
estimates, so its relative error is roughly the **sum** of theirs:

| input | its gate | implied relative |
|---|---|---|
| PD 12-month | 1pp absolute on ~9.2% | ≈ 11% |
| PD lifetime | 15% relative | 15% |
| LGD | 2pp absolute on ~65% | ≈ 3% |
| EAD | exact | 0% |

So a 2% ECL gate was never a gate on Phase 6; it was a **7.5x stricter implicit
gate on Phase 4's PD**. And it was simultaneously loose enough that a sum formed
*wrongly*, cumulative PD instead of marginal, wrong horizon, no discounting,
could pass on a short book.

Replaced with two gates for two questions:
1. **Is the arithmetic right?** Feed the engine's own expression the true
   marginal PD, LGD, exposure and discounts from a new `ground_truth_monthly`
   table and demand the key back to the cent. **1.8e-11 across 8,256 accounts**,
   exactly 0.00 for the impaired ones. This is what guards Phase 6.
2. **Are the inputs good?** A 15% backstop at the level the input gates imply.

A test asserts the first is stricter than the second, so the restructuring
cannot be quietly undone into a mere loosening. The honest summary is that an
exact check replaced an approximate one that could not discriminate, not that
a limit was relaxed because it was inconvenient.

#### The staging cliff is steepest for the accounts least likely to cross it
Both horizons are computed for every account regardless of stage, so the SICR
jump is measurable: same accounts, same month, counted two ways.

| arrears bucket | accounts | lifetime ÷ 12-month |
|---|---|---|
| 0 (current) | 6,957 | **1.633** |
| 1 (1 to 29 DPD) | 853 | 1.198 |
| 2 (30 to 59 DPD) | 258 | 1.030 |
| 3 (60 to 89 DPD) | 127 | **1.004** |

An account 60 to 89 days down will mostly default inside twelve months either way,
so lifetime adds 0.4%. Accounts tripping the 30-day backstop face a 1.019x jump;
those in probation after curing, 1.293x. The large discontinuity is reserved for
accounts crossing via the PD trigger while still current, the trigger Phase 3
measured as barely firing. **The cliff is real and, on this book, mostly
hypothetical.** That complicates the standard criticism of IFRS 9 rather than
confirming it, which is a better thing to be able to say in an interview.

#### The residual 2.25% is PD, not LGD: measured
| | portfolio ECL | vs engine |
|---|---|---|
| estimated PD, estimated LGD | 5,225,701 |, |
| estimated PD, **true** LGD | 5,212,348 | -0.26% |
| true PD, true LGD (the key) | 5,330,202 | +2.00% |

A single segment-level LGD applied to every account looks like the crude part.
It is not: swapping in the truth moves the total by a quarter of a percent. The
gap is PD error, mostly the cost of five discrete age bands standing in for a
continuous seasoning hazard. The engine **under**-provisions, which is the
dangerous direction for a loss allowance, so the sign is reported rather than
folded into an absolute figure. Next improvement is a finer PD age structure,
not a finer LGD.

#### Assessability has to propagate: found by rehearsing CI, not by CI
The full book passes everything. The **committed 400-account sample** failed the
ECL accuracy backstop at **-15.4%**, while on the same run both PD gates and
the absolute LGD gate printed `--`, *not assessed*. The engine was delivering a
verdict on a figure built from estimates it had just said it had no evidence
about.

Fixed with a rule rather than another threshold: **if the inputs to an estimate
could not be assessed, the estimate's accuracy cannot be assessed either.** The
arithmetic gate is untouched and still applies, it substitutes exact inputs, so
it reconciles at 1.8e-12 on the 162-account sample too, and silencing it would
leave the sample run proving nothing.

Fifth appearance of the same error, first time with a general form:

| # | where | reported as | actually was |
|---|---|---|---|
| 1 | `git check-ignore` exit 128 | file not ignored | ran outside a work tree |
| 2 | scorecard optimiser `success: True` | converged | didn't converge |
| 3 | PD gate on the sample | inaccurate | 60% fallback cells |
| 4 | absolute LGD gate on the sample | inaccurate | SE 4× the tolerance |
| 5 | ECL backstop on the sample | inaccurate | inputs unassessable |

Instances 3, 4 and 5 are one mechanism a layer apart, which is why this one was
written as propagation rather than a third bespoke threshold.

#### Two test-design errors, both failing on correct code
- **"Lifetime ECL must exceed 12-month ECL beyond 12 months."** False at exactly
  13 months remaining: the 13th month *is* maturity, where the amortised balance
  is zero, so no loss can arise in it. 179 accounts, correct arithmetic,
  reported as a missing horizon.
- **The same assertion, broadened, swept in Stage 3**, whose two horizons are
  equal *by design*, as another test in the same file asserts.

Cheap direction to be wrong in, but both were checks measuring something other
than what they claimed, which is the error this project keeps finding.

#### Next
Phase 7, vintage curves and portfolio MI: origination-cohort loss curves,
coverage ratios over time, and the stage-migration matrix as management
information rather than a diagnostic.

---

### 2026-11-05, Phase 5: LGD, EAD, and CI that was passing by luck

Delivered with the Phase 4 CI fix folded in, because the fix and the phase
turned out to be the same problem twice.

#### The CI failure
`pytest` passed; `run_pipeline.py --sample` exited 1. Phase 4's PD accuracy gate
failed on the 400-account sample, 12-month error 0.0282 against a 0.010 limit,
and the "best" method came out as `segmented` rather than `age_aware`.

Not a bug in the engine. **60.4% of the age-aware matrix cells had fallen back**
to the segment's pooled rate (thinnest cell: one transition) against 1.8% on the
full book. The age-aware model was never estimated; the gate was measuring a
pooled model wearing the age-aware name and reporting it as an accuracy failure.

`max_fallback_share_for_pd_assessment: 0.25` now decides, from the fallback
share alone, whether the gate can be applied at all. Above it the gate reports
`not assessed` rather than `fail`, and the pipeline continues.

#### Phase 5, and the asymmetry it was designed around
| | EAD | LGD |
|---|---|---|
| nature | contractual arithmetic | statistical estimate |
| validated | to the cent, 158,696 account-months | 2pp, and within 3 SEs |

Choosing an unsecured **term** product back in Phase 0 is what made EAD exact:
the borrower cannot increase their exposure. On a card or an overdraft EAD
becomes a second model with its own data problem, and two uncertain inputs
would obscure which one was driving the error.

#### The result: the workout censoring trap
| | defaults | mean loss |
|---|---|---|
| resolved workouts | 2,288 | **65.10%** |
| unresolved workouts | 959 | 77.05% |
| all defaults (naive) | 3,247 | 68.63% |
| **truth** | | **65.1%** |

Resolved-only lands **0.19pp** from the truth, at 0.37 standard errors. The
naive estimate overstates by **3.53pp**; the unresolved cases alone sit ~12
points high. An account that defaulted last month has barely begun collections,
so its loss-to-date is not its LGD, it is its LGD plus whatever is still to
come.

Same shape as the Phase 2 roll-rate trap, different mechanism: there the bias
came from the end of an account's series, here from the beginning of a workout.
The suite asserts the bias **still exists** as firmly as it asserts the estimate
is accurate, because an engine that stopped reproducing a known bias would be
demonstrating nothing.

#### The same error, a fourth time
The sample passed the absolute LGD gate at **0.0189 against 0.0200**. That pass
was worth nothing. Across six seeds at 400 accounts:

| seed | 42 | 101 | 7 | 2024 | 555 | 88 |
|---|---|---|---|---|---|---|
| absolute gate | pass | fail | fail | fail | fail | fail |
| error in SEs | 0.43 | 0.74 | 2.09 | 2.18 | 0.98 | 1.36 |

One pass in six, and the standard-error gate passed on all six. With ~10
resolved defaults per segment the standard error is near 4pp, so a 2pp limit is
measuring luck. **CI was green by seed and one regeneration away from red with
nothing broken.**

The absolute gate now applies only when the worst segment standard error is at
most half the tolerance. Two tests keep that honest: one reads `validate()`'s
own source to assert the precision test is computed **before** the error exists
(it failed on the first version, where the block sat underneath), and one
asserts that a 14,000-account book **is** judged, an exemption that applied at
every size would be a gate that never fires.

> A check that cannot distinguish "the answer is no" from "I could not
> determine the answer" will eventually report the second as the first.

Fourth instance: `git check-ignore` exiting 128 (Phase 0), the Phase 4 fallback
gate, the scorecard optimiser's `success: True`, and now this.

#### Three smaller defects, all controls rather than features
- **The loader bound every parameter to every SQL file.** Phase 5 runs two files
  and only one takes an argument. `run_sql_file` now binds only what a file
  references, matched outside comments, these files carry a lot of prose, and a
  `$name` in commentary must not be bound as though used.
- **The stage-status table could contradict the run.** Keyed by name, it said
  `"LGD & EAD"` against `STAGES`' `"LGD and EAD"`, which would have printed
  `NOT YET BUILT` for a stage that had just passed. Keyed by number now.
- **A missing table and an empty one are different failures.** The recovery
  loader skipped the write when there were no defaults, leaving the table absent
  rather than empty, and the SQL died with a catalog error instead of returning
  zero. Always created now, via a typed empty frame.

#### The documentation was stale, and nothing was checking it
`docs/ROADMAP.md` still listed Phase 1 as `next`. It had been wrong for four
phases, because the README carried the real state and nobody re-read the
roadmap. Worse: the fix I first wrote claimed "the README's phase table and test
count are checked by the test suite", and **no test checked either**.

`tests/test_docs.py` makes the claim true: the README's test count must equal
the real one, the README and roadmap must agree on which phases are done, the
done phases must be contiguous, and every done phase must have a methodology
section. No numeric literals from the documents appear in it, the sister
project's version of this file hardcoded the numbers it was meant to verify, so
when the README was wrong the test **defended** the error.

#### Next
Phase 6, ECL: `PD × LGD × EAD`, discounted at the effective interest rate,
reconciled to the answer key's `true_ecl_lifetime` by stage and by segment.

---

### 2026-10-31, Phase 4: the PD term structure, and a broken answer key
Three SQL files, a validation module, and the first phase that had to prove a
refinement was *worth making* rather than merely correct.

- **`sql/11_segment_age_matrix.sql`**, transition matrices by segment AND age
  band jointly, with a thin-cell fallback.
- **`sql/12_pd_term_structure.sql`**, the age-aware recursive walk. The matrix
  **changes as the walk advances**, because an account 10 months old today
  faces the 12-23 month hazard a year from now. A matrix power cannot express
  that.
- **`sql/13_account_pd.sql`**, 12-month and lifetime PD per account, by all
  three methods side by side.
- **`src/pd.py`**, accuracy against the key, error decomposition, validation.

#### The result
12-month PD, 8,256 accounts:

| method | mean est | truth | bias | mean abs err | corr |
|---|---|---|---|---|---|
| pooled | 8.81% | 7.79% | +13.1% | 0.0359 | 0.944 |
| segmented | 7.09% | 7.79% | -9.0% | 0.0165 | 0.987 |
| **age_aware** | **7.75%** | 7.79% | **-0.5%** | **0.0039** | **0.999** |

**9.2x more accurate per account than pooled** (9.5x on lifetime).

#### Why an aggregate check would have missed it
Pooled errors by grade: prime **+3.61pp**, near-prime +1.06pp, subprime
**-8.14pp**. Opposite signs, so they partly cancel: the aggregate bias is
+1.0pp while the per-account error is 3.6pp. **A provision is built account by
account, not from the headline.**

By age the error tracks the seasoning curve rather than scattering around zero
a method blind to age must overstate where the hazard is low and understate
where it peaks.

#### The bug: the answer key was only valid for performing accounts
All three methods came out ~2x the truth. The engine was right; the key was
wrong. `ground_truth` computed every account's PD as though it were performing
so an account 60-89 days down appeared at 5.4% against a real 84%.
Restricted to genuinely current accounts, age_aware gave 3.611% against a true
3.648%.

**An answer key valid for only part of the book is worse than none, because the
part it is invalid for is exactly where the numbers are largest.** It now uses
each account's actual bucket at the reporting date.

#### A threshold that was mis-specified: changed, on the record
The lifetime check failed at 0.0131 against the absolute 0.010 on an 8k book
while passing at 0.0076 on 20k. Measured across sizes, lifetime absolute error
runs ~2x the 12-month error every time, while relative errors are comparable,
because a 60-month cumulative is the accumulation of 60 estimates, so its scale
grows with the horizon.

The lifetime limit is now **relative** (15%); the 12-month absolute limit is
untouched. My rule on this project is that a limit which moves to accommodate a
result was never a limit, so both halves are recorded: **the form changed for a
structural reason, and it changed after a result was seen.** The reason sits in
`config.yaml` next to the number.

#### Next
Phase 5, LGD and EAD. Exposure is already exact arithmetic; loss given default
is the one genuinely uncertain input, and the one with the least data behind it.


### 2026-10-29, Phase 3: IFRS 9 staging, and a trigger that barely fires
The accounting judgement phase. Three SQL files and the finding that staging,
not the PD model, is what moves a provision.

- **`sql/08_pd_curve.sql`**, matrix exponentiation as a **recursive CTE**. The
  state vector carried as five columns, multiplied by the Phase 2 matrix each
  step; the mass in bucket 4 after k steps is P(default within k months).
- **`sql/09_staging.sql`**, Stage 3 at 90+ DPD; Stage 2 on the 30-day
  backstop, the relative PD test, or probation. Every account carries a
  `stage_reason`.
- **`sql/10_stage_migration.sql`**, month-on-month stage movement, account- and
  balance-weighted.
- **`src/staging.py`**, orchestration, validation, and the trigger analysis.

Book at the reporting date: **92.3% Stage 1 / 7.0% Stage 2 / 0.7% Stage 3** by
exposure. All four staging checks pass.

#### Two traps in the recursive CTE, both silent
**DECIMAL truncation.** `1.0 AS p0` is `DECIMAL(2,1)`, one decimal place, and
the type propagates through every iteration. The state vector froze at
(0.9, 0.1) forever and produced perfectly valid-looking probabilities that were
completely wrong. No error. Every literal in the anchor now carries `::DOUBLE`,
and a regression test asserts the untyped version really does freeze, so the
casts cannot be tidied away as noise.

**The absorbing row is absent from the data.** Nothing is ever *observed*
leaving default, so pivoting `roll_rate` gives NULL for the whole bucket-4 row
including its diagonal. Coalescing to zero makes probability mass vanish and the
PD curve plateau, which looks reasonable. The identity row is a modelling fact,
not a data fact, and has to be asserted.

#### The finding: the relative PD test is nearly redundant
Counting account-months where each trigger was *the* reason:

| trigger | account-months |
|---|---|
| 30-day backstop | 23,737 |
| relative PD test | **1,132** |

And **every one of the 1,132 is a 12-month loan**, none on 24, 36, 48 or 60.

Structural: a relative test needs a small denominator, and lifetime PD at
origination grows with the term (5.9% over 12 months, 38% over 60). The same
proportional deterioration clears a doubling on a short loan and cannot come
close on a long one. Meanwhile PD is monotone in the arrears bucket, so by the
time PD has doubled the account is usually already 30 days down.

**A SICR test built only on arrears can never be genuinely forward-looking,
because arrears are the thing it is supposed to anticipate.** A real
implementation uses a driver independent of arrears, behavioural score, bureau
refresh, macro overlay. This book has none, and saying so is more useful than
reporting a staging split that looks fine.

#### Next
Phase 4, the PD term structure: 12-month versus lifetime, segmented and
seasoned, validated against the analytic truth rather than against itself.


### 2026-10-27, Phase 2: roll rates, and the first estimate that meets the answer key
Three SQL files, a validation module, and the first number in this project that
had to be *estimated* rather than computed.

- **`sql/05_bucket_transitions.sql`**, one row per observed month-to-month
  move, via `LEAD` over an account timeline.
- **`sql/06_roll_rates.sql`**, the pooled transition matrix, account-weighted
  and balance-weighted.
- **`sql/07_roll_rate_segmented.sql`**, the same matrix by segment, by
  months-on-book band and by calendar quarter, in one long table keyed on
  `cut_type` / `cut_value`.
- **`src/rollrate.py`**, the matrix pivot, the comparison against truth, and
  the censoring-bias quantification.

#### The estimate reproduces the generator's matrix
| transition | observed | truth | gap |
|---|---|---|---|
| current → 1-29 | 8.47% | 8.54% | -1.5 SE |
| 1-29 → current | 62.35% | 62.00% | +1.6 SE |
| 30-59 → 60-89 | 33.93% | 33.91% | +0.0 SE |
| 60-89 → 90+ | 50.59% | 51.67% | -1.8 SE |

Every cell within **1.8 standard errors**, counted from 486,213 observed moves
against the matrix those moves were drawn from.

#### The censoring trap, quantified
`LEAD` returns NULL for every account's last month, and the last month is never
a random sample, defaults end there, and so do censored and matured accounts.
Computing a rate as *moves ÷ rows in bucket* counts those as opportunities that
produced no move, which reads as "stayed".

| roll-forward | correct | naive | understated |
|---|---|---|---|
| current → 1-29 | 8.47% | 8.20% | 3.2% |
| 60-89 → 90+ | 50.59% | 48.62% | **3.9%** |

**The bias grows as the buckets deepen**, because accounts in deep arrears are
most likely to be in their final observed month, they are about to default and
the series ends. A provision on the naive matrix would be light, and light in
exactly the segment that drives it.

`test_exactly_one_transition_is_lost_per_account` pins the arithmetic exactly:
transitions = fact rows - accounts.

#### Why the comparison is not against the config base rates
The true matrix varies by segment and by age, so no single true matrix exists
for a pooled estimate to meet. The pooled estimate estimates the
**exposure-weighted average** of the true matrices, which is what
`expected_pooled_matrix` computes. Comparing against the base rates instead
would show a multi-point gap that looks like an estimation bug but is **a bug
in the comparison**, which is worse: it sends you looking in the wrong file.

A nice confirmation fell out of it. The **cure** rates land almost exactly on
their base values (62.35 vs 62.0, 27.85 vs 28.0, 12.23 vs 12.0) while the
**worsen** rates sit visibly below theirs, because the risk multiplier scales
worsening and leaves curing alone. The data reproduced a modelling decision made
on 23 October.

#### The cuts say what they should
- segment: roll-to-default orders prime 33.9% < near-prime 46.5% < subprime 73.8%
- age: the miss rate humps at 9.8% around 12 to 23 months, the seasoning curve
  recovered from data
- quarter: the stability view a monitoring pack leads with

#### Next
Phase 3, IFRS 9 staging. The SICR rules, where the accounting judgement lives
and where the provision moves by multiples rather than percentages.


### 2026-10-26, Phase 1: the data mart
Dimensions, a fact table with a grain stated in one sentence, and eight quality
checks written as SQL.

- **`sql/01_dim_date.sql`**, calendar at month-end grain, built from month
  starts and `last_day()` so it cannot drift. Carries `month_index`
  (`year*12 + month`) so every span in months is a subtraction.
- **`sql/02_dim_account.sql`**, one row per loan, type 1, with vintage keys and
  reporting bands defined once so two reports cannot band differently.
- **`sql/03_fact_loan_month.sql`**, **one row per account per calendar month
  end on which the account was on book.** A periodic snapshot, not a
  transaction fact, so a missing month is a defect rather than an absence of
  events.
- **`sql/04_mart_quality.sql`**, eight checks returning failure counts.
- **`src/mart.py`**, orchestration and reporting only; the modelling is SQL.
- `run_pipeline.py` now builds the mart and **stops** if any check fails.

Full book: 20,000 accounts, **506,213 fact rows**, all eight checks pass. At the
reporting date: 8,568 accounts on book, 136.8m exposure, 5.4% 30+ DPD, correctly
ordered by segment (prime 3.8% / near-prime 5.5% / subprime 10.6%).

#### The test that makes the checks worth having
Not that they pass, `test_every_quality_check_can_actually_fail` builds eight
separate marts, breaks each in exactly the way one check exists to catch
(duplicating rows, deleting a dimension row, removing a month, multiplying a
balance by ten), and asserts the matching check notices.

**A check with a typo in its predicate passes on every book ever written.** It
is indistinguishable from one that works until the day it was needed. The only
way to tell them apart is to break the data on purpose. Same argument as the
football project's simulation study: a control never observed to fire is not
yet evidence of anything.

#### Additivity, written down before it bites
`scheduled_balance` is **semi-additive**, summable across accounts at a point
in time, meaningless summed across months. `is_default` counts the same default
every month it persists; `is_new_default` (a `LAG` window) fires once. Both
pinned by tests, both in column comments, because a semi-additive measure summed
the wrong way produces a confident catastrophic number.

#### A small-sample inversion worth noting
On the 400-account sample, prime showed a *higher* 30+ DPD rate than near-prime
which would be alarming if it were real. On the full 20,000-account book the
ordering is correct. Worth remembering before reading anything into a segment
cut of the sample.

#### A test that failed for the wrong reason
Running the Phase 0 suite from an unzipped folder before `git init` reported
*"the working warehouse should not be version-controlled, it is"*. It wasn't.
`git check-ignore` exits **128** outside a work tree, and the test treated
anything non-zero as "not ignored", so the absence of a repository was reported
as a `.gitignore` defect.

Now asks `git rev-parse --is-inside-work-tree` first and skips with an
explanation. **A check that cannot distinguish "the answer is no" from "I could
not determine the answer" will eventually report the second as the first**,
the same shape as the football optimiser returning `success: True` while failing
to converge.

#### Next
Phase 2, delinquency and roll rates: bucket transition matrices from the fact
table's own history, which is the empirical counterpart to the generator's
known transition matrix.


### 2026-10-23, Phase 0: the answer key comes first
Scaffolding, configuration, the synthetic book, and the analytic truth that
makes the whole project checkable.

- **`config.yaml`**, every threshold and accounting policy choice in one
  readable place: the staging rules (30-day SICR backstop, 2× relative PD
  increase above a 5% floor, 3-month probation), the default definition
  (90+ DPD), the discount basis (EIR), and the validation tolerances. All set
  before any result exists.
- **`src/generate.py`**, the book, and the answer key. Delinquency is a Markov
  chain over arrears buckets; because the transition matrix is known, true
  12-month and lifetime PD follow exactly from its powers, and exposure follows
  from the annuity formula. The engine will estimate; this already knows.
- **`src/db.py`**, thin DuckDB harness. The logic belongs in SQL under `sql/`,
  not in Python wrappers.
- **`scripts/generate_book.py`**, **`scripts/run_pipeline.py --status`**, CI on
  Python 3.11 and 3.12, a committed 400-account sample book.
- **24 tests**, headed by `test_simulated_defaults_match_the_analytic_truth`.

#### Calibration: a modelling choice that had to change
The first version scaled **cure** rates by the risk multiplier as well as
**worsen** rates. Compounded across four buckets, that produced a **135×**
difference between prime and subprime default rates, no real book looks like
that. The multiplier now scales only how often an account falls behind;
conditional on being 30 days down, cure rates are shared. Stated explicitly in
config rather than buried, and `test_riskier_segments_default_more` pins the
spread between 3× and 20×.

Calibrated result: portfolio 12-month PD **5.1%**, 36-month **24.7%**, with
prime 1.9% / near-prime 4.4% / subprime 13.8%.

#### Two performance bugs, neither of which changed a number
A generator nobody wants to rerun is a generator whose parameters stop being
questioned, so these mattered:
- **The ground truth was quadratic.** `marginal_pd_schedule` recomputed the
  whole curve for every horizon, O(T²) matrix builds per account. One forward
  pass and take differences.
- **`balance_at` was scalar-only**, forcing a Python loop over every account
  every month. Vectorised, plus memoised transition matrices (only three risk
  multipliers exist). Full book generation: **205s → 12s**.

#### A near-miss, caught before the first commit
`*.duckdb` in `.gitignore` would have excluded the committed sample book, the
exact failure my scorecard project had with `data/*`, where a clean clone
crashed. Found by a test that **asks git** (`git check-ignore`) rather than
reading `.gitignore` and reasoning about it: string-matching the rules file
would not have caught an extension-based rule.

#### DuckDB 2.x
`fetchone()` leaves a result set open and the connection then refuses further
work. Every count is drained with `fetchall()` behind one helper, so no call
site has to remember.

#### Next
Phase 1, the data mart: `dim_account`, `dim_date`, `fact_loan_month`, with the
grain documented and tested.
