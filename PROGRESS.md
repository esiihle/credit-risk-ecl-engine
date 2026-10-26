# Progress Log — Credit Risk ECL Engine

> **Purpose.** A living status tracker, updated at the end of each working
> session. Read this plus `docs/ROADMAP.md` and you know exactly where the
> project stands. Newest entry at the top.

**Current phase:** Phase 0 complete. Next: Phase 1 — the data mart.
**Overall status:** 🟢 Scaffolding, config, synthetic book with an analytic
answer key, DuckDB warehouse and CI all in place. **24 tests green.**
**Cadence:** one commit per working day.

---

## Phase checklist
- [x] Phase 0 — Scaffolding, config, synthetic book + answer key, warehouse, CI
- [ ] Phase 1 — Data mart
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
pytest                                    # 24 tests
python scripts/run_pipeline.py --sample   # the committed sample book
python scripts/run_pipeline.py --status   # the build plan
python scripts/generate_book.py           # full book, ~12s
python scripts/generate_book.py --sample  # rebuild the committed sample
python scripts/generate_book.py --summary # describe the warehouse
```

---

## Session log

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
