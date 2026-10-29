"""
Phase 3 — IFRS 9 staging.

The accounting judgement of the project. Stage 1 carries a twelve-month
expected loss; Stages 2 and 3 carry a lifetime one. On a 48-month loan that
substitution can multiply the provision several times over for an account whose
circumstances have barely changed — **staging moves the provision more than the
PD model does**, which is why every rule is config and every trigger is recorded
by name.

What is in SQL: the PD curve (a recursive CTE doing matrix exponentiation), the
stage assignment with its triggers and probation rule, and the migration matrix.

What is here: orchestration, the reporting, and the analysis of which triggers
actually bind — which turned out to be the most interesting thing in the phase.
"""
from pathlib import Path

import numpy as np
import pandas as pd

from . import db, generate

REPO_ROOT = Path(__file__).resolve().parents[1]

STAGE_NAMES = {
    1: "Stage 1 — performing (12-month ECL)",
    2: "Stage 2 — significant increase (lifetime ECL)",
    3: "Stage 3 — credit-impaired (lifetime ECL)",
}


class StagingError(Exception):
    """Raised when stages cannot be assigned or do not partition the book."""


def build(connection, config, verbose=True):
    """Build the PD curve, the stage assignment and the migration matrix."""
    staging = config["staging"]

    # The curve must reach the longest contractual term, or an account's
    # lifetime PD would join to nothing and silently read as zero — which would
    # stage a long-dated account as performing no matter how bad it got.
    longest = db.read_sql(
        connection, "SELECT MAX(term_months) AS longest FROM dim_account"
    )["longest"].iloc[0]

    steps = [
        ("08_pd_curve.sql", "pd_curve",
         {"max_horizon": int(longest)},
         "cumulative PD by starting bucket and horizon"),
        ("09_staging.sql", "account_stage",
         {"default_dpd": staging["default_dpd"],
          "sicr_dpd": staging["sicr_dpd_backstop"],
          "relative_increase": staging["sicr_relative_pd_increase"],
          "absolute_floor": staging["sicr_absolute_pd_floor"],
          "probation_months": staging["probation_months"]},
         "IFRS 9 stage for every account-month"),
        ("10_stage_migration.sql", "stage_migration", {},
         "month-on-month movement between stages"),
    ]

    counts = {}
    for filename, table, parameters, purpose in steps:
        db.run_sql_file(connection, filename, **parameters)
        counts[table] = db._count(connection, table)
        if verbose:
            print(f"  built {table:<22} {counts[table]:>10,} rows   {purpose}")

    return counts


def stage_summary(connection, config):
    """Accounts, exposure and average PD by stage, at the reporting date."""
    return db.read_sql(connection, """
        SELECT
            stage,
            COUNT(*)                        AS accounts,
            SUM(scheduled_balance)          AS exposure,
            AVG(lifetime_pd_now)            AS average_lifetime_pd,
            AVG(months_remaining)           AS average_months_remaining
        FROM account_stage
        WHERE reporting_date = CAST($reporting_date AS DATE)
        GROUP BY stage
        ORDER BY stage
    """, reporting_date=str(config["portfolio"]["reporting_date"]))


def trigger_analysis(connection):
    """Which SICR triggers actually bind, and which are dominated.

    A trigger that never fires on its own is not a control, it is a line in a
    policy document. This counts, for each trigger, the account-months where it
    fired AND no stronger trigger already had — which is the only measure of
    what a trigger contributes.
    """
    return db.read_sql(connection, """
        SELECT
            arrears_bucket,
            COUNT(*)                                                AS account_months,
            SUM(CASE WHEN trigger_dpd_backstop THEN 1 ELSE 0 END)   AS backstop,
            SUM(CASE WHEN trigger_relative_pd  THEN 1 ELSE 0 END)   AS relative_pd,
            SUM(CASE WHEN trigger_relative_pd
                      AND NOT trigger_dpd_backstop
                      AND NOT is_credit_impaired
                     THEN 1 ELSE 0 END)                             AS relative_pd_only
        FROM account_stage
        GROUP BY arrears_bucket
        ORDER BY arrears_bucket
    """)


def pd_trigger_by_term(connection):
    """Where the relative PD test has bite, by original term.

    A relative test needs a small denominator to be tripped. Lifetime PD at
    origination grows with the term, so the same proportional deterioration
    clears a doubling on a short loan and cannot get near it on a long one.
    """
    return db.read_sql(connection, """
        SELECT
            a.term_months,
            COUNT(*)                            AS pd_only_triggers,
            AVG(s.lifetime_pd_at_origination)   AS pd_at_origination,
            AVG(s.lifetime_pd_now)              AS pd_now
        FROM account_stage s
        JOIN dim_account a ON a.account_id = s.account_id
        WHERE s.trigger_relative_pd
          AND NOT s.trigger_dpd_backstop
          AND NOT s.is_credit_impaired
        GROUP BY a.term_months
        ORDER BY a.term_months
    """)


def migration_matrix(connection, measure="account_rate"):
    """Stage migration as a square frame."""
    frame = db.read_sql(connection, f"""
        SELECT from_stage, to_stage, {measure} AS rate FROM stage_migration
    """)
    stages = [1, 2, 3]
    pivot = (frame.pivot(index="from_stage", columns="to_stage", values="rate")
             .reindex(index=stages, columns=stages).fillna(0.0))
    pivot.index = [f"from Stage {s}" for s in stages]
    pivot.columns = [f"to Stage {s}" for s in stages]
    return pivot


