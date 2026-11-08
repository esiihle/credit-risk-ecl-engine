"""
Phase 6 tests, the ECL calculation.

The headline is `test_the_arithmetic_reproduces_the_key_on_exact_inputs`. Every
earlier phase checked an ESTIMATE against the truth and had to allow a
tolerance for sampling error. This phase's work is deterministic arithmetic, so
it can be checked exactly: feed the engine's own expression the true marginal
PD, LGD, exposure and discount factors, and it must return the answer key to
floating point. Nothing about Phase 4's or Phase 5's accuracy enters that.

`test_the_answer_key_provisions_credit_impaired_accounts` guards the bug this
phase found. The key recorded all 61 credit-impaired accounts at a 100%
lifetime PD and an ECL of exactly zero, internally contradictory, and in the
one direction an ECL engine must never be wrong in.

`test_using_cumulative_pd_would_overstate_the_provision` is the test that makes
the exact reconciliation worth having: it demonstrates that the most natural
wrong implementation produces a materially different number, so the check above
is distinguishing right from wrong rather than confirming a tautology.

Run with:  pytest tests/test_ecl.py -v
"""
import inspect
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed
from src import db, ecl, generate, lgd, mart, rollrate, staging
from src import pd as pdmod


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def built(config, tmp_path_factory):
    """A book large enough that every stage is populated.

    Stage 3 needs accounts sitting in the default bucket at the reporting date,
    and those are rare: default is absorbing and ends the account's series, so
    only accounts that defaulted in the final month appear. On a small book
    there may be none at all, and the impaired-account tests would then pass by
    vacuously finding nothing to check.
    """
    path = tmp_path_factory.mktemp("ecl") / "test.duckdb"

    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=14000)

    rng = set_seed(16180)
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
    pdmod.build(connection, sized, verbose=False)
    lgd.build(connection, sized, verbose=False)
    ecl.build(connection, sized, verbose=False)

    yield connection, sized
    connection.close()


def test_every_stage_is_populated(built):
    """Guards every other test in this file from passing vacuously."""
    connection, _ = built
    stages = db.read_sql(connection, """
        SELECT stage, COUNT(*) AS n FROM ecl_account GROUP BY stage
        ORDER BY stage""").set_index("stage")["n"]
    assert set(stages.index) == {1, 2, 3}
    assert (stages > 10).all(), f"a stage is too thin to test: {stages.to_dict()}"


# --- The headline: the arithmetic ------------------------------------------

def test_the_arithmetic_reproduces_the_key_on_exact_inputs(built):
    """THE GATE THAT GUARDS PHASE 6.

    The engine's ECL expression, given the true marginal PD, the true LGD, the
    exact exposure and the exact discount factors, must return the answer key.
    Any gap is an error in the arithmetic itself: cumulative instead of
    marginal PD, the wrong horizon for a stage, a missing or doubled discount,
    or the wrong treatment of an impaired account.

    Checked to a cent rather than to zero only because the key rounds the
    balances it publishes; in practice it reconciles at ~1e-11.
    """
    connection, sized = built
    residuals = ecl.arithmetic_residuals(connection)

    assert residuals["accounts"] > 0
    assert residuals["accounts_over_a_cent"] == 0
    assert residuals["max_residual_lifetime"] <= \
        sized["validation"]["max_ecl_arithmetic_residual"]
    assert residuals["max_residual_12m"] <= \
        sized["validation"]["max_ecl_arithmetic_residual"]
    # And it is not merely inside the tolerance, it is floating-point exact.
    assert residuals["max_residual_lifetime"] < 1e-6


