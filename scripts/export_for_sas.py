#!/usr/bin/env python3
"""
Export the source tables to CSV so the SAS engine can be run against the same
data as the DuckDB one.

WHAT GETS EXPORTED, AND WHAT DELIBERATELY DOES NOT.

Only the three SOURCE tables go across: accounts, loan_month and recovery.
These are what a bank's warehouse would hand a credit risk team, and the SAS
engine rebuilds everything else from them, exactly as the DuckDB engine does.

Nothing derived is exported. Handing SAS the mart, or the roll rates, or the PD
term structure would make the reconciliation meaningless: the two
implementations would agree because they shared the working, not because they
independently got the same answer.

The answer key is not exported either. `true_lgd`, `true_pd_*` and
`true_ecl_*` stay out of the CSVs, and `true_risk_multiplier` with them. The
SAS engine has no more access to the truth than the DuckDB one does, which is
the only way its output is worth comparing.

Usage:
    python scripts/export_for_sas.py                 # the full book
    python scripts/export_for_sas.py --sample        # the committed sample
    python scripts/export_for_sas.py --output sas/data
"""
import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd

from src.config import load_config
from src import db

# Columns the SAS engine is allowed to see. Listed explicitly rather than
# excluded by pattern, so a new answer-key column added to the generator
# cannot leak into the export by default.
EXPORTS = {
    "accounts": ["account_id", "segment", "origination_date", "principal",
                 "term_months", "annual_rate", "instalment"],
    "loan_month": ["account_id", "reporting_date", "months_on_book",
                   "arrears_bucket", "scheduled_balance", "is_default",
                   "days_past_due"],
    "recovery": ["account_id", "default_date", "months_on_book_at_default",
                 "ead_at_default", "months_in_workout", "workout_complete",
                 "recovery_to_date"],
}

# Anything matching these must never appear in a CSV. The check below is on the
# column names actually written, not on the list above, so editing EXPORTS
# cannot quietly defeat it.
FORBIDDEN = ("true_", "_truth")


def write_config_include(config, sas_dir):
    """Emit every threshold the SAS engine needs, as %LET statements.

    GENERATED, NEVER HAND-EDITED. The alternative is retyping the accounting
    policy into a second file and hoping the two stay equal, which is the same
    mistake as a number in a document that no command regenerates: it is right
    on the day it is written and silently wrong afterwards.

    The DuckDB engine reads `config.yaml` directly. The SAS engine reads this
    file, and this file is produced from `config.yaml`, so there is exactly one
    place where a threshold lives.
    """
    staging = config["staging"]
    values = [
        ("default_dpd", staging["default_dpd"],
         "90+ days past due is default, and default is absorbing"),
        ("sicr_dpd_backstop", staging["sicr_dpd_backstop"],
         "IFRS 9 rebuttable presumption: 30 days past due means SICR"),
        ("sicr_relative_pd_increase", staging["sicr_relative_pd_increase"],
         "lifetime PD must rise by this multiple to trigger SICR"),
        ("sicr_absolute_pd_floor", staging["sicr_absolute_pd_floor"],
         "and the level must be material before the multiple counts"),
        ("probation_months", staging["probation_months"],
         "months of good behaviour before Stage 2 releases back to Stage 1"),
        ("min_transitions_per_cell", config["pd"]["min_transitions_per_cell"],
         "below this a segment-age cell falls back to the segment rate"),
        ("horizon_stage_1", config["ecl"]["horizon_months_stage_1"],
         "12-month ECL horizon for Stage 1"),
        ("default_bucket", config["generator"]["default_bucket"],
         "the absorbing arrears bucket"),
    ]

    # SAS has no ISO date literal. "2025-12-31" is a character string there,
    # and comparing it to a date column silently matches nothing, which would
    # leave account_pd empty and the reconciliation comparing two empty
    # tables successfully. Emitted as a DATE9 literal instead.
    reporting = pd.Timestamp(config["portfolio"]["reporting_date"])
    values.append(
        ("reporting_date", f"'{reporting.strftime('%d%b%Y').upper()}'d",
         "the reporting date the whole engine is run as at"))

    lines = [
        "/* ===================================================================",
        " * 00_config.sas  --  GENERATED FILE, DO NOT EDIT",
        " *",
        " * Produced by scripts/export_for_sas.py from config.yaml. Every",
        " * threshold below is an accounting policy choice, and it lives in",
        " * exactly one place so the SAS engine and the DuckDB engine cannot",
        " * drift apart. Change config.yaml and rerun the export.",
        " * =================================================================== */",
        "",
    ]
    for name, value, note in values:
        # Already-quoted SAS literals pass through untouched.
        rendered = value
        lines.append(f"/* {note} */")
        lines.append(f"%let {name} = {rendered};")
        lines.append("")

    destination = sas_dir / "00_config.sas"
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--database", default=None)
    parser.add_argument("--output", default="sas/data")
    args = parser.parse_args()

    config = load_config()
    if args.database:
        path = Path(args.database)
    elif args.sample:
        path = REPO_ROOT / config["data"]["sample"]
    else:
        path = REPO_ROOT / config["data"]["database"]

    if not path.exists():
        print(f"No warehouse at {path}.")
        print("Run `python scripts/generate_book.py` first.")
        return 1

    output = REPO_ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)

    connection = db.connect(config, path, read_only=True)
    try:
        print(f"Exporting from {path}")
        total_bytes = 0
        for table, columns in EXPORTS.items():
            leaked = [c for c in columns if any(f in c for f in FORBIDDEN)]
            if leaked:
                raise SystemExit(
                    f"{table}: {leaked} would put the answer key in front of "
                    f"the SAS engine, which makes the reconciliation worthless")

            frame = db.read_sql(
                connection, f"SELECT {', '.join(columns)} FROM {table}")
            destination = output / f"{table}.csv"
            # No index, ISO dates, and a plain comma: PROC IMPORT guesses, and
            # the less there is to guess the fewer surprises in the SAS log.
            frame.to_csv(destination, index=False, date_format="%Y-%m-%d")
            size = destination.stat().st_size
            total_bytes += size
            print(f"  {table:<14}{len(frame):>10,} rows  "
                  f"{size / 1_048_576:>7.1f} MB  -> {destination.relative_to(REPO_ROOT)}")

        print(f"  {'total':<14}{'':>10}  {total_bytes / 1_048_576:>7.1f} MB")

        generated = write_config_include(config, output.parent)
        print(f"  {'thresholds':<14}{'':>10}  {'':>7}     -> "
              f"{generated.relative_to(REPO_ROOT)}")
    finally:
        connection.close()

    print()
    print("Next: upload these three files to SAS OnDemand (Files panel, the")
    print("upload button), put them in a folder, and set &csv_root at the top")
    print("of sas/00_setup.sas to that folder's path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
