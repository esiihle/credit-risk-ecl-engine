"""
Phase 5 tests, loss given default and exposure at default.

The headline is `test_the_naive_lgd_is_biased_upward_and_materially_so`. Every
previous phase asserted that the engine gets the right answer; this one also
asserts that the WRONG method gets a wrong answer, in a specific direction, by
an amount worth caring about. That is a stronger claim, and it is the one a
credit person would actually want evidenced: anyone can say that censoring
biases an LGD estimate upward, and the measurement is what separates knowing it
from having read it.

`test_exposure_matches_the_annuity_formula_to_the_cent` is the opposite kind of
test. EAD on a term loan is arithmetic, so the only failure available is a
transcription error between the numpy version and the SQL version, and exact
agreement rules it out.

Run with:  pytest tests/test_lgd.py -v
"""
import inspect
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed
from src import db, generate, lgd, mart, rollrate, staging
from src import pd as pdmod


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def built(config, tmp_path_factory):
    """A book big enough for LGD to be estimated AND judged.

    LGD rests on defaults, not accounts, and only the RESOLVED ones count. At a
    ~16% ever-default rate with roughly a third of workouts still open, 14,000
    accounts yield around 300 resolved defaults in the smallest segment, for a
    standard error near 0.008, inside the 0.010 precision requirement that
    makes the absolute materiality gate assessable.

    14,000 rather than 8,000 deliberately. Measured, 8,000 lands at a standard
    error of 0.00985 against that 0.01 requirement: assessable, but by less
    than a third of a percent, so a change of seed would flip the gate between
    "pass" and "not assessed" and the test below asserting it IS assessed would
    fail for no defect. A fixture that sits on a threshold tests the threshold,
    not the code. Checked across four seeds at 14,000: 0.0075-0.0081.
    """
    path = tmp_path_factory.mktemp("lgd") / "test.duckdb"

    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=14000)

    rng = set_seed(16180)
    accounts, performance, truth, recoveries = generate.generate_book(sized, rng)

    connection = db.connect(sized, path)
    db.write_table(connection, "accounts", accounts)
    db.write_table(connection, "loan_month", performance)
    db.write_table(connection, "ground_truth", truth)
    db.write_table(connection, "recovery", recoveries)
    mart.build(connection, sized, verbose=False)
    rollrate.build(connection, verbose=False)
    staging.build(connection, sized, verbose=False)
    pdmod.build(connection, sized, verbose=False)
    lgd.build(connection, sized, verbose=False)

    yield connection, sized
    connection.close()


# --- The recovery generator ------------------------------------------------

def test_every_default_event_has_a_recovery_record(built):
    """One row per default event, and defaults are where losses come from.

    A default with no recovery record is an account the LGD estimate silently
    excludes, which would bias the estimate by however those accounts differ
    from the rest.

    Also pins the GRAIN at one row per account. Default is absorbing and the
    account leaves the book, so the default flag fires in exactly one month --
    but the generator filters on `is_default` rather than on first entry, so if
    defaulted accounts were ever kept on book the filter would emit one row per
    month in default and quietly weight long-defaulted accounts more heavily in
    the LGD average.
    """
    connection, _ = built
    counts = db.read_sql(connection, """
        SELECT
            (SELECT COUNT(*) FROM fact_loan_month WHERE is_new_default)
                                                        AS default_events,
            (SELECT COUNT(*) FROM recovery)             AS recovery_rows,
            (SELECT COUNT(DISTINCT account_id) FROM recovery)
                                                        AS recovery_accounts""")
    assert counts["default_events"].iloc[0] > 0
    assert counts["recovery_rows"].iloc[0] == counts["default_events"].iloc[0]
    assert counts["recovery_rows"].iloc[0] == counts["recovery_accounts"].iloc[0]


