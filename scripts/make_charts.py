#!/usr/bin/env python3
"""
Regenerate every chart in docs/charts/ from the warehouse.

THE RULE THIS SCRIPT EXISTS TO KEEP: no figure appears in a document unless a
command regenerates it. A chart is the easiest place in a repository to break
that rule, because an image keeps showing yesterday's numbers indefinitely and
nothing complains. So the charts are never edited, never exported by hand, and
never committed without rerunning this.

Every chart also stamps the reporting date and the book size it was drawn from,
so an image that has drifted from the data can be spotted without opening the
database.

Usage:
    python scripts/make_charts.py                 # from data/processed/ecl.duckdb
    python scripts/make_charts.py --sample        # from the committed sample
    python scripts/make_charts.py --output docs/charts
"""
import argparse
import sys
import textwrap
from pathlib import Path

import matplotlib
matplotlib.use("Agg")           # No display in CI; must be set before pyplot.
import matplotlib.pyplot as plt
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config
from src import db, ecl, vintage

# A deliberately small palette. Vintage charts have dozens of lines, so colour
# carries the ORDER of cohorts rather than their identity, and a sequential map
# is the only honest way to show that.
INK = "#1b1b1b"
MUTED = "#8a8a8a"
GRID = "#e4e4e4"
ACCENT = "#1f5c8b"
WARN = "#b4461f"


def _frame(fig, ax_list, title, subtitle, stamp, wrap=118):
    """Shared furniture: title, wrapped subtitle, provenance stamp, clean axes.

    THE LAYOUT TRAP THIS FUNCTION EXISTS TO AVOID. `layout="constrained"` sizes
    the axes to fill the figure, and it accounts for `fig.suptitle` but knows
    nothing about `fig.text`. Placing a subtitle and a stamp with `fig.text` at
    fixed coordinates therefore draws them straight through the axes, which is
    exactly what the first version of these charts did, printing the subtitle
    across the plot area and the stamp through the x-axis label.

    The fix is to reserve the bands explicitly with the layout engine's `rect`,
    computed from how many lines the subtitle actually wraps to, so the axes
    are laid out inside what is left rather than on top of it.

    Also: never call `fig.autofmt_xdate()` here. It silently disables
    constrained layout, which cost my scorecard project an afternoon.
    """
    lines = textwrap.wrap(subtitle, wrap) if subtitle else []

    # Figure coordinates. Measured against the figure height so a taller figure
    # does not reserve a proportionally larger band than the text needs.
    height = fig.get_size_inches()[1]
    title_band = 0.34 / height                     # ~0.34in for the title
    line_band = 0.19 / height                      # ~0.19in per subtitle line
    stamp_band = 0.26 / height

    top_reserved = title_band + line_band * len(lines) + 0.10 / height
    fig.get_layout_engine().set(
        rect=(0.008, stamp_band, 0.984, 1.0 - stamp_band - top_reserved))

    fig.suptitle(title, fontsize=14, fontweight="bold", color=INK, ha="left",
                 x=0.010, y=0.995, va="top")
    if lines:
        fig.text(0.010, 1.0 - title_band, "\n".join(lines),
                 fontsize=9.5, color=MUTED, ha="left", va="top",
                 linespacing=1.45)
    fig.text(0.010, 0.012, stamp, fontsize=7.5, color=MUTED, ha="left",
             va="bottom")

    for ax in np.atleast_1d(ax_list).ravel():
        ax.grid(True, color=GRID, linewidth=0.7)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(MUTED)
        ax.tick_params(colors=MUTED, labelsize=9)


