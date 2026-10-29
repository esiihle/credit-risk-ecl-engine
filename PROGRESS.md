# Progress Log — Credit Risk ECL Engine

> **Purpose.** A living status tracker, updated at the end of each working
> session. Read this plus `docs/ROADMAP.md` and you know exactly where the
> project stands. Newest entry at the top.

**Current phase:** Phase 3 complete. Next: Phase 4 — PD term structure.
**Overall status:** 🟢 Mart, roll rates, and now **IFRS 9 staging** — a PD curve
built as a recursive CTE, SICR triggers with probation, and every account staged
with its reason recorded. **66 tests green.**
**Cadence:** one commit per working day.

---

## Phase checklist
- [x] Phase 0 — Scaffolding, config, synthetic book + answer key, warehouse, CI
- [x] Phase 1 — Data mart
- [x] Phase 2 — Delinquency and roll rates
- [x] Phase 3 — IFRS 9 staging
- [ ] Phase 4 — PD term structure
- [ ] Phase 5 — LGD and EAD
- [ ] Phase 6 — ECL calculation
- [ ] Phase 7 — Vintage and portfolio MI
- [ ] Phase 8 — SAS implementation and reconciliation
- [ ] Phase 9 — Documentation and study guide

## Open decisions
- [x] **Product:** unsecured personal loans — fixed term, monthly instalments,
      no collateral, so LGD stays simple and the IFRS 9 mechanics stay in focus.
      Settled 23 Oct 2026.
- [x] **Data:** synthetic, with a known analytic ground truth. Settled 23 Oct.
- [x] **Engine:** DuckDB as the executable reference, SAS `PROC SQL` as the
      parallel implementation, reconciled in Phase 8. Settled 23 Oct.
- [ ] **Scorecard PD integration** — whether Phase 4 consumes the scorecard's
      exported PD directly or reimplements the scaling. Settle before Phase 4.

## How to run
```bash
pip install -r requirements.txt
pytest                                    # 66 tests
python scripts/run_pipeline.py --sample   # the committed sample book
python scripts/run_pipeline.py --status   # the build plan
python scripts/generate_book.py           # full book, ~12s
python scripts/generate_book.py --sample  # rebuild the committed sample
python scripts/generate_book.py --summary # describe the warehouse
```

---

## Session log

### 2026-10-29 — Phase 3: IFRS 9 staging, and a trigger that barely fires
The accounting judgement phase. Three SQL files and the finding that staging —
not the PD model — is what moves a provision.

- **`sql/08_pd_curve.sql`** — matrix exponentiation as a **recursive CTE**. The
  state vector carried as five columns, multiplied by the Phase 2 matrix each
  step; the mass in bucket 4 after k steps is P(default within k months).
- **`sql/09_staging.sql`** — Stage 3 at 90+ DPD; Stage 2 on the 30-day
  backstop, the relative PD test, or probation. Every account carries a
  `stage_reason`.
- **`sql/10_stage_migration.sql`** — month-on-month stage movement, account- and
  balance-weighted.
- **`src/staging.py`** — orchestration, validation, and the trigger analysis.

Book at the reporting date: **92.3% Stage 1 / 7.0% Stage 2 / 0.7% Stage 3** by
exposure. All four staging checks pass.

#### Two traps in the recursive CTE, both silent
**DECIMAL truncation.** `1.0 AS p0` is `DECIMAL(2,1)` — one decimal place — and
the type propagates through every iteration. The state vector froze at
(0.9, 0.1) forever and produced perfectly valid-looking probabilities that were
completely wrong. No error. Every literal in the anchor now carries `::DOUBLE`,
and a regression test asserts the untyped version really does freeze, so the
casts cannot be tidied away as noise.

**The absorbing row is absent from the data.** Nothing is ever *observed*
leaving default, so pivoting `roll_rate` gives NULL for the whole bucket-4 row
including its diagonal. Coalescing to zero makes probability mass vanish and the
PD curve plateau — which looks reasonable. The identity row is a modelling fact,
not a data fact, and has to be asserted.

#### The finding: the relative PD test is nearly redundant
Counting account-months where each trigger was *the* reason:

| trigger | account-months |
|---|---|
| 30-day backstop | 23,737 |
| relative PD test | **1,132** |

And **every one of the 1,132 is a 12-month loan** — none on 24, 36, 48 or 60.

Structural: a relative test needs a small denominator, and lifetime PD at
origination grows with the term (5.9% over 12 months, 38% over 60). The same
proportional deterioration clears a doubling on a short loan and cannot come
close on a long one. Meanwhile PD is monotone in the arrears bucket, so by the
time PD has doubled the account is usually already 30 days down.

