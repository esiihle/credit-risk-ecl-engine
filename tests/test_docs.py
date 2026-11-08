"""
Tests on the documentation itself.

This project's stated rule is that no figure appears in a document unless a
command regenerates it. That rule was written after my scorecard project published
numbers typed into a comment that were wrong by a factor of three and survived
a green suite for two days, and it is worth nothing unless something checks it.

Nothing did, until the Phase 5 roadmap update claimed "the README's phase table
and test count are checked by the test suite" and the claim turned out to be
false. The file had been stale for four phases: Phases 1 to 4 were built,
tested and committed while the roadmap still listed Phase 1 as `next`.

So this file exists to make that claim true.

WHAT THIS FILE MUST NOT DO. My scorecard project's version of this file asserted
`"0.2751" in readme`, it hardcoded the very numbers it was supposed to verify,
which meant that when the README was wrong, the test DEFENDED the error rather
than catching it. So there are no numeric literals from the documents here.
Every figure is derived from the code or the other document and compared.

Run with:  pytest tests/test_docs.py -v
"""
import ast
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TESTS_DIR = REPO_ROOT / "tests"
README = REPO_ROOT / "README.md"
CHARTS = REPO_ROOT / "docs" / "charts"
CHART_SCRIPT = REPO_ROOT / "scripts" / "make_charts.py"
ROADMAP = REPO_ROOT / "docs" / "ROADMAP.md"


def count_test_functions():
    """Every `test_*` function in `tests/`, counted from the source.

    Counted by parsing rather than by running pytest in a subprocess: the count
    has to be available without recursively invoking the suite that contains
    this test. Exact only while no test is parametrised, which the test below
    asserts, if a `parametrize` ever appears, the collected count diverges
    from the function count and this measure would silently become wrong.
    """
    total = 0
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        total += sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_"))
    return total


def test_no_test_is_parametrised_so_the_count_is_exact():
    """Guards the measure the next test depends on.

    If this ever fails, the fix is to count collected tests with
    `pytest --collect-only` rather than to adjust the number in the README,
    the problem would be that the function count had stopped meaning the test
    count, not that the README was wrong.
    """
    # Matched as a DECORATOR, not as a word. The first version searched for
    # the bare string and failed on this very file, which mentions parametrize
    # twice in its own prose, a check that could not tell a use from a
    # mention, which is this project's recurring error in miniature.
    decorator = re.compile(r"^\s*@(?:pytest\.)?mark\.parametrize", re.M)
    offenders = [p.name for p in TESTS_DIR.glob("test_*.py")
                 if decorator.search(p.read_text(encoding="utf-8"))]
    assert not offenders, (
        f"{offenders} use parametrize, so counting functions no longer gives "
        f"the collected test count, see this test's docstring")


def test_the_readme_test_count_matches_the_actual_number_of_tests():
    """The README advertises a test count. It has to be the real one.

    A count that drifts is a small lie in the one place a reviewer is most
    likely to check first, and it costs the reader their trust in every other
    figure in the document.
    """
    readme = README.read_text(encoding="utf-8")
    actual = count_test_functions()

    claimed = set(int(n) for n in re.findall(r"(\d+)\s+tests?\b", readme))
    assert claimed, "the README no longer states a test count"
    assert claimed == {actual}, (
        f"README claims {sorted(claimed)} tests, the suite has {actual}")


def test_the_readme_and_the_roadmap_agree_on_which_phases_are_done():
    """The drift that prompted this file.

    Two documents list the same nine phases, and for four phases they
    disagreed. Neither was generated, so nothing caught it. Comparing them to
    each other catches the next one without either having to be authoritative.
    """
    done_in_readme = _phases_marked_done(README.read_text(encoding="utf-8"))
    done_in_roadmap = _phases_marked_done(ROADMAP.read_text(encoding="utf-8"))

    assert done_in_readme, "no phases are marked done in the README"
    assert done_in_readme == done_in_roadmap, (
        f"README says phases {sorted(done_in_readme)} are done, "
        f"ROADMAP says {sorted(done_in_roadmap)}")


