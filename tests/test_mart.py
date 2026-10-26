"""
Phase 1 tests — the data mart.

The headline is `test_every_quality_check_can_actually_fail`. A suite of checks
that all pass tells you nothing unless you know they are capable of failing: a
check with a typo in its WHERE clause passes on every book ever written, and is
indistinguishable from a check that works. So each check is run against a mart
that has been deliberately broken in the way that check exists to catch.

The rest assert the grain, the referential integrity and the arithmetic the
mart promises.

Run with:  pytest tests/test_mart.py -v
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed
from src import db, generate, mart


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def built(config, tmp_path_factory):
    """A small book, loaded and marted once for the whole module."""
    path = tmp_path_factory.mktemp("mart") / "test.duckdb"

    small = dict(config)
    small["portfolio"] = dict(config["portfolio"], n_accounts=600)

    rng = set_seed(4242)
    accounts, performance, truth = generate.generate_book(small, rng)

    connection = db.connect(small, path)
    db.write_table(connection, "accounts", accounts)
    db.write_table(connection, "loan_month", performance)
    db.write_table(connection, "ground_truth", truth)
    mart.build(connection, small, verbose=False)

    yield connection, small
    connection.close()


# --- The grain --------------------------------------------------------------

def test_the_fact_grain_is_one_row_per_account_per_month(built):
    """The single most important property of a fact table.

    A duplicated grain does not raise. It silently doubles every sum built on
    the table, and the first symptom is a provision that is twice what it
    should be, months later.
    """
    connection, _ = built
    duplicates = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT account_id, date_key
            FROM fact_loan_month
            GROUP BY account_id, date_key
            HAVING COUNT(*) > 1
        )
    """)["n"].iloc[0]
    assert duplicates == 0

    # And the surrogate key must be unique too, since later joins use it.
    keys = db.read_sql(connection, """
        SELECT COUNT(*) AS total, COUNT(DISTINCT loan_month_key) AS distinct_keys
        FROM fact_loan_month
    """).iloc[0]
    assert keys["total"] == keys["distinct_keys"]


def test_the_fact_has_a_row_for_every_source_row(built):
    """An inner join that drops rows shrinks the book without saying so."""
    connection, _ = built
    counts = db.read_sql(connection, """
        SELECT (SELECT COUNT(*) FROM loan_month)      AS source,
               (SELECT COUNT(*) FROM fact_loan_month) AS fact
    """).iloc[0]
    assert counts["fact"] == counts["source"], (
        f"{counts['source'] - counts['fact']} source rows did not survive the "
        "joins to the dimensions"
    )


def test_months_on_book_recomputed_from_dimensions_matches_the_source(built):
    """The fact derives months on book from the calendar, not from the source.

    If the generator and dim_date ever disagreed about what month a date falls
    in, this is where it would surface — which is the reason to recompute it
    rather than carry it through.
    """
    connection, _ = built
    mismatches = db.read_sql(connection, """
        SELECT COUNT(*) AS n
        FROM fact_loan_month f
        JOIN loan_month lm
          ON lm.account_id = f.account_id
         AND CAST(last_day(lm.reporting_date) AS DATE) = f.reporting_date
        WHERE f.months_on_book <> lm.months_on_book
    """)["n"].iloc[0]
    assert mismatches == 0


# --- The calendar -----------------------------------------------------------

def test_dim_date_is_month_ends_with_no_drift(built):
    """Repeatedly adding a month to 31 January lands on the 28th and stays.

    The drift is silent and would misalign every join. Built from month starts
    and last_day() instead, so this asserts the result rather than the method.
    """
    connection, _ = built
    dates = db.read_sql(connection,
                        "SELECT month_end_date FROM dim_date ORDER BY date_key")
    series = pd.to_datetime(dates["month_end_date"])

    assert (series == series + pd.offsets.MonthEnd(0)).all(), (
        "dim_date contains a date that is not a month end"
    )
    # Consecutive and complete: no month skipped, none repeated.
    months = series.dt.year * 12 + series.dt.month
    assert (months.diff().dropna() == 1).all()


def test_the_calendar_covers_every_fact_date(built):
    connection, _ = built
    uncovered = db.read_sql(connection, """
        SELECT COUNT(*) AS n
        FROM fact_loan_month f
        LEFT JOIN dim_date d ON d.date_key = f.date_key
        WHERE d.date_key IS NULL
    """)["n"].iloc[0]
    assert uncovered == 0


def test_exactly_one_reporting_date_is_flagged(built):
    connection, config = built
    flagged = db.read_sql(connection, """
        SELECT month_end_date FROM dim_date WHERE is_reporting_date
    """)
    assert len(flagged) == 1
    assert str(pd.Timestamp(flagged["month_end_date"].iloc[0]).date()) == \
        str(config["portfolio"]["reporting_date"])


# --- The account dimension --------------------------------------------------

def test_dim_account_is_one_row_per_account(built):
    connection, _ = built
    counts = db.read_sql(connection, """
        SELECT COUNT(*) AS total, COUNT(DISTINCT account_id) AS distinct_ids
        FROM dim_account
    """).iloc[0]
    assert counts["total"] == counts["distinct_ids"]


def test_every_account_falls_in_exactly_one_band(built):
    """Bands are CASE expressions, and a gap between two branches sends a row
    to NULL — where it vanishes from every grouped report."""
    connection, _ = built
    nulls = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM dim_account
        WHERE principal_band IS NULL
           OR term_band IS NULL
           OR rate_band IS NULL
           OR vintage_month IS NULL
    """)["n"].iloc[0]
    assert nulls == 0


def test_maturity_is_the_term_after_origination(built):
    connection, _ = built
    wrong = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM dim_account
        WHERE (year(maturity_date) * 12 + month(maturity_date))
            - (year(origination_date) * 12 + month(origination_date))
            <> term_months
    """)["n"].iloc[0]
    assert wrong == 0