def validate(connection, config):
    """Checks that must hold for the staging to be usable at all."""
    reporting_date = str(config["portfolio"]["reporting_date"])
    results = []

    partition = db.read_sql(connection, """
        SELECT
            (SELECT COUNT(*) FROM account_stage
              WHERE reporting_date = CAST($d AS DATE))                AS staged,
            (SELECT COUNT(*) FROM fact_loan_month
              WHERE reporting_date = CAST($d AS DATE))                AS on_book,
            (SELECT COUNT(*) FROM account_stage
              WHERE stage NOT IN (1, 2, 3))                           AS invalid_stage
    """, d=reporting_date).iloc[0]

    results.append({
        "check": "every account on book has exactly one stage",
        "failures": int(partition["on_book"] - partition["staged"]
                        + partition["invalid_stage"]),
    })

    impaired = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM account_stage
        WHERE days_past_due >= $dpd AND stage <> 3
    """, dpd=int(config["staging"]["default_dpd"]))["n"].iloc[0]
    results.append({"check": "every 90+ account is Stage 3",
                    "failures": int(impaired)})

    backstop = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM account_stage
        WHERE days_past_due >= $dpd AND stage = 1
    """, dpd=int(config["staging"]["sicr_dpd_backstop"]))["n"].iloc[0]
    results.append({"check": "no 30+ account sits in Stage 1",
                    "failures": int(backstop)})

    # Probation: an account cannot return to Stage 1 in the month after a
    # trigger, because the rolling window still sees it.
    premature = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT stage,
                   LAG(triggered_now) OVER (
                       PARTITION BY account_id ORDER BY months_on_book
                   ) AS triggered_last_month
            FROM account_stage
        )
        WHERE triggered_last_month AND stage = 1
    """)["n"].iloc[0]
    results.append({"check": "probation holds cured accounts out of Stage 1",
                    "failures": int(premature)})

    frame = pd.DataFrame(results)
    frame["passed"] = frame["failures"] == 0
    return frame


def staging_report(connection, config):
    """The phase's findings, in the order a reviewer would want them."""
    lines = []

    summary = stage_summary(connection, config)
    total_accounts = summary["accounts"].sum()
    total_exposure = summary["exposure"].sum()

    lines.append("IFRS 9 staging at the reporting date")
    lines.append("-" * 36)
    lines.append(f"  {'stage':<46}{'accounts':>10}{'exposure':>16}{'share':>8}")
    for row in summary.itertuples(index=False):
        lines.append(f"  {STAGE_NAMES[int(row.stage)]:<46}{int(row.accounts):>10,}"
                     f"{row.exposure:>16,.0f}"
                     f"{row.exposure / total_exposure:>8.1%}")
    lines.append(f"  {'':<46}{int(total_accounts):>10,}{total_exposure:>16,.0f}")

    checks = validate(connection, config)
    lines.append("")
    lines.append("Staging checks")
    lines.append("-" * 14)
    for row in checks.itertuples(index=False):
        mark = "PASS" if row.passed else "FAIL"
        detail = "" if row.passed else f"   ({row.failures:,})"
        lines.append(f"  [{mark}] {row.check}{detail}")

    # --- which triggers actually bind ---------------------------------------
    triggers = trigger_analysis(connection)
    pd_only = int(triggers["relative_pd_only"].sum())
    backstop_total = int(triggers["backstop"].sum())

    lines.append("")
    lines.append("Which SICR trigger does the work?")
    lines.append("-" * 33)
    lines.append(f"  30-day backstop fired on          {backstop_total:>10,} account-months")
    lines.append(f"  relative PD test fired ALONE on   {pd_only:>10,} account-months")
    lines.append("")

    if pd_only == 0:
        lines.append("  The relative PD test never binds: it is entirely dominated by")
        lines.append("  the backstop. With arrears as the only risk driver, PD is a")
        lines.append("  monotone function of the bucket, so the two tests are collinear.")
    else:
        by_term = pd_trigger_by_term(connection)
        lines.append("  It binds only on short-dated loans:")
        lines.append(f"    {'term':<10}{'triggers':>12}{'PD at origination':>20}{'PD now':>10}")
        for row in by_term.itertuples(index=False):
            lines.append(f"    {int(row.term_months):>3} months{int(row.pd_only_triggers):>12,}"
                         f"{row.pd_at_origination:>20.2%}{row.pd_now:>10.2%}")
        lines.append("")
        lines.append("  A relative test needs a small denominator. Lifetime PD at")
        lines.append("  origination grows with the term, so the same proportional")
        lines.append("  deterioration clears a doubling on a short loan and cannot")
        lines.append("  come close on a long one.")

    migration = migration_matrix(connection)
    lines.append("")
    lines.append("Stage migration, month on month (account-weighted)")
    lines.append("-" * 49)
    header = "  " + " " * 16 + "".join(f"{c:>14}" for c in migration.columns)
    lines.append(header)
    for name, row in migration.iterrows():
        cells = "".join(f"{value:>13.2%} " for value in row)
        lines.append(f"  {name:<16}{cells}")

    return "\n".join(lines)