def test_the_phases_marked_done_are_contiguous_from_zero():
    """A gap would mean a phase was skipped or a mark was mistyped.

    Phases build on each other, Phase 5's exposure schedule joins Phase 4's
    per-account PD, so "0,1,2,4 done" is not a state this project can be in.
    """
    done = _phases_marked_done(README.read_text(encoding="utf-8"))
    assert done == set(range(max(done) + 1)), (
        f"phases marked done are not contiguous: {sorted(done)}")


def test_every_phase_marked_done_has_a_methodology_section():
    """A phase is not finished when the code runs; it is finished when it is
    written up. The methodology document is the portfolio deliverable, and a
    phase marked done with nothing written about it is the easiest kind of gap
    to leave and the most expensive one to discover in an interview.

    Phase 0 is exempt: it is scaffolding, and the README covers it.
    """
    methodology = (REPO_ROOT / "docs" / "methodology.md").read_text(encoding="utf-8")
    documented = set(int(n) for n in re.findall(r"^##\s+(\d+)\.", methodology, re.M))
    done = _phases_marked_done(README.read_text(encoding="utf-8")) - {0}

    missing = done - documented
    assert not missing, (
        f"phases {sorted(missing)} are marked done but have no methodology "
        f"section; documented sections are {sorted(documented)}")


def _phases_marked_done(text):
    """Phase numbers whose table row says done, from a markdown phase table.

    Matches a leading `| **4** |` or `| 4 |` cell on a row whose final cell
    contains "done". Deliberately tolerant of the bolding, since the two
    documents bold differently and the bolding is not the fact being checked.
    """
    done = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        number = re.fullmatch(r"\*{0,2}(\d+)\*{0,2}", cells[0])
        if number and "done" in cells[-1].lower():
            done.add(int(number.group(1)))
    return done


# --- Charts obey the same rule as numbers ----------------------------------
#
# "No figure appears in a document unless a command regenerates it" is easy to
# keep for numbers, which are at least visible in a diff, and easy to break for
# images, which keep showing yesterday's data indefinitely while nothing
# complains. These two tests are the enforceable half of the rule.

def test_every_image_a_document_references_actually_exists():
    """A broken image is invisible in a local editor and obvious on GitHub.

    Checked across the documents rather than just the README, because the
    methodology is the one most likely to grow figures and least likely to be
    previewed before committing.
    """
    missing = []
    for document in [README] + sorted((REPO_ROOT / "docs").glob("*.md")):
        text = document.read_text(encoding="utf-8")
        for target in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text):
            if target.startswith(("http://", "https://")):
                continue
            # README links are repo-relative; docs/ links resolve from docs/.
            candidates = [REPO_ROOT / target, document.parent / target]
            if not any(path.exists() for path in candidates):
                missing.append(f"{document.name} -> {target}")
    assert not missing, f"referenced images do not exist: {missing}"


def test_every_committed_chart_is_one_the_chart_script_produces():
    """Nothing hand-made may sit in docs/charts/.

    An image exported by hand looks identical to a generated one and cannot be
    refreshed by rerunning anything, so it is exactly the figure that goes
    stale. Matching the directory's contents against the filenames the script
    writes keeps the directory honest without running matplotlib in CI.
    """
    if not CHARTS.exists():
        pytest.skip("no charts generated in this working copy")

    source = CHART_SCRIPT.read_text(encoding="utf-8")
    produced = set(re.findall(r'output\s*/\s*"([^"]+\.png)"', source))
    assert produced, "the chart script no longer names any output files"

    committed = {path.name for path in CHARTS.glob("*.png")}
    orphans = committed - produced
    assert not orphans, (
        f"{sorted(orphans)} are in docs/charts/ but scripts/make_charts.py "
        f"does not produce them, a chart nothing regenerates will go stale")
