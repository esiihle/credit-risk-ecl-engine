"""
Phase 2 — delinquency transitions and roll rates.

This is the first phase where the engine ESTIMATES something the answer key
already knows. Everything before it was plumbing; from here on, each number has
a true value it can be checked against.

The estimate: a transition matrix counted from 500,000 observed account-months.
The truth: the matrix those months were drawn from. The two are produced by
completely unrelated routes, so agreement is evidence rather than restatement —
and `compare_to_truth` below is where they meet.

What is in SQL and what is here, and why:

* the transitions, the matrices and the cuts are SQL, under `sql/`, because
  they are the artefact a credit risk reviewer reads and Phase 8 ports to SAS
* the comparison against the generator's matrix is Python, because it needs the
  generator — and because it is VALIDATION, not production logic. The engine
  must never read the true columns; the validation reads them, and keeping that
  boundary visible is the point of putting it in a separate module.
"""
from pathlib import Path

import numpy as np
import pandas as pd

from . import db, generate

REPO_ROOT = Path(__file__).resolve().parents[1]

BUILD_ORDER = [
    ("05_bucket_transitions.sql", "bucket_transition",
     "one row per observed month-to-month move"),
    ("06_roll_rates.sql", "roll_rate", "the pooled transition matrix"),
    ("07_roll_rate_segmented.sql", "roll_rate_segmented",
     "the same matrix by segment, age and quarter"),
]


class RollRateError(Exception):
    """Raised when transitions cannot be built or reconciled."""


def build(connection, verbose=True):
    """Build the transition and roll-rate tables, in order."""
    counts = {}
    for filename, table, purpose in BUILD_ORDER:
        db.run_sql_file(connection, filename)
        counts[table] = db._count(connection, table)
        if verbose:
            print(f"  built {table:<22} {counts[table]:>10,} rows   {purpose}")
    return counts


def matrix(connection, cut_type=None, cut_value=None):
    """The transition matrix as a square frame, rows summing to 1.

    Pivoted here rather than in SQL because a matrix is a presentation of the
    long table, not a different fact — and because an absorbing state has no
    outgoing rows at all, so the pivot needs filling with explicit zeros rather
    than leaving holes a reader has to interpret.
    """
    if cut_type:
        frame = db.read_sql(connection, """
            SELECT from_bucket, to_bucket, rate
            FROM roll_rate_segmented
            WHERE cut_type = $cut_type AND cut_value = $cut_value
        """, cut_type=cut_type, cut_value=cut_value)
    else:
        frame = db.read_sql(connection,
                            "SELECT from_bucket, to_bucket, rate FROM roll_rate")

    buckets = list(range(generate.N_BUCKETS))
    pivot = (frame.pivot(index="from_bucket", columns="to_bucket", values="rate")
             .reindex(index=buckets, columns=buckets)
             .fillna(0.0))
    pivot.index = [generate.BUCKETS[b] for b in buckets]
    pivot.columns = [generate.BUCKETS[b] for b in buckets]
    return pivot


def expected_pooled_matrix(connection, config):
    """The exposure-weighted average of the TRUE matrices, over observed moves.

    This is the only like-for-like comparison available for a pooled empirical
    matrix. The true matrix varies by segment and by month on book, so no single
    true matrix exists to compare against — but every observed transition had
    one, and averaging them with the weights the data actually carries gives the
    quantity the pooled estimate is estimating.

    Getting this wrong in the obvious way — comparing the pooled estimate
    against the base rates in config — would show a discrepancy of several
    percentage points and look like a bug in the estimation. It would in fact be
    a bug in the comparison: the base rates are what a near-prime account at the
    seasoning peak experiences, not what the book averages.
    """
    opportunities = db.read_sql(connection, """
        SELECT
            a.true_risk_multiplier AS multiplier,
            t.months_on_book,
            t.from_bucket,
            COUNT(*)               AS n
        FROM bucket_transition t
        JOIN dim_account a ON a.account_id = t.account_id
        GROUP BY a.true_risk_multiplier, t.months_on_book, t.from_bucket
    """)

    if opportunities.empty:
        raise RollRateError("No transitions to compare against.")

    n_buckets = generate.N_BUCKETS
    weighted = np.zeros((n_buckets, n_buckets))
    totals = np.zeros(n_buckets)

    for row in opportunities.itertuples(index=False):
        true_matrix = generate.transition_matrix(
            row.multiplier, row.months_on_book, config)
        weighted[row.from_bucket] += row.n * true_matrix[row.from_bucket]
        totals[row.from_bucket] += row.n

    with np.errstate(invalid="ignore", divide="ignore"):
        expected = np.where(totals[:, None] > 0,
                            weighted / totals[:, None], 0.0)

    frame = pd.DataFrame(expected,
                         index=[generate.BUCKETS[b] for b in range(n_buckets)],
                         columns=[generate.BUCKETS[b] for b in range(n_buckets)])
    return frame, totals


