#!/usr/bin/env python3
"""
Run the ECL engine end to end, reporting what is built and what is not.

    python scripts/run_pipeline.py              # the configured warehouse
    python scripts/run_pipeline.py --sample     # the bundled sample book

Every stage prints its status. Stages not yet implemented say so rather than
being silently skipped: a pipeline that quietly does less than it claims is how
a half-built system gets reported as finished.
"""
import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed
from src import db, mart, rollrate, staging

# The build plan, in order. Each entry is (phase, name, what it produces).
# This list is the roadmap in executable form, and `--status` prints it.
STAGES = [
    (1, "Data mart", "dim_account, dim_date, fact_loan_month"),
    (2, "Delinquency & roll rates", "bucket transitions, roll-rate matrices"),
    (3, "IFRS 9 staging", "stage 1 / 2 / 3 per the SICR rules in config"),
    (4, "PD term structure", "12-month and lifetime PD per account"),
    (5, "LGD and EAD", "loss given default and exposure at default"),
    (6, "ECL", "discounted expected credit loss, by stage and segment"),
    (7, "Vintage & portfolio MI", "vintage curves, coverage ratios"),
    (8, "SAS reconciliation", "PROC SQL implementation, reconciled"),
]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    parser.add_argument("--sample", action="store_true",
                        help="Run against the bundled sample book")
    parser.add_argument("--status", action="store_true",
                        help="Print the build plan and exit")
    args = parser.parse_args()

    config = load_config(args.config)
    set_seed(config.get("seed", 42))

    if args.status:
        print("\nBuild plan\n" + "-" * 10)
        for phase, name, produces in STAGES:
            print(f"  Phase {phase} — {name:<28} {produces}")
        print()
        return 0

    if args.sample:
        # Work on a COPY. The committed sample is a version-controlled file, and
        # building the mart into it would leave the repository dirty after
        # every run — and eventually someone commits the derived tables, which
        # then go stale against the SQL that produced them.
        source = REPO_ROOT / config["data"]["sample_path"]
        if not source.exists():
            print(f"\n  No sample book at {source}")
            print("  Rebuild it: python scripts/generate_book.py --sample\n")
            return 1
        target = REPO_ROOT / config["data"]["processed_dir"] / "sample_run.duckdb"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, target)
        print(f"\n  working on a copy of the sample at "
              f"{target.relative_to(REPO_ROOT)}")
    else:
        target = None

    print(f"\nECL engine — {config['portfolio']['product']}, "
          f"reporting date {config['portfolio']['reporting_date']}")
    print("  SYNTHETIC BOOK — true PD, EAD and LGD are known by construction\n")

    try:
        connection = db.connect(config, target, read_only=True)
    except db.DatabaseError as error:
        print(f"  {error}")
        print("\n  Nothing to run against. Build a book first:")
        print("      python scripts/generate_book.py")
        print()
        return 1

    print(db.describe(connection))
    connection.close()

    # --- Phase 1: the data mart ------------------------------------------
    # Reopened for writing: the mart is built here, not read.
    print()
    print("Stage 1 — Data mart")
    connection = db.connect(config, target)
    try:
        mart.build(connection, config)
    except Exception as error:
        print(f"  CANNOT BUILD THE MART: {error}")
        connection.close()
        return 1

    print()
    checks = mart.quality_report(connection)
    print(mart.format_quality(checks))

    if not checks["passed"].all():
        # Stopping here is the point. A mart that fails its own checks produces
        # numbers that look exactly like good ones, and every later stage would
        # inherit the defect while reporting confidently.
        print()
        print("  STOPPING. Later stages are not run against a mart that failed")
        print("  its own quality checks.")
        connection.close()
        return 1

    print(mart.mart_report(connection, config))

    # --- Phase 2: delinquency transitions and roll rates ------------------
    print()
    print("Stage 2 — Delinquency and roll rates")
    try:
        rollrate.build(connection)
    except Exception as error:
        print(f"  CANNOT BUILD TRANSITIONS: {error}")
        connection.close()
        return 1

    print()
    print(rollrate.rollrate_report(connection, config))

    # --- Phase 3: IFRS 9 staging ------------------------------------------
    print()
    print("Stage 3 — IFRS 9 staging")
    try:
        staging.build(connection, config)
    except Exception as error:
        print(f"  CANNOT ASSIGN STAGES: {error}")
        connection.close()
        return 1

    checks = staging.validate(connection, config)
    if not checks["passed"].all():
        print()
        print(staging.staging_report(connection, config))
        print()
        print("  STOPPING. Staging failed its own checks, and every provision")
        print("  below depends on it.")
        connection.close()
        return 1

    print()
    print(staging.staging_report(connection, config))
    connection.close()

    print()
    print("Stages")
    print("-" * 6)
    built = {"Data mart": True, "Delinquency & roll rates": True,
             "IFRS 9 staging": True}
    for phase, name, produces in STAGES:
        status = "built" if built.get(name) else "NOT YET BUILT"
        print(f"  Phase {phase} — {name:<28} {status}")
    print()
    print("Phase 3 complete: every account staged, with the trigger that put")
    print("it there recorded by name.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
