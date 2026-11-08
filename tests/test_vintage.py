"""
Phase 7 tests, vintage curves and portfolio MI.

THIS FILE CHECKS FOR FALSE POSITIVES, which is the opposite direction from
every earlier phase and the more useful one for management information.

The generator draws segment and origination month independently, so every
cohort on this book is the same book and the true vintage effect is exactly
zero. The engine must therefore find nothing, and `test_the_fair_comparison_
finds_no_vintage_trend` is the assertion that it does not invent one. An MI
pack that manufactures a trend costs a credit committee a policy change it did
not need, which is a more expensive mistake than a provision a few percent out.

`test_the_naive_comparison_invents_a_trend_that_is_not_there` is its partner
and the reason the first test means anything: it shows that the obvious way to
read a vintage table produces four percentage points a year of imaginary
improvement on this same data. Without it, "found no trend" could just as
easily mean the analysis is incapable of finding one.

Run with:  pytest tests/test_vintage.py -v
"""
import inspect
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed
from src import db, generate, mart, rollrate, staging, vintage


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def built(config, tmp_path_factory):
    """A book big enough for cohorts to mean anything.

    Vintage analysis divides the book by origination month, so the cohort, not
    the book, is the sample. 12,000 accounts over 48 months is roughly 250 per
    cohort, which is thin on purpose: it is the regime real monthly vintage MI
    operates in, and the regime `test_monthly_cohorts_cannot_detect_much` is
    about.
    """
    path = tmp_path_factory.mktemp("vintage") / "test.duckdb"

    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=12000)

    rng = set_seed(31415)
    accounts, performance, truth, recoveries = generate.generate_book(sized, rng)
    monthly = generate.true_monthly_schedule(accounts, sized, performance)

    connection = db.connect(sized, path)
    for name, frame in (("accounts", accounts), ("loan_month", performance),
                        ("ground_truth", truth), ("recovery", recoveries),
                        ("ground_truth_monthly", monthly)):
        db.write_table(connection, name, frame)
    mart.build(connection, sized, verbose=False)
    rollrate.build(connection, verbose=False)
    staging.build(connection, sized, verbose=False)
    common_age = vintage.build(connection, sized, verbose=False)

    yield connection, sized, common_age
    connection.close()


# --- The triangle is honest about what it does not know --------------------

def test_censored_cells_are_flagged_not_hidden(built):
    """A cohort cannot have a default rate at an age it has not reached.

    The table keeps those cells and marks them rather than dropping them,
    because a consumer who joins to the triangle and finds no row silently gets
    no number, whereas one who finds a flagged row has to decide what to do
    about it.
    """
    connection, _, _ = built
    shape = db.read_sql(connection, """
        SELECT
            SUM(CASE WHEN is_censored THEN 1 ELSE 0 END)       AS censored,
            SUM(CASE WHEN NOT is_censored THEN 1 ELSE 0 END)   AS observed,
            SUM(CASE WHEN is_censored AND age <= watched_to_age
                     THEN 1 ELSE 0 END)                        AS wrongly_flagged,
            SUM(CASE WHEN NOT is_censored AND age > watched_to_age
                     THEN 1 ELSE 0 END)                        AS wrongly_cleared
        FROM vintage_curves""").iloc[0]

    assert shape["censored"] > 0, "a right-censored triangle must have censored cells"
    assert shape["observed"] > 0
    assert shape["wrongly_flagged"] == 0
    assert shape["wrongly_cleared"] == 0


def test_the_cumulative_curve_never_falls(built):
    """Default is absorbing, so a cohort's cumulative rate cannot decrease.

    If it ever did, accounts would be leaving the numerator or entering the
    denominator at older ages, both of which would quietly change what the
    curve means.
    """
    connection, _, _ = built
    falls = db.read_sql(connection, """
        WITH stepped AS (
            SELECT vintage_month, age, cumulative_default_rate,
                   LAG(cumulative_default_rate)
                       OVER (PARTITION BY vintage_month ORDER BY age) AS previous
            FROM vintage_curves)
        SELECT COUNT(*) AS n FROM stepped
        WHERE previous IS NOT NULL
          AND cumulative_default_rate < previous - 1e-12""")["n"].iloc[0]
    assert falls == 0


