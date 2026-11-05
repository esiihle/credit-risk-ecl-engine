"""
Phase 5 — loss given default and exposure at default.

The two remaining inputs to ECL, and they could not be less alike.

EAD on a fixed-term amortising loan is arithmetic. The schedule is contractual,
the borrower cannot draw more, and the exposure in any future month follows from
the annuity formula. This module checks the SQL against the generator's own
balance function and expects agreement to the cent. If it disagrees, one of the
two is wrong, and because the formula is closed-form there is no third
possibility to consider.

LGD is the opposite. It is estimated from a few hundred resolved defaults, it
carries a standard error worth quoting, and it is subject to a censoring bias
that makes the naive estimate wrong in a specific and predictable direction.
That bias is the substance of this phase, so this module measures it rather
than describing it.
"""
import numpy as np
import pandas as pd

from . import db, generate

SQL_FILES = ["14_lgd_estimate.sql", "15_ead_schedule.sql"]


def build(connection, config, verbose=True):
    """Estimate LGD by segment and project exposure to maturity."""
    # The horizon has to reach the longest remaining term on the book, or the
    # lifetime ECL in Phase 6 would be truncated for the longest loans — and
    # truncating a lifetime measure understates it, which is the direction a
    # provision must never be wrong in by accident.
    longest = int(db.read_sql(
        connection,
        "SELECT MAX(months_remaining) AS t FROM account_pd")["t"].iloc[0])

    for name in SQL_FILES:
        if verbose:
            print(f"  {name}")
        db.run_sql_file(connection, name, max_horizon=longest)

    if verbose:
        print(f"  exposure projected to {longest} months "
              f"({db._count(connection, 'ead_schedule'):,} account-months)")
    return longest


def lgd_table(connection):
    """The per-segment LGD estimates, all three versions side by side."""
    return db.read_sql(connection, "SELECT * FROM lgd_estimate ORDER BY segment")


def censoring_bias(connection):
    """How wrong the naive LGD is, and in which direction.

    Parallel to `rollrate.censoring_bias()`, and the same lesson in a different
    mechanism: an estimate that includes observations whose outcome is not yet
    known is biased, and the bias has a sign that can be worked out in advance.

    Here the sign is upward. A workout that is halfway through has collected
    only part of what it will collect, so the loss computed from it is larger
    than the loss that will eventually be realised. Averaging those in pulls
    the estimate up.
    """
    return db.read_sql(connection, """
        SELECT
            SUM(resolved_defaults)                             AS resolved,
            SUM(unresolved_defaults)                           AS unresolved,
            SUM(unresolved_defaults)::DOUBLE
                / NULLIF(SUM(all_defaults), 0)                 AS unresolved_share,
            SUM(lgd_resolved_only * resolved_defaults)
                / NULLIF(SUM(resolved_defaults), 0)            AS lgd_resolved,
            SUM(lgd_unresolved_only * unresolved_defaults)
                / NULLIF(SUM(unresolved_defaults), 0)          AS lgd_unresolved,
            SUM(lgd_all_defaults * all_defaults)
                / NULLIF(SUM(all_defaults), 0)                 AS lgd_naive
        FROM lgd_estimate""").iloc[0]


def ead_check(connection, config, accounts=None):
    """Does the SQL exposure schedule match the generator's balance function?

    The SQL walks the annuity formula forward in DuckDB; `generate.balance_at`
    walks the same formula in numpy. They share no code. Agreement to the cent
    is therefore evidence the formula was transcribed correctly, which is the
    only thing that can go wrong with an input this deterministic.
    """
    schedule = db.read_sql(connection, """
        SELECT e.account_id, e.horizon, e.months_on_book_then, e.ead,
               a.principal, a.term_months, a.annual_rate
        FROM ead_schedule e
        JOIN dim_account a ON a.account_id = e.account_id""")

    expected = generate.balance_at(
        schedule["principal"].to_numpy(dtype=float),
        schedule["annual_rate"].to_numpy(dtype=float),
        schedule["term_months"].to_numpy(dtype=float),
        schedule["months_on_book_then"].to_numpy(dtype=float))

    difference = np.abs(schedule["ead"].to_numpy(dtype=float) - expected)
    return {
        "rows": len(schedule),
        "max_absolute_difference": float(difference.max()) if len(difference) else 0.0,
        "mean_absolute_difference": float(difference.mean()) if len(difference) else 0.0,
        "rows_over_a_cent": int((difference > 0.01).sum()),
    }