def chart_vintage_triangle(connection, stamp, output):
    """The vintage triangle: one line per cohort, censored cells absent.

    The censored cells are not drawn at all. Carrying each cohort's last known
    value forward would produce a flat tail that looks like a cohort whose
    defaults stopped, which is the exact misreading the chart is drawn to
    prevent.
    """
    curves = vintage.curves(connection, uncensored_only=True)
    cohorts = sorted(curves["vintage_month"].unique())
    colours = plt.cm.viridis(np.linspace(0, 0.92, len(cohorts)))

    fig, (left, right) = plt.subplots(
        1, 2, figsize=(13.5, 5.6), layout="constrained", width_ratios=[1.35, 1])

    for colour, cohort in zip(colours, cohorts):
        one = curves[curves["vintage_month"] == cohort]
        left.plot(one["age"], one["cumulative_default_rate"] * 100,
                  color=colour, linewidth=1.1, alpha=0.85)

    common = vintage.censoring_artefact(connection)["common_age"]
    left.axvline(common, color=WARN, linewidth=1.4, linestyle="--")
    # Positioned in AXES coordinates, not data coordinates against a y-limit
    # read before the axes were final, which put this label in the subtitle.
    left.text(0.30, 0.95, f"common age: {common} months\nthe only fair comparison",
              transform=left.transAxes, color=WARN, fontsize=8.5, va="top")
    left.set_xlabel("months on book")
    left.set_ylabel("cumulative default rate (%)")
    left.set_title("One line per monthly cohort", fontsize=10, color=INK,
                   loc="left")

    # A colourbar rather than a sentence about which end is dark. "Darkest =
    # oldest" is ambiguous on viridis, which runs dark purple to bright yellow
    # and is only monotone in lightness if you already know that.
    mappable = plt.cm.ScalarMappable(
        cmap="viridis",
        norm=plt.Normalize(vmin=0, vmax=max(len(cohorts) - 1, 1)))
    bar = fig.colorbar(mappable, ax=left, pad=0.015, fraction=0.045)
    bar.set_ticks([0, len(cohorts) - 1])
    bar.set_ticklabels([cohorts[0], cohorts[-1]])
    bar.ax.tick_params(labelsize=8, colors=MUTED)
    bar.outline.set_visible(False)
    bar.set_label("origination month", fontsize=8.5, color=MUTED)

    # Right panel: the same data, as the two answers to the same question.
    frame = vintage.comparison(connection)
    months = frame["vintage_month_index"].to_numpy(dtype=float)
    offset = months - months.min()

    right.scatter(offset, frame["rate_latest"] * 100, s=16, color=WARN,
                  label="naive, each cohort at its own latest age", zorder=3)
    right.scatter(offset, frame["rate_at_common_age"] * 100, s=16, color=ACCENT,
                  label=f"fair, every cohort at {common} months", zorder=3)
    for values, colour in ((frame["rate_latest"], WARN),
                           (frame["rate_at_common_age"], ACCENT)):
        slope, intercept = np.polyfit(offset, values.to_numpy(dtype=float) * 100, 1)
        right.plot(offset, intercept + slope * offset, color=colour,
                   linewidth=1.8, alpha=0.75)

    right.set_xlabel("months since the first cohort")
    right.set_ylabel("cumulative default rate (%)")
    right.set_title("The trend, with and without the censoring artefact",
                    fontsize=10, color=INK, loc="left")
    right.legend(frameon=False, fontsize=8.5, loc="upper right")

    artefact = vintage.censoring_artefact(connection)
    _frame(fig, [left, right], "Vintage curves, and the trap in reading them",
           f"Naive comparison shows {artefact['naive_slope_per_year'] * 100:+.2f}pp "
           f"per year of apparent improvement; comparing every cohort at the same age "
           f"shows {artefact['fair_slope_per_year'] * 100:+.2f}pp. "
           f"{artefact['artefact_share']:.0%} of the signal is the observation window, "
           f"not credit quality, and the true vintage effect on this book is zero "
           f"by construction.", stamp)

    path = output / "vintage_triangle.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_calendar_vs_cohort(connection, stamp, output):
    """Why calendar-time MI cannot answer the question cohort time can."""
    mi = vintage.calendar_mi(connection)
    settled = mi[mi["accounts_on_book"] >= 0.2 * mi["accounts_on_book"].max()]
    x = np.arange(len(settled))
    labels = settled["reporting_date"].astype(str).str.slice(0, 7).tolist()

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(12.5, 7.2),
                                      layout="constrained", sharex=True)

    top.plot(x, settled["in_arrears_share"] * 100, color=WARN, linewidth=2,
             label="accounts in arrears (%)")
    twin = top.twinx()
    twin.plot(x, settled["average_age_months"], color=ACCENT, linewidth=2,
              linestyle="--", label="average age of the book (months)")
    twin.set_ylabel("average age (months)", color=ACCENT)
    twin.tick_params(colors=ACCENT, labelsize=9)
    twin.spines["top"].set_visible(False)
    top.set_ylabel("in arrears (%)", color=WARN)
    top.set_title("Calendar time: arrears doubled, and so did the book's age",
                  fontsize=10, color=INK, loc="left")
    handles = top.get_legend_handles_labels()[0] + twin.get_legend_handles_labels()[0]
    top.legend(handles=handles, frameon=False, fontsize=8.5, loc="upper left")

    # NOT a 100% stacked area. Stage 1 holds 92-97% of the balance throughout,
    # so a full stack spends the entire panel drawing the part that does not
    # move and renders the two stages that do as slivers a pixel high. Plotting
    # only the stages under scrutiny, on a scale that fits them, is the whole
    # difference between a chart and a decoration.
    bottom.plot(x, settled["stage_2_share"] * 100, color="#c8902a", linewidth=2,
                label="Stage 2, lifetime ECL")
    bottom.plot(x, settled["stage_3_share"] * 100, color=WARN, linewidth=2,
                label="Stage 3, credit-impaired")
    bottom.fill_between(x, 0, settled["stage_2_share"] * 100,
                        color="#e8c07a", alpha=0.25)
    bottom.set_ylim(0, max(settled["stage_2_share"].max() * 100 * 1.25, 1))
    bottom.set_ylabel("share of balance (%)")
    bottom.set_title("Stage 2 and Stage 3 share of balance, Stage 1 is the "
                     "remaining 92-97% and is left out",
                     fontsize=10, color=INK, loc="left")
    bottom.legend(frameon=False, fontsize=8.5, loc="upper left", ncol=2)

    step = max(1, len(x) // 12)
    bottom.set_xticks(x[::step])
    bottom.set_xticklabels(labels[::step], rotation=45, ha="right")

    drift = vintage.age_profile_drift(connection)
    _frame(fig, [top, bottom], "The same book, read in calendar time",
           f"Arrears rose from {drift['arrears_at_start']:.1%} to "
           f"{drift['arrears_at_end']:.1%} while average age went from "
           f"{drift['age_at_start']:,.0f} to {drift['age_at_end']:,.0f} months "
           f"(correlation {drift['age_vs_arrears_correlation']:+.2f}). Cohort quality "
           f"never changed. In calendar time this looks like deterioration; in cohort "
           f"time it is maturity, and nothing on this page can tell them apart.", stamp)

    path = output / "calendar_vs_cohort.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_ecl_and_cliff(connection, config, stamp, output):
    """The provision by stage, and the size of the jump at the SICR boundary."""
    by_stage = ecl.portfolio(connection, level="stage")
    cliff = ecl.staging_cliff(connection)

    fig, (left, right) = plt.subplots(1, 2, figsize=(13, 5.2),
                                      layout="constrained")

    stages = by_stage["stage"].tolist()
    positions = np.arange(len(stages))
    left.bar(positions - 0.2, by_stage["coverage_ratio"] * 100, width=0.38,
             color=ACCENT, label="engine")
    left.bar(positions + 0.2, by_stage["true_coverage_ratio"] * 100, width=0.38,
             color=MUTED, label="answer key")
    for position, row in zip(positions, by_stage.itertuples(index=False)):
        left.text(position, row.coverage_ratio * 100, f" {row.coverage_ratio:.1%}",
                  ha="center", va="bottom", fontsize=9, color=INK)
    left.set_xticks(positions)
    left.set_xticklabels([f"Stage {s}" for s in stages])
    left.set_ylabel("ECL coverage of exposure (%)")
    left.set_title("Coverage rises with stage, as the staging rules intend",
                   fontsize=10, color=INK, loc="left")
    left.legend(frameon=False, fontsize=8.5)

    buckets = cliff["arrears_bucket"].to_numpy()
    multiples = cliff["cliff_multiple"].to_numpy(dtype=float)
    # Drawn from a baseline of 1.0, so each bar's LENGTH is the jump itself
    # rather than the multiple. A bar chart whose axis starts at 0.9 makes a
    # 1.00x bar look substantial and a 1.63x bar look seven times bigger than a
    # 1.20x one; anchoring at the no-effect point makes the lengths mean what
    # they appear to mean.
    right.bar(buckets, multiples - 1.0, bottom=1.0, color="#e8c07a",
              edgecolor=INK, linewidth=0.5)
    right.axhline(1.0, color=MUTED, linewidth=1.2)
    for bucket, multiple in zip(buckets, multiples):
        right.text(bucket, multiple, f"  {multiple:.2f}x", ha="center",
                   va="bottom", fontsize=9, color=INK)
    right.set_xticks(buckets)
    right.set_xticklabels(["current", "1 to 29 DPD", "30 to 59 DPD", "60 to 89 DPD"])
    right.set_ylabel("lifetime ECL ÷ 12-month ECL\n(bar length = the jump)")
    right.set_ylim(1.0, multiples.max() * 1.10)
    right.set_title("The staging cliff, for the same accounts counted two ways",
                    fontsize=10, color=INK, loc="left")

    total = ecl.portfolio(connection, level="portfolio").iloc[0]
    _frame(fig, [left, right], "The provision, and the cliff inside it",
           f"Portfolio ECL {total['ecl']:,.0f} against exposure "
           f"{total['exposure']:,.0f}, coverage {total['coverage_ratio']:.2%} on a "
           f"{total['relative_error']:+.2%} error against the key. The cliff is "
           f"steepest for accounts in no arrears and flat for those 60 to 89 days down, "
           f"so the accounts most likely to trip SICR face the smallest jump.", stamp)

    path = output / "ecl_and_staging_cliff.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true",
                        help="draw from the committed sample instead")
    parser.add_argument("--database", default=None)
    parser.add_argument("--output", default="docs/charts")
    args = parser.parse_args()

    config = load_config()
    if args.database:
        path = Path(args.database)
    elif args.sample:
        path = REPO_ROOT / config["data"]["sample_path"]
    else:
        path = REPO_ROOT / config["data"]["database"]

    if not path.exists():
        print(f"No warehouse at {path}.")
        print("Run `python scripts/run_pipeline.py` first, the charts are drawn")
        print("from the built tables, never from numbers typed in by hand.")
        return 1

    # The committed sample holds SOURCE tables only; the mart and everything
    # downstream is rebuilt rather than shipped, so `--sample` points at a
    # database with no `dim_account` in it. Checked here with a clear message,
    # because the alternative is a DuckDB catalog error forty lines into a
    # traceback that says nothing about what to do next.
    required = ["dim_account", "ecl_portfolio", "vintage_curves", "portfolio_mi"]
    probe = db.connect(config, path, read_only=True)
    try:
        present = set(db.table_names(probe))
    finally:
        probe.close()
    missing = [name for name in required if name not in present]
    if missing:
        print(f"{path} has no {', '.join(missing)}.")
        print()
        print("The charts are drawn from built tables, and this database holds")
        print("only the source ones, the mart is rebuilt, never committed.")
        print("Build it first:")
        print(f"    python scripts/run_pipeline.py"
              f"{' --sample' if args.sample else ''}")
        return 1

    output = REPO_ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)

    connection = db.connect(config, path, read_only=True)
    try:
        accounts = db._count(connection, "dim_account")
        reporting = config["portfolio"]["reporting_date"]
        stamp = (f"credit-risk-ecl-engine · reporting date {reporting} · "
                 f"{accounts: } accounts · regenerate with "
                 f"python scripts/make_charts.py")

        print(f"Drawing from {path}")
        written = [
            chart_vintage_triangle(connection, stamp, output),
            chart_calendar_vs_cohort(connection, stamp, output),
            chart_ecl_and_cliff(connection, config, stamp, output),
        ]
    finally:
        connection.close()

    for item in written:
        print(f"  wrote {item.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
