"""
Phase 8 tests: the SAS port, checked from a machine with no SAS on it.

This file exists because of an awkward fact. The SAS engine cannot be executed
here, so the usual guarantee that the tests cover the code does not hold for
`sas/*.sas`. Pretending otherwise would be worse than admitting it, so the
coverage is split into the two things that CAN be checked without SAS:

1. THE ALGORITHMS. SAS PROC SQL has no window functions and no recursive CTEs,
   so four constructs had to be reimplemented: LEAD, LAG, a bounded window
   frame, and a recursive walk. Each replacement is written here as plain SQL
   that DuckDB can run, and asserted to reproduce the reference exactly. If the
   iterative walk and the recursive CTE disagree, that is a logic error and it
   is caught here rather than in SAS.

2. THE SYNTAX, by exclusion. The SAS files are linted for constructs SAS does
   not support. This cannot prove the code runs, but it catches the specific
   failure that was most likely while writing it: reaching for a DuckDB
   construct out of habit.

What is left untested is genuine SAS semantics, and the honest statement is
that the first run on real SAS is the test for those. The programs print row
counts after every step so that run localises a failure immediately.

Run with:  pytest tests/test_sasport.py -v
"""
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config
from src import db, reconcile

SAS_DIR = REPO_ROOT / "sas"


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def warehouse(config, tmp_path_factory):
    """A built warehouse to check the SAS-shaped algorithms against."""
    from src.config import set_seed
    from src import generate, mart, rollrate, staging
    from src import pd as pdmod

    path = tmp_path_factory.mktemp("sasport") / "test.duckdb"
    sized = dict(config)
    sized["portfolio"] = dict(config["portfolio"], n_accounts=3000)

    rng = set_seed(8080)
    accounts, performance, truth, recoveries = generate.generate_book(sized, rng)
    connection = db.connect(sized, path)
    for name, frame in (("accounts", accounts), ("loan_month", performance),
                        ("ground_truth", truth), ("recovery", recoveries)):
        db.write_table(connection, name, frame, allow_empty=(name == "recovery"))
    mart.build(connection, sized, verbose=False)
    rollrate.build(connection, verbose=False)
    staging.build(connection, sized, verbose=False)
    pdmod.build(connection, sized, verbose=False)

    yield connection, sized
    connection.close()


# --- 1. The algorithms SAS needs, run where they can be run ----------------

def test_lag_as_a_self_join_matches_the_window_function(warehouse):
    """`is_new_default` without LAG().

    SAS has a LAG() function, but it is a DATA step queue: it returns the
    previous ROW PROCESSED, not the previous row of the same account, so it
    would carry the last month of one account into the first month of the
    next. The port joins to month t-1 of the same account instead, which has
    no such failure mode.
    """
    connection, _ = warehouse
    mismatches = db.read_sql(connection, """
        SELECT COUNT(*) AS n
        FROM fact_loan_month f
        LEFT JOIN fact_loan_month p
               ON p.account_id = f.account_id
              AND p.months_on_book = f.months_on_book - 1
        WHERE f.is_new_default <> (f.is_default AND NOT COALESCE(p.is_default, FALSE))
    """)["n"].iloc[0]
    assert mismatches == 0


def test_lead_as_a_self_join_matches_the_window_function(warehouse):
    """Bucket transitions without LEAD().

    An INNER join to month t+1 does the censoring as part of the join rather
    than as a filter afterwards, so the Phase 2 rule that a transition exists
    only where the next month is observed is structural rather than remembered.
    """
    connection, _ = warehouse
    counts = db.read_sql(connection, """
        SELECT
            (SELECT COUNT(*) FROM bucket_transition)              AS reference,
            (SELECT COUNT(*) FROM fact_loan_month f
             JOIN fact_loan_month n
               ON n.account_id = f.account_id
              AND n.months_on_book = f.months_on_book + 1)        AS self_join
    """).iloc[0]
    assert counts["reference"] == counts["self_join"]

    disagreements = db.read_sql(connection, """
        SELECT COUNT(*) AS n
        FROM bucket_transition t
        JOIN fact_loan_month f ON f.account_id = t.account_id
                              AND f.months_on_book = t.months_on_book
        JOIN fact_loan_month n ON n.account_id = t.account_id
                              AND n.months_on_book = t.months_on_book + 1
        WHERE t.to_bucket <> n.arrears_bucket
           OR t.from_bucket <> f.arrears_bucket
    """)["n"].iloc[0]
    assert disagreements == 0