def validate(connection, config, accounts=None):
    """Check the LGD estimate against the answer key and the EAD against the formula.

    The LGD check uses the generator's `true_lgd`, which is the expected loss
    the simulation drew each account's recovery around. The resolved-workout
    estimate should match it; the naive estimate should not, and the test suite
    asserts the second as firmly as the first, because an engine that fails to
    reproduce a known bias is not demonstrating anything.
    """
    limits = config["validation"]
    truth = db.read_sql(connection, """
        SELECT segment, AVG(true_lgd) AS true_lgd, COUNT(*) AS accounts
        FROM dim_account GROUP BY segment""")

    estimates = lgd_table(connection).merge(truth, on="segment", how="left")

    lgd_limit = float(limits["max_lgd_absolute_error"])
    se_limit = float(limits["max_lgd_errors_in_standard_errors"])
    precision_limit = float(limits["max_lgd_standard_error_for_absolute_gate"])
    ead_limit = float(config["reconciliation"]["max_absolute_difference"])

    # WHETHER THE ABSOLUTE GATE CAN BE APPLIED, DECIDED FIRST AND DELIBERATELY
    # BEFORE ANY ERROR EXISTS IN THIS FUNCTION.
    #
    # The question is how precise the estimate is, which is a property of the
    # SAMPLE — the number of resolved defaults and the spread of their losses —
    # and nothing to do with whether the estimate turned out well. Computing
    # the error first and then deciding whether to judge it would make this an
    # escape hatch for a bad result instead of an honest statement about a
    # small book, and the two are impossible to tell apart from the outside.
    #
    # The ordering is the control, so it is enforced by a test that reads this
    # function's source: see `test_assessability_is_decided_from_precision_
    # not_from_the_error`. That test failed when this block sat below the error
    # calculation, which is where it was first written.
    worst_standard_error = float(estimates["standard_error"].max())
    absolute_gate_assessable = worst_standard_error <= precision_limit

    # Only now the accuracy figures.
    estimates["absolute_error"] = (
        estimates["lgd_resolved_only"] - estimates["true_lgd"]).abs()
    # Expressed in standard errors as well as in absolute terms: on a few
    # hundred defaults a 1pp miss may be well within noise, and a tolerance
    # that ignores the sample size will eventually fail for being unlucky.
    estimates["errors_in_standard_errors"] = (
        estimates["absolute_error"] / estimates["standard_error"].replace(0, np.nan))

    bias = censoring_bias(connection)
    ead = ead_check(connection, config, accounts)

    checks = [
        {
            "check": "LGD from resolved workouts matches the truth",
            "measure": "max absolute error across segments",
            "value": float(estimates["absolute_error"].max()),
            "limit": lgd_limit,
            "assessable": absolute_gate_assessable,
            "passed": (float(estimates["absolute_error"].max()) <= lgd_limit
                       if absolute_gate_assessable else True),
            "status": ("not assessed" if not absolute_gate_assessable
                       else "pass" if float(estimates["absolute_error"].max()) <= lgd_limit
                       else "fail"),
        },
        {
            # No assessability caveat: an error expressed in standard errors
            # already accounts for the sample size, so this gate is meaningful
            # on a book of any size and is the one that actually guards Phase 5
            # in CI.
            "check": "that match is within sampling noise",
            "measure": "max error in standard errors",
            "value": float(estimates["errors_in_standard_errors"].max()),
            "limit": se_limit,
            "assessable": True,
            "passed": float(estimates["errors_in_standard_errors"].max()) <= se_limit,
            "status": ("pass" if float(estimates["errors_in_standard_errors"].max())
                       <= se_limit else "fail"),
        },
        {
            # Not a quality gate on the engine — a gate on the demonstration.
            # If the naive estimate stopped being biased, the phase would have
            # nothing to show, and silence would be the wrong response.
            "check": "the naive estimate is biased upward, as predicted",
            "measure": "naive minus resolved, in points",
            "value": float(bias["lgd_naive"] - bias["lgd_resolved"]),
            "limit": 0.0,
            "assessable": True,
            "passed": float(bias["lgd_naive"] - bias["lgd_resolved"]) > 0.0,
            "status": ("pass" if float(bias["lgd_naive"] - bias["lgd_resolved"]) > 0.0
                       else "fail"),
        },
        {
            "check": "SQL exposure matches the annuity formula",
            "measure": "max absolute difference, currency",
            "value": ead["max_absolute_difference"],
            "limit": ead_limit,
            "assessable": True,
            "passed": ead["max_absolute_difference"] <= ead_limit,
            "status": ("pass" if ead["max_absolute_difference"] <= ead_limit
                       else "fail"),
        },
    ]
    frame = pd.DataFrame(checks)
    frame["passed"] = frame["status"] != "fail"
    return frame, estimates, bias, ead


