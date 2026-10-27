"""
Phase 2 tests — delinquency transitions and roll rates.

Two headlines here, and they test different things.

`test_the_estimated_matrix_matches_the_known_truth` is the first time in this
project that an ESTIMATE meets the answer key. The transition matrix counted
from half a million observed account-months must reproduce the matrix those
months were drawn from. Two unrelated routes to the same quantity.

`test_the_naive_denominator_is_biased_downward` is the opposite kind of test:
it asserts that the obvious wrong implementation really is wrong, and by how
much. A test suite that only ever confirms the right answer leaves the reader
no reason to believe the right answer was hard to get.

Run with:  pytest tests/test_rollrate.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed
from src import db, generate, mart, rollrate


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def built(config, tmp_path_factory):
    """A book large enough for the transition estimates to be stable.

    Smaller than the full book so the suite stays quick, but the deep arrears
    buckets are thinly populated by construction — most accounts never reach
    60-89 DPD — so too small a book leaves nothing to estimate from.
    """
    path = tmp_path_factory.mktemp("rollrate") / "test.duckdb"

    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=6000)

    rng = set_seed(31415)
    accounts, performance, truth = generate.generate_book(sized, rng)

    connection = db.connect(sized, path)
    db.write_table(connection, "accounts", accounts)
    db.write_table(connection, "loan_month", performance)
    db.write_table(connection, "ground_truth", truth)
    mart.build(connection, sized, verbose=False)
    rollrate.build(connection, verbose=False)

    yield connection, sized
    connection.close()


# --- Censoring: the trap this phase is really about ------------------------

def test_exactly_one_transition_is_lost_per_account(built):
    """Every account's final month has no successor. Exactly one, no more.

    This is arithmetic, not approximation: transitions must equal fact rows
    minus accounts. If it is short by more, the window function is dropping
    moves — most likely the `months_on_book + 1` guard firing on a gap, which
    would mean the mart's own no-gaps check is lying.
    """
    connection, _ = built
    counts = db.read_sql(connection, """
        SELECT (SELECT COUNT(*) FROM fact_loan_month)             AS fact_rows,
               (SELECT COUNT(DISTINCT account_id) FROM fact_loan_month) AS accounts,
               (SELECT COUNT(*) FROM bucket_transition)           AS transitions
    """).iloc[0]

    assert counts["transitions"] == counts["fact_rows"] - counts["accounts"], (
        f"{counts['fact_rows'] - counts['accounts'] - counts['transitions']} "
        "transitions went missing beyond the one lost per account"
    )


def test_no_transition_leaves_default(built):
    """Default is absorbing. A move out of it would contradict the model and
    would quietly create cures that never happened."""
    connection, _ = built
    escapes = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM bucket_transition WHERE from_bucket = 4
    """)["n"].iloc[0]
    assert escapes == 0


def test_every_transition_spans_exactly_one_month(built):
    """A move computed across a gap would be two months counted as one."""
    connection, _ = built
    bad = db.read_sql(connection, """
        SELECT COUNT(*) AS n
        FROM bucket_transition t
        JOIN fact_loan_month f
          ON f.account_id = t.account_id
         AND f.months_on_book = t.months_on_book + 1
        WHERE f.arrears_bucket <> t.to_bucket
    """)["n"].iloc[0]
    assert bad == 0, "a recorded destination does not match the next month's bucket"


def test_the_naive_denominator_is_biased_downward(built):
    """The obvious wrong implementation, and the size of its error.

    Dividing by the number of ROWS in a bucket rather than the number of rows
    whose successor was observed adds a phantom "stayed" for every account's
    final month. Every roll-forward rate is then too low, and the missing mass
    lands on the diagonal.

    The bias is asserted to be real and directional, not merely nonzero — a
    test that only checked "the two differ" would pass if they differed the
    wrong way.
    """
    connection, _ = built
    bias = rollrate.censoring_bias(connection)

    rolls = bias[bias["to_bucket"] > bias["from_bucket"]]
    assert len(rolls) >= 3, "not enough roll-forward transitions to judge"

    assert (rolls["naive_rate"] < rolls["correct_rate"]).all(), (
        "the naive denominator should understate every roll-forward rate"
    )
    assert (rolls["relative_understatement"] > 0.005).all(), (
        "the bias should be material enough to matter, not a rounding wobble"
    )

    stays = bias[bias["to_bucket"] == bias["from_bucket"]]
    assert (stays["naive_rate"] < stays["correct_rate"]).all(), (
        "both rates fall under the inflated denominator; the DIAGONAL is where "
        "the missing probability mass ends up when rates are renormalised"
    )


# --- The matrix must be a matrix -------------------------------------------

def test_rows_sum_to_one_except_the_absorbing_state(built):
    connection, _ = built
    frame = rollrate.matrix(connection)

    for name, row in frame.iterrows():
        if name == generate.BUCKETS[generate.DEFAULT_BUCKET]:
            assert row.sum() == pytest.approx(0.0, abs=1e-12), (
                "default is absorbing, so it has no outgoing transitions at all"
            )
        else:
            assert row.sum() == pytest.approx(1.0, abs=1e-9), (
                f"row '{name}' sums to {row.sum():.6f}, so it is not a "
                "conditional distribution"
            )


