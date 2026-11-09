"""
Phase 8, reconciling the SAS engine against the DuckDB one.

Two implementations of the same calculation, written in different languages
against different engines, compared row by row. This is champion and challenger
reconciliation, which is what a model validation function does, and it is the
only way to show that the SAS code is correct rather than assert it.

WHY A SECOND IMPLEMENTATION IS WORTH THE EFFORT. A single engine can only be
checked against the answer key, and the answer key only exists because this
book is simulated. On a real portfolio there is no key, and the question "is
this provision right" has to be answered some other way. Two independent
implementations agreeing to the cent is that other way, and it is what a bank
actually does before a model goes into production.

WHAT COUNTS AS INDEPENDENT. The SAS engine reads only the three source CSVs.
It is handed no intermediate table and no part of the answer key, so when the
two agree they agree because they both got the same answer, not because they
shared the working.
"""
import numpy as np
import pandas as pd

from . import db


class ReconciliationError(Exception):
    """Raised when the two engines cannot be compared at all."""


def compare(reference, candidate, keys, measures, tolerances):
    """Compare two frames on shared keys and report every difference.

    Returns a summary row per measure plus the worst offending rows, because a
    maximum absolute difference tells you an engine disagrees and nothing about
    where, and "where" is the whole of the debugging.

    Rows present on one side only are counted separately from rows that differ.
    They are a different defect: a missing row usually means a join or a filter
    diverged, while a differing value usually means arithmetic did.
    """
    for frame, name in ((reference, "reference"), (candidate, "candidate")):
        missing = [k for k in keys if k not in frame.columns]
        if missing:
            raise ReconciliationError(f"{name} has no {missing} to join on")

    merged = reference.merge(candidate, on=keys, how="outer",
                             suffixes=("_ref", "_sas"), indicator=True)

    only_reference = int((merged["_merge"] == "left_only").sum())
    only_candidate = int((merged["_merge"] == "right_only").sum())
    both = merged[merged["_merge"] == "both"]

    rows = []
    worst = {}
    for measure in measures:
        left, right = f"{measure}_ref", f"{measure}_sas"
        if left not in both.columns or right not in both.columns:
            raise ReconciliationError(f"{measure} is not on both sides")

        a = pd.to_numeric(both[left], errors="coerce").to_numpy(dtype=float)
        b = pd.to_numeric(both[right], errors="coerce").to_numpy(dtype=float)
        absolute = np.abs(a - b)
        # Relative difference against the reference, with a guard: a measure
        # that is legitimately zero would otherwise report an infinite
        # relative error and drown the real findings.
        scale = np.maximum(np.abs(a), 1e-12)
        relative = absolute / scale

        limit_abs = float(tolerances["max_absolute_difference"])
        limit_rel = float(tolerances["max_relative_difference"])
        breaches = int(((absolute > limit_abs) & (relative > limit_rel)).sum())

        rows.append({
            "measure": measure,
            "compared": int(len(both)),
            "max_absolute": float(np.nanmax(absolute)) if len(absolute) else 0.0,
            "max_relative": float(np.nanmax(relative)) if len(relative) else 0.0,
            "mean_absolute": float(np.nanmean(absolute)) if len(absolute) else 0.0,
            "rows_breaching": breaches,
            # A measure breaches only if it exceeds BOTH tolerances. One cent
            # on a million is not a disagreement, and neither is a thousandth
            # of a percent on a large balance.
            "passed": breaches == 0,
        })

        if len(absolute) and np.nanmax(absolute) > 0:
            order = np.argsort(-absolute)[:5]
            worst[measure] = both.iloc[order][keys + [left, right]].assign(
                difference=absolute[order])

    return {
        "summary": pd.DataFrame(rows),
        "worst": worst,
        "rows_only_in_reference": only_reference,
        "rows_only_in_candidate": only_candidate,
        "rows_compared": int(len(both)),
        "passed": (bool(all(r["passed"] for r in rows))
                   and only_reference == 0 and only_candidate == 0),
    }