def lgd_report(connection, config, accounts=None):
    """Phase 5 in the form a credit committee would read it."""
    checks, estimates, bias, ead = validate(connection, config, accounts)

    lines = ["", "Loss given default", "-" * 18]
    lines.append(f"  {'segment':<12}{'resolved':>9}{'unresolved':>12}"
                 f"{'LGD (resolved)':>16}{'LGD (naive)':>13}{'true':>8}{'bias':>8}")
    for row in estimates.itertuples(index=False):
        lines.append(f"  {row.segment:<12}{row.resolved_defaults:>9,}"
                     f"{row.unresolved_defaults:>12,}"
                     f"{row.lgd_resolved_only:>15.2%} "
                     f"{row.lgd_all_defaults:>12.2%}"
                     f"{row.true_lgd:>8.2%}"
                     f"{row.censoring_bias:>+8.2%}")

    lines.append("")
    lines.append("  The workout censoring bias")
    lines.append(f"    resolved workouts          {int(bias['resolved']):>8,}"
                 f"   LGD {bias['lgd_resolved']:.2%}")
    lines.append(f"    unresolved workouts        {int(bias['unresolved']):>8,}"
                 f"   LGD {bias['lgd_unresolved']:.2%}  <- not LGD, LGD-so-far")
    lines.append(f"    unresolved share of defaults {bias['unresolved_share']:>6.1%}")
    lines.append(f"    naive estimate over all defaults  {bias['lgd_naive']:.2%}"
                 f"   ({bias['lgd_naive'] - bias['lgd_resolved']:+.2%})")
    lines.append("")
    lines.append("    An unresolved workout has collected only part of what it")
    lines.append("    will collect, so its loss-to-date overstates its loss. The")
    lines.append("    bias runs one way, and it runs toward a bigger provision.")

    lines.append("")
    lines.append("Exposure at default")
    lines.append("-" * 19)
    lines.append(f"  account-months projected   {ead['rows']:>12,}")
    lines.append(f"  max difference vs formula  {ead['max_absolute_difference']:>12.6f}")
    lines.append(f"  rows differing by > 1c     {ead['rows_over_a_cent']:>12,}")

    lines.append("")
    lines.append("Checks")
    lines.append("-" * 6)
    marks = {"pass": "PASS", "fail": "FAIL", "not assessed": " -- "}
    for row in checks.itertuples(index=False):
        lines.append(f"  [{marks[row.status]}] {row.check}")
        lines.append(f"         {row.measure}: {row.value:.6f} "
                     f"(limit {row.limit:.6f})")

    if (checks["status"] == "not assessed").any():
        worst = float(estimates["standard_error"].max())
        limit = float(config["validation"]
                      ["max_lgd_standard_error_for_absolute_gate"])
        lines.append("")
        lines.append(f"  The absolute LGD gate was NOT ASSESSED. The worst")
        lines.append(f"  segment standard error is {worst:.4f} against a")
        lines.append(f"  precision requirement of {limit:.4f}, so a "
                     f"{config['validation']['max_lgd_absolute_error']:.0%} ")
        lines.append("  materiality limit cannot be told apart from sampling")
        lines.append("  noise on a book this small. The standard-error gate")
        lines.append("  above still applies, and it is the one that accounts")
        lines.append("  for the sample size. The absolute figure is printed so")
        lines.append("  it can be seen, not so it can be believed.")

    return "\n".join(lines)