def test_using_cumulative_pd_would_overstate_the_provision(built):
    """Why the exact reconciliation is worth having.

    A check that cannot fail proves nothing, so the most natural wrong
    implementation is built here and shown to produce a materially different
    answer. Summing `cumulative_pd(t) x LGD x EAD(t) x discount(t)` counts the
    same default once in every month after it occurs, so it overstates badly,
    and it is an easy mistake to make, because `pd_term_structure` offers both
    columns side by side and the cumulative one is the figure quoted everywhere
    else in the project.
    """
    connection, sized = built
    comparison = db.read_sql(connection, """
        SELECT
            SUM(m.marginal_pd   * l.lgd_resolved_only * e.ead * e.discount_factor)
                                                            AS done_right,
            SUM(m.cumulative_pd * l.lgd_resolved_only * e.ead * e.discount_factor)
                                                            AS done_wrong
        FROM account_pd a
        JOIN ead_schedule e      ON e.account_id = a.account_id
        JOIN pd_term_structure m ON m.segment      = a.segment
                                AND m.start_age    = a.months_on_book
                                AND m.start_bucket = a.arrears_bucket
                                AND m.horizon      = e.horizon
        JOIN lgd_estimate l      ON l.segment = a.segment
        WHERE e.horizon >= 1 AND a.stage < 3""").iloc[0]

    overstatement = comparison["done_wrong"] / comparison["done_right"]
    assert overstatement > 3.0, (
        f"using cumulative PD overstates by only {overstatement:.2f}x, if "
        f"this ever falls near 1, the exact reconciliation above has stopped "
        f"being able to tell the two implementations apart")


# --- The bug this phase found ----------------------------------------------

def test_the_answer_key_provisions_credit_impaired_accounts(built):
    """The key recorded a 100% chance of default and no expected loss.

    `ground_truth` built ECL as the sum over future months of the probability
    of defaulting IN that month. Default is absorbing, so for an account
    already in bucket 4 every one of those increments is zero and the sum came
    out as exactly 0.00, while `true_pd_lifetime` for the same account came
    out 1.00. Sixty-one accounts, R939k of exposure, no provision behind it.

    The formula was right about what it computes and was being asked a question
    it does not answer. An impaired account's loss is not awaited in a future
    month; it is incurred, at `LGD x EAD`.

    Second time the key has been valid for only part of the book, the first
    was the same function computing every account's PD as though it were
    performing (Phase 4, `test_the_answer_key_uses_each_accounts_actual_bucket`).
    """
    connection, _ = built
    impaired = db.read_sql(connection, """
        SELECT COUNT(*)                                   AS accounts,
               SUM(true_ecl_lifetime)                     AS true_ecl,
               SUM(ead_now)                               AS exposure,
               MIN(true_pd_lifetime)                      AS min_pd,
               SUM(CASE WHEN true_ecl_lifetime <= 0 THEN 1 ELSE 0 END)
                                                          AS unprovisioned
        FROM ecl_account WHERE stage = 3""").iloc[0]

    assert impaired["accounts"] > 0
    assert impaired["min_pd"] > 0.99, "an impaired account's PD should be ~1"
    assert impaired["unprovisioned"] == 0, (
        "a credit-impaired account has a 100% probability of default; an ECL "
        "of zero against it is the answer key contradicting itself")
    # The provision has to be of the order of LGD x exposure, not a token.
    assert impaired["true_ecl"] > 0.4 * impaired["exposure"]


def test_an_impaired_account_is_provisioned_at_lgd_times_exposure(built):
    """And the engine applies the same formula the key now does.

    Checked as a definition rather than a closeness: the impaired provision
    must BE `lgd x ead`, because any horizon-based sum over future months would
    silently give zero and look like a very well-performing book.
    """
    connection, _ = built
    impaired = db.read_sql(connection, """
        SELECT account_id, ecl_reported, ecl_12m, ecl_lifetime,
               lgd_applied * ead_now AS expected
        FROM ecl_account WHERE stage = 3""")

    assert len(impaired) > 0
    assert np.allclose(impaired["ecl_reported"], impaired["expected"], atol=0.01)
    # 12-month and lifetime are equal: the loss does not grow with the horizon
    # because the event causing it has already happened.
    assert np.allclose(impaired["ecl_12m"], impaired["ecl_lifetime"], atol=0.01)


