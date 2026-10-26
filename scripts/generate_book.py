#!/usr/bin/env python3
"""
Build the synthetic loan book and load it into the warehouse.

    python scripts/generate_book.py                 # full book from config
    python scripts/generate_book.py --accounts 2000 # smaller, faster
    python scripts/generate_book.py --sample        # write the committed sample
    python scripts/generate_book.py --summary       # describe it, don't rebuild

THE BOOK IS SIMULATED. That is a deliberate choice, not a compromise: this
project's deliverable is the correctness of an ECL calculation, and a book whose
true PD, EAD and LGD are known by construction is the only kind that lets the
engine's output be checked against an exact answer. Nobody knows a real book's
true PD.
"""
import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed
from src import db, generate


def summarise(accounts, performance, truth, config):
    """What was built, in numbers a credit person would recognise."""
    lines = ["", "The book", "-" * 8]
    lines.append(f"  accounts                  {len(accounts):>10,}")
    lines.append(f"  monthly performance rows  {len(performance):>10,}")
    lines.append(f"  live at the reporting date {len(truth):>9,}")
    lines.append(f"  reporting date            {config['portfolio']['reporting_date']:>10}")

    ever_default = performance.groupby("account_id")["is_default"].max()
    lines.append(f"  ever defaulted            {ever_default.mean():>10.1%}")

    lines.append("")
    lines.append("  by segment")
    by_segment = accounts.groupby("segment").agg(
        accounts=("account_id", "size"),
        mean_principal=("principal", "mean"),
        mean_rate=("annual_rate", "mean"),
    )
    for segment, row in by_segment.iterrows():
        lines.append(f"    {segment:<12} {int(row['accounts']):>6,} accounts   "
                     f"avg {row['mean_principal']:>9,.0f}   "
                     f"rate {row['mean_rate']:.1%}")

    if len(truth):
        lines.append("")
        lines.append("  the answer key (analytic, at the reporting date)")
        lines.append(f"    mean true 12-month PD   {truth['true_pd_12m'].mean():>8.2%}")
        lines.append(f"    mean true lifetime PD   {truth['true_pd_lifetime'].mean():>8.2%}")
        lines.append(f"    total true lifetime ECL {truth['true_ecl_lifetime'].sum():>12,.0f}")
        lines.append(f"    total exposure          {truth['true_balance'].sum():>12,.0f}")
        coverage = (truth["true_ecl_lifetime"].sum()
                    / max(truth["true_balance"].sum(), 1e-9))
        lines.append(f"    implied coverage ratio  {coverage:>8.2%}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    parser.add_argument("--accounts", type=int,
                        help="Override portfolio.n_accounts")
    parser.add_argument("--database", help="Write somewhere other than configured")
    parser.add_argument("--sample", action="store_true",
                        help="Build the small committed sample instead")
    parser.add_argument("--summary", action="store_true",
                        help="Describe the existing warehouse without rebuilding")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.summary:
        connection = db.connect(config, args.database, read_only=True)
        print()
        print(db.describe(connection))
        print()
        connection.close()
        return 0

    if args.sample:
        config["portfolio"]["n_accounts"] = 400
        target = REPO_ROOT / config["data"]["sample_path"]
    else:
        target = Path(args.database) if args.database else None

    if args.accounts:
        config["portfolio"]["n_accounts"] = args.accounts

    rng = set_seed(config.get("seed", 42))

    print(f"\nGenerating {config['portfolio']['n_accounts']:,} accounts "
          f"({config['portfolio']['product']})")
    print("  SYNTHETIC DATA — the true PD, EAD and LGD are known by "
          "construction\n")

    started = time.time()
    accounts, performance, truth = generate.generate_book(config, rng)
    print(f"  simulated in {time.time() - started:.1f}s")

    connection = db.connect(config, target)
    for name, frame in (("accounts", accounts),
                        ("loan_month", performance),
                        ("ground_truth", truth)):
        rows = db.write_table(connection, name, frame)
        print(f"  wrote {name:<14} {rows:>10,} rows")

    print(summarise(accounts, performance, truth, config))
    print()
    print(db.describe(connection))
    connection.close()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