**A SICR test built only on arrears can never be genuinely forward-looking,
because arrears are the thing it is supposed to anticipate.** A real
implementation uses a driver independent of arrears — behavioural score, bureau
refresh, macro overlay. This book has none, and saying so is more useful than
reporting a staging split that looks fine.

#### Next
Phase 4 — the PD term structure: 12-month versus lifetime, segmented and
seasoned, validated against the analytic truth rather than against itself.


### 2026-10-27 — Phase 2: roll rates, and the first estimate that meets the answer key
Three SQL files, a validation module, and the first number in this project that
had to be *estimated* rather than computed.

- **`sql/05_bucket_transitions.sql`** — one row per observed month-to-month
  move, via `LEAD` over an account timeline.
- **`sql/06_roll_rates.sql`** — the pooled transition matrix, account-weighted
  and balance-weighted.
- **`sql/07_roll_rate_segmented.sql`** — the same matrix by segment, by
  months-on-book band and by calendar quarter, in one long table keyed on
  `cut_type` / `cut_value`.
- **`src/rollrate.py`** — the matrix pivot, the comparison against truth, and
  the censoring-bias quantification.

#### The estimate reproduces the generator's matrix
| transition | observed | truth | gap |
|---|---|---|---|
| current → 1-29 | 8.47% | 8.54% | −1.5 SE |
| 1-29 → current | 62.35% | 62.00% | +1.6 SE |
| 30-59 → 60-89 | 33.93% | 33.91% | +0.0 SE |
| 60-89 → 90+ | 50.59% | 51.67% | −1.8 SE |

Every cell within **1.8 standard errors**, counted from 486,213 observed moves
against the matrix those moves were drawn from.

#### The censoring trap, quantified
`LEAD` returns NULL for every account's last month, and the last month is never
a random sample — defaults end there, and so do censored and matured accounts.
Computing a rate as *moves ÷ rows in bucket* counts those as opportunities that
produced no move, which reads as "stayed".

| roll-forward | correct | naive | understated |
|---|---|---|---|
| current → 1-29 | 8.47% | 8.20% | 3.2% |
| 60-89 → 90+ | 50.59% | 48.62% | **3.9%** |

**The bias grows as the buckets deepen**, because accounts in deep arrears are
most likely to be in their final observed month — they are about to default and
the series ends. A provision on the naive matrix would be light, and light in
exactly the segment that drives it.

`test_exactly_one_transition_is_lost_per_account` pins the arithmetic exactly:
transitions = fact rows − accounts.

#### Why the comparison is not against the config base rates
The true matrix varies by segment and by age, so no single true matrix exists
for a pooled estimate to meet. The pooled estimate estimates the
**exposure-weighted average** of the true matrices — which is what
`expected_pooled_matrix` computes. Comparing against the base rates instead
would show a multi-point gap that looks like an estimation bug but is **a bug
in the comparison**, which is worse: it sends you looking in the wrong file.

A nice confirmation fell out of it. The **cure** rates land almost exactly on
their base values (62.35 vs 62.0, 27.85 vs 28.0, 12.23 vs 12.0) while the
**worsen** rates sit visibly below theirs — because the risk multiplier scales
worsening and leaves curing alone. The data reproduced a modelling decision made
on 23 October.

#### The cuts say what they should
- segment: roll-to-default orders prime 33.9% < near-prime 46.5% < subprime 73.8%
- age: the miss rate humps at 9.8% around 12–23 months, the seasoning curve
  recovered from data
- quarter: the stability view a monitoring pack leads with

#### Next
Phase 3 — IFRS 9 staging. The SICR rules, where the accounting judgement lives
and where the provision moves by multiples rather than percentages.


### 2026-10-26 — Phase 1: the data mart
Dimensions, a fact table with a grain stated in one sentence, and eight quality
checks written as SQL.

- **`sql/01_dim_date.sql`** — calendar at month-end grain, built from month
  starts and `last_day()` so it cannot drift. Carries `month_index`
  (`year*12 + month`) so every span in months is a subtraction.
- **`sql/02_dim_account.sql`** — one row per loan, type 1, with vintage keys and
  reporting bands defined once so two reports cannot band differently.
- **`sql/03_fact_loan_month.sql`** — **one row per account per calendar month
  end on which the account was on book.** A periodic snapshot, not a
  transaction fact, so a missing month is a defect rather than an absence of
  events.
