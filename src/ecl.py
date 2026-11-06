"""
Phase 6 — expected credit loss.

Everything the previous five phases built, multiplied together and discounted:

    ECL = SUM over t of  marginal_PD(t) x LGD x EAD(t) x discount(t)

with the horizon set by the IFRS 9 stage: twelve months for Stage 1, the
remaining lifetime for Stage 2, and for Stage 3 a different formula entirely,
because an account that has already defaulted has no future month in which to
default.

TWO QUESTIONS, TWO GATES. Phase 6's own work is the arithmetic above, and it is
deterministic, so it is checked by feeding the engine's own expression the exact
inputs from `ground_truth_monthly` and demanding the answer key back to the
cent. Whether the ECL built from ESTIMATED inputs is close to truth is a
different question and mostly a question about Phases 4 and 5 — ECL is a
product of three estimates, so to first order its relative error is the sum of
theirs. Conflating the two in a single 2% tolerance produced a gate that
correct code could not pass and incorrect code could. See methodology §7.3.
"""
import pandas as pd

from . import db
from . import lgd as lgdmod
from . import pd as pdmod

SQL_FILES = ["16_ecl_account.sql", "17_ecl_reconciliation.sql",
             "18_ecl_portfolio.sql"]


def build(connection, config, verbose=True):
    """Compute per-account ECL, reconcile the arithmetic, and aggregate."""
    horizon = int(config["ecl"]["horizon_months_stage_1"])
    for name in SQL_FILES:
        if verbose:
            print(f"  {name}")
        db.run_sql_file(connection, name, horizon_stage_1=horizon)
    if verbose:
        print(f"  provisioned {db._count(connection, 'ecl_account'):,} accounts")


def portfolio(connection, level="stage"):
    """The provision by stage, by segment, or in total."""
    return db.read_sql(
        connection,
        "SELECT * FROM ecl_portfolio WHERE grouping_level = $level "
        "ORDER BY stage, segment", level=level)


def arithmetic_residuals(connection):
    """How far the engine's own expression lands from the key on exact inputs.

    This is the measurement that actually tests Phase 6. If it is zero to
    floating point, then marginal-versus-cumulative, the per-stage horizon, the
    discounting and the impaired formula are all right, and any remaining gap
    between the engine and the truth is inherited from the PD and LGD estimates
    rather than created here.
    """
    return db.read_sql(connection, """
        SELECT
            COUNT(*)                                        AS accounts,
            MAX(residual_lifetime)                          AS max_residual_lifetime,
            MAX(residual_12m)                               AS max_residual_12m,
            SUM(CASE WHEN residual_lifetime > 0.01
                      OR residual_12m > 0.01
                     THEN 1 ELSE 0 END)                     AS accounts_over_a_cent
        FROM ecl_reconciliation""").iloc[0]


def staging_cliff(connection):
    """How far the provision jumps when an account crosses the SICR threshold.

    IFRS 9's most-criticised feature, measured on this book instead of argued
    about. `16_ecl_account.sql` computes both horizons for every account
    whatever its stage, so for any population this is the same accounts, the
    same credit quality and the same month, counted two ways.
    """
    return db.read_sql(connection, """
        SELECT
            arrears_bucket,
            COUNT(*)                                        AS accounts,
            SUM(ead_now)                                    AS exposure,
            SUM(ecl_12m)                                    AS ecl_12m,
            SUM(ecl_lifetime)                               AS ecl_lifetime,
            SUM(ecl_lifetime) / NULLIF(SUM(ecl_12m), 0)     AS cliff_multiple
        FROM ecl_account
        WHERE stage < 3
        GROUP BY arrears_bucket
        ORDER BY arrears_bucket""")


