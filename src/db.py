"""
The DuckDB warehouse.

Why DuckDB: it is a real analytical SQL engine that lives in a single file with
no server. Anyone can clone this repository and run the whole thing with one
command, which is the difference between a portfolio project someone can
evaluate and one they take on trust. The SQL is Postgres-flavoured, so almost
all of it ports to a bank's warehouse unchanged — and Phase 8 ports it to SAS
PROC SQL and reconciles the two.

Everything here is deliberately thin. The engine's logic belongs in SQL under
`sql/`, not in Python wrappers: the point of the project is to show the SQL.
"""
from pathlib import Path

import duckdb
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]

# Tables this project owns. Listing them in one place means `describe()` and
# the tests cannot drift from what the pipeline actually builds.
CORE_TABLES = {
    "accounts": "one row per loan, fixed at origination",
    "loan_month": "the fact table: one row per account per month on book",
    "ground_truth": "the answer key — true PD, EAD and ECL, known because the "
                    "book is simulated",
}


class DatabaseError(Exception):
    """Raised when the warehouse cannot be opened or is missing what is asked for."""


def connect(config, path=None, read_only=False):
    """Open (and create if needed) the warehouse.

    Parameters
    ----------
    config : dict
        Reads `data.database`.
    path : str | Path | None
        Overrides the configured location — used by tests, which must never
        touch the working database.
    read_only : bool
        Open without the ability to write. Worth using in reporting code: a
        report that cannot modify the warehouse cannot corrupt it.
    """
    target = Path(path) if path else REPO_ROOT / config["data"]["database"]
    target.parent.mkdir(parents=True, exist_ok=True)

    if read_only and not target.exists():
        raise DatabaseError(
            f"No database at {target}. Run `python scripts/generate_book.py` "
            "to build one."
        )

    return duckdb.connect(str(target), read_only=read_only)


def write_table(connection, name, frame, replace=True):
    """Write a dataframe as a table.

    `replace=True` by default because every table here is derived — rebuilt
    from the generator or from SQL — so an append would silently double a book.
    """
    if not isinstance(frame, pd.DataFrame):
        raise DatabaseError(f"{name}: expected a DataFrame, got {type(frame)}")
    if frame.empty:
        raise DatabaseError(f"{name}: refusing to write an empty table.")

    connection.register("_incoming", frame)
    verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE IF NOT EXISTS"

    # DuckDB 2.x holds an open result after execute() and refuses further work
    # on the connection until it is drained. CREATE TABLE AS returns a row
    # count, so it must be fetched even though nothing uses it — otherwise the
    # next statement fails with "connection has an unfinished result".
    connection.execute(f"{verb} {name} AS SELECT * FROM _incoming").fetchall()
    connection.unregister("_incoming")

    return _count(connection, name)


def read_sql(connection, sql, **parameters):
    """Run a query and return a dataframe.

    Named parameters are substituted with DuckDB's `$name` binding rather than
    string formatting — the habit matters more than the risk does here.
    """
    if parameters:
        return connection.execute(sql, parameters).df()
    return connection.execute(sql).df()


def run_sql_file(connection, path, **parameters):
    """Execute a .sql file from `sql/`.

    The engine's logic lives in those files so it can be read as SQL, reviewed
    by someone who does not write Python, and ported to SAS in Phase 8.
    """
    path = Path(path)
    if not path.is_absolute():
        path = REPO_ROOT / "sql" / path
    if not path.exists():
        raise DatabaseError(f"No SQL file at {path}")

    statement = path.read_text(encoding="utf-8")
    if parameters:
        return connection.execute(statement, parameters).df()
    return connection.execute(statement).df()


def _count(connection, name):
    """Row count, with the result fully drained.

    `fetchone()` leaves the result set open, and DuckDB 2.x then refuses any
    further work on that connection. Draining with `fetchall()` is the fix, and
    it is wrapped here so no call site has to remember.
    """
    return connection.execute(f"SELECT COUNT(*) FROM {name}").fetchall()[0][0]


def table_names(connection):
    """Every table currently in the warehouse."""
    rows = connection.execute("SHOW TABLES").fetchall()
    return sorted(row[0] for row in rows)


def describe(connection):
    """A readable summary of the warehouse: what exists and how big it is."""
    present = table_names(connection)
    lines = ["Warehouse", "-" * 9]

    if not present:
        lines.append("  empty — run `python scripts/generate_book.py`")
        return "\n".join(lines)

    for name in present:
        count = _count(connection, name)
        purpose = CORE_TABLES.get(name, "")
        lines.append(f"  {name:<18} {count:>10,} rows   {purpose}")

    missing = [t for t in CORE_TABLES if t not in present]
    if missing:
        lines.append(f"\n  NOT BUILT YET: {', '.join(missing)}")

    return "\n".join(lines)