def test_the_probation_frame_as_a_range_self_join(warehouse):
    """The rolling MAX over `ROWS BETWEEN n PRECEDING AND CURRENT ROW`.

    Joined on MONTHS_ON_BOOK rather than on row order. "Three rows back" and
    "three months back" coincide only because the grain is dense, and the mart
    quality checks are what keep it dense. A version that sorted and counted
    rows would be right today and silently wrong the first time an account had
    a missing month.
    """
    connection, sized = warehouse
    months = int(sized["staging"]["probation_months"])
    mismatches = db.read_sql(connection, f"""
        WITH by_range AS (
            SELECT a.account_id, a.months_on_book,
                   MAX(CASE WHEN w.triggered_now THEN 1 ELSE 0 END) = 1
                       AS in_or_recently_in_sicr
            FROM account_stage a
            JOIN account_stage w
              ON w.account_id = a.account_id
             AND w.months_on_book BETWEEN a.months_on_book - {months}
                                      AND a.months_on_book
            GROUP BY a.account_id, a.months_on_book)
        SELECT COUNT(*) AS n
        FROM by_range r
        JOIN account_stage s ON s.account_id = r.account_id
                            AND s.months_on_book = r.months_on_book
        WHERE r.in_or_recently_in_sicr <> s.in_or_recently_in_sicr
    """)["n"].iloc[0]
    assert mismatches == 0


def test_the_iterative_walk_matches_the_recursive_cte(warehouse):
    """THE HEADLINE. A %macro loop in place of WITH RECURSIVE.

    Iteration and recursion compute the same sequence here because the
    recursion is linear: every step depends only on the step before it. That
    is a property of this particular walk, not a general fact, so it is
    asserted rather than assumed.

    Checked on the probability mass as well as on the answer. A walk that
    produced the right default column while leaking mass elsewhere would be
    wrong in a way the PD alone could hide.
    """
    connection, _ = warehouse
    horizon = int(db.read_sql(
        connection, "SELECT MAX(horizon) h FROM pd_curve")["h"].iloc[0])

    connection.execute("""
        CREATE OR REPLACE TEMP TABLE m AS
        SELECT from_bucket,
               COALESCE(MAX(CASE WHEN to_bucket=0 THEN rate END),0)::DOUBLE p0,
               COALESCE(MAX(CASE WHEN to_bucket=1 THEN rate END),0)::DOUBLE p1,
               COALESCE(MAX(CASE WHEN to_bucket=2 THEN rate END),0)::DOUBLE p2,
               COALESCE(MAX(CASE WHEN to_bucket=3 THEN rate END),0)::DOUBLE p3,
               COALESCE(MAX(CASE WHEN to_bucket=4 THEN rate END),0)::DOUBLE p4
        FROM roll_rate GROUP BY from_bucket
        UNION ALL SELECT 4, 0.0::DOUBLE,0.0::DOUBLE,0.0::DOUBLE,0.0::DOUBLE,1.0::DOUBLE""")
    connection.execute("""
        CREATE OR REPLACE TEMP TABLE w AS
        SELECT from_bucket AS start_bucket, 0 AS horizon,
               (CASE WHEN from_bucket=0 THEN 1 ELSE 0 END)::DOUBLE p0,
               (CASE WHEN from_bucket=1 THEN 1 ELSE 0 END)::DOUBLE p1,
               (CASE WHEN from_bucket=2 THEN 1 ELSE 0 END)::DOUBLE p2,
               (CASE WHEN from_bucket=3 THEN 1 ELSE 0 END)::DOUBLE p3,
               (CASE WHEN from_bucket=4 THEN 1 ELSE 0 END)::DOUBLE p4
        FROM (SELECT DISTINCT from_bucket FROM m)""")
    for step in range(1, horizon + 1):
        connection.execute(f"""
            INSERT INTO w SELECT w.start_bucket, {step},
              w.p0*a.p0 + w.p1*b.p0 + w.p2*c.p0 + w.p3*d.p0 + w.p4*e.p0,
              w.p0*a.p1 + w.p1*b.p1 + w.p2*c.p1 + w.p3*d.p1 + w.p4*e.p1,
              w.p0*a.p2 + w.p1*b.p2 + w.p2*c.p2 + w.p3*d.p2 + w.p4*e.p2,
              w.p0*a.p3 + w.p1*b.p3 + w.p2*c.p3 + w.p3*d.p3 + w.p4*e.p3,
              w.p0*a.p4 + w.p1*b.p4 + w.p2*c.p4 + w.p3*d.p4 + w.p4*e.p4
            FROM w
            JOIN m a ON a.from_bucket=0 JOIN m b ON b.from_bucket=1
            JOIN m c ON c.from_bucket=2 JOIN m d ON d.from_bucket=3
            JOIN m e ON e.from_bucket=4
            WHERE w.horizon = {step} - 1""")

    outcome = db.read_sql(connection, """
        SELECT COUNT(*) AS compared,
               MAX(ABS(w.p4 - c.cumulative_pd)) AS worst_gap,
               MAX(ABS(w.p0+w.p1+w.p2+w.p3+w.p4 - 1.0)) AS worst_mass
        FROM w JOIN pd_curve c
          ON c.start_bucket = w.start_bucket AND c.horizon = w.horizon""").iloc[0]

    assert outcome["compared"] > 0
    assert outcome["worst_gap"] < 1e-12
    assert outcome["worst_mass"] < 1e-12


