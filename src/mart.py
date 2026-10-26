"""
Phase 1 — building the data mart.

Thin on purpose. The modelling lives in `sql/`, as SQL, because that is the
artefact this project exists to show: a credit risk analyst reviewing it should
be able to read the mart without reading Python, and Phase 8 ports those same
files to SAS `PROC SQL`.

What is here is orchestration — running the files in dependency order — and the
quality report, which turns the SQL assertions into a pass/fail a human reads.
"""
from pathlib import Path

import pandas as pd

from . import db

REPO_ROOT = Path(__file__).resolve().parents[1]

# Build order matters: the fact joins both dimensions, so they must exist first.
# Listing it here rather than relying on filename sorting means the dependency
# is stated, not implied by a naming convention someone could break.
BUILD_ORDER = [
    ("01_dim_date.sql", "dim_date", "calendar at month-end grain"),
    ("02_dim_account.sql", "dim_account", "one row per loan, fixed at origination"),
    ("03_fact_loan_month.sql", "fact_loan_month",
     "one row per account per month on book"),
]

QUALITY_SQL = "04_mart_quality.sql"


class MartError(Exception):
    """Raised when the mart cannot be built or fails its own quality checks."""


def build(connection, config, verbose=True):
    """Build every mart table, in order, and return the row counts.

    The date dimension is parameterised from the book's own span rather than
    hard-coded: a calendar that stops before the last loan matures would drop
    fact rows at an inner join, and the book would quietly shrink.
    """
    span = connection.execute("""
        SELECT MIN(origination_date) AS first_origination,
               MAX(origination_date) AS last_origination
        FROM accounts
    """).fetchall()[0]

    # Extend the calendar well past the last origination so every account's
    # full contractual life has dates to join to.
    longest_term = connection.execute(
        "SELECT MAX(term_months) FROM accounts").fetchall()[0][0]
    end_date = (pd.Timestamp(span[1])
                + pd.DateOffset(months=int(longest_term) + 1)).date()

    parameters = {
        "01_dim_date.sql": {
            "start_date": str(pd.Timestamp(span[0]).date()),
            "end_date": str(end_date),
            "reporting_date": str(config["portfolio"]["reporting_date"]),
        },
    }

    counts = {}
    for filename, table, purpose in BUILD_ORDER:
        db.run_sql_file(connection, filename, **parameters.get(filename, {}))
        counts[table] = db._count(connection, table)
        if verbose:
            print(f"  built {table:<18} {counts[table]:>10,} rows   {purpose}")

    return counts


def quality_report(connection):
    """Run the SQL quality checks and return them as a frame."""
    checks = db.run_sql_file(connection, QUALITY_SQL)
    checks["passed"] = checks["failures"] == 0
    return checks


def format_quality(checks):
    """The quality checks, as something a person reads."""
    lines = ["Mart quality", "-" * 12]
    for row in checks.itertuples(index=False):
        mark = "PASS" if row.passed else "FAIL"
        detail = "" if row.passed else f"   ({row.failures:,} rows)"
        lines.append(f"  [{mark}] {row.check_name}{detail}")

    failed = checks[~checks["passed"]]
    lines.append("")
    if len(failed) == 0:
        lines.append(f"  all {len(checks)} checks passed")
    else:
        lines.append(f"  {len(failed)} of {len(checks)} checks FAILED — the mart "
                     "is not trustworthy and nothing downstream should run")
    return "\n".join(lines)


def mart_report(connection, config):
    """What the mart contains, in terms a credit person would recognise."""
    reporting_date = config["portfolio"]["reporting_date"]

    summary = db.read_sql(connection, """
        SELECT
            COUNT(*)                                   AS accounts_live,
            SUM(scheduled_balance)                     AS exposure,
            AVG(months_on_book)                        AS avg_months_on_book,
            SUM(CASE WHEN is_delinquent THEN 1 ELSE 0 END) AS delinquent,
            SUM(CASE WHEN is_30_plus   THEN 1 ELSE 0 END)  AS dpd_30_plus,
            SUM(CASE WHEN is_90_plus   THEN 1 ELSE 0 END)  AS dpd_90_plus
        FROM fact_loan_month
        WHERE reporting_date = CAST($reporting_date AS DATE)
    """, reporting_date=str(reporting_date))

    lines = ["", f"The book at {reporting_date}", "-" * 26]
    if summary.empty or summary["accounts_live"].iloc[0] == 0:
        lines.append("  no accounts on book at the reporting date")
        return "\n".join(lines)

    row = summary.iloc[0]
    live = int(row["accounts_live"])
    lines.append(f"  accounts on book        {live:>12,}")
    lines.append(f"  exposure                {row['exposure']:>12,.0f}")
    lines.append(f"  average months on book  {row['avg_months_on_book']:>12.1f}")
    lines.append(f"  delinquent (1+ DPD)     {int(row['delinquent']):>12,}"
                 f"   {row['delinquent'] / live:>6.1%}")
    lines.append(f"  30+ DPD                 {int(row['dpd_30_plus']):>12,}"
                 f"   {row['dpd_30_plus'] / live:>6.1%}")
    lines.append(f"  90+ DPD (default)       {int(row['dpd_90_plus']):>12,}"
                 f"   {row['dpd_90_plus'] / live:>6.1%}")

    by_segment = db.read_sql(connection, """
        SELECT
            a.segment,
            COUNT(*)                                        AS accounts,
            SUM(f.scheduled_balance)                        AS exposure,
            AVG(CASE WHEN f.is_30_plus THEN 1.0 ELSE 0.0 END) AS rate_30_plus
        FROM fact_loan_month f
        JOIN dim_account a ON a.account_id = f.account_id
        WHERE f.reporting_date = CAST($reporting_date AS DATE)
        GROUP BY a.segment
        ORDER BY rate_30_plus
    """, reporting_date=str(reporting_date))

    lines.append("")
    lines.append("  by segment")
    for row in by_segment.itertuples(index=False):
        lines.append(f"    {row.segment:<12} {int(row.accounts):>7,} accounts   "
                     f"exposure {row.exposure:>12,.0f}   "
                     f"30+ DPD {row.rate_30_plus:>6.1%}")

    return "\n".join(lines)
