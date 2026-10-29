"""
Phase 3 tests — IFRS 9 staging.

Three things are being defended here.

The PD curve is matrix exponentiation written as a recursive CTE, and two of
its failure modes produce plausible numbers rather than errors: DECIMAL
truncation in the anchor, and the absorbing row being absent from the data it
is pivoted from. Both have regression tests that demonstrate the wrong version
really is wrong.

The staging rules are accounting policy. The tests assert the invariants an
auditor would check — every account in exactly one stage, nothing 90+ outside
Stage 3, nothing 30+ in Stage 1 — and that probation actually delays a return.

The trigger analysis pins a finding rather than a behaviour: the relative PD
test binds only on short-dated loans, for a structural reason that would
otherwise be rediscovered the hard way.

Run with:  pytest tests/test_staging.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed
from src import db, generate, mart, rollrate, staging


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def built(config, tmp_path_factory):
    path = tmp_path_factory.mktemp("staging") / "test.duckdb"

    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=6000)

    rng = set_seed(271828)
    accounts, performance, truth = generate.generate_book(sized, rng)

    connection = db.connect(sized, path)
    db.write_table(connection, "accounts", accounts)
    db.write_table(connection, "loan_month", performance)
    db.write_table(connection, "ground_truth", truth)
    mart.build(connection, sized, verbose=False)
    rollrate.build(connection, verbose=False)
    staging.build(connection, sized, verbose=False)

    yield connection, sized
    connection.close()


# --- The PD curve: a recursive CTE doing matrix algebra --------------------

def test_the_sql_curve_matches_a_python_matrix_power(built):
    """The recursive CTE must reproduce repeated matrix multiplication.

    Same input matrix, two implementations — one SQL, one numpy. This is the
    test that would catch an arithmetic slip in the 25-term state update, where
    a single transposed subscript gives a curve that still rises smoothly and
    still looks like a PD curve.
    """
    connection, _ = built

    empirical = rollrate.matrix(connection).to_numpy()
    # The absorbing row is absent from observed data; the SQL asserts the
    # identity row, so the comparison must too.
    empirical[generate.DEFAULT_BUCKET, :] = 0.0
    empirical[generate.DEFAULT_BUCKET, generate.DEFAULT_BUCKET] = 1.0

    for start_bucket in range(generate.N_BUCKETS):
        state = np.zeros(generate.N_BUCKETS)
        state[start_bucket] = 1.0

        expected = []
        for _ in range(24):
            state = state @ empirical
            expected.append(state[generate.DEFAULT_BUCKET])

        actual = db.read_sql(connection, """
            SELECT horizon, cumulative_pd FROM pd_curve
            WHERE start_bucket = $bucket AND horizon BETWEEN 1 AND 24
            ORDER BY horizon
        """, bucket=start_bucket)["cumulative_pd"].to_numpy()

        np.testing.assert_allclose(
            actual, expected, atol=1e-10,
            err_msg=f"SQL and numpy disagree for start_bucket {start_bucket}")


def test_decimal_truncation_in_the_anchor_would_break_the_curve():
    """The bug the `::DOUBLE` casts exist to prevent, demonstrated.

    `1.0` in SQL is DECIMAL(2,1) — one decimal place. The type propagates
    through every iteration of a recursive CTE, so each step rounds to a tenth
    and the state vector freezes. The output is a set of entirely valid-looking
    probabilities that is completely wrong, with no error raised.

    Asserted here rather than trusted, because the casts look like noise and
    the next person to tidy them away needs a failing test, not a comment.
    """
    import duckdb
    connection = duckdb.connect()

    untyped = connection.execute("""
        WITH RECURSIVE steps AS (
            SELECT 0 AS horizon, 1.0 AS p0, 0.0 AS p1
            UNION ALL
            SELECT horizon + 1, p0 * 0.9 + p1 * 0.6, p0 * 0.1 + p1 * 0.4
            FROM steps WHERE horizon < 4
        ) SELECT p0 FROM steps ORDER BY horizon
    """).fetchall()

    typed = connection.execute("""
        WITH RECURSIVE steps AS (
            SELECT 0 AS horizon, 1.0::DOUBLE AS p0, 0.0::DOUBLE AS p1
            UNION ALL
            SELECT horizon + 1, p0 * 0.9 + p1 * 0.6, p0 * 0.1 + p1 * 0.4
            FROM steps WHERE horizon < 4
        ) SELECT p0 FROM steps ORDER BY horizon
    """).fetchall()
    connection.close()

    untyped_values = [float(row[0]) for row in untyped]
    typed_values = [float(row[0]) for row in typed]

    # The truncated version sticks after one step; the typed one keeps moving.
    assert untyped_values[1] == untyped_values[4], (
        "DuckDB no longer truncates undeclared decimal literals in a recursive "
        "CTE — check whether the ::DOUBLE casts are still required before "
        "relaxing this test"
    )
    assert typed_values[1] != typed_values[4]
    assert typed_values[4] == pytest.approx(0.8583, abs=1e-4)


def test_probability_is_conserved_at_every_horizon(built):
    """If the absorbing row were coalesced to zeros instead of the identity,
    mass would leak out of default and the cumulative PD would stop rising."""
    connection, _ = built
    totals = db.read_sql(connection, """
        SELECT MIN(p0+p1+p2+p3+p4) AS lowest, MAX(p0+p1+p2+p3+p4) AS highest
        FROM pd_curve
    """).iloc[0]
    assert totals["lowest"] == pytest.approx(1.0, abs=1e-9)
    assert totals["highest"] == pytest.approx(1.0, abs=1e-9)


def test_cumulative_pd_never_falls(built):
    connection, _ = built
    falls = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT cumulative_pd - LAG(cumulative_pd) OVER (
                PARTITION BY start_bucket ORDER BY horizon) AS change
            FROM pd_curve
        ) WHERE change < -1e-12
    """)["n"].iloc[0]
    assert falls == 0

    negatives = db.read_sql(connection,
                            "SELECT COUNT(*) AS n FROM pd_curve WHERE marginal_pd < -1e-12"
                            )["n"].iloc[0]
    assert negatives == 0


