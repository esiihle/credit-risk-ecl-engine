"""
Phase 0 tests — the synthetic book and its answer key.

The headline is `test_simulated_defaults_match_the_analytic_truth`. Everything
this project will later claim rests on the ground truth being *true*: if the
simulator and the analytic PD disagree, then every validation downstream is
comparing the engine against the wrong answer, and would pass or fail for
reasons that have nothing to do with the engine.

The two are computed by genuinely independent routes — one walks random paths
through the chain, the other multiplies matrices — so agreement is evidence,
not a restatement.

Run with:  pytest tests/test_generate.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed
from src import generate as g


@pytest.fixture(scope="module")
def config():
    return load_config()


# --- The transition matrix must be a transition matrix ---------------------

def test_every_row_sums_to_one(config):
    """A matrix whose rows do not sum to 1 produces nonsense that looks fine.

    The multiplier and seasoning factor both scale raw probabilities, so a
    careless combination can push a row past certainty. The build clips and
    renormalises; this asserts it worked across the whole parameter range.
    """
    for multiplier in (0.5, 0.75, 1.0, 1.55, 3.0):
        for month in (0, 1, 14, 36, 60):
            matrix = g.transition_matrix(multiplier, month, config)
            assert matrix.shape == (g.N_BUCKETS, g.N_BUCKETS)
            np.testing.assert_allclose(matrix.sum(axis=1), 1.0, atol=1e-12)
            assert (matrix >= 0).all(), "a negative probability is not a probability"


def test_default_is_absorbing(config):
    """Once 90+ days past due, an account stays there.

    This project's default definition. Cures out of default happen in reality
    and are deliberately excluded — they complicate Stage 3 accounting and the
    job here is the core mechanics. Stated as a test so the choice is visible.
    """
    matrix = g.transition_matrix(1.0, 12, config)
    assert matrix[g.DEFAULT_BUCKET, g.DEFAULT_BUCKET] == 1.0
    assert matrix[g.DEFAULT_BUCKET, :g.DEFAULT_BUCKET].sum() == 0.0


def test_riskier_segments_default_more(config):
    """The ordering the whole segmentation depends on."""
    prime = g.analytic_pd(0.75, 0, 0, 12, config)
    near_prime = g.analytic_pd(1.00, 0, 0, 12, config)
    subprime = g.analytic_pd(1.55, 0, 0, 12, config)

    assert prime < near_prime < subprime
    # And the spread should be plausible, not the 135x a first version produced
    # by scaling cure rates as well as worsen rates.
    assert 3 < subprime / prime < 20, (
        f"prime {prime:.2%} to subprime {subprime:.2%} is a "
        f"{subprime / prime:.0f}x spread — no real book looks like that"
    )


def test_deeper_arrears_means_higher_pd(config):
    """Starting bucket must matter, monotonically."""
    pds = [g.analytic_pd(1.0, bucket, 0, 12, config) for bucket in range(4)]
    assert pds == sorted(pds)
    assert g.analytic_pd(1.0, g.DEFAULT_BUCKET, 0, 12, config) == 1.0


def test_pd_rises_with_horizon(config):
    """Cumulative default probability cannot fall as the horizon lengthens."""
    curve = g.cumulative_pd_curve(1.0, 0, 0, 36, config)
    assert curve[0] == 0.0
    assert np.all(np.diff(curve) >= -1e-12), "the cumulative curve went down"
    assert curve[-1] > curve[12] > curve[1]


def test_marginal_probabilities_sum_to_the_cumulative(config):
    """The ECL calculation uses the marginals; they must reconstruct the total."""
    horizon = 24
    curve = g.cumulative_pd_curve(1.0, 0, 0, horizon, config)
    marginal = g.marginal_pd_schedule(1.0, 0, 0, horizon, config)

    assert len(marginal) == horizon
    assert (marginal >= -1e-12).all(), "a negative marginal PD is impossible"
    assert marginal.sum() == pytest.approx(curve[-1], abs=1e-12)


# --- THE test this module exists for ---------------------------------------

def test_simulated_defaults_match_the_analytic_truth(config):
    """Random paths through the chain must reproduce the matrix algebra.

    Two independent routes to the same quantity: the simulator draws from the
    transition matrix month by month, while `analytic_pd` multiplies matrices.
    If they disagree, one of them is wrong — and since every later validation
    in this project compares the engine against the analytic value, a wrong
    answer key would make those validations meaningless while still passing.

    The tolerance is the binomial standard error at this sample size, times
    three. Tightening it further would make the test flaky for reasons that are
    about sampling, not correctness.
    """
    test_config = dict(config)
    test_config["portfolio"] = dict(config["portfolio"])
    test_config["portfolio"]["n_accounts"] = 6000
    # Originate everyone on one date so every account is observed for the same
    # window — otherwise the realised rate mixes accounts with different
    # amounts of exposure and is not comparable to a 12-month PD.
    test_config["portfolio"]["origination_start"] = "2023-12-31"
    test_config["portfolio"]["origination_end"] = "2023-12-31"
    test_config["portfolio"]["reporting_date"] = "2024-12-31"
    test_config["portfolio"]["term_months_choices"] = [36]

    rng = set_seed(20261023)
    accounts = g.build_accounts(test_config, rng)
    performance = g.simulate_performance(accounts, test_config, rng)

    # Realised: did each account reach default within its first 12 months?
    within_12 = performance[performance["months_on_book"] <= 12]
    realised = within_12.groupby("account_id")["is_default"].max()

    observed = accounts.set_index("account_id").join(realised.rename("defaulted"))

    for segment, group in observed.groupby("segment"):
        multiplier = group["true_risk_multiplier"].iloc[0]
        expected = g.analytic_pd(multiplier, 0, 0, 12, test_config)
        measured = float(group["defaulted"].mean())

        n = len(group)
        standard_error = np.sqrt(max(expected * (1 - expected), 1e-9) / n)
        tolerance = 3 * standard_error + 0.002

        assert measured == pytest.approx(expected, abs=tolerance), (
            f"{segment}: simulated {measured:.3%} vs analytic {expected:.3%} "
            f"on {n} accounts (tolerance {tolerance:.3%}). The simulator and "
            "the answer key disagree, so one of them is wrong."
        )


# --- Exposure is arithmetic, so it must be exact ---------------------------

def test_the_balance_schedule_amortises_to_zero(config):
    """A fixed-term loan is fully repaid at the end of its term, exactly."""
    assert g.balance_at(50000, 0.21, 36, 36) == pytest.approx(0.0, abs=1e-6)
    assert g.balance_at(50000, 0.21, 36, 0) == pytest.approx(50000.0)


def test_the_balance_never_increases(config):
    months = np.arange(0, 37)
    balances = g.balance_at(50000, 0.21, 36, months)
    assert np.all(np.diff(balances) <= 1e-9)


def test_instalments_repay_principal_and_interest():
    """Total payments must exceed principal by the interest charged."""
    principal, rate, term = 50000.0, 0.21, 36
    payment = g.instalment(principal, rate, term)
    assert payment * term > principal
    # And the schedule the instalment implies must close out the loan.
    i = g.monthly_rate(rate)
    balance = principal
    for _ in range(term):
        balance = balance * (1 + i) - payment
    assert balance == pytest.approx(0.0, abs=0.01)


def test_balance_at_works_on_arrays_and_scalars():
    """The simulator advances the whole book at once; a scalar-only version
    forces a Python loop over every account, every month."""
    scalar = g.balance_at(50000, 0.21, 36, 18)
    array = g.balance_at(np.array([50000.0, 50000.0]), np.array([0.21, 0.21]),
                         np.array([36, 36]), 18)
    assert isinstance(scalar, float)
    np.testing.assert_allclose(array, [scalar, scalar])


# --- The book itself --------------------------------------------------------

def test_the_book_is_internally_consistent(config):
    small = dict(config)
    small["portfolio"] = dict(config["portfolio"], n_accounts=800)

    rng = set_seed(7)
    accounts, performance, truth = g.generate_book(small, rng)

    assert len(accounts) == 800
    assert accounts["account_id"].is_unique
    assert set(performance["account_id"]) <= set(accounts["account_id"])
    assert set(truth["account_id"]) <= set(accounts["account_id"])

    # Nobody is observed for longer than their term.
    longest = performance.groupby("account_id")["months_on_book"].max()
    terms = accounts.set_index("account_id")["term_months"]
    assert (longest <= terms.loc[longest.index]).all()

    # Days past due and the arrears bucket must tell the same story.
    assert (performance.loc[performance["arrears_bucket"] == 0,
                            "days_past_due"] == 0).all()
    assert (performance.loc[performance["is_default"],
                            "days_past_due"] >= 90).all()


def test_defaulted_accounts_stop_being_observed(config):
    """Default is absorbing, so there is nothing to record after it."""
    small = dict(config)
    small["portfolio"] = dict(config["portfolio"], n_accounts=600)

    rng = set_seed(11)
    _, performance, _ = g.generate_book(small, rng)

    for account_id, rows in performance.groupby("account_id"):
        defaults = rows[rows["is_default"]]
        if len(defaults) == 0:
            continue
        assert len(defaults) == 1, "default recorded more than once"
        assert defaults["months_on_book"].iloc[0] == rows["months_on_book"].max()


def test_the_generator_is_deterministic_from_the_seed(config):
    small = dict(config)
    small["portfolio"] = dict(config["portfolio"], n_accounts=500)

    first = g.generate_book(small, set_seed(99))[1]
    second = g.generate_book(small, set_seed(99))[1]
    pd.testing.assert_frame_equal(first, second)


def test_every_artefact_is_labelled_synthetic():
    """A simulated figure that does not say it is simulated is this
    portfolio's worst bug, and it has happened once already."""
    assert "SYNTHETIC" in g.SYNTHETIC_NOTICE
    assert "not a real loan book" in g.SYNTHETIC_NOTICE.lower()
