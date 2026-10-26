# Progress Log — Credit Risk ECL Engine

> **Purpose.** A living status tracker, updated at the end of each working
> session. Read this plus `docs/ROADMAP.md` and you know exactly where the
> project stands. Newest entry at the top.

**Current phase:** Phase 1 complete. Next: Phase 2 — delinquency and roll rates.
**Overall status:** 🟢 Book, answer key, warehouse, CI, and a data mart with a
documented grain that passes eight quality checks — each of which has been
demonstrated to fail when the mart is broken. **39 tests green.**
**Cadence:** one commit per working day.

---

## Phase checklist
- [x] Phase 0 — Scaffolding, config, synthetic book + answer key, warehouse, CI
- [x] Phase 1 — Data mart
- [ ] Phase 2 — Delinquency and roll rates
- [ ] Phase 3 — IFRS 9 staging
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
pytest                                    # 39 tests
python scripts/run_pipeline.py --sample   # the committed sample book
python scripts/run_pipeline.py --status   # the build plan
python scripts/generate_book.py           # full book, ~12s
python scripts/generate_book.py --sample  # rebuild the committed sample
python scripts/generate_book.py --summary # describe the warehouse
```

---

## Session log

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