def test_the_triangle_ties_to_the_fact_table(built):
    """Every default on the book appears in exactly one cohort, once.

    Guards the join and the MIN() that picks each account's default age. A
    cohort column that silently fanned out would inflate every rate in the
    chart without changing its shape, which is the hardest kind of error to
    see.
    """
    connection, _, _ = built
    totals = db.read_sql(connection, """
        SELECT
            (SELECT SUM(defaults_by_age) FROM vintage_curves
             WHERE age = (SELECT MAX(age) FROM vintage_curves))  AS in_triangle,
            (SELECT COUNT(*) FROM fact_loan_month
             WHERE is_new_default)                               AS on_the_book,
            (SELECT SUM(cohort_size) FROM vintage_curves
             WHERE age = 0)                                      AS cohort_total,
            (SELECT COUNT(*) FROM dim_account)                   AS accounts
        """).iloc[0]
    assert totals["in_triangle"] == totals["on_the_book"]
    assert totals["cohort_total"] == totals["accounts"]


def test_matured_accounts_stay_in_the_denominator(built):
    """A cohort's base is its size at origination and never shrinks.

    Accounts that repaid without defaulting were observed for their whole lives
    and did not default, that is information. Dropping them as they mature
    would shrink the base to whoever happened to still be on book, which rises
    the rate for a reason that has nothing to do with credit.
    """
    connection, _, _ = built
    varies = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT vintage_month, COUNT(DISTINCT cohort_size) AS sizes
            FROM vintage_curves GROUP BY vintage_month)
        WHERE sizes <> 1""")["n"].iloc[0]
    assert varies == 0


# --- The headline: no false positive ---------------------------------------

def test_the_fair_comparison_finds_no_vintage_trend(built):
    """THE GATE THAT GUARDS PHASE 7.

    Segment and origination month are drawn independently in the generator, so
    there is no vintage effect to find. Comparing every cohort at a common age
    must therefore return a slope indistinguishable from zero.
    """
    connection, sized, _ = built
    artefact = vintage.censoring_artefact(connection)

    limit = float(sized["validation"]["max_spurious_vintage_slope_per_year"])
    assert abs(artefact["fair_slope_per_year"]) <= limit, (
        f"the fair comparison found a trend of "
        f"{artefact['fair_slope_per_year']:+.4f}/yr on a book with no vintage "
        f"effect at all")


def test_no_cohort_spread_beyond_what_chance_explains(built):
    """The same claim by a different route, because the slope could hide it.

    A fitted slope near zero is consistent with cohorts scattered wildly but
    symmetrically. The chi-square asks whether the scatter itself is bigger
    than binomial sampling would produce, which a trend test cannot see.
    """
    connection, sized, _ = built
    noise = vintage.noise_test(connection)

    assert not noise["vintage_effect_detected"], (
        f"chi-square {noise['chi_square']:.1f} on "
        f"{noise['degrees_of_freedom']} df, p={noise['p_value']:.4f}")
    assert noise["dispersion_ratio"] <= \
        float(sized["validation"]["max_vintage_dispersion_ratio"])


def test_the_naive_comparison_invents_a_trend_that_is_not_there(built):
    """Why the two tests above mean anything.

    If the fair comparison found nothing because the analysis is incapable of
    finding anything, both would still pass. So the obvious wrong method is run
    on the same data and shown to produce a large, confident, entirely
    imaginary trend.

    The direction is predictable from the mechanism before measuring: newer
    cohorts have lived less of their lives, so their cumulative rates are lower,
    so the naive slope must be NEGATIVE, apparent improvement. Asserting the
    sign as well as the size is what distinguishes the mechanism from noise.
    """
    connection, _, _ = built
    artefact = vintage.censoring_artefact(connection)

    assert artefact["naive_slope_per_year"] < 0, (
        "censoring must make newer cohorts look better, not worse")
    assert abs(artefact["naive_slope_per_year"]) > 0.02, (
        f"the naive trend is only "
        f"{artefact['naive_slope_per_year']:+.4f}/yr, if the artefact has "
        f"gone, this phase has nothing to demonstrate")
    assert artefact["artefact_share"] > 0.90
    assert abs(artefact["naive_slope_per_year"]) > \
        10 * abs(artefact["fair_slope_per_year"])


def test_the_comparison_never_uses_a_censored_cell(built):
    """The mechanism behind the fair comparison, asserted directly.

    Every other test here would still pass if the comparison quietly carried a
    young cohort's last known rate forward to the common age, on this book the
    difference is small. It is still the exact error the file exists to avoid,
    so it is checked as a property rather than inferred from a result.
    """
    connection, _, common_age = built
    used_censored = db.read_sql(connection, """
        SELECT COUNT(*) AS n
        FROM vintage_comparison c
        JOIN vintage_curves v ON v.vintage_month = c.vintage_month
                             AND v.age = c.common_age
        WHERE v.is_censored""")["n"].iloc[0]
    assert used_censored == 0

    # And every cohort really was watched to the comparison age.
    short = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM vintage_comparison
        WHERE watched_to_age < common_age""")["n"].iloc[0]
    assert short == 0

    # The common age is the youngest cohort's window, so no cohort is dropped.
    coverage = db.read_sql(connection, """
        SELECT cohorts_included, cohorts_total FROM vintage_comparison
        LIMIT 1""").iloc[0]
    assert coverage["cohorts_included"] == coverage["cohorts_total"]


