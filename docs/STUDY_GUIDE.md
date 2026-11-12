# Study guide

Written for myself, to prepare for talking about this project in an interview.
Public because there is nothing in it I would not say out loud.

Everything here is a number the engine regenerates. If a figure in this guide
disagrees with `docs/methodology.md`, the methodology wins and this file is
stale, and a test checks that the two agree.

---

## 1. Opening, about sixty seconds

> I built an IFRS 9 expected credit loss engine in SQL over a loan book of
> 8,256 accounts. It does the whole chain: a data mart, delinquency roll rates,
> the staging rules, a PD term structure, LGD, exposure, and the discounted
> provision at the end. Portfolio coverage comes out at 4.27%.
>
> The part I would actually talk about is the validation. The book is
> simulated, and I wrote the data generating process, so the true probability
> of default is known exactly rather than estimated. That means every number
> the engine produces has a right answer sitting behind it, and when the two
> disagree I have to work out which one is wrong. About half the time it was
> the engine. The other half it was my answer key, and those were the more
> interesting bugs.

Then stop. Let them pick where to go next.

**If they ask what to look at first**, say the vintage curves, because the
chart carries the finding on its own.

**Do not** open with the technology. Nobody is impressed that it is DuckDB.

---

## 2. Numbers to know cold

If I fumble these, the rest of the conversation is uphill.

| | |
|---|---|
| portfolio exposure | R136.8m across 8,256 accounts |
| ECL | R5.84m, coverage **4.27%** |
| stage ladder | 2.23% / 25.12% / 65.09% |
| stage split by exposure | 92.3% / 7.0% / 0.7% |
| PD accuracy, age aware vs pooled | 0.0039 vs 0.0359 mean absolute error, **9.2x better** |
| LGD, resolved workouts only | **65.10%**, against a truth of 65.1% |
| LGD, the naive estimate | 68.63%, overstated by **3.5pp** |
| exposure schedule vs the annuity formula | exact, max difference 0.000000 |
| ECL arithmetic on exact inputs | reconciles at **1.8e-11** across 8,256 accounts |
| vintage trend, naive vs fair | **-4.03pp/yr** against -0.09pp/yr |
| tests | 161 |

Two I should be able to derive rather than recall: the stage 3 coverage of
65.09% is just the LGD, because a defaulted account's loss is LGD times
exposure. And the 4.27% portfolio coverage is mostly the Stage 1 book, because
92% of the exposure sits there at 2.23%.

---

## 3. The engine in nine phases

One line each. If I cannot say what a phase does in one line I do not
understand it well enough to be asked about it.

| phase | what it does |
|---|---|
| 0 | Simulates the book from a Markov chain, and computes the true PD from the same matrix by a route the engine never touches |
| 1 | Dimensional mart: one row per account per month end, with the grain written down and tested |
| 2 | Roll rates, which is the transition matrix estimated from observed data |
| 3 | IFRS 9 staging: the SICR rules, probation, and where the accounting judgement sits |
| 4 | PD term structure three ways, pooled, segmented and age aware, measured per account against the truth |
| 5 | LGD from resolved collections workouts, and exposure from the amortisation schedule |
| 6 | The provision itself, with the horizon set by stage |
| 7 | Vintage curves and portfolio MI |
| 8 | The same engine in SAS PROC SQL, reconciled against the DuckDB one |

---

## 4. The findings, as answers

Each one is a short answer first, then the follow up a panel will actually ask.

### 4.1 The roll rate censoring trap

**Q. How do you compute a roll rate?**

Count transitions between arrears buckets month to month. The trap is the
denominator. If you take `LEAD(bucket)` and treat a null as "stayed", every
account's final observed month becomes an opportunity that produced no move.
Those final months are not a random sample, because an account about to
default often has its series end there. On my book that understates roll to
default by 3.9%, worst in the deepest bucket, so the provision comes out light
exactly where it matters.

A transition exists only where the next month is actually observed.

**Follow up: how do you know it is 3.9% and not something else?**

I built the wrong version deliberately and measured the difference, and there
is a test that asserts the naive method is still biased. If the bias ever went
away, the test would fail and I would want to know why.

### 4.2 The SICR trigger that barely fires

**Q. How do you decide an account has had a significant increase in credit
risk?**

Three ways in: the 30 day backstop, which is the rebuttable presumption in the
standard, a relative test on lifetime PD against PD at origination, and an
absolute floor so a very safe account does not trip on noise.

The finding is that on this book the PD test does almost nothing. The backstop
accounts for 23,737 account months, the PD test for 1,132, and every one of
those 1,132 is on a 12 month loan.

**Follow up: why only short loans?**

A relative test needs a small denominator. Lifetime PD at origination grows
with the term, from about 6% over 12 months to 38% over 60, so the same
proportional deterioration clears a doubling on a short loan and cannot come
close on a long one. And PD here is a function of the arrears bucket, so by the
time PD has doubled the account is usually already 30 days down and the
backstop has caught it.

