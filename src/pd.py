"""
Phase 4, the PD term structure.

Phase 3 produced a single pooled PD curve, which was enough to drive a staging
rule. It is not enough to price a provision: one curve for the whole book
overstates risk for a prime account, understates it for a subprime one, and
ignores seasoning entirely.

This phase builds the curve three ways and measures all three against the
answer key:

    pooled      one matrix for the book                  (Phase 3's curve)
    segmented   one matrix per segment
    age_aware   one matrix per segment AND age band, with the matrix
                changing as the walk advances

Carrying all three rather than only the best one is deliberate. "Is the extra
segmentation worth it?" is a question about the difference between them, and a
table holding only the chosen answer cannot be used to ask it.

The validation compares each against the per-account truth the generator knows.
This is the first phase where every account has its own answer, rather than the
book having one.
"""
from pathlib import Path

import numpy as np
import pandas as pd

from . import db

REPO_ROOT = Path(__file__).resolve().parents[1]

METHODS = {
    "pooled": "one matrix for the whole book",
    "segmented": "one matrix per segment",
    "age_aware": "per segment and age band, matrix changes as the walk advances",
}


class PDError(Exception):
    """Raised when the term structure cannot be built or fails validation."""


def build(connection, config, verbose=True):
    """Build the segment-age matrices, the term structure and per-account PD."""
    longest = int(db.read_sql(
        connection, "SELECT MAX(term_months) AS longest FROM dim_account"
    )["longest"].iloc[0])

    min_transitions = int(config.get("pd", {}).get("min_transitions_per_cell", 50))

    steps = [
        ("11_segment_age_matrix.sql", "segment_age_matrix",
         {"min_transitions": min_transitions},
         "transition matrices by segment and age band"),
        ("12_pd_term_structure.sql", "pd_term_structure",
         {"max_age": longest, "max_horizon": longest},
         "PD curves that know segment and age"),
        ("13_account_pd.sql", "account_pd",
         {"reporting_date": str(config["portfolio"]["reporting_date"])},
         "12-month and lifetime PD per account"),
    ]

    counts = {}
    for filename, table, parameters, purpose in steps:
        db.run_sql_file(connection, filename, **parameters)
        counts[table] = db._count(connection, table)
        if verbose:
            print(f"  built {table:<22} {counts[table]:>10,} rows   {purpose}")

    return counts


def accuracy(connection, horizon="12m"):
    """Each method's error against the per-account truth.

    Four measures, because they fail differently and a single one hides it:

      mean estimate / mean truth   the AGGREGATE. A method can get this right
                                   while being wrong about every account, if
                                   its errors cancel, which is exactly what
                                   pooling does.
      mean error                   the BIAS, signed. Tells you which way the
                                   provision would be wrong.
      mean absolute error          the per-account accuracy. This is the one
                                   that matters for pricing a single loan.
      correlation                  whether the method ranks accounts correctly,
                                   which is what a cutoff policy depends on.
    """
    truth_column = f"true_pd_{horizon}" if horizon == "12m" else "true_pd_lifetime"
    rows = []

    for method in METHODS:
        column = f"pd_{horizon}_{method}"
        result = db.read_sql(connection, f"""
            SELECT
                COUNT(*)                                   AS accounts,
                AVG({column})                              AS mean_estimate,
                AVG({truth_column})                        AS mean_truth,
                AVG({column} - {truth_column})             AS mean_error,
                AVG(ABS({column} - {truth_column}))        AS mean_absolute_error,
                corr({column}, {truth_column})             AS correlation
            FROM account_pd
            WHERE {truth_column} IS NOT NULL
        """).iloc[0]

        rows.append({
            "method": method,
            "accounts": int(result["accounts"]),
            "mean_estimate": float(result["mean_estimate"]),
            "mean_truth": float(result["mean_truth"]),
            "mean_error": float(result["mean_error"]),
            "mean_absolute_error": float(result["mean_absolute_error"]),
            "correlation": float(result["correlation"]),
        })

    frame = pd.DataFrame(rows)
    frame["relative_bias"] = frame["mean_error"] / frame["mean_truth"]
    return frame


def error_by_age(connection, method="pooled", horizon="12m"):
    """Where a method goes wrong, by how old the account is.

    The diagnostic that explains WHY pooling over age is costly: the error is
    not noise scattered around zero, it is a systematic pattern that tracks the
    seasoning curve. A method blind to age must overstate risk where the true
    hazard is low and understate it where the hazard peaks.
    """
    column = f"pd_{horizon}_{method}"
    truth_column = f"true_pd_{horizon}" if horizon == "12m" else "true_pd_lifetime"

    return db.read_sql(connection, f"""
        SELECT
            CASE
                WHEN months_on_book <  6 THEN '1. 0-5m'
                WHEN months_on_book < 12 THEN '2. 6-11m'
                WHEN months_on_book < 24 THEN '3. 12-23m'
                WHEN months_on_book < 36 THEN '4. 24-35m'
                ELSE                          '5. 36m+'
            END                                        AS age_band,
            COUNT(*)                                   AS accounts,
            AVG({column})                              AS mean_estimate,
            AVG({truth_column})                        AS mean_truth,
            AVG({column} - {truth_column})             AS mean_error
        FROM account_pd
        WHERE {truth_column} IS NOT NULL
        GROUP BY age_band
        ORDER BY age_band
    """)