def test_monthly_cohorts_cannot_detect_much(built):
    """The practical finding, pinned so it does not get lost.

    A monthly cohort here holds a few hundred accounts, which is too few to
    detect anything smaller than a very large shift in credit quality. That is
    not a defect in the engine, it is a fact about the book, and it is the
    answer to "should we chart vintages monthly?", which is no.

    Grouping has to help, and by the square root of the group size: quarterly
    cohorts detect a smaller effect than monthly, annual smaller still.
    """
    connection, _, _ = built
    noise = vintage.noise_test(connection)

    assert noise["detectable_monthly"] > noise["detectable_quarterly"]
    assert noise["detectable_quarterly"] > noise["detectable_annual"]

    # Pooling n cohorts narrows the interval by sqrt(n). Checked because if the
    # grouping arithmetic were wrong, the recommendation drawn from it would be
    # too, and it is the recommendation, not the number, that reaches a reader.
    assert np.isclose(noise["detectable_monthly"] / noise["detectable_quarterly"],
                      np.sqrt(3.0), rtol=1e-6)

    # And on this book the monthly figure really is too large to be useful: it
    # cannot see a change smaller than half the base rate itself.
    assert noise["detectable_monthly"] > 0.5 * noise["pooled_rate"]


# --- Calendar time, and what it cannot say ---------------------------------

def test_calendar_time_shows_deterioration_that_is_only_maturity(built):
    """The reason cohort analysis exists, demonstrated on a book with no trend.

    Arrears rise substantially over the period. Cohort quality is constant by
    construction. The rise is therefore entirely the book's age profile, and
    nothing in the calendar-time table can establish that. which is the point.
    """
    connection, _, _ = built
    drift = vintage.age_profile_drift(connection)

    assert drift["age_at_end"] > drift["age_at_start"] * 2
    assert drift["arrears_at_end"] > drift["arrears_at_start"]
    assert drift["age_vs_arrears_correlation"] > 0.5, (
        "if arrears did not track the book's age, the maturity explanation "
        "would not be the one this book demonstrates")


def test_the_calendar_mi_ties_to_the_fact_table(built):
    """Monthly aggregates must reproduce the book they summarise."""
    connection, _, _ = built
    mismatch = db.read_sql(connection, """
        SELECT
            SUM(ABS(m.accounts_on_book - f.accounts))        AS account_gap,
            SUM(ABS(m.balance_on_book - f.balance))          AS balance_gap,
            SUM(ABS(m.new_defaults - f.new_defaults))        AS default_gap
        FROM portfolio_mi m
        JOIN (SELECT reporting_date, COUNT(*) AS accounts,
                     SUM(scheduled_balance) AS balance,
                     SUM(CASE WHEN is_new_default THEN 1 ELSE 0 END) AS new_defaults
              FROM fact_loan_month GROUP BY reporting_date) f
          ON f.reporting_date = m.reporting_date""").iloc[0]

    assert mismatch["account_gap"] == 0
    assert mismatch["balance_gap"] < 0.01
    assert mismatch["default_gap"] == 0