**Follow up: so what would you do about it?**

Use a risk driver that is not a function of arrears. A refreshed behavioural
score, a bureau pull, a macro overlay. A SICR test built only on arrears can
never be genuinely forward looking, because arrears are the thing it is
supposed to anticipate.

### 4.3 Age aware PD, and what segmentation buys

**Q. How accurate is your PD model?**

Three methods, all measured per account against the truth. Mean absolute error
on 12 month PD: pooled 0.0359, segmented 0.0165, age aware 0.0039. So age aware
is about nine times better per account than a single pooled curve.

**Follow up: why does pooling fail?**

Two reasons, and they are different. Across segments the errors have opposite
signs, so they partly cancel in the book total: pooled overstates prime by
3.6pp and understates subprime by 8.1pp, while the aggregate bias is only about
1pp. Across ages the error tracks the seasoning curve, because a method blind
to age has to overstate risk where the true hazard is low and understate it
where it peaks.

**The point worth making:** the pooled method's aggregate bias is 1pp and its
per account error is 3.6pp. A provision is built account by account, so
validating a model on aggregates alone will miss this entirely.

### 4.4 LGD and the workout censoring trap

**Q. How do you estimate loss given default?**

From resolved collections workouts only. The naive version averages loss to
date over every default, including the ones that defaulted last month and have
barely started collecting. For those accounts the figure is not LGD, it is LGD
plus however much is still to come.

Resolved only: 65.10%, against a truth of 65.1%. Naive: 68.63%. The unresolved
cases on their own sit at 77.05%, about twelve points above the resolved ones.

**Follow up: you threw away 30% of your defaults. Is that not worse?**

It is the trade, and I think it is the right side of it. An estimate built on
fewer resolved cases is noisy. One built on more unresolved cases is biased,
and biased in a direction I can work out in advance. Noise shrinks when the
book grows; bias does not.

**Follow up: how is this different from the roll rate problem?**

Same shape, different end. There the bias came from the end of an account's
series, here from the start of a workout, and in Phase 7 it comes from the end
of the observation window. Every time the denominator includes observations
whose outcome is not yet known.

### 4.5 The answer key had no provision against its own defaults

**Q. Tell me about a bug you found.**

This is the one I would lead with.

My answer key recorded all 61 credit impaired accounts at a lifetime PD of 1.00
and an ECL of exactly 0.00. R939k of defaulted exposure with nothing behind it.

One line caused it. ECL was built as the sum over future months of the
probability of defaulting in that month. Default is absorbing, so for an
account already in default every one of those increments is zero and the sum is
zero. The formula was not wrong about what it computes, which is expected loss
from future default events. It was being asked a question it does not answer.
An impaired account's default is not a future event to weight by its
probability. It has already happened, and the loss is LGD times exposure.

**Why it matters:** if I had built the engine to agree with the key, I would
have shipped something that provisions nothing against the defaulted part of
the book. That is the single most wrong thing an ECL engine can do.

**Follow up: how did you catch it?**

By comparing the engine to the key and asking which one was wrong, rather than
assuming the engine was. It was the second time the key turned out to be valid
for only part of the book. The first was in the same function, computing every
account's PD as though it were performing.

### 4.6 The staging cliff is steepest where it is least likely to be crossed

**Q. What do you think of IFRS 9's staging approach?**

The usual criticism is the discontinuity: crossing the SICR line swaps a twelve
month horizon for a lifetime one and the provision jumps, with no change in
credit quality. I measured it, because the engine computes both horizons for
every account whatever its stage.

| arrears bucket | lifetime ÷ 12 month |
|---|---|
| current | 1.633 |
| 1 to 29 DPD | 1.198 |
| 30 to 59 DPD | 1.030 |
| 60 to 89 DPD | 1.004 |

The cliff is steepest for accounts in no arrears and essentially flat for those
deep in them. An account 60 to 89 days down will mostly default inside twelve
months either way, so extending the horizon adds 0.4%.

Which means the accounts that actually trip the 30 day backstop face the
smallest jump. The big discontinuity is reserved for accounts crossing on the
PD trigger while still current, and that is the trigger I already showed barely
fires. So the cliff is real and on this book it is mostly hypothetical.

**Why this answer works:** it does not just repeat the standard criticism, it
complicates it with a measurement.

### 4.7 The vintage comparison that invents a trend

**Q. How do you tell whether lending quality is getting better or worse?**

Vintage curves, grouped by origination month, and the trap is comparing cohorts
at different ages. A cohort originated last December has been watched for 12
months. One from four years ago for 59. Compare their cumulative default rates
as they stand and the newer one always looks better for reasons that have
nothing to do with credit.