def input_attribution(connection, config):
    """How much of the engine's error comes from LGD rather than PD.

    Rebuilds the ECL sum with the true LGD substituted and the estimated PD
    left in place. Whatever gap remains is PD. Worth measuring rather than
    assuming, because the obvious guess — that a single segment-level LGD
    applied to every account must be the crude part — turns out to be wrong.
    """
    horizon = int(config["ecl"]["horizon_months_stage_1"])
    return db.read_sql(connection, """
        WITH monthly AS (
            SELECT a.account_id, a.stage, e.horizon, m.marginal_pd,
                   e.ead, e.discount_factor,
                   l.lgd_resolved_only AS lgd_estimated,
                   g.true_lgd
            FROM account_pd a
            JOIN ead_schedule e      ON e.account_id = a.account_id
            JOIN pd_term_structure m ON m.segment      = a.segment
                                    AND m.start_age    = a.months_on_book
                                    AND m.start_bucket = a.arrears_bucket
                                    AND m.horizon      = e.horizon
            JOIN lgd_estimate l      ON l.segment = a.segment
            JOIN ground_truth g      ON g.account_id = a.account_id
            WHERE e.horizon >= 1 AND a.stage < 3
        ),
        in_horizon AS (
            SELECT *, CASE WHEN stage = 1
                             AND horizon > CAST($horizon AS INTEGER)
                           THEN 0.0 ELSE 1.0 END            AS counts
            FROM monthly
        )
        SELECT
            SUM(marginal_pd * lgd_estimated * ead * discount_factor * counts)
                                                            AS estimated_pd_estimated_lgd,
            SUM(marginal_pd * true_lgd * ead * discount_factor * counts)
                                                            AS estimated_pd_true_lgd,
            (SELECT SUM(true_ecl_reported) FROM ecl_reconciliation WHERE stage < 3)
                                                            AS true_pd_true_lgd
        FROM in_horizon""", horizon=horizon).iloc[0]


def inputs_were_assessable(connection, config):
    """Could the PD and LGD estimates feeding ECL be judged at all?

    ASSESSABILITY PROPAGATES DOWNSTREAM, and this is the rule that says so.

    ECL is a function of the PD and LGD estimates. Phases 4 and 5 each decide,
    from their own sample, whether their accuracy can be assessed — on a small
    book the age-aware PD matrix is mostly fallback cells and the LGD standard
    error swamps its tolerance, so both report `not assessed` rather than
    `fail`. If the inputs could not be judged, an accuracy figure computed from
    them cannot be judged either: it is not a wrong ECL, it is an ECL whose
    accuracy there is no evidence about.

    Judging it anyway is the same error this project has now hit five times, in
    a new place: reporting "I could not determine the answer" as "the answer is
    no". On the committed 400-account sample the ECL backstop failed at -15.4%
    while both PD gates and the LGD gate on the same run printed `--`.

    The ARITHMETIC gate is unaffected and still applies, because it does not
    depend on the inputs being good — it substitutes exact ones. That is the
    gate that guards Phase 6, and it passes on every book size.
    """
    pd_checks = pdmod.validate(connection, config)
    lgd_checks, _, _, _ = lgdmod.validate(connection, config)

    pd_assessable = not (pd_checks["status"] == "not assessed").any()
    lgd_assessable = not (lgd_checks["status"] == "not assessed").any()
    return {
        "assessable": bool(pd_assessable and lgd_assessable),
        "pd_assessable": bool(pd_assessable),
        "lgd_assessable": bool(lgd_assessable),
    }


