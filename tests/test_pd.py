"""
Phase 4 tests — the PD term structure.

The headline is `test_the_age_aware_curve_beats_the_pooled_one`. Every earlier
phase checked that something was *correct*; this one checks that a refinement
was *worth making*. A segmentation that does not improve accuracy is cost
without benefit, and the only way to know is to build the simpler version too
and measure both.

`test_the_answer_key_uses_each_accounts_actual_bucket` guards the bug this
phase uncovered: an answer key that was only valid for performing accounts, and
made the engine look wrong by a factor of two on everyone else.

Run with:  pytest tests/test_pd.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed
from src import db, generate, mart, rollrate, staging
from src import pd as pdmod


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def built(config, tmp_path_factory):
    path = tmp_path_factory.mktemp("pd") / "test.duckdb"

    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=8000)

    rng = set_seed(16180)
    accounts, performance, truth = generate.generate_book(sized, rng)

    connection = db.connect(sized, path)
    db.write_table(connection, "accounts", accounts)
    db.write_table(connection, "loan_month", performance)
    db.write_table(connection, "ground_truth", truth)
    mart.build(connection, sized, verbose=False)
    rollrate.build(connection, verbose=False)
    staging.build(connection, sized, verbose=False)
    pdmod.build(connection, sized, verbose=False)

    yield connection, sized
    connection.close()


# --- The answer key, and the bug this phase found --------------------------

def test_the_answer_key_uses_each_accounts_actual_bucket(config):
    """The key must answer the same question the engine is asked.

    The first version computed every account's true PD as though it were
    performing. That was harmless until PD was compared per account across the
    whole book, at which point an account 60-89 days past due appeared in the
    key at 5.4% against a real 84% — and the engine looked wrong by a factor
    of two when it was right.

    An answer key valid for only part of the book is worse than none, because
    the part it is invalid for is where the numbers are largest.
    """
    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=3000)

    rng = set_seed(99)
    accounts = generate.build_accounts(sized, rng)
    performance = generate.simulate_performance(accounts, sized, rng)

    with_performance = generate.ground_truth(accounts, sized, performance)
    assert "true_start_bucket" in with_performance.columns
    assert not with_performance["truth_assumes_performing"].any()
    # Accounts in arrears at the reporting date must carry a higher true PD
    # than performing ones; if the key ignored the bucket they would be equal.
    in_arrears = with_performance[with_performance["true_start_bucket"] > 0]
    performing = with_performance[with_performance["true_start_bucket"] == 0]
    if len(in_arrears) and len(performing):
        assert in_arrears["true_pd_12m"].mean() > performing["true_pd_12m"].mean() * 2

    # And the old behaviour is still reachable, clearly flagged, for anyone
    # who deliberately wants a performing-only key.
    without = generate.ground_truth(accounts, sized)
    assert without["truth_assumes_performing"].all()
    assert (without["true_start_bucket"] == 0).all()


def test_the_answer_key_covers_exactly_the_accounts_on_book(built):
    """The key and the estimate must be over the same population, or the
    comparison silently compares different books."""
    connection, _ = built
    counts = db.read_sql(connection, """
        SELECT (SELECT COUNT(*) FROM account_pd)                      AS estimated,
               (SELECT COUNT(*) FROM ground_truth)                    AS in_key,
               (SELECT COUNT(*) FROM account_pd WHERE true_pd_12m IS NULL)
                                                                      AS unmatched
    """).iloc[0]
    assert counts["unmatched"] == 0, (
        f"{counts['unmatched']} accounts have an estimate but no truth"
    )
    assert counts["estimated"] == counts["in_key"]


# --- The curves must be curves ---------------------------------------------

def test_probability_is_conserved_in_the_age_aware_walk(built):
    connection, _ = built
    totals = db.read_sql(connection, """
        SELECT MIN(surviving + cumulative_pd) AS lowest,
               MAX(surviving + cumulative_pd) AS highest
        FROM pd_term_structure
    """).iloc[0]
    assert totals["lowest"] == pytest.approx(1.0, abs=1e-9)
    assert totals["highest"] == pytest.approx(1.0, abs=1e-9)


def test_the_walk_changes_matrix_as_it_ages(built):
    """The defining property of this phase.

    Two accounts in the same segment and bucket, looking 12 months ahead from
    different starting ages, must get different PDs — otherwise the age-aware
    walk is reading one matrix and the whole file is ceremony.
    """
    connection, _ = built
    curves = db.read_sql(connection, """
        SELECT start_age, cumulative_pd FROM pd_term_structure
        WHERE segment = 'subprime' AND start_bucket = 0 AND horizon = 12
          AND start_age IN (2, 14, 40)
        ORDER BY start_age
    """)
    assert len(curves) == 3
    values = curves["cumulative_pd"].to_numpy()
    assert len(set(np.round(values, 6))) == 3, (
        f"the 12-month PD is the same at every age ({values}) — the walk is "
        "not picking up the age band"
    )
    # And it should hump: risk peaks mid-life, not at origination or maturity.
    assert values[1] > values[0] and values[1] > values[2]


def test_segments_order_at_every_age(built):
    connection, _ = built
    inversions = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT start_age,
                   MAX(CASE WHEN segment='prime'      THEN cumulative_pd END) AS p,
                   MAX(CASE WHEN segment='near_prime' THEN cumulative_pd END) AS n,
                   MAX(CASE WHEN segment='subprime'   THEN cumulative_pd END) AS s
            FROM pd_term_structure
            WHERE start_bucket = 0 AND horizon = 12
            GROUP BY start_age
        ) WHERE NOT (p < n AND n < s)
    """)["n"].iloc[0]
    assert inversions == 0, "risk grades do not order at every age"