On my book the naive comparison slopes down at 4.03pp a year with a correlation
of -0.85. Four straight years of apparent improvement. Compare every cohort at
the same age and the slope is 0.09pp a year. So 97.8% of the signal is the
observation window.

And the true vintage effect on this book is exactly zero, because segment and
origination month are drawn independently in the generator. There was nothing
there to find.

**Follow up: so is the remaining spread real?**

No. Observed standard deviation across cohorts is 0.0103 against 0.0108
expected from binomial sampling alone, and a chi square gives p = 0.66.

**Follow up: what would you tell a credit committee?**

That a monthly vintage chart on a book this size cannot detect anything smaller
than 4.29pp, which against a 5.16% base rate is most of the effects anyone
cares about. Quarterly gets you to 2.48pp, annual to 1.24pp. The right response
to a noisy monthly triangle is to group the cohorts, not to explain the
wiggles.

### 4.8 Calendar time versus cohort time

**Q. Your arrears doubled. Is the book deteriorating?**

Arrears went from 7.56% to 15.00% over the period. The average age of the book
went from 3.0 to 28.3 months at the same time, correlation 0.85. Cohort quality
never changed. In calendar time it reads as deterioration. In cohort time it is
a book growing up.

**The honest version:** age plus vintage equals period exactly, so the three
effects cannot be separated from observational data without an outside
assumption. On my book two of them are zero by construction, which is the only
reason the decomposition can be checked at all. On a real book it cannot be,
and the right thing to tell a committee is that calendar movement has at least
two explanations and the data will not choose between them.

---

## 5. The thread that runs through the project

Worth having ready, because it is the thing that makes the project look like
engineering rather than a tutorial.

> A check that cannot distinguish "the answer is no" from "I could not
> determine the answer" will eventually report the second as the first.

Six instances so far:

| # | where | reported as | actually was |
|---|---|---|---|
| 1 | `git check-ignore` exit 128 | file not ignored | ran outside a work tree |
| 2 | scorecard optimiser `success: True` | converged | failed to converge |
| 3 | PD gate on the sample | inaccurate | 60% fallback cells, never estimated |
| 4 | absolute LGD gate on the sample | inaccurate | standard error 4x the tolerance |
| 5 | ECL backstop on the sample | inaccurate | its own inputs were unassessable |
| 6 | vintage gates on the sample | a false trend | 8 accounts per cohort |

Instances 3 to 6 are the same mechanism one layer apart, which is why the fifth
was fixed as a general rule rather than a third bespoke threshold: if the
inputs to an estimate could not be assessed, the estimate's accuracy cannot be
either. The sixth added a sibling: a gate is assessable only when the sample
supports the statistic it is built from.

**If asked for one sentence on what the project taught me**, that is it.

---

## 6. The hard questions

### Why synthetic data? Is that not a cop out?

It is a choice made for a reason, and the opposite choice on my scorecard
project for the opposite reason.

What this project has to demonstrate is that a calculation is correct. A real
loan book gives realism and nothing that helps with that, because nobody knows
its true PD, so the engine could only be compared against somebody else's
estimate. Here the transition matrix is known, so true PD follows from its
powers, exposure from the annuity formula, LGD from a known distribution. The
engine estimates and the key already knows.

On the scorecard the finding itself was the deliverable, so real data was
essential, and an early synthetic stand in produced published figures that were
wrong by a factor of three. Simulation is right in one case and disqualifying
in the other. Knowing which situation you are in is the skill.

### How do you know the engine is right, not just consistent?

Four different ways, and they fail differently:

1. **Against the answer key**, which is computed by matrix algebra and the
   annuity formula, routes the engine never touches.
2. **Exactly, where the maths allows it.** The exposure schedule matches the
   annuity formula to the cent. The ECL arithmetic, fed exact inputs,
   reproduces the key at 1.8e-11.
3. **By showing the wrong method is wrong.** Tests assert the naive roll rate
   is still biased, the naive LGD still overstated, cumulative PD still
   overstates the provision by more than 3x. A suite that only confirms right
   answers gives a reader no reason to think they were hard to get.
4. **By a second implementation.** Phase 8 rewrites the engine in SAS and
   reconciles row by row, which is what a model validation function does and
   the only method that survives on a real book with no answer key.

### Why SAS as well as SQL?

Because that is what SA bank credit risk teams run, and because two
implementations agreeing is how you show a model is right when there is no
answer key.

It also turned out to be the most instructive part. PROC SQL has no window
functions and no recursive CTEs, so `LEAD`, `LAG`, the bounded window frame for
probation and the recursive PD walk all had to be rebuilt as self joins and a
macro loop. The port is not a translation, it is a reimplementation, and
reimplementation is exactly where reconciliation earns its keep.

I validated all four replacements in DuckDB before writing any SAS, so the SAS
run carries syntax risk rather than logic risk.

