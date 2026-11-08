"""
Phase 7, vintage curves and portfolio MI.

Two views of the same book that answer different questions, and a demonstration
of what goes wrong when they are confused.

CALENDAR TIME (`portfolio_mi`) says what the book looks like now and how it got
here. It cannot say why: a rising Stage 2 share means lending has deteriorated,
or it means last year's growth is reaching the ages where arrears appear, and
nothing in calendar time separates those.

COHORT TIME (`vintage_curves`) holds age fixed and can. Group accounts by the
month they were originated, track each cohort's cumulative default rate by age,
and compare cohorts only at ages they have all reached.

That last clause is the whole difficulty. A cohort originated last December has
been watched for twelve months; one from four years ago for fifty-nine. Compare
their cumulative default rates as they stand and the newer one always wins, for
reasons that have nothing to do with credit. On this book that artefact
manufactures four percentage points a year of imaginary improvement, see
`censoring_artefact()`.

The generator draws segment and origination month INDEPENDENTLY, so every
cohort is the same book and the true vintage effect is exactly zero. That makes
the validation unusually strong: the engine must conclude "no vintage effect",
and anything else it concludes is a false positive it manufactured itself.
"""
import numpy as np
import pandas as pd
from scipy import stats

from . import db

SQL_FILES = ["19_vintage_curves.sql", "20_vintage_comparison.sql",
             "21_portfolio_mi.sql"]


def build(connection, config, verbose=True):
    """Build the triangle, the fair comparison and the calendar-time MI."""
    oldest = int(db.read_sql(
        connection,
        "SELECT MAX(months_on_book) AS m FROM fact_loan_month")["m"].iloc[0])

    # The comparison age: the oldest age the YOUNGEST cohort has reached, so
    # every cohort is included and none is carried forward. Derived from the
    # data rather than configured, because a hard-coded age would silently
    # start excluding cohorts, or silently start comparing censored ones, as
    # soon as the reporting date or the origination window moved.
    common_age = int(db.read_sql(connection, """
        SELECT MIN(watched) AS m FROM (
            SELECT a.vintage_month, MAX(f.months_on_book) AS watched
            FROM dim_account a
            JOIN fact_loan_month f ON f.account_id = a.account_id
            GROUP BY a.vintage_month)""")["m"].iloc[0])

    for name in SQL_FILES:
        if verbose:
            print(f"  {name}")
        db.run_sql_file(connection, name, max_age=oldest, common_age=common_age)

    if verbose:
        cohorts = db.read_sql(
            connection,
            "SELECT COUNT(DISTINCT vintage_month) AS n FROM vintage_curves"
        )["n"].iloc[0]
        print(f"  {cohorts} monthly cohorts, watched to {oldest} months, "
              f"compared at {common_age}")
    return common_age


def curves(connection, uncensored_only=True):
    """The vintage triangle."""
    where = "WHERE NOT is_censored" if uncensored_only else ""
    return db.read_sql(
        connection,
        f"SELECT * FROM vintage_curves {where} ORDER BY vintage_month_index, age")


def comparison(connection):
    """Every cohort at a common age, with the naive figure beside it."""
    return db.read_sql(connection,
                       "SELECT * FROM vintage_comparison "
                       "ORDER BY vintage_month_index")


def censoring_artefact(connection):
    """How much apparent credit improvement is manufactured by the calendar.

    Fits the cohort default rate against origination month twice: once on the
    naive figure every cohort happens to show today, once on the fair figure at
    a common age. The first slope is the signal an unguarded vintage table
    reports. The second is the truth.

    The direction is predictable before measuring: a newer cohort has lived
    less of its life, so its cumulative rate is lower, so the naive slope must
    be negative on a book with a growing origination window. The size is not
    predictable, and is the reason to measure rather than warn.
    """
    frame = comparison(connection)
    months = frame["vintage_month_index"].to_numpy(dtype=float)
    out = {"cohorts": int(len(frame))}

    for column, label in (("rate_latest", "naive"),
                          ("rate_at_common_age", "fair")):
        rate = frame[column].to_numpy(dtype=float)
        slope, _ = np.polyfit(months, rate, 1)
        out[f"{label}_slope_per_year"] = float(slope * 12.0)
        out[f"{label}_correlation"] = float(np.corrcoef(months, rate)[0, 1])

    out["artefact_share"] = float(
        1.0 - abs(out["fair_slope_per_year"])
        / max(abs(out["naive_slope_per_year"]), 1e-12))
    # A difference of two rates, so it is in percentage POINTS. Named and
    # formatted as such wherever it is printed: calling 0.1848 "18.48%" invites
    # the reader to divide it by the base rate, which is a different quantity.
    out["worst_naive_excess"] = float(frame["naive_excess"].max())
    # The cohort that excess belongs to, and how much longer it was watched.
    # Reported together because the two numbers are the cause and the effect,
    # and quoting the excess beside the common age (rather than beside the
    # cohort's own extra months) made the first version of the report say a
    # cohort watched for 47 months had been watched "12 months longer".
    worst = frame.loc[frame["naive_excess"].idxmax()]
    out["worst_naive_cohort"] = str(worst["vintage_month"])
    out["worst_extra_months"] = int(worst["extra_months_watched"])
    out["common_age"] = int(frame["common_age"].iloc[0])
    return out