def test_deeper_arrears_means_higher_pd_at_every_horizon(built):
    connection, _ = built
    inversions = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT horizon, cumulative_pd,
                   LAG(cumulative_pd) OVER (
                       PARTITION BY horizon ORDER BY start_bucket) AS shallower
            FROM pd_curve WHERE horizon > 0
        ) WHERE shallower IS NOT NULL AND cumulative_pd < shallower - 1e-12
    """)["n"].iloc[0]
    assert inversions == 0


# --- The staging rules ------------------------------------------------------

def test_every_account_on_book_has_exactly_one_stage(built):
    connection, config = built
    checks = staging.validate(connection, config)
    failed = checks[~checks["passed"]]
    assert len(failed) == 0, f"failing checks: {failed.to_dict('records')}"


def test_stages_are_only_ever_1_2_or_3(built):
    connection, _ = built
    invalid = db.read_sql(connection,
                          "SELECT COUNT(*) AS n FROM account_stage WHERE stage NOT IN (1,2,3)"
                          )["n"].iloc[0]
    assert invalid == 0


def test_the_backstop_cannot_be_outranked(built):
    """IFRS 9's 30-day presumption is rebuttable in principle, not in this
    implementation. Nothing 30+ may sit in Stage 1."""
    connection, config = built
    leaks = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM account_stage
        WHERE days_past_due >= $dpd AND stage = 1
    """, dpd=int(config["staging"]["sicr_dpd_backstop"]))["n"].iloc[0]
    assert leaks == 0