def compare_to_truth(connection, config):
    """Empirical roll rates against the weighted average of the true matrices.

    Returns one row per (from, to) pair with the estimate, the truth, the gap
    and the binomial standard error of the estimate. A gap larger than a few
    standard errors is a real discrepancy; a gap smaller than one is sampling.
    """
    empirical = db.read_sql(connection, """
        SELECT from_bucket, to_bucket, transitions, opportunities, rate
        FROM roll_rate
    """)
    expected, totals = expected_pooled_matrix(connection, config)

    rows = []
    for row in empirical.itertuples(index=False):
        truth = float(expected.iloc[int(row.from_bucket), int(row.to_bucket)])
        n = float(row.opportunities)
        # Binomial standard error at the TRUE rate, which is the right null to
        # judge the estimate against.
        standard_error = np.sqrt(max(truth * (1 - truth), 1e-12) / max(n, 1))

        rows.append({
            "from_bucket": generate.BUCKETS[int(row.from_bucket)],
            "to_bucket": generate.BUCKETS[int(row.to_bucket)],
            "observed": float(row.rate),
            "truth": truth,
            "gap": float(row.rate) - truth,
            "standard_error": standard_error,
            "gap_in_standard_errors": (float(row.rate) - truth) / standard_error,
            "opportunities": int(n),
        })

    return pd.DataFrame(rows)


def censoring_bias(connection):
    """What treating an unobserved successor as "stayed" would cost.

    The naive roll rate divides moves to bucket j by the number of ROWS in
    bucket i, rather than by the number of rows whose successor was observed.
    Every account's final month has no successor, so the naive denominator is
    too large and every rate is biased toward zero — with the fake mass landing
    on the diagonal, since a missing move reads as "nothing happened".

    Quantified here rather than asserted, because "a small bias" and "a bias
    that doubles the roll-to-default rate" call for different reactions.
    """
    comparison = db.read_sql(connection, """
        WITH naive_denominator AS (
            -- Every fact row, whether or not its successor was observed.
            SELECT arrears_bucket AS from_bucket, COUNT(*) AS rows_in_bucket
            FROM fact_loan_month
            GROUP BY arrears_bucket
        ),
        observed AS (
            SELECT from_bucket, to_bucket, COUNT(*) AS transitions
            FROM bucket_transition
            GROUP BY from_bucket, to_bucket
        )
        SELECT
            r.from_bucket,
            r.to_bucket,
            r.rate                                            AS correct_rate,
            CAST(o.transitions AS DOUBLE) / n.rows_in_bucket  AS naive_rate,
            r.opportunities,
            n.rows_in_bucket
        FROM roll_rate r
        JOIN observed o
          ON o.from_bucket = r.from_bucket AND o.to_bucket = r.to_bucket
        JOIN naive_denominator n ON n.from_bucket = r.from_bucket
        ORDER BY r.from_bucket, r.to_bucket
    """)
    comparison["understatement"] = (
        comparison["correct_rate"] - comparison["naive_rate"])
    comparison["relative_understatement"] = (
        comparison["understatement"] / comparison["correct_rate"].replace(0, np.nan))
    return comparison


def format_matrix(frame, title):
    """A transition matrix as something a person reads."""
    lines = [title, "-" * len(title), ""]
    header = "  " + " " * 22 + "".join(f"{c[:11]:>13}" for c in frame.columns)
    lines.append(header)
    for name, row in frame.iterrows():
        cells = "".join(f"{value:>12.2%} " for value in row)
        lines.append(f"  {name:<22}{cells}")
    return "\n".join(lines)


def rollrate_report(connection, config):
    """The phase's findings, in the order a reviewer would want them."""
    lines = []
    lines.append(format_matrix(matrix(connection),
                               "Transition matrix (pooled, account-weighted)"))
    lines.append("")
    lines.append("  The 90+ row is empty by construction, not for want of data:")
    lines.append("  default is absorbing, so no transition ever leaves it.")

    comparison = compare_to_truth(connection, config)
    worst = comparison.reindex(
        comparison["gap_in_standard_errors"].abs().sort_values(ascending=False).index)

    lines.append("")
    lines.append("Estimate vs the known truth")
    lines.append("-" * 27)
    lines.append(f"  {'transition':<34}{'observed':>10}{'truth':>10}"
                 f"{'gap':>10}{'in SEs':>9}")
    for row in worst.itertuples(index=False):
        label = f"{row.from_bucket[:14]} -> {row.to_bucket[:14]}"
        lines.append(f"  {label:<34}{row.observed:>10.2%}{row.truth:>10.2%}"
                     f"{row.gap:>+10.2%}{row.gap_in_standard_errors:>+9.1f}")

    largest = worst.iloc[0]
    lines.append("")
    lines.append(f"  largest discrepancy: {largest.gap_in_standard_errors:+.1f} "
                 "standard errors")
    lines.append("  (a gap under ~3 SEs is sampling; more is a real difference)")

    bias = censoring_bias(connection)
    rolls = bias[bias["to_bucket"] > bias["from_bucket"]]
    lines.append("")
    lines.append("Censoring: what the naive denominator would cost")
    lines.append("-" * 47)
    lines.append(f"  {'roll-forward transition':<34}{'correct':>10}{'naive':>10}"
                 f"{'understated by':>16}")
    for row in rolls.itertuples(index=False):
        label = (f"{generate.BUCKETS[int(row.from_bucket)][:14]} -> "
                 f"{generate.BUCKETS[int(row.to_bucket)][:14]}")
        lines.append(f"  {label:<34}{row.correct_rate:>10.2%}"
                     f"{row.naive_rate:>10.2%}"
                     f"{row.relative_understatement:>15.1%}")

    return "\n".join(lines)