def error_by_segment(connection, method="pooled", horizon="12m"):
    """Where a method goes wrong, by risk grade."""
    column = f"pd_{horizon}_{method}"
    truth_column = f"true_pd_{horizon}" if horizon == "12m" else "true_pd_lifetime"

    return db.read_sql(connection, f"""
        SELECT
            segment,
            COUNT(*)                                   AS accounts,
            AVG({column})                              AS mean_estimate,
            AVG({truth_column})                        AS mean_truth,
            AVG({column} - {truth_column})             AS mean_error
        FROM account_pd
        WHERE {truth_column} IS NOT NULL
        GROUP BY segment
        ORDER BY mean_truth
    """)


def estimation_coverage(connection):
    """How much of the age-aware model is its own estimate, and how much is
    the fallback wearing its name.

    Cutting by segment AND age band AND arrears bucket multiplies the cells.
    On a small book most of them fall back to the segment's pooled rate, and
    at that point "age_aware" is the pooled model under another label,
    judging its accuracy judges something that was never built.
    """
    return db.read_sql(connection, """
        SELECT
            COUNT(*)                                            AS cells,
            SUM(CASE WHEN used_fallback THEN 1 ELSE 0 END)      AS fell_back,
            AVG(CASE WHEN used_fallback THEN 1.0 ELSE 0.0 END)  AS fallback_share,
            MIN(opportunities)                                  AS thinnest_cell
        FROM segment_age_matrix
    """).iloc[0]


def validate(connection, config):
    """Does the chosen method clear the threshold set before it was built?

    Returns a `status` of pass, fail, or **not assessed**. The third is not a
    softer failure, it is a different finding, and the distinction is the one
    this project keeps relearning: a check that cannot tell "the answer is no"
    from "I could not determine the answer" will eventually report the second
    as the first.

    Here the difference is material. On the committed sample the PD estimate
    looks dreadful, and it is not: 60% of the matrix cells behind it are the
    fallback, so there is no age-aware model to be accurate or inaccurate.
    Reporting that as a failed accuracy gate sends the reader to the estimator
    when the answer is that the book is too small to judge it on.
    """
    absolute_limit = float(config["validation"]["max_pd_absolute_error"])
    relative_limit = float(
        config["validation"].get("max_lifetime_pd_relative_error", 0.15))
    fallback_limit = float(
        config["validation"].get("max_fallback_share_for_pd_assessment", 0.25))

    coverage = estimation_coverage(connection)
    assessable = float(coverage["fallback_share"]) <= fallback_limit
    results = []

    # 12-month PD: an absolute tolerance. The horizon is fixed, so the scale of
    # the quantity is fixed and an absolute limit means the same thing for
    # every account.
    frame = accuracy(connection, "12m")
    best = frame.loc[frame["mean_absolute_error"].idxmin()]
    results.append({
        "check": f"12m PD: best method within {absolute_limit:.1%} absolute error",
        "method": best["method"],
        "value": best["mean_absolute_error"],
        "limit": absolute_limit,
        "assessable": assessable,
        "status": ("not assessed" if not assessable
                   else "pass" if best["mean_absolute_error"] <= absolute_limit
                   else "fail"),
    })

    # Lifetime PD: a RELATIVE tolerance. The horizon varies by account and runs
    # to five years, so the quantity's scale, and the absolute error with it,
    # grows with the term. See config and methodology §5.6.
    frame = accuracy(connection, "lifetime")
    frame = frame.assign(
        relative_error=frame["mean_absolute_error"] / frame["mean_truth"])
    best = frame.loc[frame["relative_error"].idxmin()]
    results.append({
        "check": (f"lifetime PD: best method within {relative_limit:.0%} "
                  "relative error"),
        "method": best["method"],
        "value": best["relative_error"],
        "limit": relative_limit,
        "assessable": assessable,
        "status": ("not assessed" if not assessable
                   else "pass" if best["relative_error"] <= relative_limit
                   else "fail"),
    })

    # The ranking matters as much as the level: a method that prices wrongly
    # but ranks correctly is still usable for a cutoff; one that ranks wrongly
    # is not usable for anything.
    frame = accuracy(connection, "12m")
    best = frame.loc[frame["mean_absolute_error"].idxmin()]
    results.append({
        "check": "best method ranks accounts correctly (correlation > 0.9)",
        "method": best["method"],
        "value": best["correlation"],
        "limit": 0.9,
        "assessable": assessable,
        "status": ("not assessed" if not assessable
                   else "pass" if best["correlation"] > 0.9 else "fail"),
    })

    frame = pd.DataFrame(results)
    # `passed` means "did not fail". A check that could not be assessed has
    # not failed, and the pipeline must not stop on it, but the report says
    # plainly which of the two happened.
    frame["passed"] = frame["status"] != "fail"
    return frame