def test_the_thin_cell_fallback_fires_where_it_should(built):
    """A cell estimated on a handful of moves must not be believed.

    Without the fallback a single stray transition in a thin deep-arrears cell
    becomes a 100% roll rate, and the recursive walk compounds it into a
    lifetime PD of 1 for every account in that segment and age band.
    """
    connection, config = built
    limit = int(config["pd"]["min_transitions_per_cell"])

    wrong = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM segment_age_matrix
        WHERE used_fallback <> (opportunities < $limit)
    """, limit=limit)["n"].iloc[0]
    assert wrong == 0, "the fallback flag disagrees with the threshold"

    # Every row must still sum to 1 even where the fallback was used — a
    # mixture of own-cell and pooled rates is not automatically normalised.
    bad_rows = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT segment, age_band, from_bucket, SUM(rate) AS total
            FROM segment_age_matrix
            GROUP BY segment, age_band, from_bucket
            HAVING ABS(SUM(rate) - 1.0) > 1e-6
        )
    """)["n"].iloc[0]
    assert bad_rows == 0, (
        "a transition row does not sum to 1 — most likely a fallback mixing "
        "own-cell rates with pooled ones without renormalising"
    )


# --- The 12-month horizon ---------------------------------------------------

def test_the_12_month_pd_is_capped_at_the_remaining_term(built):
    """A loan with seven months left cannot default in month nine.

    Reading a 12-month PD off the curve without capping credits the account
    with risk it has no time to run, and overstates the Stage 1 provision on
    every account approaching maturity.
    """
    connection, _ = built
    uncapped = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM account_pd
        WHERE horizon_12m > months_remaining OR horizon_12m > 12
    """)["n"].iloc[0]
    assert uncapped == 0

    # And where the cap binds, the 12-month PD must equal the lifetime PD:
    # the whole remaining life fits inside twelve months.
    inconsistent = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM account_pd
        WHERE months_remaining <= 12
          AND ABS(pd_12m_age_aware - pd_lifetime_age_aware) > 1e-9
    """)["n"].iloc[0]
    assert inconsistent == 0