- **`sql/04_mart_quality.sql`** — eight checks returning failure counts.
- **`src/mart.py`** — orchestration and reporting only; the modelling is SQL.
- `run_pipeline.py` now builds the mart and **stops** if any check fails.

Full book: 20,000 accounts, **506,213 fact rows**, all eight checks pass. At the
reporting date: 8,568 accounts on book, 136.8m exposure, 5.4% 30+ DPD, correctly
ordered by segment (prime 3.8% / near-prime 5.5% / subprime 10.6%).

#### The test that makes the checks worth having
Not that they pass — `test_every_quality_check_can_actually_fail` builds eight
separate marts, breaks each in exactly the way one check exists to catch
(duplicating rows, deleting a dimension row, removing a month, multiplying a
balance by ten), and asserts the matching check notices.

**A check with a typo in its predicate passes on every book ever written.** It
is indistinguishable from one that works until the day it was needed. The only
way to tell them apart is to break the data on purpose. Same argument as the
football project's simulation study: a control never observed to fire is not
yet evidence of anything.

#### Additivity, written down before it bites
`scheduled_balance` is **semi-additive** — summable across accounts at a point
in time, meaningless summed across months. `is_default` counts the same default
every month it persists; `is_new_default` (a `LAG` window) fires once. Both
pinned by tests, both in column comments, because a semi-additive measure summed
the wrong way produces a confident catastrophic number.

#### A small-sample inversion worth noting
On the 400-account sample, prime showed a *higher* 30+ DPD rate than near-prime
— which would be alarming if it were real. On the full 20,000-account book the
ordering is correct. Worth remembering before reading anything into a segment
cut of the sample.

#### A test that failed for the wrong reason
Running the Phase 0 suite from an unzipped folder before `git init` reported
*"the working warehouse should not be version-controlled — it is"*. It wasn't.
`git check-ignore` exits **128** outside a work tree, and the test treated
anything non-zero as "not ignored", so the absence of a repository was reported
as a `.gitignore` defect.

Now asks `git rev-parse --is-inside-work-tree` first and skips with an
explanation. **A check that cannot distinguish "the answer is no" from "I could
not determine the answer" will eventually report the second as the first** —
the same shape as the football optimiser returning `success: True` while failing
to converge.

#### Next
Phase 2 — delinquency and roll rates: bucket transition matrices from the fact
table's own history, which is the empirical counterpart to the generator's
known transition matrix.


### 2026-10-23 — Phase 0: the answer key comes first
Scaffolding, configuration, the synthetic book, and the analytic truth that
makes the whole project checkable.

- **`config.yaml`** — every threshold and accounting policy choice in one
  readable place: the staging rules (30-day SICR backstop, 2× relative PD
  increase above a 5% floor, 3-month probation), the default definition
  (90+ DPD), the discount basis (EIR), and the validation tolerances. All set
  before any result exists.
- **`src/generate.py`** — the book, and the answer key. Delinquency is a Markov
  chain over arrears buckets; because the transition matrix is known, true
  12-month and lifetime PD follow exactly from its powers, and exposure follows
  from the annuity formula. The engine will estimate; this already knows.
- **`src/db.py`** — thin DuckDB harness. The logic belongs in SQL under `sql/`,
  not in Python wrappers.
- **`scripts/generate_book.py`**, **`scripts/run_pipeline.py --status`**, CI on
  Python 3.11 and 3.12, a committed 400-account sample book.
- **24 tests**, headed by `test_simulated_defaults_match_the_analytic_truth`.

#### Calibration: a modelling choice that had to change
The first version scaled **cure** rates by the risk multiplier as well as
**worsen** rates. Compounded across four buckets, that produced a **135×**
difference between prime and subprime default rates — no real book looks like
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
  whole curve for every horizon — O(T²) matrix builds per account. One forward
  pass and take differences.
- **`balance_at` was scalar-only**, forcing a Python loop over every account
  every month. Vectorised, plus memoised transition matrices (only three risk
  multipliers exist). Full book generation: **205s → 12s**.

#### A near-miss, caught before the first commit
`*.duckdb` in `.gitignore` would have excluded the committed sample book — the
exact failure the scorecard project had with `data/*`, where a clean clone
crashed. Found by a test that **asks git** (`git check-ignore`) rather than
reading `.gitignore` and reasoning about it: string-matching the rules file
would not have caught an extension-based rule.

#### DuckDB 2.x
`fetchone()` leaves a result set open and the connection then refuses further
work. Every count is drained with `fetchall()` behind one helper, so no call
site has to remember.

#### Next
Phase 1 — the data mart: `dim_account`, `dim_date`, `fact_loan_month`, with the
grain documented and tested.