def noise_test(connection):
    """Is the spread between cohorts bigger than chance explains?

    On this book the answer must be no, because cohort quality is constant by
    construction. Reported as both a variance ratio and a chi-square, because
    they fail differently: the ratio says how much excess dispersion there is,
    the test says whether that much is surprising.

    Also reports the smallest vintage effect this book could actually DETECT,
    which is the number that decides whether monthly cohorts are worth charting
    at all.
    """
    frame = comparison(connection)
    sizes = frame["cohort_size"].to_numpy(dtype=float)
    defaults = frame["defaults_at_common_age"].to_numpy(dtype=float)
    rates = frame["rate_at_common_age"].to_numpy(dtype=float)

    pooled = float(defaults.sum() / sizes.sum())
    mean_size = float(sizes.mean())
    expected_sd = float(np.sqrt(pooled * (1.0 - pooled) / mean_size))
    observed_sd = float(rates.std(ddof=1))

    expected_counts = sizes * pooled
    chi_square = float(
        (((defaults - expected_counts) ** 2)
         / (sizes * pooled * (1.0 - pooled))).sum())
    degrees = len(frame) - 1
    p_value = float(stats.chi2.sf(chi_square, degrees))

    # Minimum detectable difference between two cohorts, 80% power at 5%, which
    # is 2.8 standard errors of the difference.
    def detectable(group_months):
        se = np.sqrt(2.0 * pooled * (1.0 - pooled) / (mean_size * group_months))
        return float(2.8 * se)

    return {
        "cohorts": int(len(frame)),
        "mean_cohort_size": mean_size,
        "pooled_rate": pooled,
        "observed_sd": observed_sd,
        "expected_sd": expected_sd,
        "dispersion_ratio": float(observed_sd / expected_sd) if expected_sd else np.nan,
        "chi_square": chi_square,
        "degrees_of_freedom": degrees,
        "p_value": p_value,
        "vintage_effect_detected": bool(p_value < 0.05),
        "detectable_monthly": detectable(1),
        "detectable_quarterly": detectable(3),
        "detectable_annual": detectable(12),
    }


def calendar_mi(connection):
    """The book month by month."""
    return db.read_sql(connection, "SELECT * FROM portfolio_mi ORDER BY month_index")


def age_profile_drift(connection):
    """How much of the calendar-time movement is simply the book ageing.

    The question `portfolio_mi` cannot answer on its own. If the average age of
    the book is rising and cohort quality is flat, then a worsening arrears
    rate is maturity, not deterioration, and the correlation between the two
    is the cheapest available evidence for which it is.
    """
    frame = calendar_mi(connection)
    # The first year is excluded: a book that starts empty has almost no
    # accounts and an age profile dominated by whoever was originated first, so
    # its ratios swing for arithmetic reasons rather than credit ones.
    settled = frame[frame["accounts_on_book"] >= 0.2 * frame["accounts_on_book"].max()]

    age = settled["average_age_months"].to_numpy(dtype=float)
    arrears = settled["in_arrears_share"].to_numpy(dtype=float)
    stage_2 = settled["stage_2_share"].to_numpy(dtype=float)

    return {
        "months": int(len(settled)),
        "age_at_start": float(age[0]),
        "age_at_end": float(age[-1]),
        "arrears_at_start": float(arrears[0]),
        "arrears_at_end": float(arrears[-1]),
        "age_vs_arrears_correlation": float(np.corrcoef(age, arrears)[0, 1]),
        "age_vs_stage_2_correlation": float(np.corrcoef(age, stage_2)[0, 1]),
    }