def test_lifetime_pd_is_never_below_the_12_month_pd(built):
    connection, _ = built
    for method in pdmod.METHODS:
        inversions = db.read_sql(connection, f"""
            SELECT COUNT(*) AS n FROM account_pd
            WHERE pd_lifetime_{method} < pd_12m_{method} - 1e-9
        """)["n"].iloc[0]
        assert inversions == 0, f"{method}: lifetime PD below 12-month PD"


# --- THE test: was the refinement worth making? ----------------------------

def test_the_age_aware_curve_beats_the_pooled_one(built):
    """Each refinement must earn its place, measured against the answer key.

    Not "the engine is correct" — every phase so far has checked that. This
    checks that added complexity bought accuracy, which is the only
    justification for it. A segmentation that does not improve the estimate is
    cost without benefit, and the only way to know is to build the simpler
    version alongside and measure both.
    """
    connection, _ = built

    for horizon in ("12m", "lifetime"):
        frame = pdmod.accuracy(connection, horizon).set_index("method")

        pooled = frame.loc["pooled", "mean_absolute_error"]
        segmented = frame.loc["segmented", "mean_absolute_error"]
        age_aware = frame.loc["age_aware", "mean_absolute_error"]

        assert segmented < pooled, (
            f"{horizon}: segmenting made the estimate WORSE "
            f"({segmented:.4f} vs {pooled:.4f})"
        )
        assert age_aware < segmented, (
            f"{horizon}: adding age made the estimate WORSE "
            f"({age_aware:.4f} vs {segmented:.4f})"
        )
        assert age_aware < pooled / 3, (
            f"{horizon}: the age-aware method is only {pooled / age_aware:.1f}x "
            "better than pooling, which does not justify the extra machinery"
        )

        # Ranking must improve too, not just the level.
        assert (frame.loc["age_aware", "correlation"]
                > frame.loc["pooled", "correlation"])


def test_the_pooled_error_tracks_seasoning_rather_than_being_noise(built):
    """Pooling over age fails systematically, not randomly.

    If the error were noise it would scatter around zero. It does not: a method
    blind to age must overstate risk where the true hazard is low and
    understate it where it peaks, so the error has a sign that follows the
    seasoning curve. That is what makes it worth fixing rather than averaging
    away.
    """
    connection, _ = built
    by_age = pdmod.error_by_age(connection, "pooled", "12m")

    assert len(by_age) >= 3
    errors = by_age["mean_error"].to_numpy()

    # Both signs must appear — a purely one-sided error would be a simple
    # calibration offset, not a seasoning effect.
    assert errors.min() < 0 < errors.max(), (
        f"the pooled error has one sign at every age ({errors}), which is a "
        "level problem rather than the seasoning pattern this test describes"
    )
    # And the spread must be material next to the quantity being estimated.
    assert errors.max() - errors.min() > 0.01


def test_the_pooled_error_cancels_in_aggregate_but_not_per_account(built):
    """The reason an aggregate check is not enough.

    Pooling understates subprime and overstates prime. Those errors partly
    cancel in the book total, so a method can look acceptable on the headline
    number while mispricing every individual account — and a provision is
    built account by account.
    """
    connection, _ = built
    by_segment = pdmod.error_by_segment(connection, "pooled", "12m")

    errors = by_segment["mean_error"].to_numpy()
    assert errors.min() < 0 < errors.max(), (
        "pooling should overstate some segments and understate others"
    )

    frame = pdmod.accuracy(connection, "12m").set_index("method")
    aggregate_bias = abs(frame.loc["pooled", "mean_error"])
    per_account_error = frame.loc["pooled", "mean_absolute_error"]

    assert per_account_error > aggregate_bias * 2, (
        f"the per-account error ({per_account_error:.4f}) should be much larger "
        f"than the aggregate bias ({aggregate_bias:.4f}) — that gap IS the "
        "cancellation this test is about"
    )


def test_the_chosen_method_clears_the_configured_threshold(built):
    connection, config = built
    checks = pdmod.validate(connection, config)
    failed = checks[~checks["passed"]]
    assert len(failed) == 0, f"failing checks: {failed.to_dict('records')}"