def test_recovery_never_exceeds_the_exposure_it_is_recovering(built):
    """Collections cannot bring in more than was owed.

    Not a tautology: the generator draws the ultimate recovery with lognormal
    dispersion around its expectation, and without the clip a low-LGD account
    with a lucky draw would recover more than 100% of its exposure and produce
    a negative loss.
    """
    connection, _ = built
    breaches = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM recovery
        WHERE recovery_to_date > ead_at_default + 0.01""")["n"].iloc[0]
    assert breaches == 0


def test_recovery_grows_with_time_in_workout(built):
    """The mechanism the whole phase depends on.

    If recovery did not accumulate with workout age there would be no censoring
    bias to find, and a test suite that passes on data without the mechanism is
    testing nothing.
    """
    connection, _ = built
    by_age = db.read_sql(connection, """
        SELECT CASE WHEN months_in_workout < 3 THEN 'early'
                    WHEN months_in_workout < 9 THEN 'middle'
                    ELSE 'late' END                           AS stage_of_workout,
               AVG(recovery_to_date / ead_at_default)         AS recovered_share,
               COUNT(*)                                       AS n
        FROM recovery WHERE ead_at_default > 0
        GROUP BY 1""").set_index("stage_of_workout")

    assert (by_age["n"] > 20).all(), "need all three workout stages populated"
    assert (by_age.loc["early", "recovered_share"]
            < by_age.loc["middle", "recovered_share"]
            < by_age.loc["late", "recovered_share"])


# --- The headline: the censoring bias --------------------------------------

def test_lgd_from_resolved_workouts_matches_the_truth(built):
    """The estimate that excludes open workouts should find the real LGD."""
    connection, sized = built
    checks, estimates, _, _ = lgd.validate(connection, sized)

    assert not (checks["status"] == "fail").any(), (
        f"failed: {checks[checks['status'] == 'fail']['check'].tolist()}")
    assert (estimates["absolute_error"]
            <= sized["validation"]["max_lgd_absolute_error"]).all()
    assert (estimates["errors_in_standard_errors"]
            <= sized["validation"]["max_lgd_errors_in_standard_errors"]).all()


def test_the_naive_lgd_is_biased_upward_and_materially_so(built):
    """THE POINT OF PHASE 5.

    Averaging loss-to-date over ALL defaults, resolved or not, is the obvious
    thing to do and it is wrong. Each unresolved workout contributes a loss
    that has not finished falling, so the average is pulled up.

    Two assertions, because either alone would be weak. That the bias is
    POSITIVE is the direction the mechanism predicts, and a sign error would
    pass a magnitude-only test. That it is LARGE is what makes the distinction
    worth drawing at all: a bias of a basis point would be a curiosity.

    One percentage point of LGD is the floor used here. Measured on this book
    the bias runs near three and a half, and the unresolved cases on their own
    sit roughly twelve points above the resolved ones.
    """
    connection, _ = built
    bias = lgd.censoring_bias(connection)

    naive_overstatement = bias["lgd_naive"] - bias["lgd_resolved"]
    assert naive_overstatement > 0.01, (
        f"naive LGD overstated by only {naive_overstatement:.4f}, if the "
        f"censoring bias has gone, the recovery generator has stopped "
        f"accumulating recoveries over the workout period")

    # And the unresolved cases, looked at alone, are where it comes from.
    assert bias["lgd_unresolved"] > bias["lgd_resolved"] + 0.05
    assert 0.05 < bias["unresolved_share"] < 0.60, (
        "the unresolved share drives how big the naive bias is; outside this "
        "range the book is not exercising the mechanism")


def test_the_estimate_never_silently_uses_unresolved_workouts(built):
    """A guard on the thing that would quietly undo the phase.

    If `lgd_resolved_only` were ever computed over all defaults, every accuracy
    test above would still pass on a book where workouts happened to be mostly
    complete. So this checks the DEFINITION rather than the result: the
    resolved-only figure must equal the average over resolved rows alone,
    recomputed here from the recovery table without going through the view.
    """
    connection, _ = built
    recomputed = db.read_sql(connection, """
        SELECT a.segment,
               AVG(LEAST(GREATEST(1.0 - r.recovery_to_date
                                      / NULLIF(r.ead_at_default, 0), 0.0), 1.0))
                   AS lgd
        FROM recovery r
        JOIN dim_account a ON a.account_id = r.account_id
        WHERE r.workout_complete AND r.ead_at_default > 0
        GROUP BY a.segment""").set_index("segment")["lgd"]

    published = lgd.lgd_table(connection).set_index("segment")["lgd_resolved_only"]
    for segment, value in published.items():
        assert abs(value - recomputed[segment]) < 1e-9


# --- Exposure at default ---------------------------------------------------

def test_exposure_matches_the_annuity_formula_to_the_cent(built):
    """The SQL schedule and the numpy balance function share no code.

    They implement the same closed-form annuity formula in two languages, so
    agreement is evidence of correct transcription. Any disagreement at all is
    a bug in one of them, there is no sampling noise in arithmetic.
    """
    connection, sized = built
    result = lgd.ead_check(connection, sized)

    assert result["rows"] > 0
    assert result["rows_over_a_cent"] == 0
    assert result["max_absolute_difference"] < 0.01


def test_exposure_falls_every_month_and_ends_at_zero(built):
    """An amortising term loan's balance is monotone down to zero at maturity.

    This is the property that makes EAD the easy input, and the property a
    revolving product would NOT have, which is why Phase 0 chose a term
    product. If it ever failed, the amortisation would be wrong in a way that
    changes lifetime ECL for every account.
    """
    connection, _ = built
    shape = db.read_sql(connection, """
        WITH stepped AS (
            SELECT account_id, horizon, ead,
                   LEAD(ead) OVER (PARTITION BY account_id ORDER BY horizon)
                       AS next_ead
            FROM ead_schedule)
        SELECT
            SUM(CASE WHEN next_ead > ead + 0.01 THEN 1 ELSE 0 END) AS rises,
            SUM(CASE WHEN ead < -0.01 THEN 1 ELSE 0 END)           AS negatives
        FROM stepped""")
    assert shape["rises"].iloc[0] == 0
    assert shape["negatives"].iloc[0] == 0

    final = db.read_sql(connection, """
        SELECT MAX(ead) AS worst FROM ead_schedule e
        WHERE e.months_on_book_then = (SELECT term_months FROM dim_account a
                                       WHERE a.account_id = e.account_id)""")
    if not np.isnan(final["worst"].iloc[0]):
        assert abs(final["worst"].iloc[0]) < 0.01


def test_the_discount_factor_is_the_effective_interest_rate(built):
    """IFRS 9 discounts expected losses at the asset's EIR, not a market rate.

    Checked explicitly because it is a policy choice that looks like an
    implementation detail, and because the same monthly rate has to serve two
    purposes: building the amortisation schedule and discounting the loss off
    it. Using one rate for the schedule and another for the discount would be a
    plausible-looking error with no visible symptom.
    """
    connection, _ = built
    mismatch = db.read_sql(connection, """
        SELECT COUNT(*) AS n
        FROM ead_schedule e
        JOIN dim_account a ON a.account_id = e.account_id
        WHERE ABS(e.discount_factor
                  - 1.0 / POWER(POWER(1.0 + a.annual_rate, 1.0/12.0), e.horizon))
              > 1e-9""")["n"].iloc[0]
    assert mismatch == 0


# --- The horizon ------------------------------------------------------------

def test_the_schedule_reaches_the_longest_remaining_term(built):
    """A truncated horizon would understate lifetime ECL.

    Lifetime ECL sums over every remaining month. If the schedule stopped
    short, the loss in the missing months would simply be absent, and a
    provision that is too small by omission is the worst failure available
    here: it looks like a tighter number rather than a missing one.
    """
    connection, _ = built
    coverage = db.read_sql(connection, """
        SELECT COUNT(*) AS accounts_short
        FROM account_pd p
        LEFT JOIN (SELECT account_id, MAX(horizon) AS furthest
                   FROM ead_schedule GROUP BY account_id) e
               ON e.account_id = p.account_id
        WHERE p.months_remaining > 0
          AND (e.furthest IS NULL OR e.furthest < p.months_remaining)""")
    assert coverage["accounts_short"].iloc[0] == 0


def test_run_sql_file_only_binds_the_parameters_a_file_uses(config, tmp_path):
    """A loader bug found while wiring this phase.

    Phase 5 runs two SQL files in one step, and only the second takes a
    parameter. Passing both files the same arguments made DuckDB reject the
    first with a count mismatch, so the loader now binds only what each file
    references, and ignores `$name` appearing in commentary.

    Worth a test because the failure mode is a loud error today and a silently
    mis-bound parameter if the comment-stripping ever regresses.
    """
    used = db._parameters_used("""
        -- a comment mentioning $not_a_parameter
        /* and a block comment with $also_not */
        SELECT $real_one AS x, $second AS y""")
    assert used == {"real_one", "second"}

    path = tmp_path / "takes_nothing.sql"
    path.write_text("SELECT 1 AS x  -- mentions $unused only in a comment")
    connection = db.connect(config, tmp_path / "t.duckdb")
    try:
        frame = db.run_sql_file(connection, path, unused=5, other=6)
        assert frame["x"].iloc[0] == 1
    finally:
        connection.close()


# --- "Too imprecise to judge" is not "wrong" --------------------------------
#
# The same distinction Phase 4 drew for the PD coverage gate, and it came from
# the same discovery: the committed sample was passing the absolute LGD gate on
# the strength of its seed. Measured across six seeds at 400 accounts, one
# passed and five failed, while the standard-error gate passed on all six. CI
# runs on that sample, so it was green by luck and one regeneration away from
# red with nothing actually broken.

def test_a_book_too_imprecise_to_judge_reports_not_assessed_not_failed(config, tmp_path):
    """A 400-account book cannot support a 2pp materiality gate.

    With ~10 resolved defaults per segment the standard error is near 4pp, so
    an estimate lands more than 2pp from the truth roughly half the time by
    chance. Reporting that as a FAILURE would be reporting "I could not
    determine the answer" as "the answer is no", the error this project has
    now hit in four different places, and the one worth being loudest about.
    """
    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=400)

    rng = set_seed(2024)   # a seed measured to miss the absolute gate outright
    accounts, performance, truth, recoveries = generate.generate_book(sized, rng)

    connection = db.connect(sized, tmp_path / "small.duckdb")
    try:
        for name, frame in (("accounts", accounts), ("loan_month", performance),
                            ("ground_truth", truth), ("recovery", recoveries)):
            db.write_table(connection, name, frame)
        mart.build(connection, sized, verbose=False)
        rollrate.build(connection, verbose=False)
        staging.build(connection, sized, verbose=False)
        pdmod.build(connection, sized, verbose=False)
        lgd.build(connection, sized, verbose=False)

        checks, estimates, _, _ = lgd.validate(connection, sized)
        absolute = checks[checks["check"].str.contains("matches the truth")].iloc[0]

        # The estimate really is further off than the limit...
        assert estimates["absolute_error"].max() > \
            sized["validation"]["max_lgd_absolute_error"]
        # ...and the engine declines to call that a failure.
        assert absolute["status"] == "not assessed"
        assert not absolute["assessable"]
        assert not (checks["status"] == "fail").any()
        assert checks["passed"].all()

        # The sample-size-aware gate still applies and still has teeth.
        noise = checks[checks["check"].str.contains("sampling noise")].iloc[0]
        assert noise["assessable"]
        assert noise["status"] == "pass"
    finally:
        connection.close()


def test_a_book_precise_enough_is_actually_assessed(built):
    """The exemption must not become an escape hatch.

    A "not assessed" that applied at every book size would be a gate that never
    fires, which is indistinguishable from having no gate. So the other half of
    the claim has to be asserted too: once the estimate is precise enough, the
    absolute limit is applied and the engine is held to it.
    """
    connection, sized = built
    checks, estimates, _, _ = lgd.validate(connection, sized)
    absolute = checks[checks["check"].str.contains("matches the truth")].iloc[0]

    assert absolute["assessable"], (
        f"a {sized['portfolio']['n_accounts']: }-account book should support "
        f"the absolute gate; worst standard error was "
        f"{estimates['standard_error'].max():.5f}")
    assert absolute["status"] == "pass"


def test_assessability_is_decided_from_precision_not_from_the_error(config):
    """The decision must not be able to see the result it is excusing.

    If assessability were computed after the error, a bad estimate could be
    waved through by whatever reasoning made the error large. So the rule is
    structural: the precision test is computed from the standard error, which
    is a property of the SAMPLE, and it is computed BEFORE the absolute error
    appears anywhere in the function.

    Asserted against the source text rather than the behaviour, because this is
    a claim about the order of the reasoning and no input can demonstrate it.
    """
    source = inspect.getsource(lgd.validate)
    decision = source.index("absolute_gate_assessable =")
    before = source[:decision]

    assert "worst_standard_error" in before

    # Matching on the ASSIGNMENT, not on the bare words. The first version of
    # this assertion searched for "absolute_error" and matched the config key
    # `max_lgd_absolute_error`, reading a threshold's NAME as evidence that
    # the error had been computed. It failed on correct code, which is the
    # cheap direction for a check to be wrong in, but it was still a check
    # measuring something other than what it claimed.
    assert 'estimates["absolute_error"]' not in before, (
        "the absolute error is computed before assessability is decided, the "
        "gate can now see the result it would be excusing")
    assert 'estimates["errors_in_standard_errors"]' not in before

    # And the decision does rest on the sample, not on the outcome.
    assert "standard_error" in before


def test_an_empty_recovery_table_is_created_rather_than_skipped(config, tmp_path):
    """A book with no defaults still needs the table.

    Written because the loader's first version skipped the write when there
    were no recoveries, which left the table absent instead of empty, and the
    Phase 5 SQL then failed with a catalog error rather than returning zero
    rows. "No defaults" and "no recovery table" are different facts, and only
    one of them is a problem.

    The typed empty frame is what makes this possible: an untyped empty frame
    gives DuckDB no schema to infer, so it cannot be written at all.
    """
    empty = generate.simulate_recoveries(
        accounts=generate.build_accounts(
            dict(config, portfolio=dict(config["portfolio"], n_accounts=10)),
            set_seed(1)),
        performance=__import__("pandas").DataFrame({
            "account_id": __import__("pandas").Series(dtype="object"),
            "is_default": __import__("pandas").Series(dtype="bool"),
            "reporting_date": __import__("pandas").Series(dtype="datetime64[ns]"),
            "months_on_book": __import__("pandas").Series(dtype="int64"),
            "scheduled_balance": __import__("pandas").Series(dtype="float64")}),
        config=config,
        rng=set_seed(1))

    assert empty.empty
    assert list(empty.columns) == [
        "account_id", "default_date", "months_on_book_at_default",
        "ead_at_default", "months_in_workout", "workout_complete",
        "recovery_to_date", "observed_lgd_to_date"]

    connection = db.connect(config, tmp_path / "empty.duckdb")
    try:
        assert db.write_table(connection, "recovery", empty, allow_empty=True) == 0
        assert db._count(connection, "recovery") == 0
        # And without the flag it is still refused, so the guard is not gone.
        with pytest.raises(db.DatabaseError, match="empty"):
            db.write_table(connection, "recovery", empty)
    finally:
        connection.close()