def test_the_monthly_schedule_agrees_with_the_account_level_key(built):
    """Pins two functions that compute the same truth by the same formulas.

    `true_monthly_schedule` repeats `ground_truth`'s loop at a finer grain. The
    formulas are shared but the loops are not, so they could drift apart, and
    if they did, the exact reconciliation above would start comparing the
    engine against something that no longer matched the key it reports against.
    """
    connection, _ = built
    agreement = db.read_sql(connection, """
        WITH rebuilt AS (
            SELECT t.account_id,
                   SUM(t.true_marginal_pd * g.true_lgd
                       * t.true_ead * t.true_discount_factor) AS ecl
            FROM ground_truth_monthly t
            JOIN ground_truth g ON g.account_id = t.account_id
            GROUP BY t.account_id)
        SELECT MAX(ABS(r.ecl - g.true_ecl_lifetime)) AS worst,
               COUNT(*)                              AS accounts
        FROM rebuilt r JOIN ground_truth g ON g.account_id = r.account_id""").iloc[0]

    assert agreement["accounts"] > 0
    assert agreement["worst"] < 0.01


# --- The horizon is the accounting judgement -------------------------------

def test_stage_1_is_twelve_months_and_stage_2_is_lifetime(built):
    """The stage decides the horizon. and that is the whole of IFRS 9 here.

    Asserted on the reported figure rather than on the code: a Stage 1 account
    must carry its 12-month ECL and a Stage 2 account its lifetime ECL, and for
    accounts with enough term left those two differ, so getting the mapping
    backwards is detectable.

    THE THRESHOLD IS 14 MONTHS, NOT 13, and the reason is worth knowing. An
    account with exactly 13 months left has its 13th month AT maturity, where
    the amortised balance is exactly zero, so no loss can arise in it and
    `ecl_lifetime` equals `ecl_12m` to the cent, correctly. The first version of
    this test used `months_remaining > 12` and failed on 179 such accounts,
    reporting correct arithmetic as a missing horizon. The engine was right and
    the test's assumption about it was wrong.
    """
    connection, _ = built
    mapping = db.read_sql(connection, """
        SELECT
            SUM(CASE WHEN stage = 1 AND ABS(ecl_reported - ecl_12m) > 0.01
                     THEN 1 ELSE 0 END)                   AS stage_1_wrong,
            SUM(CASE WHEN stage = 2 AND ABS(ecl_reported - ecl_lifetime) > 0.01
                     THEN 1 ELSE 0 END)                   AS stage_2_wrong,
            -- Restricted to stages 1 and 2. Stage 3 is EXCLUDED because its
            -- 12-month and lifetime figures are equal by design -- the loss is
            -- already incurred, so it does not grow with the horizon -- and
            -- the previous version of this query swept those accounts in and
            -- reported that intentional equality as a missing horizon.
            SUM(CASE WHEN months_remaining >= 14 AND stage < 3
                          AND ecl_lifetime <= ecl_12m + 0.01
                     THEN 1 ELSE 0 END)                   AS no_horizon_effect,
            -- Monotonicity holds with no tolerance at all, everywhere.
            SUM(CASE WHEN ecl_lifetime < ecl_12m - 1e-9 THEN 1 ELSE 0 END)
                                                          AS lifetime_below_12m,
            -- And the maturity month really does carry no exposure.
            (SELECT COUNT(*) FROM ead_schedule e
             JOIN dim_account a ON a.account_id = e.account_id
             WHERE e.months_on_book_then = a.term_months
               AND e.ead > 0.01)                          AS exposure_at_maturity
        FROM ecl_account""").iloc[0]

    assert mapping["stage_1_wrong"] == 0
    assert mapping["stage_2_wrong"] == 0
    assert mapping["lifetime_below_12m"] == 0
    assert mapping["exposure_at_maturity"] == 0
    assert mapping["no_horizon_effect"] == 0, (
        "lifetime ECL should exceed 12-month ECL wherever 14 or more months "
        "remain; if not, the horizon is not being applied")


def test_the_staging_cliff_shrinks_as_arrears_deepen(built):
    """The phase's substantive finding, asserted so it cannot quietly vanish.

    IFRS 9 is criticised for the discontinuity at the SICR boundary, and the
    measured answer is more interesting than the criticism: the jump is largest
    for accounts in NO arrears and almost nil for accounts 60-89 days down,
    because the latter will mostly default inside twelve months regardless, so
    extending the horizon to lifetime adds little.

    Which means the cliff is steepest for exactly the accounts least likely to
    trip the arrears-based trigger.
    """
    connection, _ = built
    cliff = ecl.staging_cliff(connection).set_index("arrears_bucket")

    assert {0, 1, 2, 3}.issubset(set(cliff.index))
    multiples = cliff.loc[[0, 1, 2, 3], "cliff_multiple"].tolist()
    assert all(m >= 1.0 for m in multiples), "lifetime cannot be below 12-month"
    # Monotone decreasing in arrears depth.
    assert multiples == sorted(multiples, reverse=True), (
        f"the cliff should shrink as arrears deepen, got {multiples}")
    # And the spread is material, not a rounding artefact.
    assert multiples[0] > multiples[-1] + 0.25