def test_no_bucket_is_skipped(built):
    """Arrears progress one bucket at a time; a jump would mean a missing month."""
    connection, _ = built
    jumps = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM bucket_transition
        WHERE ABS(to_bucket - from_bucket) > 1
    """)["n"].iloc[0]
    assert jumps == 0


# --- THE test: the estimate meets the answer key ---------------------------

def test_the_estimated_matrix_matches_the_known_truth(built):
    """Counted transitions must reproduce the matrix they were drawn from.

    The comparison is against the EXPOSURE-WEIGHTED AVERAGE of the true
    matrices, not against the base rates in config. The true matrix varies by
    segment and by month on book, so no single true matrix exists to compare a
    pooled estimate against — but every observed transition had one, and
    averaging them with the weights the data carries gives the quantity the
    pooled estimate is estimating.

    Comparing against the config base rates instead would show a gap of several
    percentage points and look like a bug in the estimation. It would be a bug
    in the comparison.
    """
    connection, config = built
    comparison = rollrate.compare_to_truth(connection, config)

    assert len(comparison) >= 8, "too few transitions compared to mean anything"

    # Four standard errors: loose enough not to flake across seeds, tight
    # enough that a real estimation error cannot hide behind it.
    worst = comparison.reindex(
        comparison["gap_in_standard_errors"].abs().sort_values(ascending=False).index
    ).iloc[0]

    assert abs(worst["gap_in_standard_errors"]) < 4.0, (
        f"{worst['from_bucket']} -> {worst['to_bucket']}: observed "
        f"{worst['observed']:.3%} against a true {worst['truth']:.3%}, a gap of "
        f"{worst['gap_in_standard_errors']:+.1f} standard errors on "
        f"{worst['opportunities']:,} opportunities. The estimate and the matrix "
        "it was drawn from disagree."
    )

    # And no systematic drift: the gaps should scatter around zero, not sit
    # mostly on one side, which would indicate a bias rather than noise.
    mean_gap = comparison["gap_in_standard_errors"].mean()
    assert abs(mean_gap) < 1.5, (
        f"the gaps average {mean_gap:+.2f} standard errors — that is a "
        "systematic bias, not sampling noise"
    )


def test_comparing_against_config_base_rates_would_be_wrong(built):
    """Documents why `expected_pooled_matrix` exists at all.

    The base rates are what a near-prime account at the seasoning peak
    experiences. The book averages something else, because it is a mix of
    segments at a mix of ages. This asserts the two genuinely differ, so the
    weighting is doing work rather than being ceremony.
    """
    connection, config = built
    expected, _ = rollrate.expected_pooled_matrix(connection, config)
    base = config["generator"]["base_transitions"]

    weighted_miss = expected.iloc[0, 1]
    assert weighted_miss != pytest.approx(base["miss_from_current"], abs=0.005), (
        "the weighted-average miss rate happens to equal the base rate, so "
        "this test cannot tell the two approaches apart — pick a transition "
        "where seasoning and segment mix actually bite"
    )


# --- The cuts must say something ------------------------------------------

def test_roll_to_default_orders_by_segment(built):
    """If the grading does not order the roll rates, it is decorative."""
    connection, _ = built
    rates = db.read_sql(connection, """
        SELECT cut_value, rate, opportunities
        FROM roll_rate_segmented
        WHERE cut_type = 'segment' AND from_bucket = 3 AND to_bucket = 4
        ORDER BY rate
    """)
    assert list(rates["cut_value"]) == ["prime", "near_prime", "subprime"], (
        f"roll-to-default does not order by risk grade: "
        f"{rates[['cut_value', 'rate']].to_dict('records')}"
    )


def test_seasoning_is_visible_in_the_miss_rate(built):
    """Risk peaks part-way through a loan's life, not at origination.

    If this were flat, the generator's seasoning curve would not be reaching
    the data — and Phase 7's vintage analysis would have nothing to show.
    """
    connection, config = built
    by_age = db.read_sql(connection, """
        SELECT cut_value, rate FROM roll_rate_segmented
        WHERE cut_type = 'months_on_book_band'
          AND from_bucket = 0 AND to_bucket = 1
        ORDER BY cut_value
    """)
    rates = by_age["rate"].to_numpy()
    assert len(rates) >= 4

    peak = int(np.argmax(rates))
    assert 0 < peak < len(rates) - 1, (
        f"the miss rate peaks at band {peak} of {len(rates)} — seasoning should "
        "hump in the middle, not at either end"
    )
    assert rates.max() / rates.min() > 1.2, "the seasoning effect is too faint to see"


def test_every_cut_carries_its_sample_size(built):
    """A rate without a denominator invites reading a trend into nine accounts."""
    connection, _ = built
    missing = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM roll_rate_segmented
        WHERE opportunities IS NULL OR opportunities = 0
    """)["n"].iloc[0]
    assert missing == 0


def test_segmented_rows_sum_to_one_within_each_cut(built):
    connection, _ = built
    bad = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT cut_type, cut_value, from_bucket, SUM(rate) AS total
            FROM roll_rate_segmented
            GROUP BY cut_type, cut_value, from_bucket
            HAVING ABS(SUM(rate) - 1.0) > 1e-9
        )
    """)["n"].iloc[0]
    assert bad == 0
