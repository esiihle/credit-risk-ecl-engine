# Methodology — Credit Risk ECL Engine

Derivations and reasoning, one section per phase. Written as each phase lands,
so the document and the code cannot drift apart.

---

## 1. The book and its answer key  *(built 23 Oct 2026)*

### 1.1 Why the data is simulated

The deliverable of this project is the correctness of a calculation, not a
finding about a population. That single sentence decides the data strategy.

A real loan book offers realism and nothing else that matters here: nobody knows
its true probability of default, so an engine run over it can only be compared
against another estimate. A simulated book whose data-generating process is
written down offers the one thing that makes validation possible — an answer
computed by a route the engine never touches.

The companion [credit default scorecard](https://github.com/esiihle/credit-default-scorecard)
makes the opposite choice for the opposite reason, and the contrast is worth
holding on to. There the *finding* was the point — does this characteristic
predict default? — so real data was essential, and a synthetic stand-in once
produced published figures that were wrong by a factor of three. Here the
*mechanism* is the point. Simulation is right in one case and disqualifying in
the other, and knowing which is which is the actual skill.

Every table and every report this project writes says the book is synthetic.

### 1.2 Delinquency as a Markov chain

Arrears are modelled as a discrete-time Markov chain over buckets:

| bucket | meaning |
|---|---|
| 0 | current |
| 1 | 1–29 days past due |
| 2 | 30–59 days past due |
| 3 | 60–89 days past due |
| 4 | 90+ days past due — **default, absorbing** |

Each month an account either falls further behind, cures toward current, or
stays put. The 90+ bucket is absorbing: this project's default definition, and
the Basel/IFRS 9 convention. Cures out of default do happen in reality and are
deliberately excluded — they complicate Stage 3 accounting, and the job here is
the core mechanics.

Each account's matrix $P$ is built from base rates in `config.yaml`, scaled by
its segment's risk multiplier and by a seasoning factor.

### 1.3 Why the chain is the point

Because $P$ is known, the truth follows exactly:

$$\Pr(\text{default within } k \text{ months} \mid \text{bucket } s) = (P^k)[s, 4]$$

Seasoning makes $P$ age-dependent, so it is a product of matrices rather than a
single power, but the principle holds: **no simulation is needed for the truth,
only for the data the engine will see.**

The engine will later estimate 12-month and lifetime PD from observed
transitions. `cumulative_pd_curve` already knows the answer.
`test_simulated_defaults_match_the_analytic_truth` asserts that random paths
through the chain reproduce the matrix algebra to within the binomial standard
error — two genuinely independent routes to the same quantity, which makes
agreement evidence rather than a restatement.

### 1.4 Seasoning

Default risk on a term loan is low immediately after origination — the borrower
has just been underwritten and has just received the money — rises to a peak in
the first couple of years, then falls as survivors reveal themselves to be good
payers. Modelled as a bump $x e^{1-x}$ peaking at `seasoning_peak_month`.

Without it every vintage would look identical, and the vintage analysis in
Phase 7 would have nothing to show.

### 1.5 Exposure is arithmetic

A fixed-term amortising loan's balance is deterministic:

$$\text{instalment} = \frac{P i}{1 - (1+i)^{-n}}, \qquad
\text{balance}_t = P\,\frac{(1+i)^n - (1+i)^t}{(1+i)^n - 1}$$

So EAD at any future month is exact, and every exposure the engine produces can
be checked to the cent. This is part of why a **term** product was chosen over
a revolving one: on a credit card, exposure at default depends on undrawn
commitment and a credit conversion factor, and "lifetime" is genuinely
ambiguous. Those are real problems worth solving later, not while establishing
the mechanics.

### 1.6 Calibration, and a modelling choice that had to change

The base rates were calibrated so the generated book resembles a South African
unsecured personal loan portfolio: roughly 5% 12-month PD and 25% over a
36-month life, with prime near 2%, near-prime 4–5% and subprime 13–14%.

**A first version scaled cure rates by the risk multiplier as well as worsen
rates** — riskier borrowers both fell behind more often and recovered less
often. The effect compounded across four buckets into a **135× difference**
between prime and subprime default rates. No real book looks like that.

The modelling statement is now explicit, in config and in code: the risk
multiplier scales how often an account *falls behind*; conditional on being 30
days down, cure rates are shared. It is a simplification, and it is stated
rather than buried. `test_riskier_segments_default_more` pins the spread
between 3× and 20×, so the old behaviour cannot return unnoticed.

### 1.7 Two performance bugs worth recording

Neither changed a number. Both would have made the project unpleasant to work
on, which is its own kind of defect — a generator nobody wants to rerun is a
generator whose parameters stop being questioned.

* **The ground truth was quadratic.** `marginal_pd_schedule` called a
  fixed-horizon PD routine for every month, and each call walked the chain from
  the start — O(T²) matrix builds per account, several million across the book.
  Computing the whole cumulative curve in one forward pass and taking
  differences made it linear.
* **`balance_at` was scalar-only**, which forced a Python loop over every
  account for every month. Vectorising it, together with memoising the
  transition matrices (there are only three risk multipliers), took full book
  generation from **205 seconds to 12**.

### 1.8 A near-miss on `.gitignore`

`*.duckdb` was written into `.gitignore` to keep the working warehouse out of
version control. It would also have excluded `data/sample/sample_book.duckdb`,
which is committed on purpose so a fresh clone runs immediately — the exact
failure the companion scorecard project had with `data/*`, where a clean clone
crashed and two tests failed.

Caught before the first commit, by a test that **asks git** rather than reading
`.gitignore` and reasoning about it. String-matching the rules file would not
have caught an extension-based rule; `git check-ignore` is the only authority on
what git ignores.


---

## 2. The data mart  *(built 26 Oct 2026)*

### 2.1 Why a mart at all

The generator already produces a tidy table of account-months. Modelling it
into dimensions and a fact adds work, so it needs a reason.

The reason is that every later phase asks the same questions of the same data —
group by segment, by vintage, by quarter; compare a month to the one before —
and those questions are about *the calendar* and *the account*, not about the
loan-month observation. Defining them once, in a dimension, means every report
agrees. Deriving them per query is how two reports come to disagree about which
quarter December belongs to, and then nobody can say which provision is right.

### 2.2 The grain, stated once

> **One row per account per calendar month end on which the account was on
> book.**

Everything else follows from that sentence, which is why it is written at the
top of `sql/03_fact_loan_month.sql` and enforced by a test rather than left to
be inferred.

This is a **periodic snapshot** fact table, not a transaction fact:

* a transaction fact records *events* — a payment, a drawdown — and a row
  exists only when something happened
* a periodic snapshot records *state* at a regular interval, whether or not
  anything happened

Provisioning needs the second. The question is always "what did the book look
like at month end", never "what happened in between". The practical consequence
is sharp: **a gap in an account's monthly series is a defect, not an absence of
events**, and `04_mart_quality.sql` looks for exactly that.

### 2.3 Additivity — the thing most often got wrong

A measure's additivity is a property of the measure, not of the query, and
getting it wrong produces a confident number with no meaning.

| measure | across accounts | across time |
|---|---|---|
| `scheduled_balance` | additive — this is the book's exposure | **NOT additive** |
| `is_default` | additive — accounts in default now | **NOT additive** |
| `is_new_default` | additive | additive |

`scheduled_balance` is **semi-additive**. Summing it across accounts at one
month end gives exposure. Summing it across months gives a number that means
nothing: an account owing 10,000 for twelve months did not owe 120,000.

`is_default` has the same shape for a different reason — it counts the same
default again every month it persists, so a sum across time answers "how many
account-months were spent in default", which is a legitimate question but not
the one anybody asked. `is_new_default`, computed with a `LAG` window, fires
once per defaulted account and is the measure a default-rate report wants.

These are in column comments, and `test_balance_is_semi_additive` and
`test_is_new_default_fires_exactly_once_per_defaulted_account` pin them.

### 2.4 A slowly changing dimension, and why type 1 is right here

`dim_account` is type 1: attributes are overwritten, no history kept. That is
correct **because every attribute in it is fixed at origination** — the
principal advanced, the contractual term, the rate, the segment scored at
application.

It would be wrong the moment the book gained an attribute that changes: a
refreshed behavioural score, a repriced rate. Those belong in the fact table if
they change monthly, or in a type-2 dimension with effective dates. Type 1 on a
changing attribute silently rewrites history, and *a provision restated because
a dimension was overwritten* is a finding an auditor does not forget.

The choice is recorded in the file rather than left implicit, because the next
person to add a column needs to know which rule they are working under.

### 2.5 A calendar that drifts

Building month ends by repeatedly adding one month to 31 January lands on 28
February — and then stays on the 28th for the rest of the series. The drift is
silent, and every join from the fact table to the calendar would start missing.

The fix is to generate month *starts* and take `last_day()` of each, which
cannot drift. `test_dim_date_is_month_ends_with_no_drift` asserts the result
rather than the method, so a future rewrite is held to the same property.

`month_index` — `year * 12 + month` — is carried as a column so that spans in
months are a subtraction. Months on book, vintage offsets and maturity
arithmetic all reduce to integer differences, with no date functions and no
leap-year thinking.

### 2.6 Quality checks as SQL, and the question that makes them worth writing

The eight checks live in `sql/04_mart_quality.sql`, not in Python, because they
are assertions about the *warehouse*: a credit risk reviewer should be able to
read and rerun them without a Python environment, and Phase 8 ports them to SAS
alongside everything else.

Each returns a failure count:

| check | the bug it exists to catch |
|---|---|
| grain is unique | a duplicated join silently doubles every sum built on the fact |
| no orphaned accounts | a fact row with no dimension row vanishes from any inner-joined report |
| no orphaned dates | as above, through the calendar |
| no gaps in monthly series | the defining property of a periodic snapshot |
| balance never increases | an amortising loan's balance cannot rise |
| DPD agrees with bucket | two columns that must tell the same story |
| rows within account life | nothing before origination or after maturity |
| default is absorbing | a cure out of 90+ would contradict the model |

**The test that makes these worth having is not that they pass.** It is
`test_every_quality_check_can_actually_fail`, which builds eight separate marts,
breaks each one in exactly the way one check exists to catch — duplicating rows,
deleting a dimension row, removing a month, multiplying a balance by ten — and
asserts that the matching check notices.

A check with a typo in its predicate passes on every book ever written. It is
indistinguishable from a check that works, right up until the day it was
supposed to catch something. The only way to tell them apart is to break the
data on purpose.

This is the same argument as the simulation study in the companion football
project, which exists to show that a backtester reporting "no edge" would have
reported one had there been an edge to find. **A control that has never been
observed to fire is not yet evidence of anything.**

### 2.7 Stopping on failure

`run_pipeline.py` halts if any quality check fails, and says so. It does not
warn and continue.

That is deliberate, and it is the lesson this portfolio has now learned three
times: a mart that fails its own checks produces numbers that look exactly like
good ones. Every later stage would inherit the defect and report confidently.
The football project shipped sample-built charts to its front page because the
guard was a warning rather than a refusal; here the guard refuses.

### 2.8 A test that failed for the wrong reason

Running the Phase 0 suite from an unzipped folder, before `git init`, produced:

```
FAILED test_git_ignores_the_working_data_but_not_the_committed_sample
AssertionError: the working warehouse should not be version-controlled — it is...
```

The warehouse was fine. `git check-ignore` exits **128** outside a work tree —
neither "ignored" nor "not ignored" — and the test treated anything other than
0 as "not ignored". So the absence of a repository was reported as a
`.gitignore` defect.

The test now asks `git rev-parse --is-inside-work-tree` first and skips with an
explanation, and treats any exit code other than 0 or 1 as a failure of the
*test*, not of the thing under test.

Worth recording because the failure mode generalises past git: **a check that
cannot distinguish "the answer is no" from "I could not determine the answer"
will eventually report the second as the first.** The same confusion appeared
in the football project, where an optimiser returned `success: True` while
failing to converge, and in the scorecard, where perfect separation showed up as
a huge standard error rather than an error. In each case the system had a way to
say "I don't know" and used a confident value instead.


---

## 3. Delinquency transitions and roll rates  *(built 27 Oct 2026)*

The first phase in which the engine **estimates** something. Everything before
was plumbing; from here each number has a true value behind it.

### 3.1 What a roll rate is

The probability that an account in one arrears bucket is in another a month
later. Collected across all buckets it is a transition matrix, and it is the
most-used artefact in retail credit MI: it drives the PD term structure in
Phase 4, it is the first place a deteriorating book shows up, and it is what a
collections team is managed against.

### 3.2 The censoring trap

The obvious implementation takes `LEAD(arrears_bucket)` and calls it the
destination. That mishandles the last row of every account — and the last row
of every account is never a random sample:

* accounts that **defaulted** end on their default row. The move *into* default
  is observed from the row before; there is no move out, because default is
  absorbing.
* accounts still on book at the reporting date are **right-censored**. Their
  next month exists in reality; we have not seen it.
* accounts that **matured** simply stop.

`LEAD` returns NULL in all three cases. The damage is done not by the NULL but
by the denominator: computing a rate as *moves to j ÷ rows in bucket i* counts
every account's final month as an opportunity that produced no move, which
reads as "stayed".

So a transition exists only where the next month is actually observed. Rows
without one are excluded, never defaulted to anything.

**The bias is directional and worst where it matters.** Measured on this book:

| roll-forward | correct | naive | understated by |
|---|---|---|---|
| current → 1-29 | 8.47% | 8.20% | 3.2% |
| 1-29 → 30-59 | 16.36% | 15.75% | 3.7% |
| 30-59 → 60-89 | 33.93% | 32.60% | 3.9% |
| **60-89 → 90+** | **50.59%** | **48.62%** | **3.9%** |

The understatement grows as the buckets deepen, because accounts in deep
arrears are the most likely to be in their final observed month — they are
about to default, and the series ends. A provision built on the naive matrix
would be systematically light, and light in exactly the segment that drives it.

`test_exactly_one_transition_is_lost_per_account` pins the arithmetic:
transitions must equal fact rows minus accounts, exactly.
`test_the_naive_denominator_is_biased_downward` asserts the wrong
implementation really is wrong, and by a material amount — a suite that only
ever confirms the right answer gives a reader no reason to think the right
answer was hard to get.

### 3.3 The estimate meets the answer key

| transition | observed | truth | gap (SEs) |
|---|---|---|---|
| current → 1-29 | 8.47% | 8.54% | −1.5 |
| 1-29 → current | 62.35% | 62.00% | +1.6 |
| 1-29 → 30-59 | 16.36% | 16.45% | −0.5 |
| 30-59 → 60-89 | 33.93% | 33.91% | +0.0 |
| 60-89 → 90+ | 50.59% | 51.67% | −1.8 |

Every cell within **1.8 standard errors**. The estimate is counted from half a
million observed account-months; the truth is the matrix those months were
drawn from. Two unrelated routes, so agreement is evidence rather than
restatement.

### 3.4 Why the comparison is not against the config base rates

This is the subtle part, and getting it wrong would have looked like an
estimation bug.

The true matrix varies by **segment** (riskier borrowers miss more often) and
by **month on book** (seasoning). So no single true matrix exists for a pooled
estimate to be compared against. What the pooled estimate estimates is the
**exposure-weighted average** of the true matrices, with the weights the data
actually carries — which is what `expected_pooled_matrix` computes, by looking
up the true matrix for every observed opportunity and averaging.

Comparing the pooled estimate against the base rates in `config.yaml` instead
would show a gap of several percentage points. The base rates are what a
near-prime account at the seasoning peak experiences; the book averages
something else. The discrepancy would be real, and it would be a bug **in the
comparison, not in the estimate** — the worst kind, because it sends you
looking in the wrong file.

`test_comparing_against_config_base_rates_would_be_wrong` asserts the two
genuinely differ, so the weighting is doing work rather than being ceremony.

An aside worth keeping: the **cure** rates come out almost exactly on their
base values — 62.35% against a base 62.0%, 27.85% against 28.0%, 12.23% against
12.0% — while the **worsen** rates sit visibly below theirs. That is not a
coincidence, it is §1.6 showing through: the risk multiplier scales worsening
and leaves curing alone. The data reproduces a modelling decision made three
weeks earlier, which is as good a check on both as exists.

### 3.5 The cuts

`roll_rate_segmented` holds the same matrix by segment, by month-on-book band
and by calendar quarter, in one long table keyed on `cut_type` / `cut_value`.
Adding a fourth cut later adds rows, not a schema change.

**By segment** — roll-to-default runs prime 33.9% < near-prime 46.5% <
subprime 73.8%. If the grading did not order these, the segmentation would be
decorative.

**By age** — the miss rate humps: 6.7% in the first six months, peaking at 9.8%
around 12–23 months, falling to 6.4% beyond three years. Exactly the seasoning
curve the generator was given, recovered from the data.

**By quarter** — the stability view, and the one a monitoring pack leads with.
Roll rates drifting quarter on quarter is the earliest visible sign a book is
changing, and it appears well before the default rate moves.

Every row carries its `opportunities` count, because a rate computed on nine
accounts will swing wildly and look like a trend.