def reconcile_table(connection, sas_frame, table, keys, measures, config):
    """Compare one SAS export against its DuckDB table."""
    reference = db.read_sql(connection, f"SELECT * FROM {table}")
    # SAS upper-cases nothing by default on CSV export, but PROC EXPORT has
    # been known to, and a column-name mismatch would read as a missing
    # measure rather than as a naming problem.
    sas_frame = sas_frame.rename(columns={c: c.lower() for c in sas_frame.columns})
    reference = reference.rename(columns={c: c.lower() for c in reference.columns})

    return compare(reference[keys + measures], sas_frame[keys + measures],
                   keys, measures, config["reconciliation"])


# What gets reconciled, and on what. Keys first, then the measures that matter.
# Intermediate tables are included deliberately: if only the final answer were
# compared, a disagreement would have nowhere to be localised, and two errors
# that happened to cancel would read as a pass.
TABLES = {
    "roll_rate": {
        "keys": ["from_bucket", "to_bucket"],
        "measures": ["transitions", "opportunities", "rate"],
        "note": "the pooled transition matrix, where the LEAD replacement lands",
    },
    "pd_curve": {
        "keys": ["start_bucket", "horizon"],
        "measures": ["cumulative_pd", "marginal_pd", "surviving"],
        "note": "the recursive walk, rebuilt as a macro loop",
    },
    "segment_age_matrix": {
        "keys": ["segment", "age_band", "from_bucket", "to_bucket"],
        "measures": ["transitions", "opportunities", "rate"],
        "note": "matrices by segment and age, with the thin-cell fallback",
    },
    "account_pd": {
        "keys": ["account_id"],
        "measures": ["pd_12m_pooled", "pd_lifetime_pooled",
                     "pd_12m_segmented", "pd_lifetime_segmented",
                     "pd_12m_age_aware", "pd_lifetime_age_aware",
                     "stage", "horizon_12m", "horizon_lifetime"],
        "note": "per-account PD and stage, the end of the ported chain",
    },
}


def report(results, config):
    """The reconciliation as a model validation function would read it."""
    limits = config["reconciliation"]
    lines = ["", "SAS against DuckDB", "-" * 18]
    lines.append(f"  tolerance: {limits['max_absolute_difference']} absolute "
                 f"AND {limits['max_relative_difference']} relative")
    lines.append("  (a row breaches only if it exceeds both, so a cent on a")
    lines.append("   million is not treated as a disagreement)")

    everything_passed = True
    for table, outcome in results.items():
        lines.append("")
        lines.append(f"  {table}  {TABLES[table]['note']}")
        lines.append(f"    rows compared              {outcome['rows_compared']:>10,}")
        if outcome["rows_only_in_reference"] or outcome["rows_only_in_candidate"]:
            lines.append(f"    ONLY IN DUCKDB             "
                         f"{outcome['rows_only_in_reference']:>10,}")
            lines.append(f"    ONLY IN SAS                "
                         f"{outcome['rows_only_in_candidate']:>10,}")

        for row in outcome["summary"].itertuples(index=False):
            mark = "ok  " if row.passed else "FAIL"
            lines.append(f"    [{mark}] {row.measure:<24}"
                         f"max abs {row.max_absolute:>12.3e}   "
                         f"max rel {row.max_relative:>10.3e}")
        if not outcome["passed"]:
            everything_passed = False
            for measure, frame in outcome["worst"].items():
                if frame["difference"].max() > float(limits["max_absolute_difference"]):
                    lines.append(f"    worst rows for {measure}:")
                    for line in frame.to_string(index=False).split("\n"):
                        lines.append(f"      {line}")

    lines.append("")
    if everything_passed:
        lines.append("  RECONCILED. Two implementations, two languages, same")
        lines.append("  answer. Neither saw the other's working.")
    else:
        lines.append("  NOT RECONCILED. The engines disagree beyond tolerance.")
        lines.append("  The worst rows are printed above; start at the earliest")
        lines.append("  table that fails, because everything after it inherits.")
    return "\n".join(lines)