def test_discounting_reduces_the_provision_and_uses_the_eir(built):
    """IFRS 9 discounts at the asset's effective interest rate.

    Two claims. That discounting happens at all, an undiscounted sum would be
    larger, and silently so. And that the rate is the EIR rather than anything
    else, which is checked against `dim_account.annual_rate` directly because
    using one rate to build the amortisation schedule and another to discount
    off it would be a plausible-looking error with no visible symptom.
    """
    connection, _ = built
    effect = db.read_sql(connection, """
        SELECT
            SUM(m.marginal_pd * l.lgd_resolved_only * e.ead * e.discount_factor)
                                                            AS discounted,
            SUM(m.marginal_pd * l.lgd_resolved_only * e.ead) AS undiscounted
        FROM account_pd a
        JOIN ead_schedule e      ON e.account_id = a.account_id
        JOIN pd_term_structure m ON m.segment      = a.segment
                                AND m.start_age    = a.months_on_book
                                AND m.start_bucket = a.arrears_bucket
                                AND m.horizon      = e.horizon
        JOIN lgd_estimate l      ON l.segment = a.segment
        WHERE e.horizon >= 1""").iloc[0]
    assert effect["discounted"] < effect["undiscounted"]

    wrong_rate = db.read_sql(connection, """
        SELECT COUNT(*) AS n
        FROM ead_schedule e
        JOIN dim_account a ON a.account_id = e.account_id
        WHERE ABS(e.discount_factor
                  - 1.0 / POWER(POWER(1.0 + a.annual_rate, 1.0/12.0), e.horizon))
              > 1e-9""")["n"].iloc[0]
    assert wrong_rate == 0


# --- Properties a provision must have --------------------------------------

def test_no_provision_is_negative_or_exceeds_its_exposure(built):
    """Neither is possible if the inputs are sane, so both are checked.

    An ECL above the exposure would mean expecting to lose more than is owed;
    a negative one would mean expecting a profit from default. Either would
    indicate an LGD outside [0, 1] or a PD schedule summing above 1, and both
    would survive a check on the portfolio total.
    """
    connection, _ = built
    bad = db.read_sql(connection, """
        SELECT
            SUM(CASE WHEN ecl_reported < 0 THEN 1 ELSE 0 END)          AS negative,
            SUM(CASE WHEN ecl_reported > ead_now + 0.01 THEN 1 ELSE 0 END)
                                                                       AS over_exposure
        FROM ecl_account""").iloc[0]
    assert bad["negative"] == 0
    assert bad["over_exposure"] == 0


def test_coverage_rises_with_stage(built):
    """A provision that did not increase with stage would mean staging is doing
    nothing, which would make Phase 3 decorative. Checked as a ratio so it is
    independent of how much exposure happens to sit in each stage.
    """
    connection, _ = built
    coverage = ecl.portfolio(connection, level="stage").set_index("stage")
    ratios = coverage.loc[["1", "2", "3"], "coverage_ratio"].tolist()
    assert ratios == sorted(ratios), f"coverage should rise with stage: {ratios}"
    # And the steps are substantial, not incidental.
    assert ratios[1] > ratios[0] * 3
    assert ratios[2] > ratios[1] * 1.5


