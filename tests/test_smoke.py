"""
Phase 0 smoke tests.

These do not test the ECL mathematics — those tests arrive with each phase.
They check the scaffolding holds together: the config loads and its invariants
are enforced, every module imports, the warehouse opens, and the bundled sample
is present and usable. A green run here means a fresh clone is wired correctly.

Run with:  pytest
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config, set_seed, ConfigError
from src import db, generate


def test_config_loads_and_has_the_blocks_the_project_needs():
    config = load_config()
    for block in ("data", "portfolio", "generator", "staging", "ecl",
                  "validation", "reconciliation"):
        assert block in config, f"config.yaml is missing the '{block}' block"


def test_segment_shares_must_sum_to_one():
    """Every account belongs to exactly one segment, so the shares partition."""
    config = load_config()
    config["portfolio"] = dict(config["portfolio"])
    config["portfolio"]["segments"] = {"only": {"share": 0.7, "risk_multiplier": 1.0}}

    from src.config import _validate
    with pytest.raises(ConfigError, match="sum to"):
        _validate(config)


def test_sicr_must_trigger_before_default():
    """If the 30-day backstop were above the default threshold, Stage 2 would
    be unreachable and every account would jump from Stage 1 to Stage 3."""
    config = load_config()
    config["staging"] = dict(config["staging"], sicr_dpd_backstop=120)

    from src.config import _validate
    with pytest.raises(ConfigError, match="BEFORE default"):
        _validate(config)


def test_transition_probabilities_cannot_exceed_certainty():
    config = load_config()
    config["generator"] = dict(config["generator"])
    config["generator"]["base_transitions"] = dict(
        config["generator"]["base_transitions"], cure_from_1=0.8, worsen_from_1=0.5)

    from src.config import _validate
    with pytest.raises(ConfigError, match="no probability left"):
        _validate(config)


def test_the_warehouse_opens_and_describes_itself(tmp_path):
    config = load_config()
    connection = db.connect(config, tmp_path / "test.duckdb")
    assert "empty" in db.describe(connection)

    rng = set_seed(1)
    small = dict(config)
    small["portfolio"] = dict(config["portfolio"], n_accounts=200)
    accounts, performance, truth = generate.generate_book(small, rng)

    assert db.write_table(connection, "accounts", accounts) == 200
    assert db.write_table(connection, "loan_month", performance) == len(performance)
    assert db.write_table(connection, "ground_truth", truth) == len(truth)

    summary = db.describe(connection)
    for table in db.CORE_TABLES:
        assert table in summary
    assert "NOT BUILT YET" not in summary
    connection.close()


def test_writing_an_empty_table_is_refused(tmp_path):
    """An empty table is almost always a bug upstream, and it is far easier to
    diagnose here than three stages later when a join returns nothing."""
    import pandas as pd
    config = load_config()
    connection = db.connect(config, tmp_path / "test.duckdb")
    with pytest.raises(db.DatabaseError, match="empty"):
        db.write_table(connection, "nothing", pd.DataFrame())
    connection.close()


def test_a_missing_database_says_how_to_build_one(tmp_path):
    config = load_config()
    with pytest.raises(db.DatabaseError, match="generate_book"):
        db.connect(config, tmp_path / "absent.duckdb", read_only=True)


def test_the_bundled_sample_is_committed_and_usable():
    """A fresh clone must be able to run something without generating a book."""
    config = load_config()
    sample = REPO_ROOT / config["data"]["sample_path"]
    assert sample.exists(), (
        f"{sample} is missing. It is committed on purpose so a clone works "
        "immediately — rebuild with `python scripts/generate_book.py --sample`"
    )

    connection = db.connect(config, sample, read_only=True)
    present = db.table_names(connection)
    for table in db.CORE_TABLES:
        assert table in present, f"the sample has no '{table}' table"
    assert db._count(connection, "accounts") > 0
    connection.close()


def test_git_ignores_the_working_data_but_not_the_committed_sample():
    """Asks git, rather than reading .gitignore and reasoning about it.

    A sister project wrote `data/*` and silently excluded its own committed
    sample; a fresh clone then crashed. This project nearly repeated it with a
    blanket `*.duckdb` rule, which would have ignored the sample database by
    extension. String-matching the rules file would not reliably catch either
    — the only authority on what git ignores is git.
    """
    import subprocess

    # `git check-ignore` exits 128 outside a work tree, which is neither
    # "ignored" nor "not ignored". Treating 128 as "not ignored" is how the
    # first version of this test failed confusingly for anyone running pytest
    # from an unzipped folder before `git init` — the test reported that the
    # working warehouse was version-controlled, which was not the problem.
    inside_repo = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT, capture_output=True)
    if inside_repo.returncode != 0:
        pytest.skip(
            "not inside a git work tree, so there is nothing for git to "
            "ignore. This is expected when running from an unzipped folder "
            "before `git init`."
        )

    def ignored(path):
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=REPO_ROOT, capture_output=True)
        if result.returncode not in (0, 1):
            pytest.fail(
                f"git check-ignore failed unexpectedly on {path}: "
                f"{result.stderr.decode().strip()}"
            )
        return result.returncode == 0

    config = load_config()
    sample = Path(config["data"]["sample_path"])
    assert not ignored(sample), (
        f"{sample} is git-ignored, so a fresh clone would not have it and "
        "nothing would run out of the box"
    )

    assert ignored(Path(config["data"]["database"])), (
        "the working warehouse should not be version-controlled — it is "
        "reproducible from the seed"
    )
    assert ignored(Path("data/raw/anything.csv"))
    assert ignored(Path("data/processed/anything.parquet"))


def test_running_the_pipeline_does_not_modify_the_committed_sample():
    """`--sample` must work on a copy.

    The sample book is version-controlled. Building the mart into it would
    leave the repository dirty after every run, and eventually someone commits
    the derived tables — which then go stale against the SQL that produced
    them, with nothing to say so.
    """
    import re
    source = (REPO_ROOT / "scripts" / "run_pipeline.py").read_text()

    assert "shutil.copy" in source, (
        "run_pipeline.py --sample should copy the sample before building"
    )
    # And the copy must land outside data/sample/.
    assert re.search(r'processed_dir.*sample_run', source, re.S), (
        "the working copy should go into data/processed/, not beside the "
        "committed sample"
    )


def test_the_committed_sample_holds_source_tables_only():
    """Derived tables are rebuilt, never shipped.

    A committed mart would be a second source of truth that no command
    regenerates — the exact failure mode this portfolio has already had with
    numbers typed into comments.
    """
    config = load_config()
    sample = REPO_ROOT / config["data"]["sample_path"]
    connection = db.connect(config, sample, read_only=True)
    present = set(db.table_names(connection))
    connection.close()

    assert present == set(db.CORE_TABLES), (
        f"the sample should hold exactly {sorted(db.CORE_TABLES)}, found "
        f"{sorted(present)}. Rebuild with "
        "`python scripts/generate_book.py --sample`"
    )