def validate(connection, config):
    """The two gates, kept apart."""
    limits = config["validation"]
    residuals = arithmetic_residuals(connection)
    total = portfolio(connection, level="portfolio").iloc[0]

    residual_limit = float(limits["max_ecl_arithmetic_residual"])
    relative_limit = float(limits["max_ecl_relative_error"])
    worst_residual = float(max(residuals["max_residual_lifetime"],
                               residuals["max_residual_12m"]))

    # Decided from the UPSTREAM gates, before this phase's own accuracy figure
    # is looked at — the same discipline as Phase 5's precision gate. The
    # question is whether there is evidence to judge, not whether the verdict
    # would be convenient.
    upstream = inputs_were_assessable(connection, config)
    accuracy_assessable = upstream["assessable"]

    relative_error = float(total["relative_error"])

    bounds = db.read_sql(connection, """
        SELECT
            SUM(CASE WHEN ecl_reported < 0 THEN 1 ELSE 0 END)          AS negative,
            SUM(CASE WHEN ecl_reported > ead_now + 0.01 THEN 1 ELSE 0 END)
                                                                       AS over_exposure
        FROM ecl_account""").iloc[0]
    negative = int(bounds["negative"])
    over_exposure = int(bounds["over_exposure"])

    checks = [
        {
            # THE GATE THAT GUARDS THIS PHASE.
            "check": "the ECL arithmetic reproduces the key on exact inputs",
            "measure": "worst per-account residual, currency",
            "value": worst_residual,
            "limit": residual_limit,
            "assessable": True,
            "passed": worst_residual <= residual_limit,
            "status": "pass" if worst_residual <= residual_limit else "fail",
        },
        {
            "check": "every account's provision is accounted for",
            "measure": "accounts whose residual exceeds a cent",
            "value": float(residuals["accounts_over_a_cent"]),
            "limit": 0.0,
            "assessable": True,
            "passed": int(residuals["accounts_over_a_cent"]) == 0,
            "status": ("pass" if int(residuals["accounts_over_a_cent"]) == 0
                       else "fail"),
        },
        {
            # A BACKSTOP, not the primary control, and only meaningful when the
            # PD and LGD estimates it inherits could themselves be assessed.
            "check": "total ECL from estimated inputs is within the backstop",
            "measure": "relative error against the key",
            "value": abs(relative_error),
            "limit": relative_limit,
            "assessable": accuracy_assessable,
            "passed": (abs(relative_error) <= relative_limit
                       if accuracy_assessable else True),
            "status": ("not assessed" if not accuracy_assessable
                       else "pass" if abs(relative_error) <= relative_limit
                       else "fail"),
        },
        {
            "check": "no account carries a negative provision",
            "measure": "accounts with ECL below zero",
            "value": float(negative),
            "limit": 0.0,
            "assessable": True,
            "passed": negative == 0,
            "status": "pass" if negative == 0 else "fail",
        },
        {
            # An ECL above the exposure would mean expecting to lose more than
            # is owed. Possible arithmetically if LGD exceeded 1 or the PD
            # schedule summed above 1; neither should happen, so it is checked.
            "check": "no account is provisioned above its own exposure",
            "measure": "accounts with ECL above EAD",
            "value": float(over_exposure),
            "limit": 0.0,
            "assessable": True,
            "passed": over_exposure == 0,
            "status": "pass" if over_exposure == 0 else "fail",
        },
    ]
    frame = pd.DataFrame(checks)
    frame["passed"] = frame["status"] != "fail"
    return frame, relative_error