def test_the_portfolio_rollup_ties_to_the_accounts(built):
    """Aggregates must sum to their own detail.

    `ecl_portfolio` stacks three grouping levels in one table, which is
    convenient and makes double-counting easy. Each level must independently
    reproduce the per-account total.
    """
    connection, _ = built
    accounts_total = db.read_sql(connection, """
        SELECT SUM(ecl_reported) AS ecl, SUM(ead_now) AS exposure,
               COUNT(*) AS n FROM ecl_account""").iloc[0]

    for level in ("portfolio", "stage", "stage x segment"):
        rolled = db.read_sql(connection, """
            SELECT SUM(ecl) AS ecl, SUM(exposure) AS exposure,
                   SUM(accounts) AS n
            FROM ecl_portfolio WHERE grouping_level = $level""",
            level=level).iloc[0]
        assert abs(rolled["ecl"] - accounts_total["ecl"]) < 0.01, level
        assert abs(rolled["exposure"] - accounts_total["exposure"]) < 0.01, level
        assert rolled["n"] == accounts_total["n"], level


# --- Where the error comes from --------------------------------------------

def test_the_remaining_error_is_pd_not_lgd(built):
    """Measured rather than assumed, because the obvious guess is wrong.

    A single segment-level LGD applied to every account looks like the crude
    part of this engine. Substituting each account's true LGD moves the
    portfolio total by a fraction of a percent, so it is not: the residual gap
    is PD estimation error inherited from Phase 4, and inside the tolerance
    Phase 4 set for itself.

    Worth pinning, because it is the answer to "what would you improve next"
    and it should be an answer the code can still demonstrate.
    """
    connection, sized = built
    attribution = ecl.input_attribution(connection, sized)

    estimated = float(attribution["estimated_pd_estimated_lgd"])
    true_lgd = float(attribution["estimated_pd_true_lgd"])
    truth = float(attribution["true_pd_true_lgd"])

    lgd_effect = abs(true_lgd / estimated - 1.0)
    total_gap = abs(truth / estimated - 1.0)

    assert total_gap > 0, "there should be some gap to attribute"
    assert lgd_effect < 0.02, (
        f"LGD granularity moves the total by {lgd_effect:.2%}")
    assert lgd_effect < total_gap, (
        "LGD should account for less of the gap than PD does")


def test_the_arithmetic_gate_is_stricter_than_the_accuracy_backstop(config):
    """The restructuring this phase made, asserted so it is not undone.

    A single 2% ECL tolerance was replaced by two gates: an exact reconciliation
    of the arithmetic, and a loose backstop on end-to-end accuracy. The defence
    for changing a threshold after seeing a result is that the FORM was wrong,
    so the replacement has to actually be stricter on this phase's own work,
    not merely looser overall.

    The arithmetic gate is in currency per account; the backstop is relative on
    a portfolio total. Compared here as fractions of the portfolio provision,
    which is the only way they are commensurable at all.
    """
    limits = config["validation"]
    residual = float(limits["max_ecl_arithmetic_residual"])
    backstop = float(limits["max_ecl_relative_error"])

    # A cent per account, across even a small book, is a vanishing share of the
    # provision, orders of magnitude tighter than the backstop.
    assert residual > 0
    assert backstop > 0
    assert residual / 1_000_000.0 < backstop / 1000.0

    # And the backstop must be no tighter than the input gates it inherits
    # from, or it is re-testing Phase 4 rather than guarding Phase 6.
    assert backstop >= float(limits["max_lifetime_pd_relative_error"]), (
        "an ECL tolerance tighter than the PD tolerance feeding it cannot be "
        "met by correct code; see methodology §7.3")


# --- Assessability propagates downstream ------------------------------------
#
# The fifth appearance of one error, and the first time it had a general form.
# Phases 4 and 5 each decide from their own sample whether their accuracy can be
# assessed. ECL is a function of both estimates, so if neither could be judged,
# an accuracy figure computed from them cannot be judged either.
#
# Found by rehearsing CI in a clean room: on the committed 400-account sample
# the ECL backstop failed at -15.4% while, on the same run, both PD gates and
# the LGD absolute gate printed `--`. The pipeline was reporting "there is no
# evidence about this" as "this is wrong".

