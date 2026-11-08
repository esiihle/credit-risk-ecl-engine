# Overview

## The problem

Under IFRS 9 a bank holds a provision for **expected credit losses** on every
loan. Not losses that have happened, but losses it expects. The calculation,
in outline:

```
ECL = Σ  PD(t) × LGD × EAD(t) × discount(t)
```

The sum runs over either the next twelve months or the remaining lifetime of
the loan, depending on whether credit risk has increased significantly since
origination. That last clause is called **staging**, and it is where the
judgement lives and where the provision can move by multiples.

This project builds that engine over a loan book, in SQL, and checks its
output against an answer that is known exactly.

## Why this is the project

It is a South African bank credit risk role in miniature. The work is SQL over
a loan warehouse: window functions for delinquency and roll rates, dimensional
modelling for the mart, recursive logic for transition matrices, and careful
arithmetic for the provision itself. IFRS 9 is the standard every SA bank
provisions under, and the staging rules are the thing an auditor asks about.

## The data

A **synthetic** unsecured personal loan book, generated from a documented data
generating process. That is a positive choice rather than a compromise.

What this project has to demonstrate is that a calculation is correct. A real
loan book would be more realistic and useless for the job, because nobody knows
a real book's true probability of default, so there would be nothing to check
the engine against except another estimate. Here delinquency is a Markov chain
whose transition matrix is known, so the true 12 month and lifetime PD follow
exactly from its powers, exposure follows exactly from the annuity formula, and
loss given default is drawn from a known distribution.

So the engine estimates and the answer key already knows. Where they disagree,
one of them is wrong, and working out which is most of the job.

Every artefact says it is synthetic, in the file and in the table.

## The approach

- **Every threshold in `config.yaml`**, set before results are known. Under
  IFRS 9 these are not implementation details, they are accounting policy.
- **The logic in SQL**, under `sql/`, so it reads as SQL and ports to a bank's
  warehouse, and to SAS in Phase 8.
- **Two implementations, reconciled.** Phase 8 reimplements the engine in SAS
  `PROC SQL` and reconciles it against the DuckDB reference account by account.
  That is what a model validation function does, and it is the only way to show
  that the SAS code is correct rather than assert it.
- **No number in a document that a command cannot regenerate.** On my scorecard
  project I once published figures typed into a comment that were wrong by a
  factor of three, and they survived a green test suite for two days.