def gate_precision(connection, config):
    """Can the vintage statistics be judged on a book this size?

    Decided from the SAMPLE, how many accounts are in a cohort and how many
    defaults are expected in one, before any slope or p-value is looked at.
    Vintage analysis divides the book by origination month, so the cohort is
    the sample, and a book that is ample for a portfolio statistic can be
    hopeless for a cohort one: the 400-account sample gives 8 accounts and 0.4
    expected defaults per cohort.

    Two conditions, for two kinds of gate:

      TREND     the standard error of the fitted slope must be at most half the
                tolerance, so a gate meant to catch an invented trend is not
                itself reporting noise. On the sample that slope has a standard
                error of 0.92pp/yr against a 0.5pp/yr limit, and the fitted
                value of -1.56pp/yr sits 1.7 standard errors from zero, no
                evidence of anything, and a failure under the unguarded gate.

      DISPERSION  the expected default count per cohort must be at least 5, the
                standard validity condition for a chi-square. Not a threshold
                invented for this project.
    """
    limits = config["validation"]
    frame = comparison(connection)
    noise_inputs = db.read_sql(connection, """
        SELECT SUM(defaults_at_common_age) AS defaults,
               SUM(cohort_size)            AS accounts,
               AVG(cohort_size)            AS mean_size
        FROM vintage_comparison""").iloc[0]

    pooled = float(noise_inputs["defaults"]) / float(noise_inputs["accounts"])
    mean_size = float(noise_inputs["mean_size"])
    expected_defaults = mean_size * pooled

    # Standard error of the ordinary-least-squares slope, in rate per year.
    months = frame["vintage_month_index"].to_numpy(dtype=float)
    spread = float(((months - months.mean()) ** 2).sum())
    slope_se = float(
        np.sqrt(pooled * (1.0 - pooled) / mean_size / spread) * 12.0
    ) if spread > 0 and mean_size > 0 else np.inf

    slope_limit = float(limits["max_spurious_vintage_slope_per_year"])
    fraction = float(limits["max_vintage_slope_se_fraction_of_limit"])
    min_expected = float(limits["min_expected_defaults_per_cohort"])

    return {
        "mean_cohort_size": mean_size,
        "expected_defaults_per_cohort": expected_defaults,
        "slope_standard_error_per_year": slope_se,
        "trend_assessable": bool(slope_se <= slope_limit * fraction),
        "dispersion_assessable": bool(expected_defaults >= min_expected),
    }