def test_probation_delays_the_return_to_stage_1(built):
    """Without probation an account hovering at 30 days oscillates between
    Stage 1 and Stage 2 every month, and the provision swings by the whole
    difference between a 12-month and a lifetime loss each time.

    This asserts both halves: nobody returns too early, and somebody is
    actually being held — a probation rule that never binds is not a rule.
    """
    connection, config = built
    probation_months = int(config["staging"]["probation_months"])

    too_early = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT stage, LAG(triggered_now) OVER (
                PARTITION BY account_id ORDER BY months_on_book) AS triggered_before
            FROM account_stage
        ) WHERE triggered_before AND stage = 1
    """)["n"].iloc[0]
    assert too_early == 0, "an account returned to Stage 1 the month after a trigger"

    held = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM account_stage
        WHERE stage = 2 AND NOT triggered_now
    """)["n"].iloc[0]
    assert held > 0, (
        f"no account is being held in Stage 2 by probation, so the "
        f"{probation_months}-month rule is not doing anything"
    )


def test_every_staged_account_has_a_recorded_reason(built):
    """An auditor asking why an account is in Stage 2 needs an answer per
    account. Reconstructing it later from thresholds is guesswork."""
    connection, _ = built
    unexplained = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM account_stage
        WHERE stage > 1 AND (stage_reason IS NULL OR stage_reason = 'performing')
    """)["n"].iloc[0]
    assert unexplained == 0


# --- The finding this phase turned up --------------------------------------

def test_the_relative_pd_trigger_binds_only_on_short_dated_loans(built):
    """Pins a structural result, not a behaviour.

    A relative test needs a small denominator. Lifetime PD at origination grows
    with the term, so the same proportional deterioration clears a doubling on
    a 12-month loan and cannot come close on a 60-month one. The consequence is
    that the test contributes almost nothing beyond the 30-day backstop, and
    what it does contribute is concentrated entirely at the short end.

    If a future change makes the trigger fire across all terms, that is either
    a real improvement or a calibration error — and either way it should be
    noticed rather than absorbed silently.
    """
    connection, _ = built

    by_term = staging.pd_trigger_by_term(connection)
    if by_term.empty:
        pytest.skip("the relative PD trigger never fired on this book at all")

    terms = sorted(by_term["term_months"].tolist())
    assert terms == [min(terms)], (
        f"the relative PD trigger now fires on terms {terms}, not just the "
        "shortest. Check whether the thresholds were changed, because this "
        "test records a structural property of a relative test on a term loan."
    )

    triggers = staging.trigger_analysis(connection)
    backstop = int(triggers["backstop"].sum())
    pd_only = int(triggers["relative_pd_only"].sum())
    assert pd_only < backstop, (
        "the relative PD test now does more work than the backstop, which "
        "would be a material change in staging policy"
    )


def test_the_pd_trigger_never_fires_on_a_fully_performing_account(built):
    """An account that has never missed a payment cannot have doubled its
    lifetime PD: its horizon only shortens, so its PD only falls."""
    connection, _ = built
    impossible = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM account_stage
        WHERE arrears_bucket = 0 AND trigger_relative_pd
    """)["n"].iloc[0]
    assert impossible == 0


# --- Migration --------------------------------------------------------------

def test_migration_rows_sum_to_one(built):
    connection, _ = built
    bad = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT from_stage, SUM(account_rate) AS total
            FROM stage_migration GROUP BY from_stage
            HAVING ABS(SUM(account_rate) - 1.0) > 1e-9
        )
    """)["n"].iloc[0]
    assert bad == 0


def test_no_account_migrates_out_of_stage_3(built):
    """Default is absorbing in this model, so Stage 3 is too. A cure out of it
    would create recoveries the book never had."""
    connection, _ = built
    escapes = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM stage_migration
        WHERE from_stage = 3 AND to_stage < 3 AND accounts > 0
    """)["n"].iloc[0]
    assert escapes == 0


def test_stage_2_is_a_genuine_waypoint(built):
    """Accounts must be able to both deteriorate into Stage 3 and cure back to
    Stage 1. If migration were one-way the staging would be a ratchet, and the
    provision would only ever rise."""
    connection, _ = built
    migration = db.read_sql(connection, """
        SELECT from_stage, to_stage, account_rate FROM stage_migration
        WHERE from_stage = 2
    """).set_index("to_stage")["account_rate"]

    assert migration.get(1, 0) > 0, "no account ever cures out of Stage 2"
    assert migration.get(3, 0) > 0, "no account ever deteriorates into Stage 3"
