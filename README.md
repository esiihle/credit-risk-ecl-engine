# 📊 Credit Risk ECL Engine

**An IFRS 9 expected-credit-loss engine over a loan book, built in SQL: data mart, delinquency roll rates, SICR staging, PD term structure, LGD/EAD, discounted ECL — validated against an answer key that is known exactly, and reimplemented in SAS `PROC SQL` and reconciled.**

![tests](https://github.com/esiihle/credit-risk-ecl-engine/actions/workflows/tests.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![engine](https://img.shields.io/badge/SQL-DuckDB%20%2B%20SAS-orange)
![license](https://img.shields.io/badge/license-MIT-green)

---

## The problem

Under IFRS 9 a bank must provision for losses it **expects**, not losses that
have happened:

```
ECL = Σ  PD(t) × LGD × EAD(t) × discount(t)
```

over the next twelve months, or the remaining lifetime of the loan, depending on
whether credit risk has increased significantly since origination. That clause —
**staging** — is where the accounting judgement lives, and where the provision
moves by multiples rather than percentages.

This repository builds that engine, in SQL, over a loan book whose true
probability of default is known.

## Why the book is simulated

The deliverable here is the **correctness of a calculation**, and that decides
the data strategy.

A real loan book offers realism and nothing else that matters: nobody knows its
true PD, so an engine run over it can only be compared against another estimate.
This book's delinquency is a Markov chain whose transition matrix is written
down, so:

- **true PD** follows exactly from the matrix — `(P^k)[s, default]`
- **true EAD** follows exactly from the annuity formula
- **true LGD** is drawn from a known distribution

The engine estimates. The answer key already knows. Where they disagree, one of
them is wrong — and that is a finding, not a mystery.

> The companion [credit default scorecard](https://github.com/esiihle/credit-default-scorecard)
> makes the **opposite** choice for the opposite reason: there the *finding* was
> the point, so real data was essential, and a synthetic stand-in once produced
> published figures wrong by a factor of three. Here the *mechanism* is the
> point. Knowing which case you are in is the actual skill.

Every table and report says the book is synthetic.

## Current state — Phase 3 complete

Book, answer key, warehouse, CI, a data mart, a transition matrix reconciled
against the one the book was generated from, and **IFRS 9 staging** — a PD curve
built as a recursive CTE, SICR triggers with probation, every account staged
with its reason recorded. **66 tests pass.**

```bash
git clone https://github.com/esiihle/credit-risk-ecl-engine.git
cd credit-risk-ecl-engine
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

pytest                                   # 66 tests
python scripts/run_pipeline.py --sample  # runs on the committed sample book
python scripts/run_pipeline.py --status  # the build plan
python scripts/generate_book.py          # build the full book (~12s)
```

A sample book is committed, so a fresh clone runs immediately without generating
anything.

## The build plan

| Phase | What | |
|---|---|---|
| **0** | Scaffolding, config, synthetic book + answer key, warehouse, CI | **done** |
| **1** | Data mart: `dim_account`, `dim_date`, `fact_loan_month` | **done** |
| **2** | Delinquency buckets and roll rates | **done** |
| **3** | IFRS 9 staging — the SICR rules | **done** |
| 4 | PD term structure, 12-month vs lifetime | next |
| 5 | LGD and EAD | |
| 6 | ECL calculation and discounting at the EIR | |
| 7 | Vintage curves and portfolio MI | |
| 8 | SAS `PROC SQL` implementation + reconciliation | |
| 9 | Documentation and study guide | |

Phase 4 is the join to the scorecard project: that one produces a PD, this one
consumes it to produce a balance-sheet provision.

## Two implementations, reconciled

Phase 8 reimplements the engine in SAS `PROC SQL` — what South African bank
credit risk teams actually run — and reconciles it against the DuckDB reference
account by account, to a tolerance set in `config.yaml` before either was built.

That is not a workaround for lacking a SAS licence during development. It is
**champion/challenger reconciliation**, which is what a model validation
function does, and it is the only way SAS code can be *shown* to be correct
rather than asserted to be.

## Repository structure

```
config.yaml     every threshold and accounting policy choice, in one readable place
src/            config, generate (the book + its answer key), db (DuckDB harness)
sql/            the engine's logic, as SQL — readable, reviewable, portable
scripts/        generate_book.py · run_pipeline.py
tests/          66 tests; the answer key is checked against the simulation,
                and every mart quality check is shown to fail when it should
docs/           OVERVIEW · ROADMAP · methodology
data/sample/    a small committed book so a clone runs out of the box
.github/        CI: the suite plus a pipeline run, on Python 3.11 and 3.12
```

## How this project handles numbers

No figure appears in any document unless a command regenerates it. Thresholds go
in `config.yaml` *before* results are known — a limit that moves to accommodate
an outcome was never a limit. Both habits are here because a sister project
published numbers typed into a comment that were wrong by a factor of three and
survived a green test suite for two days.

## Author

**Sihle Myeza** — BSc Computer Science (Wits), data analyst targeting
quantitative and credit-risk roles. Built in the open, one commit at a time.

## License

MIT — see [`LICENSE`](LICENSE).
