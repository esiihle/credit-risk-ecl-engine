#!/usr/bin/env python3
"""
Compare the SAS engine's output against the DuckDB reference, table by table.

Run this after the SAS programs have produced their sas_*.csv files and you
have downloaded them back into the repository.

    python scripts/generate_book.py          # build the book
    python scripts/run_pipeline.py           # build the DuckDB reference
    python scripts/export_for_sas.py         # write the three source CSVs
    # ... upload, run sas/run_all.sas, download the sas_*.csv files ...
    python scripts/reconcile_sas.py --sas-dir sas/results

Exit code 0 means the two engines agree inside the tolerance set in
config.yaml before either of them was built. Exit code 1 means they do not,
and the worst rows are printed so the disagreement has somewhere to start.

Missing files are reported and skipped rather than treated as a pass. A
reconciliation that silently compares nothing is the worst available outcome,
because it looks exactly like a reconciliation that worked.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config
from src import db, reconcile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sas-dir", default="sas/results",
                        help="folder holding the sas_*.csv files")
    parser.add_argument("--database", default=None)
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--only", default=None,
                        help="reconcile one table instead of all of them")
    args = parser.parse_args()

    config = load_config()
    if args.database:
        path = Path(args.database)
    elif args.sample:
        path = REPO_ROOT / config["data"]["sample"]
    else:
        path = REPO_ROOT / config["data"]["database"]

    if not path.exists():
        print(f"No warehouse at {path}. Run scripts/run_pipeline.py first.")
        return 1

    sas_dir = REPO_ROOT / args.sas_dir
    if not sas_dir.exists():
        print(f"No SAS results at {sas_dir}.")
        print()
        print("Expected the sas_*.csv files written by sas/09_export.sas.")
        print("Download them from SAS OnDemand into that folder and rerun.")
        return 1

    wanted = [args.only] if args.only else list(reconcile.TABLES)

    connection = db.connect(config, path, read_only=True)
    results, missing = {}, []
    try:
        for table in wanted:
            spec = reconcile.TABLES[table]
            csv = sas_dir / f"sas_{table}.csv"
            if not csv.exists():
                missing.append(csv.name)
                continue
            frame = pd.read_csv(csv)
            results[table] = reconcile.reconcile_table(
                connection, frame, table, spec["keys"], spec["measures"], config)

        if results:
            print(reconcile.report(results, config))
    finally:
        connection.close()

    if missing:
        print()
        print(f"  NOT COMPARED: {', '.join(missing)} were not in {sas_dir}.")
        print("  Those tables are unreconciled, which is not the same as")
        print("  reconciled. Rerun sas/09_export.sas and download them.")

    if not results:
        print()
        print("  Nothing was compared at all. Treating that as a failure,")
        print("  because an empty reconciliation passes by default and looks")
        print("  identical to one that worked.")
        return 1

    return 0 if (all(r["passed"] for r in results.values()) and not missing) else 1


if __name__ == "__main__":
    raise SystemExit(main())