# --- 2. The SAS files, linted for what SAS cannot run ----------------------

# Constructs this project uses freely in DuckDB and that PROC SQL does not
# have. Reaching for one out of habit is the most likely way the port breaks,
# and it is the one failure a machine without SAS can still catch.
UNSUPPORTED = {
    r"\bOVER\s*\(": "window functions do not exist in PROC SQL",
    r"\bWITH\s+RECURSIVE\b": "no recursive CTEs; use a %macro loop",
    r"^\s*WITH\s+\w+\s+AS\s*\(": "PROC SQL has no common table expressions",
    r"\bFILTER\s*\(\s*WHERE": "no FILTER clause; use CASE WHEN inside the aggregate",
    r"::": "no cast operator; SAS numerics are all 8-byte floats",
    r"\bgenerate_series\b": "no generate_series; build a numbers table in a DATA step",
    r"\bunnest\s*\(": "no unnest",
    r"\bQUALIFY\b": "no QUALIFY",
    r"\bNULLIF\s*\(": "no NULLIF; use CASE WHEN x = 0 THEN . ELSE x END",
    r"\bGREATEST\s*\(": "SAS spells this MAX(a, b)",
    r"\bLEAST\s*\(": "SAS spells this MIN(a, b)",
    r"\bSTDDEV_SAMP\b": "SAS spells this STD()",
    r"\bILIKE\b": "no ILIKE",
    r"\bEPOCH\b": "no EPOCH",
    r"\bTRUE\b|\bFALSE\b": "SAS has no boolean literals; use 1 and 0",
}


def sas_files():
    return sorted(p for p in SAS_DIR.glob("*.sas"))


def test_there_are_sas_programs_to_lint():
    """Guards every test below from passing because the folder is empty."""
    assert len(sas_files()) >= 7


@pytest.mark.parametrize("path", sas_files(), ids=lambda p: p.name)
def test_no_sas_program_uses_a_construct_sas_lacks(path):
    """The lint. Comments are stripped first, since the files explain at
    length which constructs SAS lacks and would otherwise fail on their own
    documentation. That is the same mistake as a check matching a threshold's
    NAME instead of its value, which this project has already made once.
    """
    text = path.read_text(encoding="utf-8")
    # Strip /* ... */ comments, which is the only comment form used here.
    code = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)

    found = []
    for pattern, why in UNSUPPORTED.items():
        for match in re.finditer(pattern, code, flags=re.I | re.M):
            line = code[:match.start()].count("\n") + 1
            found.append(f"{path.name}: {match.group(0)!r} ({why})")
    assert not found, "constructs SAS cannot run:\n  " + "\n  ".join(found)


def test_the_lint_would_catch_a_real_violation(tmp_path):
    """A lint that has never fired is indistinguishable from a broken one.

    Same argument as `test_every_quality_check_can_actually_fail` in Phase 1:
    the only way to tell a working check from a typo in its predicate is to
    feed it something it should reject.
    """
    offender = tmp_path / "bad.sas"
    offender.write_text(
        "proc sql;\n"
        "  create table x as\n"
        "  select a, LAG(b) OVER (partition by a) as prev,\n"
        "         CAST(c AS DOUBLE) as d\n"
        "  from y;\n"
        "quit;\n", encoding="utf-8")

    code = re.sub(r"/\*.*?\*/", " ", offender.read_text(encoding="utf-8"), flags=re.S)
    hits = [p for p in UNSUPPORTED if re.search(p, code, flags=re.I | re.M)]
    assert r"\bOVER\s*\(" in hits


