# Roadmap

Ten phases, one commit per working day. Each phase lands working code, tests
that pin its behaviour, and a methodology section explaining the reasoning.

| Phase | What | Status |
|---|---|---|
| **0** | **Scaffolding, config, synthetic book with a known answer key, DuckDB warehouse, CI** | **done — 23 Oct 2026** |
| **1** | **Data mart: `dim_account`, `dim_date`, `fact_loan_month`, documented grain** | **done** |
| **2** | **Delinquency buckets and roll rates — window functions, transition matrices** | **done** |
| **3** | **IFRS 9 staging — the SICR rules, where the accounting judgement lives** | **done** |
| **4** | **PD term structure, 12-month vs lifetime — consumes the scorecard's PD** | **done** |
| **5** | **LGD from resolved workouts only, EAD by amortisation** | **done — 5 Nov 2026** |
| 6 | ECL calculation and discounting at the effective interest rate | next |
| 7 | Vintage curves and portfolio MI | |
| 8 | SAS `PROC SQL` implementation and the reconciliation harness | |
| 9 | README, methodology, study guide | |

## The through-line

Phase 4 is the join to the companion
[credit default scorecard](https://github.com/esiihle/credit-default-scorecard):
that project produces a probability of default, and this one consumes it to
produce a balance-sheet provision. Being able to walk an interviewer from WOE
binning to a reported ECL figure is worth more than either project alone.

## Why the answer key comes first

Phase 0 builds the data *and the analytic truth that goes with it* before any
engine code exists. That ordering is deliberate. An ECL engine validated only
against its own output is validated against nothing; this one can be checked
against a PD computed from matrix algebra and an exposure computed from the
annuity formula, neither of which the engine ever sees.


---

## A note on this table

It was stale for four phases. Phases 1 to 4 were built, tested and committed
while this file still listed Phase 1 as `next`, because the README carried the
current state and nobody re-read the roadmap.

Left recorded rather than quietly corrected, because it is the same failure this
project keeps writing down in other forms: **a number or a status that no
command regenerates is not a result, it is a note about a result, and notes
drift.** The README's phase table and test count are checked by the test suite.
This file is prose, so it is not, and it drifted.

Per-phase commit dates are in the git history, which is the one record that
cannot drift.