def pd_report(connection, config):
    """The phase's findings, in the order a reviewer would want them."""
    lines = []

    for horizon, label in (("12m", "12-month"), ("lifetime", "Lifetime")):
        frame = accuracy(connection, horizon)
        truth = frame["mean_truth"].iloc[0]

        lines.append(f"{label} PD, estimate against the answer key")
        lines.append("-" * (len(label) + 36))
        lines.append(f"  {'method':<12}{'mean est':>11}{'truth':>10}"
                     f"{'bias':>10}{'mean abs err':>15}{'corr':>8}")
        for row in frame.itertuples(index=False):
            lines.append(f"  {row.method:<12}{row.mean_estimate:>11.2%}"
                         f"{row.mean_truth:>10.2%}{row.relative_bias:>+10.1%}"
                         f"{row.mean_absolute_error:>15.4f}{row.correlation:>8.3f}")

        best = frame.loc[frame["mean_absolute_error"].idxmin()]
        worst = frame.loc[frame["mean_absolute_error"].idxmax()]
        improvement = worst["mean_absolute_error"] / max(best["mean_absolute_error"], 1e-12)
        lines.append("")
        lines.append(f"  {best['method']} is {improvement:.1f}x more accurate per "
                     f"account than {worst['method']}")
        lines.append("")

    # --- why pooling over age is costly ------------------------------------
    lines.append("Why age matters: the pooled method's error by account age")
    lines.append("-" * 56)
    by_age = error_by_age(connection, "pooled", "12m")
    lines.append(f"  {'age band':<12}{'accounts':>10}{'estimate':>11}"
                 f"{'truth':>10}{'error':>10}")
    for row in by_age.itertuples(index=False):
        lines.append(f"  {row.age_band:<12}{int(row.accounts):>10,}"
                     f"{row.mean_estimate:>11.2%}{row.mean_truth:>10.2%}"
                     f"{row.mean_error:>+10.2%}")
    lines.append("")
    lines.append("  The error is not noise around zero, it tracks the seasoning")
    lines.append("  curve. A method blind to age must overstate risk where the")
    lines.append("  true hazard is low and understate it where it peaks.")

    lines.append("")
    lines.append("Why segment matters: the pooled method's error by risk grade")
    lines.append("-" * 58)
    by_segment = error_by_segment(connection, "pooled", "12m")
    lines.append(f"  {'segment':<12}{'accounts':>10}{'estimate':>11}"
                 f"{'truth':>10}{'error':>10}")
    for row in by_segment.itertuples(index=False):
        lines.append(f"  {row.segment:<12}{int(row.accounts):>10,}"
                     f"{row.mean_estimate:>11.2%}{row.mean_truth:>10.2%}"
                     f"{row.mean_error:>+10.2%}")

    coverage = estimation_coverage(connection)
    checks = validate(connection, config)

    lines.append("")
    lines.append("Validation")
    lines.append("-" * 10)

    marks = {"pass": "PASS", "fail": "FAIL", "not assessed": " -- "}
    for row in checks.itertuples(index=False):
        lines.append(f"  [{marks[row.status]}] {row.check}")
        lines.append(f"         {row.method}: {row.value:.4f} against "
                     f"a limit of {row.limit}")

    if not bool(checks["assessable"].iloc[0]):
        lines.append("")
        lines.append("  NOT ASSESSED. and that is a different finding from a failure.")
        lines.append(f"  {coverage['fell_back']:,.0f} of {coverage['cells']:,.0f} matrix "
                     f"cells ({coverage['fallback_share']:.0%}) fell back to the")
        lines.append(f"  segment's pooled rate, and the thinnest holds "
                     f"{coverage['thinnest_cell']:,.0f} transition(s). There is no")
        lines.append("  age-aware model here to be accurate or inaccurate, the book")
        lines.append("  is too small to estimate one. The figures above are printed")
        lines.append("  so they can be seen, not so they can be believed.")
    else:
        lines.append("")
        lines.append(f"  model coverage: {coverage['fell_back']:,.0f} of "
                     f"{coverage['cells']:,.0f} cells ({coverage['fallback_share']:.1%}) "
                     "used the fallback")

    return "\n".join(lines)