# --- Additivity -------------------------------------------------------------

def test_balance_is_semi_additive(built):
    """Summing a balance across months produces a number with no meaning.

    An account owing 10,000 for twelve months did not owe 120,000. The test
    cannot stop someone writing that sum, but it documents the property and
    pins the relationship: the across-months total is necessarily far larger
    than the point-in-time one, which is exactly why it misleads.
    """
    connection, config = built
    totals = db.read_sql(connection, """
        SELECT
            (SELECT SUM(scheduled_balance) FROM fact_loan_month
              WHERE reporting_date = CAST($reporting_date AS DATE)) AS at_a_point_in_time,
            (SELECT SUM(scheduled_balance) FROM fact_loan_month)    AS across_all_months
    """, reporting_date=str(config["portfolio"]["reporting_date"])).iloc[0]

    assert totals["across_all_months"] > totals["at_a_point_in_time"] * 5, (
        "the across-months sum should be very much larger — if it is not, the "
        "fact table may not hold the history it is supposed to"
    )


def test_is_new_default_fires_exactly_once_per_defaulted_account(built):
    """`is_default` counts one default every month it persists; `is_new_default`
    counts it once. Only the second is additive across time."""
    connection, _ = built
    offenders = db.read_sql(connection, """
        SELECT COUNT(*) AS n FROM (
            SELECT account_id,
                   SUM(CASE WHEN is_new_default THEN 1 ELSE 0 END) AS new_defaults,
                   MAX(CASE WHEN is_default THEN 1 ELSE 0 END)     AS ever_default
            FROM fact_loan_month
            GROUP BY account_id
            HAVING new_defaults <> ever_default
        )
    """)["n"].iloc[0]
    assert offenders == 0


# --- THE test this module exists for ----------------------------------------

def test_every_quality_check_can_actually_fail(config, tmp_path):
    """Break the mart on purpose, once per check, and confirm the check notices.

    A suite in which everything passes is worth nothing unless the checks are
    capable of failing. A check with a typo in its predicate passes on every
    book ever written and is indistinguishable from one that works — until the
    day it was supposed to catch something.

    Each corruption below is the realistic version of the bug: a duplicated
    join, a dropped dimension row, a missing month, a mangled balance.
    """
    small = dict(config)
    small["portfolio"] = dict(config["portfolio"], n_accounts=300)

    def fresh_mart(name):
        path = tmp_path / f"{name}.duckdb"
        rng = set_seed(5)
        accounts, performance, truth = generate.generate_book(small, rng)
        connection = db.connect(small, path)
        db.write_table(connection, "accounts", accounts)
        db.write_table(connection, "loan_month", performance)
        db.write_table(connection, "ground_truth", truth)
        mart.build(connection, small, verbose=False)
        return connection

    def failures_for(connection, check_name):
        checks = mart.quality_report(connection)
        row = checks[checks["check_name"] == check_name]
        assert len(row) == 1, f"no check named '{check_name}'"
        return int(row["failures"].iloc[0])

    corruptions = [
        # (check it should trip, SQL that breaks the mart that way)
        ("grain is unique",
         """INSERT INTO fact_loan_month
            SELECT * FROM fact_loan_month LIMIT 5"""),

        ("no orphaned accounts",
         """DELETE FROM dim_account
            WHERE account_id = (SELECT MIN(account_id) FROM dim_account)"""),

        ("no orphaned dates",
         """DELETE FROM dim_date
            WHERE date_key = (SELECT MIN(date_key) FROM fact_loan_month)"""),

        ("no gaps in monthly series",
         """DELETE FROM fact_loan_month
            WHERE loan_month_key = (
                SELECT loan_month_key FROM fact_loan_month
                WHERE months_on_book = 1 LIMIT 1)"""),

        ("balance never increases",
         """UPDATE fact_loan_month SET scheduled_balance = scheduled_balance * 10
            WHERE loan_month_key = (
                SELECT loan_month_key FROM fact_loan_month
                WHERE months_on_book = 2 LIMIT 1)"""),

        ("DPD agrees with bucket",
         """UPDATE fact_loan_month SET days_past_due = 45
            WHERE arrears_bucket = 0 AND loan_month_key = (
                SELECT loan_month_key FROM fact_loan_month
                WHERE arrears_bucket = 0 LIMIT 1)"""),

        ("rows within account life",
         """UPDATE fact_loan_month SET months_on_book = -3
            WHERE loan_month_key = (SELECT MIN(loan_month_key) FROM fact_loan_month)"""),

        ("default is absorbing",
         """UPDATE fact_loan_month SET is_default = TRUE, days_past_due = 100,
                                       arrears_bucket = 4
            WHERE loan_month_key = (
                SELECT loan_month_key FROM fact_loan_month
                WHERE months_on_book = 0 LIMIT 1)"""),
    ]

    for check_name, corruption in corruptions:
        connection = fresh_mart(check_name.replace(" ", "_"))

        assert failures_for(connection, check_name) == 0, (
            f"'{check_name}' failed on a clean mart — the check itself is wrong"
        )

        connection.execute(corruption).fetchall()

        assert failures_for(connection, check_name) > 0, (
            f"'{check_name}' did NOT notice a mart broken in exactly the way "
            f"it exists to catch. The check is decorative:\n  {corruption}"
        )
        connection.close()


def test_a_clean_mart_passes_every_check(built):
    connection, _ = built
    checks = mart.quality_report(connection)
    failed = checks[~checks["passed"]]
    assert len(failed) == 0, f"failing checks: {failed['check_name'].tolist()}"
    assert len(checks) == 8, "a check was added or removed without updating this test"