def validate(connection, config):
    """Check the MI against what is known to be true of this book.

    The known truth is unusually complete here: segment and origination month
    are drawn independently, so the true vintage effect is exactly zero. The
    engine is therefore checked for FALSE POSITIVES, which is the opposite
    direction from every earlier phase and the more useful one for MI. An MI
    pack that invents a trend costs a credit committee a policy change it did
    not need.
    """
    limits = config["validation"]

    # Decided FIRST, from the sample, before any statistic below is computed.
    precision = gate_precision(connection, config)
    trend_ok = precision["trend_assessable"]
    dispersion_ok = precision["dispersion_assessable"]

    artefact = censoring_artefact(connection)
    noise = noise_test(connection)
    drift = age_profile_drift(connection)

    censored_used = int(db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM vintage_comparison c
        JOIN vintage_curves v
          ON v.vintage_month = c.vintage_month AND v.age = c.common_age
        WHERE v.is_censored""")["n"].iloc[0])

    tie_out = int(db.read_sql(connection, """
        SELECT ABS(
            (SELECT SUM(defaults_by_age) FROM vintage_curves
             WHERE age = (SELECT MAX(age) FROM vintage_curves))
          - (SELECT COUNT(*) FROM fact_loan_month WHERE is_new_default)
        ) AS d""")["d"].iloc[0])

    def graded(passed, assessable):
        if not assessable:
            return "not assessed"
        return "pass" if passed else "fail"

    slope_limit = float(limits["max_spurious_vintage_slope_per_year"])
    dispersion_limit = float(limits["max_vintage_dispersion_ratio"])

    checks = [
        {
            # THE HEADLINE. There is no vintage effect, so the fair comparison
            # must not find one.
            "check": "the fair comparison finds no vintage trend",
            "measure": "fair slope, pp of default rate per year",
            "value": abs(artefact["fair_slope_per_year"]),
            "limit": slope_limit,
            "assessable": trend_ok,
            "status": graded(
                abs(artefact["fair_slope_per_year"]) <= slope_limit, trend_ok),
        },
        {
            "check": "and no cohort-to-cohort spread beyond chance",
            "measure": "chi-square p-value across cohorts",
            "value": noise["p_value"],
            "limit": 0.05,
            "assessable": dispersion_ok,
            "status": graded(noise["p_value"] >= 0.05, dispersion_ok),
        },
        {
            "check": "dispersion between cohorts matches binomial noise",
            "measure": "observed SD / expected SD",
            "value": noise["dispersion_ratio"],
            "limit": dispersion_limit,
            "assessable": dispersion_ok,
            "status": graded(
                noise["dispersion_ratio"] <= dispersion_limit, dispersion_ok),
        },
        {
            # Not a quality gate on the engine, a gate on the demonstration.
            # If the naive comparison stopped being badly wrong, the phase
            # would have nothing to show, and silence would be the wrong
            # response. Same role as Phase 5's bias check.
            "check": "the naive comparison is badly misleading, as predicted",
            "measure": "share of the apparent trend that is artefact",
            "value": artefact["artefact_share"],
            "limit": 0.90,
            "assessable": trend_ok,
            "status": graded(artefact["artefact_share"] >= 0.90, trend_ok),
        },
        {
            # Structural, not statistical: true or false at any book size, so
            # it always applies.
            "check": "every cohort is compared at an age it reached",
            "measure": "censored cells used in the comparison",
            "value": float(censored_used),
            "limit": 0.0,
            "assessable": True,
            "status": "pass" if censored_used == 0 else "fail",
        },
        {
            # Also structural: every default on the book must appear in exactly
            # one cohort, which is arithmetic rather than inference.
            "check": "the vintage triangle ties to the fact table",
            "measure": "defaults in the triangle minus defaults on the book",
            "value": float(tie_out),
            "limit": 0.0,
            "assessable": True,
            "status": "pass" if tie_out == 0 else "fail",
        },
    ]
    frame = pd.DataFrame(checks)
    frame["passed"] = frame["status"] != "fail"
    return frame, artefact, noise, drift


def vintage_report(connection, config):
    """Phase 7 as a credit committee would read it."""
    checks, artefact, noise, drift = validate(connection, config)
    frame = comparison(connection)

    lines = ["", "Vintage curves", "-" * 14]
    lines.append(f"  {noise['cohorts']} monthly cohorts, "
                 f"{noise['mean_cohort_size']:,.0f} accounts each on average")
    lines.append(f"  compared at a common age of {artefact['common_age']} months, "
                 f"the oldest age every cohort has reached")

    lines.append("")
    lines.append("  A sample of the triangle, cumulative default rate by age:")
    sample = db.read_sql(connection, """
        SELECT vintage_month, cohort_size, watched_to_age,
               MAX(CASE WHEN age = 12 THEN cumulative_default_rate END) AS at_12,
               MAX(CASE WHEN age = 24 THEN cumulative_default_rate END) AS at_24,
               MAX(CASE WHEN age = 36 THEN cumulative_default_rate END) AS at_36,
               MAX(CASE WHEN age = 48 THEN cumulative_default_rate END) AS at_48
        FROM vintage_curves
        WHERE NOT is_censored
          AND vintage_month IN ('2021-01','2021-07','2022-01','2022-07',
                                '2023-01','2023-07','2024-01','2024-07','2024-12')
        GROUP BY vintage_month, cohort_size, watched_to_age
        ORDER BY vintage_month""")
    lines.append(f"  {'vintage':<10}{'size':>6}{'watched':>9}"
                 f"{'age 12':>9}{'age 24':>9}{'age 36':>9}{'age 48':>9}")
    for row in sample.itertuples(index=False):
        cells = "".join(
            f"{value:>9.2%}" if value is not None and not pd.isna(value)
            else f"{'--':>9}"
            for value in (row.at_12, row.at_24, row.at_36, row.at_48))
        lines.append(f"  {row.vintage_month:<10}{row.cohort_size:>6,}"
                     f"{row.watched_to_age:>9}{cells}")
    lines.append("    -- means the cohort has not lived that long yet. Those")
    lines.append("    cells are the trap, and they are left blank on purpose.")

    lines.append("")
    lines.append("Is lending getting better or worse?")
    lines.append("-" * 35)
    lines.append("  The same question, answered twice.")
    lines.append(f"    naive, each cohort at its own latest age")
    lines.append(f"      trend {artefact['naive_slope_per_year']:+.2%} per year"
                 f"   (correlation {artefact['naive_correlation']:+.3f})")
    lines.append(f"    fair, every cohort at {artefact['common_age']} months")
    lines.append(f"      trend {artefact['fair_slope_per_year']:+.2%} per year"
                 f"   (correlation {artefact['fair_correlation']:+.3f})")
    lines.append("")
    lines.append(f"    {artefact['artefact_share']:.1%} of the apparent "
                 f"improvement is artefact.")
    lines.append(f"    The {artefact['worst_naive_cohort']} cohort's naive rate "
                 f"sits {artefact['worst_naive_excess'] * 100:.2f}pp")
    lines.append(f"    above its {artefact['common_age']}-month rate, not "
                 f"because it lent badly,")
    lines.append(f"    but because it has been watched "
                 f"{artefact['worst_extra_months']} months longer.")

    lines.append("")
    lines.append("Is the remaining spread real?")
    lines.append("-" * 29)
    lines.append(f"    pooled {artefact['common_age']}-month default rate "
                 f"{noise['pooled_rate']:>8.2%}")
    lines.append(f"    observed SD across cohorts       {noise['observed_sd']:>8.4f}")
    lines.append(f"    expected SD if purely chance     {noise['expected_sd']:>8.4f}")
    lines.append(f"    ratio                            "
                 f"{noise['dispersion_ratio']:>8.3f}")
    lines.append(f"    chi-square {noise['chi_square']:.2f} on "
                 f"{noise['degrees_of_freedom']} df, p = {noise['p_value']:.3f}")
    verdict = ("a vintage effect IS detected" if noise["vintage_effect_detected"]
               else "no vintage effect is detectable, which is correct")
    lines.append(f"    -> {verdict}")

    lines.append("")
    lines.append("  Smallest difference between two cohorts this book could")
    lines.append("  detect at all (80% power), by how cohorts are grouped:")
    lines.append(f"    monthly    {noise['detectable_monthly']:>7.2%}")
    lines.append(f"    quarterly  {noise['detectable_quarterly']:>7.2%}")
    lines.append(f"    annual     {noise['detectable_annual']:>7.2%}")
    lines.append("")
    lines.append(f"    Against a {noise['pooled_rate']:.2%} base rate, monthly "
                 f"cohorts cannot")
    lines.append(f"    detect anything smaller than "
                 f"{noise['detectable_monthly'] / noise['pooled_rate']:.0%} of the "
                 f"base rate itself.")
    lines.append("    A monthly vintage chart on a book this size is mostly a")
    lines.append("    picture of sampling noise. Quarterly is the minimum, and")
    lines.append("    annual is the first grouping that could see a 1pp shift.")

    lines.append("")
    lines.append("Calendar time, and why it cannot answer the question")
    lines.append("-" * 52)
    lines.append(f"  Over {drift['months']} settled months:")
    lines.append(f"    average age of the book  "
                 f"{drift['age_at_start']:.1f} -> {drift['age_at_end']:.1f} months")
    lines.append(f"    accounts in arrears      "
                 f"{drift['arrears_at_start']:.2%} -> {drift['arrears_at_end']:.2%}")
    lines.append(f"    correlation, age vs arrears   "
                 f"{drift['age_vs_arrears_correlation']:+.3f}")
    lines.append(f"    correlation, age vs Stage 2   "
                 f"{drift['age_vs_stage_2_correlation']:+.3f}")
    lines.append("")
    lines.append("    Arrears rose as the book aged, and cohort quality never")
    lines.append("    changed. In calendar time that looks like deterioration;")
    lines.append("    in cohort time it is maturity. Only the second view can")
    lines.append("    tell them apart, which is the whole reason to build it.")

    lines.append("")
    lines.append("Checks")
    lines.append("-" * 6)
    marks = {"pass": "PASS", "fail": "FAIL", "not assessed": " -- "}
    for row in checks.itertuples(index=False):
        lines.append(f"  [{marks[row.status]}] {row.check}")
        lines.append(f"         {row.measure}: {row.value:.6g} "
                     f"(limit {row.limit:.6g})")

    if (checks["status"] == "not assessed").any():
        precision = gate_precision(connection, config)
        lines.append("")
        lines.append("  Some checks were NOT ASSESSED. Vintage analysis divides")
        lines.append("  the book by origination month, so the COHORT is the")
        lines.append("  sample, and this one is too thin to support them:")
        lines.append(f"    accounts per cohort            "
                     f"{precision['mean_cohort_size']:>8.1f}")
        lines.append(f"    expected defaults per cohort   "
                     f"{precision['expected_defaults_per_cohort']:>8.2f}"
                     f"   (a chi-square needs 5)")
        lines.append(f"    standard error of the slope    "
                     f"{precision['slope_standard_error_per_year']:>8.4f}/yr"
                     f" (limit is "
                     f"{config['validation']['max_spurious_vintage_slope_per_year']:.4f})")
        lines.append("")
        lines.append("    A gate meant to catch an invented trend must not")
        lines.append("    itself be reporting noise. The figures above are")
        lines.append("    printed so they can be seen, not so they can be")
        lines.append("    believed.")

    return "\n".join(lines)
