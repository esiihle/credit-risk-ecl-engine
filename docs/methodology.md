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
