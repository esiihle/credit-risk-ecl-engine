# Roadmap

Ten phases, one commit per working day. Each phase lands working code, tests
that pin its behaviour, and a methodology section explaining the reasoning.

| Phase | What | Status |
|---|---|---|
| **0** | **Scaffolding, config, synthetic book with a known answer key, DuckDB warehouse, CI** | **done — 23 Oct 2026** |
| 1 | Data mart: `dim_account`, `dim_date`, `fact_loan_month`, documented grain | next |
| 2 | Delinquency buckets and roll rates — window functions, transition matrices | |
| 3 | IFRS 9 staging — the SICR rules, where the accounting judgement lives | |
| 4 | PD term structure, 12-month vs lifetime — **consumes the scorecard's PD** | |
| 5 | LGD and EAD | |
| 6 | ECL calculation and discounting at the effective interest rate | |
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