def test_stage_shares_sum_to_one(built):
    """A mix that does not sum to 100% means accounts fell out of the join.

    `portfolio_mi` left-joins staging onto the fact table, so an account with
    no stage row would quietly vanish from the mix while remaining in the
    denominator, making every share slightly too small, in a way no single
    share looks wrong for.
    """
    connection, _, _ = built
    bad = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM portfolio_mi
        WHERE ABS(stage_1_share + stage_2_share + stage_3_share - 1.0) > 1e-6""")
    assert bad["n"].iloc[0] == 0


# --- The gates --------------------------------------------------------------

def test_every_phase_7_check_passes(built):
    """The suite the pipeline runs, run here too."""
    connection, sized, _ = built
    checks, _, _, _ = vintage.validate(connection, sized)
    failed = checks[~checks["passed"]]
    assert failed.empty, f"failed: {failed['check'].tolist()}"


def test_the_common_age_is_derived_from_the_data(built):
    """Not configured, because a configured age would rot.

    The right comparison age is the youngest cohort's observation window, which
    moves whenever the reporting date or the origination window moves. Pinned
    in config it would silently start either excluding cohorts or comparing
    censored ones, and neither would announce itself.
    """
    connection, _, common_age = built
    youngest = db.read_sql(connection, """
        SELECT MIN(watched_to_age) AS m FROM vintage_curves""")["m"].iloc[0]
    assert common_age == youngest

    stored = db.read_sql(connection, """
        SELECT DISTINCT common_age FROM vintage_comparison""")
    assert len(stored) == 1
    assert stored["common_age"].iloc[0] == youngest


# --- The cohort is the sample, not the book --------------------------------
#
# Sixth appearance of one error, caught again by rehearsing CI in a clean room
# rather than by CI. Vintage analysis divides the book by origination month, so
# a book that is ample for a portfolio statistic can be hopeless for a cohort
# one: the committed 400-account sample gives 8 accounts and 0.4 expected
# defaults per cohort.
#
# Unguarded, the Phase 7 gates failed on that sample: the fitted slope came
# out -1.56pp/yr against a 0.5pp/yr limit, which sounds damning until you note
# its standard error is 0.92pp/yr, putting it 1.7 standard errors from zero.
# A gate built to catch invented trends was itself reporting noise.

def test_cohorts_too_thin_to_judge_report_not_assessed(config, tmp_path):
    """The committed sample's own size, so this is the case CI actually hits."""
    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=400)

    rng = set_seed(config.get("seed", 42))
    accounts, performance, truth, recoveries = generate.generate_book(sized, rng)

    connection = db.connect(sized, tmp_path / "thin.duckdb")
    try:
        for name, frame in (("accounts", accounts), ("loan_month", performance),
                            ("ground_truth", truth), ("recovery", recoveries)):
            db.write_table(connection, name, frame,
                           allow_empty=(name == "recovery"))
        mart.build(connection, sized, verbose=False)
        rollrate.build(connection, verbose=False)
        staging.build(connection, sized, verbose=False)
        vintage.build(connection, sized, verbose=False)

        precision = vintage.gate_precision(connection, sized)
        assert not precision["trend_assessable"]
        assert not precision["dispersion_assessable"]
        assert precision["expected_defaults_per_cohort"] < 5.0

        checks, artefact, _, _ = vintage.validate(connection, sized)

        # The slope really does breach the limit...
        assert abs(artefact["fair_slope_per_year"]) > \
            sized["validation"]["max_spurious_vintage_slope_per_year"]
        # ...and the engine declines to call that a failure.
        trend = checks[checks["check"].str.contains("no vintage trend")].iloc[0]
        assert trend["status"] == "not assessed"
        assert not (checks["status"] == "fail").any()
        assert checks["passed"].all()

        # THE STRUCTURAL CHECKS STILL APPLY. They are arithmetic, not
        # inference, so they hold at any size, and an exemption that silenced
        # them too would leave the sample run proving nothing.
        for name in ("age it reached", "ties to the fact table"):
            row = checks[checks["check"].str.contains(name)].iloc[0]
            assert row["assessable"], name
            assert row["status"] == "pass", name
    finally:
        connection.close()


def test_cohorts_large_enough_are_actually_judged(built):
    """The other half, so the exemption cannot become an escape hatch."""
    connection, sized, _ = built
    precision = vintage.gate_precision(connection, sized)

    assert precision["trend_assessable"], precision
    assert precision["dispersion_assessable"], precision

    checks, _, _, _ = vintage.validate(connection, sized)
    assert (checks["status"] == "pass").all(), (
        f"not all assessed: {checks[['check', 'status']].to_dict('records')}")


def test_gate_precision_is_decided_from_the_sample_not_the_result(config):
    """The decision must not see the result it would be excusing.

    Same discipline as Phases 5 and 6, asserted the same way, against the
    source, because this is a claim about the order of the reasoning that no
    input can demonstrate.

    `gate_precision` may read cohort sizes and default counts, which are
    properties of the sample. It must not read the fitted slope, the p-value
    or the artefact share, which are the results it governs.
    """
    source = inspect.getsource(vintage.gate_precision)
    for forbidden in ("censoring_artefact", "noise_test", "fair_slope",
                      "p_value", "artefact_share"):
        assert forbidden not in source, (
            f"gate_precision reads {forbidden}, the decision can now see the "
            f"result it would be excusing")

    source = inspect.getsource(vintage.validate)
    decision = source.index("precision = gate_precision(")
    before = source[:decision]
    assert "censoring_artefact(" not in before
    assert "noise_test(" not in before