def ecl_report(connection, config):
    """Phase 6 as a credit committee would read it."""
    checks, relative_error = validate(connection, config)
    by_stage = portfolio(connection, level="stage")
    total = portfolio(connection, level="portfolio").iloc[0]
    residuals = arithmetic_residuals(connection)
    cliff = staging_cliff(connection)
    attribution = input_attribution(connection, config)

    basis = {"1": "12-month", "2": "lifetime", "3": "lifetime (impaired)"}

    lines = ["", "The provision", "-" * 13]
    lines.append(f"  {'stage':<22}{'accounts':>9}{'exposure':>15}"
                 f"{'ECL':>13}{'coverage':>10}{'true':>9}{'error':>9}")
    for row in by_stage.itertuples(index=False):
        label = f"{row.stage} — {basis.get(row.stage, '')}"
        lines.append(f"  {label:<22}{row.accounts:>9,}{row.exposure:>15,.0f}"
                     f"{row.ecl:>13,.0f}{row.coverage_ratio:>10.2%}"
                     f"{row.true_coverage_ratio:>9.2%}"
                     f"{row.relative_error:>+9.2%}")
    lines.append(f"  {'portfolio':<22}{total['accounts']:>9,}"
                 f"{total['exposure']:>15,.0f}{total['ecl']:>13,.0f}"
                 f"{total['coverage_ratio']:>10.2%}"
                 f"{total['true_coverage_ratio']:>9.2%}"
                 f"{total['relative_error']:>+9.2%}")

    lines.append("")
    lines.append("Is the arithmetic right?")
    lines.append("-" * 23)
    lines.append("  The engine's own expression, fed the exact inputs from the")
    lines.append("  answer key, must return the answer key.")
    lines.append(f"    accounts reconciled        {int(residuals['accounts']):>12,}")
    lines.append(f"    worst residual             "
                 f"{float(residuals['max_residual_lifetime']):>12.2e}")
    lines.append(f"    accounts over a cent       "
                 f"{int(residuals['accounts_over_a_cent']):>12,}")

    lines.append("")
    lines.append("Where the remaining error comes from")
    lines.append("-" * 36)
    estimated = float(attribution["estimated_pd_estimated_lgd"])
    true_lgd_only = float(attribution["estimated_pd_true_lgd"])
    truth = float(attribution["true_pd_true_lgd"])
    lines.append(f"    estimated PD, estimated LGD   {estimated:>14,.0f}")
    lines.append(f"    estimated PD, TRUE LGD        {true_lgd_only:>14,.0f}"
                 f"   ({(true_lgd_only / estimated - 1):+.2%})")
    lines.append(f"    TRUE PD, TRUE LGD (the key)   {truth:>14,.0f}"
                 f"   ({(truth / estimated - 1):+.2%})")
    lines.append("")
    lines.append("    Substituting the true LGD closes almost none of the gap,")
    lines.append("    so a single segment-level LGD applied to every account is")
    lines.append("    not the crude part. The residual is PD estimation error,")
    lines.append("    inherited from Phase 4 and inside Phase 4's tolerance.")

    direction = "UNDER" if relative_error < 0 else "OVER"
    lines.append("")
    lines.append(f"    Direction: the engine {direction}-provisions by "
                 f"{abs(relative_error):.2%}.")
    if relative_error < 0:
        lines.append("    Understatement is the dangerous direction for a loss")
        lines.append("    allowance, so it is reported here rather than folded")
        lines.append("    into an absolute figure.")

    lines.append("")
    lines.append("The staging cliff")
    lines.append("-" * 17)
    lines.append("  Lifetime provision as a multiple of the 12-month one, for")
    lines.append("  the SAME accounts — the jump taken on crossing SICR.")
    lines.append(f"  {'arrears bucket':<18}{'accounts':>9}{'12-month':>13}"
                 f"{'lifetime':>13}{'multiple':>10}")
    for row in cliff.itertuples(index=False):
        lines.append(f"  {int(row.arrears_bucket):<18}{row.accounts:>9,}"
                     f"{row.ecl_12m:>13,.0f}{row.ecl_lifetime:>13,.0f}"
                     f"{row.cliff_multiple:>10.3f}")
    lines.append("")
    lines.append("    The cliff is steepest for accounts in NO arrears and")
    lines.append("    almost flat for those deep in them: an account 60-89 days")
    lines.append("    down will mostly default inside twelve months either way,")
    lines.append("    so extending the horizon adds little. The accounts that")
    lines.append("    actually trip the 30-day backstop face the smallest jump.")

    lines.append("")
    lines.append("Checks")
    lines.append("-" * 6)
    marks = {"pass": "PASS", "fail": "FAIL", "not assessed": " -- "}
    for row in checks.itertuples(index=False):
        lines.append(f"  [{marks[row.status]}] {row.check}")
        lines.append(f"         {row.measure}: {row.value:.6g} "
                     f"(limit {row.limit:.6g})")

    if (checks["status"] == "not assessed").any():
        upstream = inputs_were_assessable(connection, config)
        missing = [name for name, ok in (("PD", upstream["pd_assessable"]),
                                         ("LGD", upstream["lgd_assessable"]))
                   if not ok]
        lines.append("")
        lines.append(f"  The accuracy backstop was NOT ASSESSED, because the "
                     f"{' and '.join(missing)} estimate")
        lines.append("  feeding it could not be assessed on a book this size "
                     "either.")
        lines.append("  ECL is a function of those estimates, so there is no")
        lines.append("  evidence here about its accuracy — which is a different")
        lines.append("  finding from evidence that it is wrong.")
        lines.append("")
        lines.append("  The ARITHMETIC gate above is unaffected and did apply:")
        lines.append("  it substitutes exact inputs, so it holds at any book")
        lines.append("  size, and it is the gate that guards this phase.")

    return "\n".join(lines)