def test_the_accuracy_backstop_is_not_assessed_when_its_inputs_were_not(config, tmp_path):
    """A 400-account book cannot support a judgement about ECL accuracy.

    Its age-aware PD matrix is 60% fallback cells and its LGD standard error is
    four times its own tolerance, so Phases 4 and 5 both decline to judge
    themselves. ECL inherits that, and says so.
    """
    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=400)

    # The COMMITTED SAMPLE's own seed and size, so this test reproduces the
    # exact case that failed in CI rather than an approximation of it. On
    # another seed the relative error happens to land under the backstop
    # (11.96% on seed 16180), and the test would then pass without the
    # exemption doing any work at all.
    rng = set_seed(config.get("seed", 42))
    accounts, performance, truth, recoveries = generate.generate_book(sized, rng)
    monthly = generate.true_monthly_schedule(accounts, sized, performance)

    connection = db.connect(sized, tmp_path / "small.duckdb")
    try:
        for name, frame in (("accounts", accounts), ("loan_month", performance),
                            ("ground_truth", truth), ("recovery", recoveries),
                            ("ground_truth_monthly", monthly)):
            db.write_table(connection, name, frame)
        mart.build(connection, sized, verbose=False)
        rollrate.build(connection, verbose=False)
        staging.build(connection, sized, verbose=False)
        pdmod.build(connection, sized, verbose=False)
        lgd.build(connection, sized, verbose=False)
        ecl.build(connection, sized, verbose=False)

        upstream = ecl.inputs_were_assessable(connection, sized)
        assert not upstream["assessable"], (
            "this test needs a book whose inputs cannot be assessed; if the "
            "sample has grown, pick a smaller one")

        checks, relative_error = ecl.validate(connection, sized)
        backstop = checks[checks["check"].str.contains("backstop")].iloc[0]

        # The error really does exceed the limit...
        assert abs(relative_error) > sized["validation"]["max_ecl_relative_error"]
        # ...and the engine declines to call that a failure.
        assert backstop["status"] == "not assessed"
        assert not backstop["assessable"]
        assert not (checks["status"] == "fail").any()
        assert checks["passed"].all()

        # THE ARITHMETIC GATE STILL APPLIES. It substitutes exact inputs, so it
        # is independent of how good the estimates are, and it is what actually
        # guards this phase. An exemption that silenced it too would leave the
        # sample run proving nothing at all.
        arithmetic = checks[checks["check"].str.contains("arithmetic")].iloc[0]
        assert arithmetic["assessable"]
        assert arithmetic["status"] == "pass"
        residuals = ecl.arithmetic_residuals(connection)
        assert residuals["max_residual_lifetime"] < 1e-6, (
            "the arithmetic must reconcile exactly at every book size")
    finally:
        connection.close()


def test_a_book_whose_inputs_are_assessable_is_actually_judged(built):
    """The other half, so the exemption cannot become an escape hatch.

    If assessability never resolved to true, the backstop would be a gate that
    never fires, indistinguishable from having no gate.
    """
    connection, sized = built
    upstream = ecl.inputs_were_assessable(connection, sized)
    assert upstream["assessable"], (
        f"a {sized['portfolio']['n_accounts']: }-account book should support "
        f"the accuracy backstop; upstream said {upstream}")

    checks, _ = ecl.validate(connection, sized)
    backstop = checks[checks["check"].str.contains("backstop")].iloc[0]
    assert backstop["assessable"]
    assert backstop["status"] == "pass"


def test_assessability_is_decided_upstream_not_from_the_ecl_error(config):
    """The decision must not be able to see the result it would be excusing.

    Same discipline as Phase 5's precision gate, and asserted the same way,
    against the source text, because this is a claim about the order of the
    reasoning that no input can demonstrate.

    `inputs_were_assessable` must reach its verdict from the upstream gates
    alone. If it ever consulted this phase's own relative error, a bad ECL
    could excuse itself.
    """
    source = inspect.getsource(ecl.inputs_were_assessable)
    for forbidden in ("relative_error", "ecl_portfolio", "arithmetic_residuals"):
        assert forbidden not in source, (
            f"inputs_were_assessable reads {forbidden}, the decision can now "
            f"see the result it would be excusing")

    # And in validate(), the verdict is taken before the error is read.
    source = inspect.getsource(ecl.validate)
    decision = source.index("accuracy_assessable =")
    before = source[:decision]
    assert "inputs_were_assessable" in before
    assert 'relative_error = float(' not in before, (
        "the relative error is computed before assessability is decided")