def test_every_threshold_the_sas_uses_is_generated_not_typed():
    """Accounting policy must live in one place.

    The SAS programs reference thresholds as macro variables, and those come
    from `00_config.sas`, which `scripts/export_for_sas.py` writes from
    `config.yaml`. If a program hard-coded 30 or 90 instead, the two engines
    could reconcile perfectly while both being wrong, or drift apart the first
    time a policy changed.
    """
    generated = (SAS_DIR / "00_config.sas").read_text(encoding="utf-8")
    declared = set(re.findall(r"%let\s+(\w+)\s*=", generated))
    assert {"default_dpd", "sicr_dpd_backstop", "probation_months",
            "min_transitions_per_cell", "horizon_stage_1",
            "reporting_date"} <= declared

    for path in sas_files():
        if path.name == "00_config.sas":
            continue
        code = re.sub(r"/\*.*?\*/", " ", path.read_text(encoding="utf-8"), flags=re.S)
        for literal, macro in ((r"days_past_due\s*>=\s*90", "default_dpd"),
                               (r"days_past_due\s*>=\s*30", "sicr_dpd_backstop")):
            assert not re.search(literal, code), (
                f"{path.name} hard-codes a threshold that belongs in "
                f"&{macro}, generated from config.yaml")


def test_the_config_include_is_regenerated_from_config_yaml(config):
    """The generated file must agree with config.yaml right now.

    It is committed so the SAS side can be read without running anything,
    which makes it exactly the kind of file that goes stale. Checked rather
    than trusted.
    """
    generated = (SAS_DIR / "00_config.sas").read_text(encoding="utf-8")
    values = dict(re.findall(r"%let\s+(\w+)\s*=\s*([^;]+);", generated))

    assert int(values["default_dpd"]) == config["staging"]["default_dpd"]
    assert int(values["sicr_dpd_backstop"]) == config["staging"]["sicr_dpd_backstop"]
    assert int(values["probation_months"]) == config["staging"]["probation_months"]
    assert int(values["horizon_stage_1"]) == config["ecl"]["horizon_months_stage_1"]
    assert (int(values["min_transitions_per_cell"])
            == config["pd"]["min_transitions_per_cell"])


# --- 3. The reconciliation harness itself ----------------------------------

def test_the_harness_detects_a_planted_discrepancy(warehouse):
    """The harness is the thing that will say "the port is correct".

    So it has to be shown capable of saying the opposite. A reconciliation
    that cannot fail is a reconciliation that proves nothing, and on this
    project that lesson has been learned in four other places.
    """
    connection, sized = warehouse
    reference = db.read_sql(connection, "SELECT * FROM pd_curve")

    clean = reconcile.compare(
        reference, reference.copy(),
        keys=["start_bucket", "horizon"],
        measures=["cumulative_pd", "marginal_pd"],
        tolerances=sized["reconciliation"])
    assert clean["passed"]
    assert clean["rows_compared"] == len(reference)

    tampered = reference.copy()
    tampered.loc[tampered.index[0], "cumulative_pd"] += 0.05
    dirty = reconcile.compare(
        reference, tampered,
        keys=["start_bucket", "horizon"],
        measures=["cumulative_pd", "marginal_pd"],
        tolerances=sized["reconciliation"])
    assert not dirty["passed"]
    assert dirty["summary"].set_index("measure").loc["cumulative_pd", "rows_breaching"] == 1
    assert "cumulative_pd" in dirty["worst"]


def test_a_missing_row_is_reported_separately_from_a_wrong_one(warehouse):
    """They are different defects and they get debugged differently.

    A row present on one side only usually means a join or a filter diverged.
    A row whose value differs usually means arithmetic did. Collapsing them
    into one count sends you looking in the wrong place.
    """
    connection, sized = warehouse
    reference = db.read_sql(connection, "SELECT * FROM pd_curve")
    short = reference.iloc[1:].copy()

    outcome = reconcile.compare(
        reference, short,
        keys=["start_bucket", "horizon"],
        measures=["cumulative_pd"],
        tolerances=sized["reconciliation"])

    assert not outcome["passed"]
    assert outcome["rows_only_in_reference"] == 1
    assert outcome["rows_only_in_candidate"] == 0
    # And no value disagreed, because none of the shared rows changed.
    assert outcome["summary"]["rows_breaching"].sum() == 0


def test_a_cent_on_a_million_is_not_a_disagreement(warehouse):
    """Both tolerances must be breached, not either.

    Floating point arithmetic in two languages will not agree bit for bit, and
    a reconciliation that fires on the last decimal place is one nobody reads.
    """
    connection, sized = warehouse
    reference = db.read_sql(connection, "SELECT * FROM account_pd")[
        ["account_id", "scheduled_balance"]].copy()
    nudged = reference.copy()
    nudged["scheduled_balance"] = nudged["scheduled_balance"] + 0.001

    outcome = reconcile.compare(
        reference, nudged, keys=["account_id"],
        measures=["scheduled_balance"], tolerances=sized["reconciliation"])
    assert outcome["passed"], (
        "a tenth of a cent on a loan balance should not be reported as the "
        "two engines disagreeing")
