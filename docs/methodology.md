# Methodology: Credit Risk ECL Engine

Derivations and reasoning, one section per phase. I write each section as the
phase lands, so the document and the code cannot drift apart.

---

## 1. The book and its answer key  *(built 23 Oct 2026)*

### 1.1 Why the data is simulated

What this project has to demonstrate is that a calculation is correct, not that
some characteristic predicts default. That decides the data strategy.

A real loan book gives you realism and nothing else that helps here. Nobody
knows its true probability of default, so an engine run over it can only be
compared against somebody else's estimate. A simulated book whose data
generating process is written down gives you the one thing that makes
validation possible: an answer computed by a route the engine never touches.

My [credit default scorecard](https://github.com/esiihle/credit-default-scorecard)
made the opposite call for the opposite reason, and the contrast is worth
keeping in mind. There the finding was the deliverable (does this
characteristic predict default?), so real data was essential, and an earlier
synthetic stand-in produced published figures that were wrong by a factor of
three. Here the mechanism is the deliverable. Simulation is right in one case
and disqualifying in the other, and the skill is knowing which one you are in.

Every table and every report this project writes states that the book is
synthetic.

### 1.2 Delinquency as a Markov chain

Arrears are modelled as a discrete time Markov chain over buckets:

| bucket | meaning |
|---|---|
| 0 | current |
| 1 | 1 to 29 days past due |
| 2 | 30 to 59 days past due |
| 3 | 60 to 89 days past due |
| 4 | 90+ days past due, **default, absorbing** |

Each month an account either falls further behind, cures towards current, or
stays where it is. The 90+ bucket is absorbing, which is this project's default
definition and the Basel and IFRS 9 convention. Cures out of default do happen
in reality and I have left them out deliberately: they complicate Stage 3
accounting, and the job here is the core mechanics.

Each account's matrix $P$ is built from base rates in `config.yaml`, scaled by
its segment's risk multiplier and by a seasoning factor.

### 1.3 Why the chain is the point

Because $P$ is known, the truth follows exactly:

$$\Pr(\text{default within } k \text{ months} \mid \text{bucket } s) = (P^k)[s, 4]$$

Seasoning makes $P$ age dependent, so it is a product of matrices rather than a
single power, but the principle holds. No simulation is needed for the truth,
only for the data the engine will see.

The engine later estimates 12 month and lifetime PD from observed transitions.
`cumulative_pd_curve` already knows the answer.
`test_simulated_defaults_match_the_analytic_truth` asserts that random paths
through the chain reproduce the matrix algebra to within the binomial standard
error. Those are two genuinely independent routes to the same quantity, which
is what makes agreement evidence rather than a restatement.

### 1.4 Seasoning

Default risk on a term loan is low immediately after origination, since the
borrower has just been underwritten and has just received the money. It rises
to a peak in the first couple of years, then falls as the survivors reveal
themselves to be good payers. I model it as a bump $x e^{1-x}$ peaking at
`seasoning_peak_month`.

Without it every vintage would look identical and the vintage analysis in
Phase 7 would have nothing to show.

### 1.5 Exposure is arithmetic

A fixed term amortising loan's balance is deterministic:

$$\text{instalment} = \frac{P i}{1 - (1+i)^{-n}}, \qquad
\text{balance}_t = P\,\frac{(1+i)^n - (1+i)^t}{(1+i)^n - 1}$$

So EAD at any future month is exact, and every exposure the engine produces can
be checked to the cent. This is part of why I chose a term product over a
revolving one. On a credit card, exposure at default depends on the undrawn
commitment and a credit conversion factor, and "lifetime" is genuinely
ambiguous. Those are real problems worth solving later, not while establishing
the mechanics.

### 1.6 Calibration, and a modelling choice that had to change

I calibrated the base rates so the generated book resembles a South African
unsecured personal loan portfolio: roughly 5% 12 month PD and 25% over a 36
month life, with prime near 2%, near prime 4 to 5% and subprime 13 to 14%.

The first version scaled cure rates by the risk multiplier as well as worsen
rates, so riskier borrowers both fell behind more often and recovered less
often. The effect compounded across four buckets into a **135x difference**
between prime and subprime default rates. No real book looks like that.

The modelling statement is now explicit, in config and in code: the risk
multiplier scales how often an account falls behind, and conditional on being
30 days down, cure rates are shared. It is a simplification, and I would rather
state it than bury it. `test_riskier_segments_default_more` pins the spread
between 3x and 20x, so the old behaviour cannot come back unnoticed.

### 1.7 Two performance bugs

Neither changed a number, but both would have made the project unpleasant to
work on. and that is its own kind of defect. A generator nobody wants to rerun
is a generator whose parameters stop being questioned.

* **The ground truth was quadratic.** `marginal_pd_schedule` called a fixed
  horizon PD routine for every month, and each call walked the chain from the
  start. That is O(T²) matrix builds per account, several million across the
  book. Computing the whole cumulative curve in one forward pass and taking
  differences made it linear.
* **`balance_at` was scalar only**, which forced a Python loop over every
  account for every month. Vectorising it, together with memoising the
  transition matrices (there are only three risk multipliers), took full book
  generation from **205 seconds to 12**.

### 1.8 A near miss on `.gitignore`

I wrote `*.duckdb` into `.gitignore` to keep the working warehouse out of
version control. It would also have excluded `data/sample/sample_book.duckdb`,
which is committed on purpose so that a fresh clone runs immediately. That is
the exact failure my scorecard project had with `data/*`, where a clean clone
crashed and two tests failed.

Caught before the first commit, by a test that **asks git** rather than reading
`.gitignore` and reasoning about it. String matching the rules file would not
have caught an extension based rule. `git check-ignore` is the only authority
on what git actually ignores.


---

## 2. The data mart  *(built 26 Oct 2026)*

### 2.1 Why a mart at all

The generator already produces a tidy table of account months. Modelling it
into dimensions and a fact adds work, so it needs a reason.

The reason is that every later phase asks the same questions of the same data.
Group by segment, by vintage, by quarter. Compare a month to the one before.
Those questions are about the calendar and the account, not about the loan
month observation. Defining them once, in a dimension, means every report
agrees. Deriving them per query is how two reports come to disagree about which
quarter December belongs to, and then nobody can say which provision is right.

### 2.2 The grain, stated once

> **One row per account per calendar month end on which the account was on
> book.**

Everything else follows from that sentence, which is why it sits at the top of
`sql/03_fact_loan_month.sql` and is enforced by a test rather than left to be
inferred.

This is a **periodic snapshot** fact table, not a transaction fact:

* a transaction fact records events, such as a payment or a drawdown, and a row
  exists only when something happened
* a periodic snapshot records state at a regular interval, whether or not
  anything happened

Provisioning needs the second. The question is always what the book looked like
at month end, never what happened in between. The practical consequence is
sharp: a gap in an account's monthly series is a defect rather than an absence
of events, and `04_mart_quality.sql` looks for exactly that.

### 2.3 Additivity, the thing most often got wrong

A measure's additivity is a property of the measure, not of the query, and
getting it wrong produces a confident number with no meaning.

| measure | across accounts | across time |
|---|---|---|
| `scheduled_balance` | additive, this is the book's exposure | **not additive** |
| `is_default` | additive, accounts in default now | **not additive** |
| `is_new_default` | additive | additive |

`scheduled_balance` is **semi additive**. Summing it across accounts at one
month end gives exposure. Summing it across months gives a number that means
nothing: an account owing 10,000 for twelve months did not owe 120,000.

`is_default` has the same shape for a different reason. It counts the same
default again every month it persists, so a sum across time answers how many
account months were spent in default, which is a legitimate question but not
the one anybody asked. `is_new_default`, computed with a `LAG` window, fires
once per defaulted account and is the measure a default rate report wants.

These are in column comments, and `test_balance_is_semi_additive` and
`test_is_new_default_fires_exactly_once_per_defaulted_account` pin them.

### 2.4 A slowly changing dimension, and why type 1 is right here

`dim_account` is type 1: attributes are overwritten and no history is kept.
That is correct **because every attribute in it is fixed at origination**: the
principal advanced, the contractual term, the rate, the segment scored at
application.

It would be wrong the moment the book gained an attribute that changes, such as
a refreshed behavioural score or a repriced rate. Those belong in the fact
table if they change monthly, or in a type 2 dimension with effective dates.
Type 1 on a changing attribute silently rewrites history, and a provision
restated because a dimension was overwritten is a finding an auditor does not
forget.

I recorded the choice in the file rather than leaving it implicit, because the
next person to add a column needs to know which rule they are working under.

### 2.5 A calendar that drifts

Building month ends by repeatedly adding one month to 31 January lands on 28
February, and then stays on the 28th for the rest of the series. The drift is
silent, and every join from the fact table to the calendar would start missing.

The fix is to generate month starts and take `last_day()` of each, which cannot
drift. `test_dim_date_is_month_ends_with_no_drift` asserts the result rather
than the method, so a future rewrite is held to the same property.

`month_index`, which is `year * 12 + month`, is carried as a column so that
spans in months are a subtraction. Months on book, vintage offsets and maturity
arithmetic all reduce to integer differences, with no date functions and no
leap year thinking.

### 2.6 Quality checks as SQL, and the question that makes them worth writing

The eight checks live in `sql/04_mart_quality.sql` rather than in Python,
because they are assertions about the warehouse. A credit risk reviewer should
be able to read and rerun them without a Python environment, and Phase 8 ports
them to SAS alongside everything else.

Each returns a failure count:

| check | the bug it exists to catch |
|---|---|
| grain is unique | a duplicated join silently doubles every sum built on the fact |
| no orphaned accounts | a fact row with no dimension row vanishes from any inner joined report |
| no orphaned dates | as above, through the calendar |
| no gaps in monthly series | the defining property of a periodic snapshot |
| balance never increases | an amortising loan's balance cannot rise |
| DPD agrees with bucket | two columns that must tell the same story |
| rows within account life | nothing before origination or after maturity |
| default is absorbing | a cure out of 90+ would contradict the model |

The test that makes these worth having is not that they pass. It is
`test_every_quality_check_can_actually_fail`, which builds eight separate
marts, breaks each one in exactly the way one check exists to catch
(duplicating rows, deleting a dimension row, removing a month, multiplying a
balance by ten) and asserts that the matching check notices.

A check with a typo in its predicate passes on every book ever written. It is
indistinguishable from a check that works, right up until the day it was
supposed to catch something. The only way to tell them apart is to break the
data on purpose.

This is the same argument as the simulation study in my football model, which
exists to show that a backtester reporting "no edge" would have reported one
had there been an edge to find. A control that has never been observed to fire
is not yet evidence of anything.

### 2.7 Stopping on failure

`run_pipeline.py` halts if any quality check fails, and says so. It does not
warn and continue.

That is deliberate, and it is a lesson I have now learned three times across
these projects. A mart that fails its own checks produces numbers that look
exactly like good ones. Every later stage would inherit the defect and report
confidently. My football project shipped sample built charts to its front page
because the guard was a warning rather than a refusal. Here the guard refuses.

### 2.8 A test that failed for the wrong reason

Running the Phase 0 suite from an unzipped folder, before `git init`, produced:

```
FAILED test_git_ignores_the_working_data_but_not_the_committed_sample
AssertionError: the working warehouse should not be version-controlled, it is...
```

The warehouse was fine. `git check-ignore` exits **128** outside a work tree,
which is neither "ignored" nor "not ignored", and the test treated anything
other than 0 as "not ignored". So the absence of a repository was reported as a
`.gitignore` defect.

The test now asks `git rev-parse --is-inside-work-tree` first and skips with an
explanation, and treats any exit code other than 0 or 1 as a failure of the
test rather than of the thing under test.

I record it because the failure mode generalises well past git. **A check that
cannot distinguish "the answer is no" from "I could not determine the answer"
will eventually report the second as the first.** The same confusion turned up
in my football project, where an optimiser returned `success: True` while
failing to converge, and in the scorecard, where perfect separation showed up
as a huge standard error rather than as an error. In each case the system had a
way to say "I don't know" and used a confident value instead.


---

## 3. Delinquency transitions and roll rates  *(built 27 Oct 2026)*

The first phase in which the engine estimates something. Everything before this
was plumbing. From here every number has a true value sitting behind it.

### 3.1 What a roll rate is

The probability that an account in one arrears bucket is in another a month
later. Collected across all buckets it is a transition matrix, and it is the
most used artefact in retail credit MI. It drives the PD term structure in
Phase 4, it is the first place a deteriorating book shows up, and it is what a
collections team gets managed against.

### 3.2 The censoring trap

The obvious implementation takes `LEAD(arrears_bucket)` and calls it the
destination. That mishandles the last row of every account, and the last row of
every account is never a random sample:

* accounts that **defaulted** end on their default row. The move into default
  is observed from the row before, and there is no move out, because default is
  absorbing.
* accounts still on book at the reporting date are **right censored**. Their
  next month exists in reality; we just have not seen it.
* accounts that **matured** simply stop.

`LEAD` returns NULL in all three cases. The damage is done not by the NULL but
by the denominator. Computing a rate as moves to j divided by rows in bucket i
counts every account's final month as an opportunity that produced no move,
which reads as "stayed".

So a transition exists only where the next month is actually observed. Rows
without one are excluded, never defaulted to anything.

The bias is directional, and it is worst where it matters. Measured on this
book:

| roll forward | correct | naive | understated by |
|---|---|---|---|
| current to 1-29 | 8.47% | 8.20% | 3.2% |
| 1-29 to 30-59 | 16.36% | 15.75% | 3.7% |
| 30-59 to 60-89 | 33.93% | 32.60% | 3.9% |
| **60-89 to 90+** | **50.59%** | **48.62%** | **3.9%** |

The understatement grows as the buckets deepen, because accounts in deep
arrears are the most likely to be in their final observed month. They are about
to default, and the series ends. A provision built on the naive matrix would be
systematically light, and light in exactly the segment that drives it.

`test_exactly_one_transition_is_lost_per_account` pins the arithmetic:
transitions must equal fact rows minus accounts, exactly.
`test_the_naive_denominator_is_biased_downward` asserts that the wrong
implementation really is wrong, and by a material amount. A suite that only
ever confirms the right answer gives a reader no reason to think the right
answer was hard to get.

### 3.3 The estimate meets the answer key

| transition | observed | truth | gap (SEs) |
|---|---|---|---|
| current to 1-29 | 8.47% | 8.54% | -1.5 |
| 1-29 to current | 62.35% | 62.00% | +1.6 |
| 1-29 to 30-59 | 16.36% | 16.45% | -0.5 |
| 30-59 to 60-89 | 33.93% | 33.91% | +0.0 |
| 60-89 to 90+ | 50.59% | 51.67% | -1.8 |

Every cell lands within **1.8 standard errors**. The estimate is counted from
half a million observed account months; the truth is the matrix those months
were drawn from. Two unrelated routes, so agreement is evidence rather than
restatement.

### 3.4 Why the comparison is not against the config base rates

This is the subtle part, and getting it wrong would have looked like an
estimation bug.

The true matrix varies by segment, because riskier borrowers miss more often,
and by month on book, because of seasoning. So no single true matrix exists for
a pooled estimate to be compared against. What the pooled estimate actually
estimates is the exposure weighted average of the true matrices, with the
weights the data happens to carry. That is what `expected_pooled_matrix`
computes, by looking up the true matrix for every observed opportunity and
averaging.

Comparing the pooled estimate against the base rates in `config.yaml` instead
would show a gap of several percentage points. The base rates are what a near
prime account at the seasoning peak experiences, and the book averages
something else. The discrepancy would be real, and it would be a bug in the
comparison rather than in the estimate, which is the worst kind, because it
sends you looking in the wrong file.

`test_comparing_against_config_base_rates_would_be_wrong` asserts that the two
genuinely differ, so the weighting is doing work rather than being ceremony.

One aside worth keeping. The cure rates come out almost exactly on their base
values (62.35% against a base of 62.0%, 27.85% against 28.0%, 12.23% against
12.0%) while the worsen rates sit visibly below theirs. That is not a
coincidence, it is §1.6 showing through: the risk multiplier scales worsening
and leaves curing alone. The data reproduces a modelling decision I made three
weeks earlier, which is about as good a check on both as exists.

### 3.5 The cuts

`roll_rate_segmented` holds the same matrix by segment, by month on book band
and by calendar quarter, in one long table keyed on `cut_type` and `cut_value`.
Adding a fourth cut later adds rows rather than a schema change.

**By segment.** Roll to default runs prime 33.9%, near prime 46.5%, subprime
73.8%. If the grading did not order these, the segmentation would be
decorative.

**By age.** The miss rate humps: 6.7% in the first six months, peaking at 9.8%
around 12 to 23 months, falling to 6.4% beyond three years. That is exactly the
seasoning curve the generator was given, recovered from the data.

**By quarter.** The stability view, and the one a monitoring pack leads with.
Roll rates drifting quarter on quarter is the earliest visible sign a book is
changing, and it shows up well before the default rate moves.

Every row carries its `opportunities` count, because a rate computed on nine
accounts will swing wildly and look like a trend.


---

## 4. IFRS 9 staging  *(built 29 Oct 2026)*

The accounting judgement of the project.

| stage | meaning | loss horizon |
|---|---|---|
| 1 | performing | **12 months** |
| 2 | significant increase in credit risk | **lifetime** |
| 3 | credit impaired | **lifetime** |

The jump from Stage 1 to Stage 2 replaces a twelve month horizon with the
remaining life of the loan. On a 48 month loan that can multiply the provision
several times over for an account whose circumstances have barely changed.
Staging moves the provision more than the PD model does, which is why every
rule lives in `config.yaml` and every trigger is recorded by name against the
account it staged.

### 4.1 A PD curve as a recursive CTE

Staging needs a lifetime PD, and Phase 2 produced a transition matrix. The
bridge is matrix exponentiation: propagate a state vector forward month by
month, and the mass accumulated in the default bucket after *k* steps is
exactly P(default within *k* months).

`sql/08_pd_curve.sql` does this in SQL with a recursive CTE, carrying the state
vector as five columns and multiplying by the matrix at each step. It is the
empirical counterpart of `generate.cumulative_pd_curve`, which does the same
thing in Python from the matrix the book was generated from, and
`test_the_sql_curve_matches_a_python_matrix_power` holds the two together.

There are two traps here, and both produce plausible numbers rather than
errors.

#### Trap 1: DECIMAL truncation in the anchor

Writing `1.0 AS p0` gives `DECIMAL(2,1)`. One decimal place. The type
propagates through every iteration, so each step rounds to a tenth and the
state vector freezes:

| horizon | `1.0 AS p0` | `1.0::DOUBLE AS p0` |
|---|---|---|
| 0 | 1.0 | 1.0 |
| 1 | 0.9 | 0.9 |
| 2 | **0.9** | 0.87 |
| 3 | **0.9** | 0.861 |
| 4 | **0.9** | 0.8583 |

No error, no warning, just a column of entirely valid looking probabilities
that is completely wrong. Had this gone unnoticed, every lifetime PD in the
project would have been built on a frozen vector, and the provision with it.

So there is a `::DOUBLE` on every literal in the anchor.
`test_decimal_truncation_in_the_anchor_would_break_the_curve` asserts that the
untyped version really does freeze, so the casts cannot be tidied away by
someone who takes them for noise.

#### Trap 2: the absorbing row is absent from the data

`roll_rate` is built from observed transitions, and nothing is ever observed
leaving default. Pivoting it therefore yields NULL for the entire bucket 4 row,
including its diagonal.

Coalescing those NULLs to zero, which is the obvious default, means an account
that defaults gets multiplied by a row of zeros at the next step and its
probability mass vanishes. Cumulative PD would stop rising, and a PD curve that
plateaus looks reasonable enough to ship.

The absorbing row has to be supplied as the identity row. That is a modelling
fact rather than a data fact, which is exactly why it has to be written down
rather than inferred from a table that cannot contain it.
`test_probability_is_conserved_at_every_horizon` checks that the total stays at
1.0.

### 4.2 The triggers

**Stage 3** is `days_past_due >= 90`, the Basel and IFRS 9 default definition,
stated in config rather than assumed in code.

**Stage 2** has three ways in, any one of which is sufficient:

1. **The 30 day backstop.** IFRS 9 carries a rebuttable presumption that credit
   risk has increased significantly once an account is 30 days past due. It is
   a backstop by design: waiting for arrears defeats the purpose of a forward
   looking standard.
2. **The relative PD test**, comparing lifetime PD now against lifetime PD at
   origination. Relative rather than absolute, because a move from 2% to 6% is
   a tripling for a prime borrower while 6% is unremarkable for a subprime one.
   An absolute threshold would stage most of the subprime book on day one and
   never catch a deteriorating prime account.
3. **An absolute floor on that test.** Without it, 0.1% to 0.3% trips the
   doubling test and a very safe portfolio migrates on noise. The floor says a
   relative increase only counts once the level is material.

Both sides of the relative test are computed from the same curve. Comparing
today's curve against a PD stored at origination by last year's model would
measure model change rather than credit deterioration, and that distinction has
caused real restatements.

### 4.3 Probation

An account that stops triggering does not return to Stage 1 immediately. It has
to behave for `staging.probation_months` first.

Without probation, an account hovering around 30 days past due oscillates
between Stage 1 and Stage 2 every month, and the provision swings by the whole
difference between a twelve month and a lifetime loss each time. That
volatility is an artefact of the rule rather than a change in credit risk, and
it is impossible to explain to an audit committee because there is nothing to
explain.

It is implemented as a rolling `MAX` over a bounded window frame, so the entire
probation rule is one window function.

### 4.4 The finding: which trigger actually does the work

A trigger that never fires on its own is not a control, it is a line in a
policy document. Counting the account months where each trigger fired and no
stronger trigger already had:

| trigger | account months where it was the reason |
|---|---|
| 30 day backstop | 23,737 |
| relative PD test | **1,132** |

And every one of those 1,132 is a 12 month loan. Not one on 24, 36, 48 or 60
months.

The reason is structural. A relative test needs a small denominator. Lifetime
PD at origination grows with the term, from 5.9% over 12 months to 38% over 60,
so the same proportional deterioration clears a doubling on a short loan and
cannot come close on a long one. Meanwhile PD is a monotone function of the
arrears bucket, so by the time an account's PD has doubled it is usually
already 30 days down and caught by the backstop anyway.

The consequence is worth stating plainly: as configured, the relative PD test
is nearly redundant. It contributes 4.5% as many Stage 2 months as the
backstop, concentrated entirely at the short end of the book.

What a real implementation does about this is use a risk driver that is not a
function of arrears: a refreshed behavioural score, a bureau pull, a
macroeconomic overlay. A SICR test built only on arrears data can never be
genuinely forward looking, because arrears are the thing it is supposed to
anticipate. This book has no such variable, and saying so is more useful than
quietly reporting a staging split that looks fine.

`test_the_relative_pd_trigger_binds_only_on_short_dated_loans` pins the
finding, so a future change that makes the trigger fire across all terms gets
noticed rather than absorbed.

### 4.5 Where the book sits

| stage | accounts | exposure | share |
|---|---|---|---|
| 1 | 7,907 | 126,289,603 | 92.3% |
| 2 | 598 | 9,572,408 | 7.0% |
| 3 | 63 | 939,053 | 0.7% |

Month on month migration, account weighted:

| | to Stage 1 | to Stage 2 | to Stage 3 |
|---|---|---|---|
| from Stage 1 | 98.31% | 1.69% | 0.00% |
| from Stage 2 | 9.92% | 79.54% | 10.54% |
| from Stage 3 | n/a | n/a | n/a |

Stage 2 is a genuine waypoint rather than a ratchet: roughly one in ten cures
back to Stage 1 each month and roughly one in ten deteriorates into Stage 3.
The Stage 3 row is empty because default is absorbing in this model, so no
Stage 3 account has a next month. That is the same reason the 90+ row of the
transition matrix is empty, and it is not missing data in either case.


---

## 5. The PD term structure  *(built 31 Oct 2026)*

Phase 3 produced one pooled PD curve, which was enough to drive a staging rule.
It is not enough to price a provision. A single curve overstates risk for a
prime account, understates it for a subprime one, and ignores seasoning
entirely.

This phase builds the curve three ways and measures all three against the
answer key.

### 5.1 Three methods, kept side by side

| method | matrix used |
|---|---|
| `pooled` | one for the whole book (Phase 3's curve) |
| `segmented` | one per segment |
| `age_aware` | one per segment and age band, changing as the walk advances |

All three are carried in `account_pd` rather than collapsing to the best one.
Asking whether the extra segmentation is worth it is a question about the
difference between them, and a table holding only the chosen answer cannot be
used to ask it.

The age aware walk is the one that needed new machinery. A matrix power assumes
the matrix is constant, and here it is not, because an account that is 10
months old today faces the 12 to 23 month hazard a year from now. So the
recursive term in `sql/12_pd_term_structure.sql` joins to the matrix for the
account's age at that step rather than at the start of the walk.

### 5.2 The result

12 month PD, 8,256 accounts:

| method | mean estimate | truth | bias | mean abs error | correlation |
|---|---|---|---|---|---|
| pooled | 8.81% | 7.79% | +13.1% | 0.0359 | 0.944 |
| segmented | 7.09% | 7.79% | -9.0% | 0.0165 | 0.987 |
| **age_aware** | **7.75%** | 7.79% | **-0.5%** | **0.0039** | **0.999** |

Lifetime PD:

| method | mean estimate | truth | bias | mean abs error |
|---|---|---|---|---|
| pooled | 15.38% | 11.53% | +33.4% | 0.0690 |
| segmented | 13.72% | 11.53% | +19.0% | 0.0313 |
| **age_aware** | **11.80%** | 11.53% | **+2.3%** | **0.0072** |

The age aware curve is **9.2x more accurate per account** than the pooled one
on a 12 month horizon, and 9.5x on lifetime. Each refinement earns its place,
which is the only justification for the extra complexity.

### 5.3 Why pooling fails, and why an aggregate check would miss it

Pooled error by risk grade:

| segment | estimate | truth | error |
|---|---|---|---|
| prime | 7.65% | 4.04% | **+3.61%** |
| near prime | 8.91% | 7.85% | +1.06% |
| subprime | 12.56% | 20.70% | **-8.14%** |

The errors have opposite signs, so they partly cancel in the book total. The
pooled method's aggregate bias is +1.0 percentage points while its per account
error is 3.6, and a provision is built account by account rather than from the
headline.

That gap is the argument against validating a model on aggregates alone, and
`test_the_pooled_error_cancels_in_aggregate_but_not_per_account` asserts it
directly: the per account error must be more than twice the aggregate bias.

Pooled error by account age:

| age band | estimate | truth | error |
|---|---|---|---|
| 12 to 23m | 9.85% | 10.59% | -0.75% |
| 24 to 35m | 8.99% | 7.11% | +1.88% |
| 36m+ | 6.86% | 3.84% | +3.02% |

This is not noise around zero. It tracks the seasoning curve, because a method
blind to age has to overstate risk where the true hazard is low and understate
it where the hazard peaks. A systematic error with a known cause is worth
fixing; random error would only be worth more data.

### 5.4 The thin cell fallback

Cutting by segment and by age multiplies the number of cells, and the deep
arrears cells are thinly populated, because most accounts never reach 60 to 89
days past due. A cell estimated on a handful of transitions is not an estimate.

So `min_transitions_per_cell: 50`. Below that, the cell falls back to the
segment's rate pooled over all ages. Without it, a single stray transition in a
thin cell becomes a 100% roll rate, and the recursive walk compounds that into
a lifetime PD of 1 for every account in that segment and age band.

On the full book the fallback fires three times, all in the 60 to 89 bucket. A
test checks that the flag agrees with the threshold and, which is the part that
is easy to get wrong, that rows still sum to 1 after the mixture, since
blending own cell rates with pooled ones is not automatically normalised.

### 5.5 The bug this phase found in the answer key

Comparing per account PD across the whole book, all three methods came out at
roughly twice the truth. The engine was not wrong. The key was.

`ground_truth` computed every account's true PD as though the account were
performing, with a Phase 0 note saying the comparison would be like for like on
performing accounts. That held until PD was compared per account across the
whole book, at which point an account 60 to 89 days past due appeared in the
key at 5.4% against a real 84%.

Restricted to accounts actually current, the age aware estimate was 3.611%
against a true 3.648%, so it had been right all along.

> An answer key that is valid for only part of the book is worse than no answer
> key, because the part it is invalid for is exactly where the numbers are
> largest. A key that is wrong everywhere gets noticed. One that is wrong on
> 16% of accounts, in the direction of understating them, looks like a model
> defect.

`ground_truth` now takes the performance frame and uses each account's actual
bucket at the reporting date. The old behaviour is still reachable and sets
`truth_assumes_performing`, so a consumer can tell which kind of key it holds.

### 5.6 A threshold that was mis-specified, and changed on the record

The lifetime PD check failed at 0.0131 against the absolute 0.010 limit on an
8,000 account book, while passing at 0.0076 on the full 20,000.

Measured across book sizes:

| accounts | 12 month MAE | lifetime MAE | 12 month relative | lifetime relative |
|---|---|---|---|---|
| 4,000 | 0.0068 | 0.0116 | 7.8% | 9.3% |
| 8,000 | 0.0054 | 0.0131 | 7.0% | 11.5% |
| 20,000 | 0.0037 | 0.0076 | 4.5% | 6.4% |

The lifetime absolute error runs about twice the 12 month error at every size,
while the relative errors are comparable. The reason is structural: a lifetime
PD over 60 months is the accumulation of 60 monthly estimates, so its scale,
and the absolute error with it, grows with the horizon. Holding a 60 month
cumulative to the same absolute tolerance as a 12 month one was silently
holding it to a far tighter relative standard.

So the lifetime limit is now expressed relatively, as
`max_lifetime_pd_relative_error: 0.15`.

My own rule on this project is that a limit which moves to accommodate a result
was never a limit, so the distinction matters and I am recording it rather than
glossing over it. The form of the tolerance changed for a structural reason,
and it changed after a result was seen. Both halves of that are true. The 12
month absolute limit is untouched, the reason is written in `config.yaml` next
to the number, and anyone who disagrees can see exactly what was done and when.


---

## 6. Loss given default and exposure at default  *(built 5 Nov 2026)*

ECL is `PD × LGD × EAD`, discounted. Phase 4 produced the PD. This phase
produces the other two, and they are not alike at all.

| | EAD | LGD |
|---|---|---|
| nature | contractual arithmetic | statistical estimate |
| source | the amortisation schedule | a few thousand resolved defaults |
| failure mode available | a transcription error | bias, and sampling noise |
| tolerance | to the cent | 2pp, and within 3 standard errors |

That asymmetry is the whole content of the phase, and I set it up back in Phase
0 by choosing an unsecured term product. On a term loan the borrower cannot
increase their exposure: the schedule is fixed and the balance only falls. On a
credit card or an overdraft, EAD becomes the hard input, because it depends on
how much of an undrawn limit a distressed borrower draws before defaulting,
which needs a credit conversion factor, which is a second model with its own
data problem. One genuinely uncertain input is enough to demonstrate the
machinery. Two would obscure which one was driving the error.

### 6.1 Exposure at default

`sql/15_ead_schedule.sql` projects, for every live account and every month to
maturity, the balance the account would carry if it defaulted then:

```
i         = (1 + annual_rate)^(1/12) - 1
balance_t = P × ((1+i)^n - (1+i)^t) / ((1+i)^n - 1)
```

I wrote it in SQL rather than carrying it over from the Python generator, so
the whole exposure calculation is reviewable in one place and ports to SAS in
Phase 8. It produced **158,696 account months** on the full book, projected to
the longest remaining term of 48 months.

The same monthly rate serves twice: it builds the schedule and it discounts the
loss off it. IFRS 9 requires discounting at the asset's effective interest
rate, so this is a policy choice that looks like an implementation detail.
Using one rate for the schedule and another for the discount would be a
plausible looking error with no visible symptom, which is why there is a test
asserting they are the same rate.

Validation is exact. The SQL and `generate.balance_at` implement the same
closed form formula in two languages and share no code, so agreement is
evidence of correct transcription, and there is no sampling noise in arithmetic
to excuse a gap:

```
account-months projected        158,696
max difference vs formula      0.000000
rows differing by > 1c                0
```

Two structural properties are also asserted: the balance never rises, and it
reaches zero at maturity. Both are properties a revolving product would not
have, and if either failed the amortisation would be wrong in a way that
changes lifetime ECL for every account on the book.

### 6.2 LGD and the workout censoring trap

LGD is estimated from what a bank actually has: a set of defaulted accounts,
the exposure at the moment each defaulted, and however much has been collected
so far. The generator produces exactly that in `recovery`, one row per default
event, and never exposes `true_lgd` to the engine.

The naive estimate is the obvious one:

```
observed loss = 1 - (recovered so far) / (exposure at default)
```

averaged over all defaults. It is wrong, and it is wrong in a direction you can
work out before measuring anything. Collections on an unsecured default runs
for months, twelve in this configuration. An account that defaulted last month
has barely started; one that defaulted three years ago is finished. Both sit in
the same table, and for the unresolved account that figure is not its LGD. It
is its LGD plus however much is still to come.

Measured on the full book:

| | defaults | mean loss |
|---|---|---|
| resolved workouts | 2,288 | **65.10%** |
| unresolved workouts | 959 | 77.05% |
| all defaults (naive) | 3,247 | 68.63% |
| **truth** | | **65.1%** |

The estimate from resolved workouts only lands within **0.19pp** of the truth,
at 0.37 standard errors. The naive estimate overstates it by **3.53pp**, and
the unresolved cases viewed alone sit roughly twelve points above the resolved
ones. Per segment:

| segment | resolved | unresolved | LGD (resolved) | LGD (naive) | true | bias |
|---|---|---|---|---|---|---|
| prime | 475 | 232 | 65.08% | 69.10% | 65.16% | +4.01pp |
| near_prime | 793 | 326 | 65.35% | 68.84% | 65.16% | +3.49pp |
| subprime | 1,020 | 401 | 64.91% | 68.23% | 64.94% | +3.32pp |

This is the same shape as the Phase 2 roll rate trap by a different mechanism.
There the bias came from the end of an account's series; here it comes from the
beginning of a workout. In both cases the naive denominator includes
observations whose outcome is not yet known, and in both cases the bias has a
sign that follows from the mechanism rather than from the data.

So the estimate uses resolved workouts only. The cost is real: excluding
unresolved cases throws away 29.5% of the defaults, and on a small book that is
the difference between an estimate and a guess. That is the trade, and I think
it is the right side of it. An estimate built on fewer resolved cases is noisy,
one built on more unresolved cases is wrong, and noise is the error that
shrinks when the book grows.

The test suite asserts that the bias exists as firmly as it asserts the
estimate is accurate. An engine that failed to reproduce a known bias would be
demonstrating nothing, and a test that only checked the right answer would pass
on data where the mechanism had quietly stopped working.

### 6.3 LGD is reported with a standard error

Unlike PD, which rests on half a million observed transitions, LGD rests on a
few hundred resolved defaults per segment. The standard error is therefore
worth quoting, roughly 0.5pp on the full book, and the engine quotes it. A
point estimate presented without one invites more confidence than the data
supports, and on an input that multiplies directly into the provision that is
not a presentational matter.

Realised losses are clipped to `[0, 1]`, and the count of clipped cases is
reported rather than hidden. A recovery exceeding the exposure gives a negative
loss, and collection costs can push a loss above 100%. Neither is an error, but
an LGD outside `[0, 1]` breaks the arithmetic downstream.

### 6.4 "Too imprecise to judge" is not "wrong", and this is the fourth time

I set the Phase 5 gates from sampling theory before any estimate existed: 2pp
absolute, which is the materiality question (would a provision built on this be
meaningfully wrong?), and 3 standard errors, which is the statistical question
(is the gap bigger than noise explains?).

The committed 400 account sample passed the absolute gate at 0.0189 against
0.0200. That is a pass, and it was worth nothing. Measured across six seeds at
that book size:

| seed | resolved per segment | max abs error | max standard error | error in SEs | absolute gate |
|---|---|---|---|---|---|
| 42 | 10 | 0.0189 | 0.0439 | 0.43 | pass |
| 101 | 11 | 0.0243 | 0.0395 | 0.74 | **fail** |
| 7 | 11 | 0.0493 | 0.0274 | 2.09 | **fail** |
| 2024 | 10 | 0.0608 | 0.0586 | 2.18 | **fail** |
| 555 | 9 | 0.0491 | 0.0501 | 0.98 | **fail** |
| 88 | 10 | 0.0478 | 0.0515 | 1.36 | **fail** |

One pass and five failures, while the standard error gate passed on all six,
worst case 2.18 against its limit of 3.0. With around 10 resolved defaults per
segment the standard error is near 4pp, so an estimate lands more than 2pp from
the truth roughly half the time by chance alone. CI runs on that sample, so CI
was green on the luck of the seed and one regeneration away from red with
nothing broken.

A 2pp materiality limit only means something if the estimate is precise enough
for a 2pp gap to be distinguishable from noise. So the absolute gate is now
applied only when the worst segment standard error is at most half the
tolerance, which is to say when the estimate is at least twice as precise as
the limit it is held to. Below that it reports `not assessed`, which is a
different statement from `fail`. The standard error gate always applies,
because an error expressed in standard errors already accounts for the sample
size, and it is the gate that actually guards Phase 5 in CI.

Two things make this an honest exemption rather than an escape hatch, and both
are enforced by tests rather than by intention:

1. **The decision is made from the input, before the result exists.**
   `validate()` computes the precision test from the standard error, which is a
   property of the sample, and the accuracy figures only afterwards. The
   ordering is the control, so a test reads the function's own source and
   asserts that the error is not computed first. It failed on my first version
   of the code, where the assessability block sat below the error calculation.
2. **A book precise enough really is judged.** An exemption that applied at
   every size would be a gate that never fires, which is indistinguishable from
   having no gate. So the other half is asserted too: at 14,000 accounts the
   gate is assessable and the engine is held to it.

This is the fourth instance of one error on this project:

> A check that cannot distinguish "the answer is no" from "I could not
> determine the answer" will eventually report the second as the first.

The others were `git check-ignore` exiting 128 outside a work tree, read as a
file not being ignored (Phase 0); the Phase 4 PD accuracy gate failing a 400
account sample where 60% of matrix cells had fallen back, so the age aware
model was never estimated at all; and, in my scorecard project, an optimiser
reporting `success: True` while failing to converge. The remedy has been the
same every time, which is to make the engine say which of the two it found, and
the fact that it keeps turning up in new forms is why I write it down rather
than trust myself to remember it.

### 6.5 Three smaller defects found while wiring the phase

I record these because each one is a control that was wrong rather than a
feature that was missing.

**The loader bound every parameter to every SQL file.** Phase 5 runs two files
in one step and only the second takes an argument, so passing both the same
arguments made DuckDB reject the first with a count mismatch. `run_sql_file`
now binds only the parameters a file actually references, matched outside
comments, because these files carry a great deal of commentary and a `$name`
mentioned in prose must not be bound as though it were used.

**The stage status display could disagree with the run it described.** The
summary table was keyed by stage name, so it had to spell every stage
identically to the `STAGES` list. The first version said `"LGD & EAD"` against
the list's `"LGD and EAD"`, which would have printed `NOT YET BUILT` for a
stage that had just run and passed. It is keyed by phase number now. A status
display that can contradict the run is worse than none, because it is believed.

**A missing table and an empty table are different failures.** The recovery
loader originally skipped the write when there were no defaults, which left the
table absent rather than empty, and the SQL then died with a catalog error
instead of returning zero rows. A book small enough to produce no defaults has
no recoveries. and that is correct. The table is now always created, which
needed a typed empty frame since an untyped one gives DuckDB no schema to
infer, and `write_table`'s refusal to write empty tables is now an explicit opt
out used only where emptiness is a legitimate state, with a test that the
refusal still holds everywhere else.

### 6.6 What Phase 5 would look like on a real book

Three things would change, and all three make it harder:

- **The workout period would have to be estimated**, not configured. Twelve
  months is an assumption here. On a real portfolio you would measure the
  recovery curve and decide where it flattens, and that choice would move the
  LGD estimate.
- **Recoveries would be discounted.** Cash collected in month 30 is worth less
  than cash collected in month 3, and IFRS 9 LGD is a present value measure. I
  left it out here because it would obscure the censoring mechanism the phase
  is built to show, and because adding it is arithmetic rather than a modelling
  question.
- **LGD would be modelled, not averaged.** A segment average is the right
  starting point and the wrong finishing point. Real LGD varies with the loan's
  size, its age at default, the collections channel and the macro environment.
  The WOE and logistic machinery from my scorecard project applies directly,
  with the target changed from default to loss.


---

## 7. The ECL calculation  *(built 6 Nov 2026)*

Everything the previous five phases built, multiplied together and discounted:

```
ECL = SUM over t of  marginal_PD(t) × LGD × EAD(t) × discount(t)
```

`marginal_PD(t)` is the probability of defaulting in month t having survived to
t-1, not the cumulative PD quoted everywhere else in this project. Using the
cumulative figure counts the same default once in every month after it
happened. Measured on this book it overstates the provision by more than 3x,
and it is an easy mistake to make, because `pd_term_structure` offers both
columns side by side.

### 7.1 The horizon is the accounting judgement

The arithmetic is identical for every account. What differs is how far the sum
runs. and that is decided by the stage set in Phase 3:

| stage | basis | horizon |
|---|---|---|
| 1 | 12 month ECL | t = 1 to min(12, remaining term) |
| 2 | lifetime ECL | t = 1 to remaining term |
| 3 | lifetime, credit impaired | a different formula; see §7.2 |

The result on the full book:

| stage | accounts | exposure | ECL | coverage | true coverage | error |
|---|---|---|---|---|---|---|
| 1, 12 month | 7,625 | 126,289,603 | 2,821,097 | 2.23% | 2.30% | -2.99% |
| 2, lifetime | 570 | 9,572,408 | 2,404,604 | 25.12% | 25.30% | -0.73% |
| 3, impaired | 61 | 939,053 | 611,250 | 65.09% | 68.24% | -4.62% |
| **portfolio** | **8,256** | **136,801,064** | **5,836,951** | **4.27%** | **4.36%** | **-2.25%** |

A coverage ladder of 2.2% / 25.1% / 65.1% is the shape a credit committee would
expect, and it comes out of the staging rules rather than being imposed.

### 7.2 The answer key had no provision against its defaulted book

Phase 6 began with me comparing the engine's ECL to the key and finding that
the key recorded all 61 credit impaired accounts at a lifetime PD of 1.00 and
an ECL of exactly 0.00. That is R939k of defaulted exposure with nothing behind
it.

The cause is in one line. `ground_truth` built ECL from `np.diff(curve)`, the
probability of defaulting in each future month. Default is absorbing, so for an
account already in bucket 4 the cumulative curve is 1.0 at t=0 and stays there,
every forward increment is zero, and the sum is zero.

The formula was not wrong about what it computes, which is expected loss from
future default events. It was being asked a question it does not answer. For an
impaired account the default is not a future event to be weighted by its
probability. It has already happened. The loss is the exposure that will not be
recovered:

```
ECL = LGD × EAD at the reporting date, undiscounted
```

Undiscounted because the loss has already arisen rather than being awaited in
some future month, and because the LGD it multiplies is not yet a present value
measure either (§6.6). Discounting one side of a product and not the other is
worse than discounting neither. Twelve month and lifetime ECL are equal for an
impaired account, because the loss does not grow with the horizon when the
event causing it has already occurred.

This is the second time the answer key has been valid for only part of the
book. The first was the same function computing every account's PD as though it
were performing (§5.6). Both were found the same way, by comparing the key
against the engine and asking which of the two was wrong, and both had the same
shape: a formula correct for the majority case, applied to a population it did
not describe. Both times, the part it was invalid for was the part where the
numbers are largest.

Had I built the engine to match the key, it would have shipped provisioning
nothing against its defaulted accounts, which is the single most wrong thing an
ECL engine can do.

A smaller consistency defect followed from the fix. The impaired ECL was first
computed from the unrounded balance while the key publishes that balance
rounded to 2dp, which left a residual of 0.0038 that nobody could reproduce
from the key's own columns. Immaterial as money, and unacceptable as a
discrepancy nobody can explain. My rule on this project is that every figure
must be regenerable, and a key holding a number its own columns cannot
reproduce breaks that rule at the source.

### 7.3 One tolerance was answering two questions, and neither well

`max_ecl_relative_error: 0.02` was set in Phase 0, before any result existed.
The engine missed it, at -2.25%.

Nothing was broken. The arithmetic reconciles to 1.8e-11, and both inputs sit
comfortably inside their own tolerances. The threshold itself could not be met
by correct code, for a reason that follows from the formula: ECL is a product
of three estimated inputs, so to first order its relative error is the sum of
their relative errors.

| input | its own gate | implied relative error |
|---|---|---|
| PD, 12 month | 1pp absolute on a portfolio PD near 9.2% | about 11% |
| PD, lifetime | 15% relative | 15% |
| LGD | 2pp absolute on an LGD near 65% | about 3% |
| EAD | exact | 0% |

Every input gate can pass while ECL is 14 to 18% out. A 2% ECL gate was
therefore never a gate on this phase's work. It was a second, 7.5x stricter,
implicit gate on Phase 4's PD.

And it failed in the other direction too. A sum formed wrongly, using
cumulative PD or the wrong horizon for a stage or no discounting at all, can
land within a few percent on a short book and pass. A tolerance that is
simultaneously unmeetable by correct code and passable by incorrect code is
measuring the wrong thing.

So I replaced the single threshold with two gates for two questions:

1. **Is the arithmetic right?** It is deterministic, so it can be checked
   exactly. `17_ecl_reconciliation.sql` feeds the engine's own ECL expression
   the true marginal PD, LGD, exposure and discount factors from
   `ground_truth_monthly`, and requires the answer key back to the cent. It
   reconciles at 1.8e-11 across 8,256 accounts, and 0.00 exactly for the
   impaired ones. This is the gate that guards Phase 6, and it tests only what
   Phase 6 builds: marginal versus cumulative PD, the per stage horizon,
   discounting at the EIR, and the impaired formula.
2. **Are the inputs good?** A 15% backstop against gross error, set at the
   level the input gates imply rather than tighter than them.

My rule is that a limit which moves to accommodate a result was never a limit,
so I am recording the change rather than glossing over it. The defence is that
the form was wrong, not that the number was inconvenient: the loose gate was
replaced by a far stricter one on this phase's actual work, and a test asserts
that relationship so it cannot be quietly undone. Saying "we relaxed it to 15%"
would be the dishonest summary of what happened. The honest one is that an
exact check replaced an approximate one that could not discriminate.

### 7.4 Where the remaining 2.25% comes from, measured

The obvious suspect is LGD, since a single segment level figure applied to
every account looks like the crude part of the engine. Measured, it is not.

| | portfolio ECL | vs engine |
|---|---|---|
| estimated PD, estimated LGD (the engine) | 5,225,701 | n/a |
| estimated PD, **true** LGD | 5,212,348 | -0.26% |
| true PD, true LGD (the key) | 5,330,202 | +2.00% |

Substituting each account's true LGD closes almost none of the gap. The
residual is PD estimation error, inherited from Phase 4 and inside the
tolerance Phase 4 set for itself, and most of it is the cost of approximating a
continuous seasoning hazard with five discrete age bands.

Direction matters more than magnitude here. The engine under provisions by
2.25%, and understatement is the dangerous direction for a loss allowance, so
the sign is reported prominently rather than folded into an absolute figure. It
is a disclosed limitation inherited from an input that passed its own gate,
rather than a defect introduced by this phase, and the next thing worth
building is a finer PD age structure rather than a finer LGD.

### 7.5 The staging cliff, measured rather than asserted

The most criticised feature of IFRS 9 is the discontinuity at the SICR
boundary: the horizon switches from 12 months to lifetime and the provision
multiplies, with no change in the account's credit quality beyond having
crossed a line.

`16_ecl_account.sql` computes both horizons for every account whatever its
stage, so the size of that jump is a measurable property of this book. Same
accounts, same credit quality, same month, counted two ways:

| arrears bucket | accounts | 12 month ECL | lifetime ECL | multiple |
|---|---|---|---|---|
| 0 (current) | 6,957 | 2,094,124 | 3,420,593 | **1.633** |
| 1 (1 to 29 DPD) | 853 | 896,614 | 1,073,837 | 1.198 |
| 2 (30 to 59 DPD) | 258 | 1,179,861 | 1,215,829 | 1.030 |
| 3 (60 to 89 DPD) | 127 | 965,295 | 969,390 | **1.004** |

The finding complicates the standard criticism rather than confirming it. The
cliff is steepest for accounts in no arrears at all and essentially flat for
those deep in them. An account 60 to 89 days down will mostly default inside
twelve months either way, so extending its horizon to lifetime adds 0.4%.

Which means the accounts that actually trip the 30 day backstop face the
smallest jump, and the large discontinuity is reserved for accounts that cross
via the PD based trigger while still current, which is the trigger Phase 3
measured as barely firing (§4.3). Split by why they are in Stage 2:

| reason | accounts | exposure | cliff multiple |
|---|---|---|---|
| 30 day backstop | 385 | 6,353,710 | 1.019 |
| in probation after cure | 185 | 3,218,698 | 1.293 |

So the cliff is real, and on this book it is mostly hypothetical.

### 7.6 Assessability propagates downstream

I found this by rehearsing CI in a clean room before committing, which is the
only reason it was found at all, because the full book passes everything.

On the committed 400 account sample the ECL accuracy backstop failed at -15.4%,
while on the same run both Phase 4 PD gates and the Phase 5 absolute LGD gate
printed `--`, meaning not assessed. The engine was simultaneously saying it had
no evidence about its PD and LGD estimates and delivering a verdict on an
accuracy figure computed from them.

That is incoherent, and the remedy is a rule rather than another threshold:

> If the inputs to an estimate could not be assessed, the estimate's accuracy
> cannot be assessed either.

`ecl.inputs_were_assessable()` consults the Phase 4 and Phase 5 gates, and if
either declined to judge itself the ECL backstop reports not assessed too. Two
properties make it an honest exemption rather than an escape hatch, and both
are enforced by tests:

1. **The arithmetic gate is unaffected and still applies.** It substitutes
   exact inputs, so it is independent of how good the estimates are. It
   reconciles at 1.8e-12 on the 162 account sample and 1.8e-11 on the full
   book. It is what guards this phase, and silencing it too would leave the
   sample run proving nothing.
2. **The verdict is reached from the upstream gates alone**, before this
   phase's own error is read. A test asserts against the function's source that
   it never consults the ECL relative error, because a decision that can see
   the result it would be excusing is not a decision.

This is the fifth appearance of one error, and the first time it had a general
form rather than a local fix:

| # | where | reported as | actually was |
|---|---|---|---|
| 1 | `git check-ignore` exit 128 (Phase 0) | file not ignored | ran outside a work tree |
| 2 | scorecard optimiser `success: True` | converged | failed to converge |
| 3 | PD accuracy gate on the sample (Phase 4) | inaccurate | 60% fallback cells, never estimated |
| 4 | absolute LGD gate on the sample (Phase 5) | inaccurate | standard error 4x the tolerance |
| 5 | ECL backstop on the sample (Phase 6) | inaccurate | its own inputs were unassessable |

> A check that cannot distinguish "the answer is no" from "I could not
> determine the answer" will eventually report the second as the first.

Instances 3, 4 and 5 are the same mechanism one layer apart, which is why I
expressed the fifth as propagation instead of a third bespoke threshold.

### 7.7 Two test design errors

Both were cases of a test asserting something about the engine that was not
true, and both failed on correct code. That is the cheap direction to be wrong
in, but they were still checks measuring the wrong thing.

**"Lifetime ECL must exceed 12 month ECL wherever more than 12 months
remain."** False for an account with exactly 13 months left, because the 13th
month is maturity, where the amortised balance is exactly zero, so no loss can
arise in it and the two figures are equal to the cent. 179 accounts, correct
arithmetic, reported as a missing horizon.

**The same assertion, broadened, then swept in Stage 3.** Impaired accounts
have equal 12 month and lifetime ECL by design, because the loss does not grow
with the horizon when the event causing it has already happened, and another
test in the same file asserts exactly that. The fix was to exclude Stage 3
explicitly and say why in the query.

### 7.8 What is still missing

- **No forward looking macro overlay.** IFRS 9 requires ECL to reflect expected
  future conditions, usually through several macroeconomic scenarios weighted
  by probability. This engine is a through the cycle calculation on one
  scenario. It is the largest remaining gap against the standard, and it is a
  modelling project of its own rather than an omission to patch.
- **No PD term structure beyond the contractual term**, which is correct for a
  term loan and would be the hard question for a revolving product.
- **LGD is not discounted**, carried forward from §6.6.


---

## 8. Vintage curves and portfolio MI  *(built 8 Nov 2026)*

Two views of the same book, answering different questions.

**Calendar time** (`portfolio_mi`) says what the book looks like now and how it
got here. It cannot say why. A rising Stage 2 share means lending has got
worse, or it means last year's growth is reaching the ages where arrears
appear, and nothing in calendar time separates those two.

**Cohort time** (`vintage_curves`) holds age fixed and can. Group accounts by
the month they were originated and track each cohort's cumulative default rate
by age.

This is the first phase whose gates check for false positives. Every earlier
phase asked whether an estimate was accurate enough. This one asks whether the
MI invents something that is not there. An MI pack that manufactures a trend
costs a credit committee a policy change it did not need, which is a more
expensive error than a provision a few percent out.

The check is unusually sharp because the truth is known exactly. The generator
draws segment and origination month independently, so every cohort is the same
book and the true vintage effect is zero.

### 8.1 Right censoring, and the third of its family here

A cohort originated in December 2024 has been watched for 12 months at this
reporting date. One from January 2021 has been watched for 59. Their
cumulative default rates are not comparable, and the newer one always looks
better, because less of its life has happened yet.

| phase | measure | bias comes from |
|---|---|---|
| 2 | roll rates | the **end** of an account's series |
| 5 | LGD | the **beginning** of a workout |
| 7 | vintage curves | the **end** of the observation window |

Every time, the naive denominator included observations whose outcome is not
yet known. Every time, the bias ran in a direction the mechanism predicts
before any data is seen.

### 8.2 The same question, answered twice

| | trend in 12 month default rate | correlation with origination month |
|---|---|---|
| naive, each cohort at its own latest age | **-4.03pp per year** | -0.853 |
| fair, every cohort at 12 months | **-0.09pp per year** | -0.099 |

**97.8% of the apparent improvement is artefact.** The naive reading says
lending quality improved by four points a year for four straight years, with a
correlation of -0.85, which is the kind of number that gets a credit policy
loosened. The truth is a flat line. The 2021-04 cohort's naive rate sits
18.48pp above its 12 month rate, not because it lent badly but because it has
been watched 44 months longer.

The fair comparison costs information. Restricting 48 cohorts to a common 12
months discards four years of observation on the oldest ones. That is the
trade, and the alternative is not more information, it is a number that means
something different in every row. For a comparison that keeps more history the
move is to drop the youngest cohorts rather than to extend the age, so
`$common_age` is a parameter and `cohorts_included` records what the choice
cost.

The comparison age is derived rather than configured: it is the youngest
cohort's observation window. Pinned in config it would silently start either
excluding cohorts or comparing censored ones as soon as the reporting date
moved, and neither would announce itself.

### 8.3 Is the remaining spread real? No, and monthly cohorts could not see it

| | |
|---|---|
| pooled 12 month default rate | 5.16% |
| observed SD across 48 cohorts | 0.01028 |
| expected SD from binomial sampling alone | 0.01084 |
| **ratio** | **0.948** |
| chi square | 42.50 on 47 df, **p = 0.659** |

Indistinguishable from chance, which is correct, because there is nothing
there.

The more useful number is what this book could have detected. Smallest
difference between two cohorts at 80% power:

| cohorts grouped | minimum detectable difference |
|---|---|
| monthly (about 420 accounts) | **4.29pp** |
| quarterly | 2.48pp |
| annual | 1.24pp |

Against a 5.16% base rate, a monthly vintage chart cannot see anything smaller
than 83% of the base rate itself. It is a picture of sampling noise with a
trend line through it. Quarterly is the minimum defensible grouping here, and
annual is the first that could detect a 1pp shift.

That is the practical conclusion of the phase, and it is the opposite of what a
vintage chart invites: the right response to a noisy monthly triangle is to
group the cohorts, not to explain the wiggles.

### 8.4 Calendar time shows a deterioration that is only maturity

Over 54 settled months on a book whose cohort quality never changed:

| | start | end |
|---|---|---|
| average age of the book | 3.0 months | 28.3 months |
| accounts in arrears | 7.56% | 15.00% |

Correlation of average age with arrears is +0.850, and with Stage 2 share
+0.902. Arrears doubled. Nothing about lending changed. In calendar time that
reads as deterioration; in cohort time it is a book growing up.

This is the age, period and cohort identification problem in its simplest form.
The three are linearly dependent, since age plus vintage equals period exactly,
so their effects cannot be separately identified from observational data
without an outside assumption. Here two of them are zero by construction, which
is the only reason the decomposition can be checked at all. On a real book it
cannot be, and the honest thing to tell a committee is that calendar time
movement has at least two explanations and the data alone will not choose
between them.

### 8.5 Charts, and the rule they have to obey

`scripts/make_charts.py` regenerates everything in `docs/charts/` from the
warehouse. No chart is edited, exported by hand, or committed without rerunning
it, and each one stamps its reporting date, book size and the command that
produced it. An image is the easiest place in a repository to keep showing
yesterday's numbers while nothing complains.

I fixed three layout defects in the making, all of the same kind, which is
something drawn where nothing knew it was:

- **`fig.text` ignores constrained layout.** The title band was drawn straight
  through the axes: subtitle across the plot area, provenance stamp through the
  x axis label. Constrained layout accounts for `suptitle` and nothing else, so
  the bands are now reserved explicitly via the layout engine's `rect`,
  computed from how many lines the subtitle wraps to.
- **A 100% stacked stage mix spent the whole panel on the part that does not
  move.** Stage 1 holds 92 to 97% of the balance throughout, leaving Stage 2
  and 3 as slivers a pixel high. Replaced with just the two stages under
  scrutiny, on a scale that fits them.
- **A bar chart of the staging cliff started its axis at 0.9.** A 1.00x bar
  looked substantial and a 1.63x bar looked seven times a 1.20x one. The bars
  are drawn from a baseline of 1.0 now, so their length is the jump itself.

One more carried over from my scorecard project: never call
`fig.autofmt_xdate()` alongside constrained layout, because it disables it
without saying so.

### 8.6 The cohort is the sample, not the book

Caught by rehearsing CI in a clean room before committing. The full book passes
everything, and the committed 400 account sample failed two Phase 7 gates.

Vintage analysis divides the book by origination month, so the cohort is the
sample. A book that is ample for a portfolio statistic can be hopeless for a
cohort one:

| | full book | committed sample |
|---|---|---|
| accounts per cohort | 417 | 8.3 |
| expected defaults per cohort | 21.5 | **0.40** |
| standard error of the fitted slope | 0.135pp/yr | **0.92pp/yr** |
| fitted fair slope | -0.09pp/yr | -1.56pp/yr |

On the sample the fitted slope breaches the 0.5pp/yr limit, and it sits 1.7
standard errors from zero, which is no evidence of anything. A gate built to
catch invented trends was itself reporting noise.

Two conditions now decide whether each kind of gate applies, and both are
standard tests read off the sample rather than the result:

- **Trend gates** (the fitted slope, the artefact share) need the slope's
  standard error to be at most half the tolerance. That is the same "twice as
  precise as the limit it is held to" rule as the Phase 5 LGD gate.
- **Dispersion gates** (chi square, variance ratio) need at least 5 expected
  defaults per cohort, which is the textbook validity condition for a chi
  square rather than a threshold I invented here.

The structural checks, that no censored cell is used in the comparison and that
the triangle ties to the fact table, are arithmetic rather than inference. They
apply at every book size and they are what keeps the sample run meaningful.

This is the sixth appearance of one error:

| # | where | reported as | actually was |
|---|---|---|---|
| 1 | `git check-ignore` exit 128 | file not ignored | ran outside a work tree |
| 2 | scorecard optimiser `success: True` | converged | failed to converge |
| 3 | PD gate on the sample | inaccurate | 60% fallback cells |
| 4 | absolute LGD gate on the sample | inaccurate | SE 4x the tolerance |
| 5 | ECL backstop on the sample | inaccurate | its inputs were unassessable |
| 6 | vintage gates on the sample | a false trend | 8 accounts per cohort |

Instances 3 to 6 are the same mechanism in four places, and the general form
stated in §7.6, that if the inputs could not be assessed the output cannot be
either, now has a sibling: a gate is assessable only when the sample supports
the statistic it is built from.

### 8.7 What this phase does not do

- **No quarterly or annual cohort tables**, even though §8.3 concludes that
  they are what this book can support. The minimum detectable effect is
  computed for them, but the grouped triangles are not built. That is the
  obvious next extension and it is small.
- **No macro overlay on the cohort curves**, which is where a real vintage
  analysis would go next. A cohort originated into a rate hiking cycle behaves
  differently, and separating that from cohort quality needs the period effect
  this book does not have.
- **No account level survival model.** The triangle is a cumulative incidence
  rather than a hazard, and it flattens at older ages partly because accounts
  mature and leave. Cross cohort comparison stays valid here because term mix
  is drawn independently of origination month, but on a real book where it is
  not, the honest tool is a survival model with term as a covariate.