### What is missing?

I would rather say this before they find it.

- **No forward looking macro overlay.** IFRS 9 wants ECL to reflect expected
  future conditions, usually several macroeconomic scenarios weighted by
  probability. This is a through the cycle calculation on one scenario. It is
  the largest gap against the standard.
- **LGD is not discounted.** IFRS 9 LGD is a present value measure. Recoveries
  here are not discounted, which is arithmetic rather than a modelling
  question, and I left it out because it would obscure the censoring mechanism
  the phase exists to show.
- **LGD is averaged, not modelled.** A segment average is the right starting
  point and the wrong finishing point.
- **The SAS port stops at `account_pd`.** LGD, exposure and the ECL itself are
  still DuckDB only.
- **No cures out of default.** Default is absorbing, which simplifies Stage 3.

### Where is the engine wrong, and by how much?

It under provisions by 2.25% against the key. I measured where that comes from
rather than guessing: substituting each account's true LGD moves the total by
0.26%, so it is almost entirely PD estimation error, mostly the cost of
approximating a continuous seasoning hazard with five discrete age bands.

Direction matters more than size. Understatement is the dangerous direction for
a loss allowance, so the engine reports the sign prominently rather than an
absolute figure.

### What would you do differently?

Set the ECL tolerance from the input tolerances in the first place. I set a 2%
limit on total ECL in Phase 0 and it could not be met by correct code, because
ECL is a product of three estimated inputs and its relative error is roughly
the sum of theirs, and the PD gate alone permits 15%. So the ECL gate was 7.5x
stricter than the input it inherits from, while also being loose enough that a
sum formed wrongly could pass.

I split it into two gates: an exact reconciliation of the arithmetic on true
inputs, and a loose backstop on end to end accuracy. The thing I would keep is
the habit of writing down why a threshold changed, including that it changed
after a result was seen.

### How long did it take?

About three weeks of evenings, one phase per session, with the progress log
written as each phase landed.

---

## 7. Traps to avoid in the room

- **Do not say "it's just synthetic data" apologetically.** It is a design
  decision with a defence. Give the defence.
- **Do not claim the engine is production ready.** It has no macro overlay.
  Say so first.
- **Do not quote a number I cannot derive or source.** If I am unsure, say the
  shape of it and offer to look it up.
- **Do not lead with the recurring error thread.** It lands much better as an
  answer to "what did you learn" than as an opening.
- **If asked something I have not thought about**, say so and reason out loud.
  That is a better answer than a confident wrong one, and this project is
  partly about exactly that distinction.

---

## 8. Drill sheet

Answer out loud, then check against the sections above. No peeking.

1. What is ECL, in one formula, and what does each term mean?
2. Why does the staging decision move the provision more than the PD model?
3. What is the grain of `fact_loan_month`, and why does it matter?
4. What is a semi additive measure? Give one from this project.
5. Why is `is_new_default` the right measure for a default rate and
   `is_default` the wrong one?
6. Why does a transition only exist where the next month is observed?
7. How much does the naive roll rate understate roll to default, and where is
   it worst?
8. Name the three SICR triggers. Which one does the work on this book?
9. Why does the relative PD test only bind on short dated loans?
10. What is probation for, and what happens without it?
11. Why is lifetime PD a product of matrices rather than a matrix power?
12. What does the thin cell fallback do, and what breaks without it?
13. How much better is age aware PD than pooled, and by what measure?
14. Why can an aggregate check miss a per account error?
15. Why does the naive LGD overstate the loss? By how much?
16. Why is EAD exact here and hard on a credit card?
17. Why does a credit impaired account need a different ECL formula?
18. What is the staging cliff, and where is it steepest?
19. How much of the apparent vintage improvement is artefact, and why?
20. Why can a monthly vintage chart on this book not detect a real effect?
21. What is the age, period and cohort identification problem?
22. Why does a 2% tolerance on ECL fail even when the engine is correct?
23. What does "not assessed" mean, and why is it different from "failed"?
24. What can PROC SQL not do, and what did you do instead?
25. How would you validate this engine if there were no answer key?

---

## 9. One page to reread before walking in

- Coverage **4.27%**, ladder **2.23 / 25.12 / 65.09**, exposure R136.8m.
- Age aware PD is **9.2x** better per account than pooled.
- LGD **65.10%** resolved, **68.63%** naive, truth 65.1%.
- Arithmetic reconciles at **1.8e-11**. Exposure is exact.
- Vintage: naive **-4.03pp/yr**, fair **-0.09pp/yr**, **97.8%** artefact.
- Engine **under** provisions by 2.25%, and it is PD not LGD.
- Best bug: the answer key with **no provision against its own defaults**.
- Best line: a check that cannot tell "no" from "I don't know" will eventually
  report the second as the first.
