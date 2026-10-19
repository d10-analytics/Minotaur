"""Behavioral proofs for the baseline/branch equivalence instrument."""

from __future__ import annotations

import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from minotaur.graph_model.provenance import NodeClass, RelationshipKind
from minotaur.language_interpreter.python import analyze_python_files
from minotaur.language_interpreter.workspace import Workspace

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_equivalence.py"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "equivalence_root"
JAVASCRIPT_ROOT = ROOT / "tests" / "fixtures" / "analyzer_semantics" / "javascript"
SQL_ROOT = ROOT / "tests" / "fixtures" / "analyzer_semantics" / "sql"
# Workload keys whose roots are Python; language-specific workload rules are
# asserted on these only.
PYTHON_ROOT_KEYS = ("equivalence_root", "src", "Onyx")
# Compare against the final source behavior, including analyzer version stamps
# and the declaration roles recorded on accessor and overload symbol nodes.
BASELINE_COMMIT = "741bb5fe612fa161b42ab73a6e0eac5ce444db50"
# Freeze the source validated by the historical viewer and navigation comparisons.
PRE_STAMP_COMMIT = "c43c45d60605e75ee9013580ba7a59fbf5af88dd"
VIEWER_ADVANCE_COMMIT = "52a8b29d376e99c582d686dbead579acc438de37"
# Preserve the historical fixture provenance independently of output revisions.
FIXTURE_PARENT_COMMIT = "d32d4c9ecf1f25839c5055d37bb5fc970d28e77b"


@pytest.fixture(scope="session")
def session_clone(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A throwaway clone whose ``.git`` owns every harness worktree (``D-11``).

    ``git worktree add`` registers a worktree in whichever repository owns the
    checkout.  Doing it from the developer checkout leaves dangling
    registrations in the user's ``.git`` when a session is interrupted, so the
    tests clone once and register their worktrees in the clone instead.
    """

    clone = tmp_path_factory.mktemp("equivalence-clone") / "checkout"
    created = subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(clone)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode:
        raise RuntimeError(f"could not clone the repository: {created.stderr}")
    yield clone
    shutil.rmtree(clone, ignore_errors=True)


@pytest.fixture(scope="session")
def baseline_src(session_clone: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Materialise a throwaway git worktree of the Baseline commit."""

    checkout = tmp_path_factory.mktemp("equivalence-baseline") / "checkout"
    added = subprocess.run(
        [
            "git",
            "-C",
            str(session_clone),
            "worktree",
            "add",
            "--detach",
            str(checkout),
            BASELINE_COMMIT,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode:
        raise RuntimeError(f"could not create baseline worktree: {added.stderr}")
    try:
        yield checkout / "src"
    finally:
        subprocess.run(
            ["git", "-C", str(session_clone), "worktree", "remove", "--force", str(checkout)],
            capture_output=True,
            text=True,
            check=False,
        )
        # An interrupted session can leave the registration behind after the
        # temporary directory is reaped; prune so the clone's ``.git`` never
        # carries a dangling worktree entry from a test run.
        subprocess.run(
            ["git", "-C", str(session_clone), "worktree", "prune"],
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture(scope="session")
def harness() -> types.ModuleType:
    """Import the harness script itself for its unit-level guarantees."""

    loader = importlib.util.spec_from_file_location("equivalence_harness", SCRIPT)
    assert loader is not None and loader.loader is not None
    module = importlib.util.module_from_spec(loader)
    sys.modules["equivalence_harness"] = module
    loader.loader.exec_module(module)
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def _workload_file(
    tmp_path: Path,
    key: str,
    *,
    selection: str = "app.py",
    queries: list[dict[str, object]] | None = None,
) -> Path:
    """Write a per-root query workload keyed by the root directory's name."""

    if queries is None:
        queries = [
            {
                "name": "definitions-main",
                "command": "definitions",
                "args": ["main"],
                "variants": {"json": True, "no_refresh": True},
            }
        ]
    path = tmp_path / f"queries-{key}.json"
    path.write_text(
        json.dumps({key: {"selection": selection, "queries": queries}}), encoding="utf-8"
    )
    return path


def _source_root(tmp_path: Path, name: str = "source") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    return root


def test_identical_tree_guard_runs_before_import_or_provenance(baseline_src: Path) -> None:
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(baseline_src),
    )
    assert result.returncode == 1
    assert "identical trees" in result.stderr


def test_import_provenance_guard_rejects_a_tree_that_shadows_nothing(
    baseline_src: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "empty-src"
    empty.mkdir()
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(empty),
    )
    assert result.returncode == 1
    assert "minotaur imported from" in result.stderr
    assert f"outside {empty.resolve()}" in result.stderr
    assert "not a repository checkout" not in result.stderr


def test_self_test_detects_every_injection_class_on_the_javascript_root(
    baseline_src: Path,
) -> None:
    """One fixture-scoped run serves every self-test assertion (``R-08``).

    Each injection must produce its own differing row, so a harness that had
    stopped comparing HTML, query output, Drift, hash-ordered output or the
    JavaScript interpretation could not still print PASS.  The JavaScript
    fixture root keeps the run small while making the JavaScript semantics
    injection live rather than skipped.
    """

    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--root",
        str(JAVASCRIPT_ROOT),
        "--self-test",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("serialize", "visualize", "query", "javascript", "drift", "hash-order"):
        assert f"self-test: injection {name} detected" in result.stdout
    differing = [line for line in result.stdout.splitlines() if line.endswith(": DIFFERENT")]
    root = JAVASCRIPT_ROOT.resolve()
    assert any(f"root={root} artifact=analyze graph SHA-256" in line for line in differing)
    assert any(f"root={root} artifact=visualize HTML SHA-256" in line for line in differing)
    assert any("query=" in line for line in differing)
    assert any(f"root={root} artifact=Drift" in line for line in differing)
    assert any("seed-determinism" in line for line in differing)
    assert "self-test: PASS" in result.stdout
    assert "self-test mode: provenance guard skipped" in result.stderr


def test_self_test_fails_when_an_injection_no_longer_perturbs_output(
    harness: types.ModuleType,
    baseline_src: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonzero exit is not evidence that the compared artifact diverged.

    An injection that stopped applying -- a renamed helper, a moved module --
    breaks the import guard instead, which exits nonzero for a reason that has
    nothing to do with byte identity.  The self-test must reject that.
    """

    def _break_import(src: Path) -> None:
        (src / "minotaur" / "__init__.py").write_text("raise ImportError\n", encoding="utf-8")

    root = _source_root(tmp_path)
    arguments = harness._parser().parse_args(
        [
            "--baseline-src",
            str(baseline_src),
            "--branch-src",
            str(ROOT / "src"),
            "--queries",
            str(_workload_file(tmp_path, root.name)),
            "--root",
            str(root),
            "--self-test",
        ]
    )
    marker = "artifact=analyze graph SHA-256: DIFFERENT"
    monkeypatch.setattr(harness, "INJECTIONS", (("stale", _break_import, marker),))
    result = harness._self_test(arguments)
    captured = capsys.readouterr()
    assert result == 1
    assert "self-test: PASS" not in captured.out
    assert "no longer perturbs what the harness compares" in captured.err


def test_clean_equal_heads_in_distinct_worktrees_are_refused(
    session_clone: Path, baseline_src: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "same-head"
    added = subprocess.run(
        [
            "git",
            "-C",
            str(session_clone),
            "worktree",
            "add",
            "--detach",
            str(worktree),
            BASELINE_COMMIT,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode:
        pytest.skip(f"git worktree unavailable: {added.stderr}")
    try:
        result = _run(
            "--baseline-src",
            str(baseline_src),
            "--branch-src",
            str(worktree / "src"),
        )
        assert result.returncode == 1
        assert "same clean HEAD" in result.stderr
        assert str(baseline_src) in result.stderr
        assert str(worktree / "src") in result.stderr
    finally:
        subprocess.run(
            [
                "git",
                "-C",
                str(session_clone),
                "worktree",
                "remove",
                "--force",
                str(worktree),
            ],
            capture_output=True,
            text=True,
            check=False,
        )


def test_plain_source_copy_is_rejected_by_provenance_guard(
    baseline_src: Path, tmp_path: Path
) -> None:
    copied = tmp_path / "copy"
    shutil.copytree(baseline_src, copied)
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(copied),
    )
    assert result.returncode == 1
    assert "not a repository checkout" in result.stderr


def test_committed_fixture_root_has_a_python_substrate_for_every_query_class() -> None:
    """Root (1) must be code, not an artifact directory.

    The harness compared two empty graphs when root (1) was a checked-in graph
    with no ``.py`` file beside it.
    """

    sources = sorted(path.name for path in FIXTURE_ROOT.rglob("*.py"))
    assert sources, "the fixture root must contain Python sources"
    workloads = json.loads((ROOT / "scripts/equivalence_queries.json").read_text())
    fixture = workloads[FIXTURE_ROOT.name]
    commands = {entry["command"] for entry in fixture["queries"]}
    assert commands == {"definitions", "callers", "impact", "unreferenced", "context", "diff"}
    assert (FIXTURE_ROOT / fixture["selection"]).is_file()


def test_root_star_fixture_is_a_non_vacuous_module_only_import_fact() -> None:
    """The committed root-star source is analyzed as a real graph fact."""

    result = analyze_python_files(
        Workspace(FIXTURE_ROOT),
        (FIXTURE_ROOT / "root_star.py",),
    )
    assert result.diagnostics == ()
    assert result.document.extensions == {
        "minotaur-python": {
            "imports_resolved": 0,
            "imports_root_mismatched": 0,
            "imports_unresolved": 1,
        }
    }
    labels = {node.id: node.label for node in result.document.nodes}
    module = next(
        node
        for node in result.document.nodes
        if node.node_class == NodeClass.SYMBOL and node.label == "root_star"
    )
    unresolved = [
        node
        for node in result.document.nodes
        if node.node_class == NodeClass.UNRESOLVED_REFERENCE and node.reference_text == "."
    ]
    assert len(unresolved) == 1
    assert not any(
        node.node_class == NodeClass.UNRESOLVED_REFERENCE and node.reference_text != "."
        for node in result.document.nodes
    )
    reference = next(
        relationship
        for relationship in result.document.relationships
        if relationship.source == module.id
        and relationship.target == unresolved[0].id
        and relationship.kind == RelationshipKind.REFERENCES.value
    )
    assert labels[reference.target] == "."
    assert reference.evidence[0].locations == (unresolved[0].location,)
    assert unresolved[0].location is not None
    assert unresolved[0].location.path == "root_star.py"
    assert unresolved[0].location.range.start.line == 0


def test_fixture_pin_is_a_full_sha_and_fixture_commit_is_its_immediate_child() -> None:
    """The fixture baseline remains tied to the reviewed behavior head."""

    assert len(BASELINE_COMMIT) == 40
    resolved = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", BASELINE_COMMIT],
        capture_output=True,
        text=True,
        check=False,
    )
    assert resolved.returncode == 0
    assert resolved.stdout.strip() == BASELINE_COMMIT

    added = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "log",
            "--diff-filter=A",
            "--format=%H",
            "-1",
            "--",
            "tests/fixtures/equivalence_root/root_star.py",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert added.returncode == 0
    fixture_commit = added.stdout.strip()
    assert fixture_commit
    parent = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", f"{fixture_commit}^"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert parent.returncode == 0
    assert parent.stdout.strip() == FIXTURE_PARENT_COMMIT
    ancestor = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "merge-base",
            "--is-ancestor",
            FIXTURE_PARENT_COMMIT,
            BASELINE_COMMIT,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ancestor.returncode == 0


def _non_diff_workload(
    tmp_path: Path, *, exclude_names: frozenset[str] = frozenset()
) -> tuple[Path, dict[str, object]]:
    """Strip ``diff`` for the historical baseline-advance comparisons.

    Those tests compare two pre-pin commits whose ``diff`` exit status and
    envelope predate the committed ``difference`` expectation, so keeping
    ``diff`` in would make them compare an unrelated contract change.
    """

    source_workload = json.loads(
        (ROOT / "scripts" / "equivalence_queries.json").read_text(encoding="utf-8")
    )
    fixture_workload = source_workload[FIXTURE_ROOT.name]
    fixture_workload["queries"] = [
        item
        for item in fixture_workload["queries"]
        if item.get("command") != "diff" and item.get("name") not in exclude_names
    ]
    workload_path = tmp_path / "equivalence-without-diff.json"
    workload_path.write_text(json.dumps(source_workload), encoding="utf-8")
    return workload_path, fixture_workload


def test_every_non_control_query_answers_on_the_committed_fixture_root(
    baseline_src: Path,
) -> None:
    """The committed workload runs whole, including its expected difference.

    ``query diff`` answers with exit 1 and output for a whole-root selection
    against a narrower one, so the committed ``diff-selection`` entry declares
    ``difference`` and counts as an answer rather than a vacuous run (``D-09``).
    """

    fixture_workload = json.loads(
        (ROOT / "scripts" / "equivalence_queries.json").read_text(encoding="utf-8")
    )[FIXTURE_ROOT.name]

    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--root",
        str(FIXTURE_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VACUOUS" not in result.stderr
    summary = next(line for line in result.stdout.splitlines() if " summary: " in line)
    answered, expected = summary.split("queries_answered=")[1].split("/")
    workload = fixture_workload
    diff_entries = [item for item in workload["queries"] if item.get("expect") == "difference"]
    assert len(diff_entries) == 1 and diff_entries[0]["command"] == "diff"
    non_answers = sum(1 for item in workload["queries"] if item.get("expect") in {"empty", "error"})
    assert non_answers >= 4
    assert int(answered) == int(expected) - non_answers, summary
    assert int(expected) == len(workload["queries"]), summary
    assert "DIFFERENT" not in result.stdout


def test_default_run_compares_every_language_root(baseline_src: Path) -> None:
    """No ``--root`` compares the fixture and baseline ``src`` per language plus
    the JavaScript and SQL sample roots, and exits 0 at the pin (``R-01``)."""

    workloads = json.loads((ROOT / "scripts" / "equivalence_queries.json").read_text())
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VACUOUS" not in result.stderr
    assert "FAILED" not in result.stdout
    assert "DIFFERENT" not in result.stdout
    assert "roots_compared=4 roots_present=4 roots_requested=4" in result.stdout

    roots = (
        (FIXTURE_ROOT, "equivalence_root"),
        (baseline_src, "src"),
        (JAVASCRIPT_ROOT, "javascript"),
        (SQL_ROOT, "sql"),
    )
    for root, key in roots:
        assert f"root={root.resolve()} artifact=analyze graph SHA-256: IDENTICAL" in result.stdout
        summary = next(
            line for line in result.stdout.splitlines() if f"root={root} summary:" in line
        )
        answered, expected = summary.split("queries_answered=")[1].split("/")
        queries = workloads[key]["queries"]
        non_answers = sum(1 for item in queries if item.get("expect") in {"empty", "error"})
        assert int(expected) == len(queries), summary
        assert int(answered) == len(queries) - non_answers, summary


def test_a_root_that_fails_is_not_counted_as_compared(baseline_src: Path, tmp_path: Path) -> None:
    """``totals:`` counts a root only when its comparison succeeded (``R-01``)."""

    good = _source_root(tmp_path, name="good-root")
    bad = _source_root(tmp_path, name="bad-root")
    workload = tmp_path / "mixed-roots.json"
    workload.write_text(
        json.dumps(
            {
                good.name: {
                    "selection": "app.py",
                    "queries": [
                        {
                            "name": "definitions-main",
                            "command": "definitions",
                            "args": ["main"],
                            "variants": {"json": True, "no_refresh": True},
                        }
                    ],
                },
                bad.name: {
                    "selection": "app.py",
                    "queries": [
                        {
                            "name": "definitions-missing",
                            "command": "definitions",
                            "args": ["not a valid symbol"],
                            "variants": {"no_refresh": True},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(workload),
        "--root",
        str(good),
        "--root",
        str(bad),
    )
    assert result.returncode == 1
    assert "query=definitions-missing" in result.stderr
    assert "VACUOUS expected=ok" in result.stderr
    assert "only 1 of 2 present roots were compared" in result.stderr
    assert "roots_compared=1 roots_present=2 roots_requested=2" in result.stdout


def test_equivalence_side_diff_selection_uses_exit_one_for_valid_difference(
    harness: types.ModuleType, tmp_path: Path
) -> None:
    """The harness-side explicit diff expectation follows deliberate exits."""
    root = tmp_path / "diff-root"
    root.mkdir()
    source = root / "app.py"
    source.write_text("def initial():\n    pass\n", encoding="utf-8")
    side = harness.Side("branch", ROOT / "src")
    old_graph = root / "old.json"
    new_graph = root / "new.json"
    assert harness._analyze(side, root, old_graph).returncode == 0
    source.write_text("def initial():\n    pass\n\ndef added():\n    pass\n", encoding="utf-8")
    assert harness._analyze(side, root, new_graph).returncode == 0
    result = harness._run(
        side,
        ["-m", "minotaur", "query", "diff", str(old_graph), str(new_graph)],
        cwd=root,
    )
    assert result.returncode == 1
    assert b"added" in result.stdout


def test_difference_expectation_is_validated_and_only_for_diff(
    harness: types.ModuleType, tmp_path: Path
) -> None:
    """``difference`` is the diff command's own exit-1 answer (``D-09``)."""

    def workload(expect: str, command: str) -> Path:
        path = tmp_path / f"queries-{expect}-{command}.json"
        path.write_text(
            json.dumps(
                {
                    "root": {
                        "selection": "app.py",
                        "queries": [
                            {"name": "entry", "command": command, "args": [], "expect": expect}
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        return path

    loaded = harness._load_queries(workload("difference", "diff"))
    assert loaded["root"].queries[0]["expect"] == "difference"
    with pytest.raises(ValueError, match="difference from a non-diff command"):
        harness._load_queries(workload("difference", "definitions"))

    differing = harness.Completed(1, b"removed x\n", b"")
    assert harness._differing(differing)
    assert not harness._differing(harness.Completed(0, b"no changes\n", b""))
    assert not harness._differing(harness.Completed(1, b"", b""))
    assert not harness._differing(harness.Completed(1, b"x\n", b"", timed_out=True))


def test_child_environment_threads_the_hash_seed(harness: types.ModuleType) -> None:
    """The self-determinism cell needs a second seed in the child (``D-07``)."""

    assert harness._env(Path("/src"))["PYTHONHASHSEED"] == "0"
    assert harness._env(Path("/src"), hash_seed=0)["PYTHONHASHSEED"] == "0"
    assert harness._env(Path("/src"), hash_seed=7)["PYTHONHASHSEED"] == "7"


def test_seed_determinism_cell_reruns_the_branch_side_under_another_seed(
    harness: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cell is self-determinism: one side, two seeds, equal bytes."""

    side = harness.Side("branch", ROOT / "src")
    probes = [
        harness.SeedProbe(
            label=f"root={FIXTURE_ROOT} query=definitions variant=human",
            args=["-m", "minotaur", "query", "definitions", "main"],
            seed_zero=harness.Completed(0, b"answer\n", b""),
        )
    ]
    seen: list[tuple[str, int]] = []

    def matching(side, args, *, cwd=None, timeout=300, hash_seed=0):
        seen.append((side.name, hash_seed))
        return harness.Completed(0, b"answer\n", b"")

    monkeypatch.setattr(harness, "_run", matching)
    assert harness._compare_seed_determinism(side, FIXTURE_ROOT, probes)
    assert seen == [("branch", harness.SELF_DETERMINISM_SEED)]

    def diverging(side, args, *, cwd=None, timeout=300, hash_seed=0):
        return harness.Completed(0, b"other\n", b"")

    monkeypatch.setattr(harness, "_run", diverging)
    assert not harness._compare_seed_determinism(side, FIXTURE_ROOT, probes)


def test_a_query_that_produces_no_output_fails_the_run(baseline_src: Path, tmp_path: Path) -> None:
    """Two identical error messages are not evidence of equivalence."""

    root = _source_root(tmp_path)
    queries = _workload_file(
        tmp_path,
        root.name,
        queries=[
            {
                "name": "callers-missing",
                "command": "callers",
                "args": ["app.this_symbol_does_not_exist"],
                "variants": {"json": True},
            }
        ],
    )
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(queries),
        "--root",
        str(root),
    )
    assert result.returncode == 1
    assert "VACUOUS expected=ok" in result.stderr
    assert "query=callers-missing" in result.stderr
    assert "DIFFERENT" not in result.stdout


def test_a_negative_control_that_stops_erroring_fails_the_run(
    baseline_src: Path, tmp_path: Path
) -> None:
    """The intentional error query must keep being the error path."""

    root = _source_root(tmp_path)
    queries = _workload_file(
        tmp_path,
        root.name,
        queries=[
            {
                "name": "definitions-main",
                "command": "definitions",
                "args": ["main"],
                "expect": "error",
                "variants": {},
            }
        ],
    )
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(queries),
        "--root",
        str(root),
    )
    assert result.returncode == 1
    assert "VACUOUS expected=error" in result.stderr


def test_a_root_without_a_query_workload_fails(baseline_src: Path, tmp_path: Path) -> None:
    root = _source_root(tmp_path, name="unlisted")
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, "some-other-root")),
        "--root",
        str(root),
    )
    assert result.returncode == 1
    assert "no query workload for root" in result.stderr
    assert "no artifact comparisons were performed" in result.stderr


def test_a_missing_required_root_fails_instead_of_being_skipped(
    baseline_src: Path, tmp_path: Path
) -> None:
    """A typo in ``--root`` used to print 'skipped' and exit 0."""

    present = _source_root(tmp_path)
    missing = tmp_path / "not-there"
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, present.name)),
        "--root",
        str(present),
        "--root",
        str(missing),
    )
    assert result.returncode == 1
    assert f"required root does not exist: {missing.resolve()}" in result.stderr


def test_a_missing_optional_root_is_skipped_loudly(baseline_src: Path, tmp_path: Path) -> None:
    """Only the documented optional root (D-06 root (3)) may be absent."""

    present = _source_root(tmp_path)
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, present.name)),
        "--root",
        str(present),
        "--optional-root",
        str(tmp_path / "onyx"),
    )
    assert result.returncode == 0, result.stderr
    assert "SKIPPED optional root" in result.stderr


def test_a_present_optional_root_without_a_workload_fails_the_run(
    baseline_src: Path, tmp_path: Path
) -> None:
    """Round 2 keyed workloads by directory name and made a *present* optional
    root abort the run, so D-06 root (3) was silently never compared."""

    present = _source_root(tmp_path)
    onyx = _source_root(tmp_path, name="onyx-checkout")
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, present.name)),
        "--root",
        str(present),
        "--optional-root",
        str(onyx),
    )
    assert result.returncode == 1
    assert "no query workload for root" in result.stderr
    assert "pass --workload KEY" in result.stderr
    assert "only 1 of 2 present roots were compared" in result.stderr
    assert "roots_compared=1 roots_present=2 roots_requested=2" in result.stdout


def test_workload_key_maps_an_optional_root_whose_directory_has_another_name(
    baseline_src: Path, tmp_path: Path
) -> None:
    present = _source_root(tmp_path)
    onyx = _source_root(tmp_path, name="onyx-checkout")
    queries = json.loads(_workload_file(tmp_path, present.name).read_text(encoding="utf-8"))
    queries["Onyx"] = queries[present.name]
    workload = tmp_path / "keyed.json"
    workload.write_text(json.dumps(queries), encoding="utf-8")
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(workload),
        "--root",
        str(present),
        "--optional-root",
        str(onyx),
        "--workload",
        "Onyx",
    )
    assert result.returncode == 0, result.stderr
    assert f"root={onyx.resolve()} summary:" in result.stdout
    assert "roots_compared=2 roots_present=2 roots_requested=2" in result.stdout


def test_more_workload_keys_than_optional_roots_is_an_error(
    baseline_src: Path, tmp_path: Path
) -> None:
    present = _source_root(tmp_path)
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, present.name)),
        "--root",
        str(present),
        "--workload",
        "Onyx",
    )
    assert result.returncode == 1
    assert "1 --workload keys for 0 optional roots" in result.stderr


def test_language_files_treats_a_suffix_less_selection_as_a_whole_root(
    harness: types.ModuleType, tmp_path: Path
) -> None:
    """A directory/module selection has no language to expand (``AUD-01``)."""

    (tmp_path / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    assert harness._language_files(tmp_path, "instrument_reader") is None
    assert harness._language_files(tmp_path, ".") is None
    assert harness._language_files(tmp_path, "app.py") == (tmp_path / "app.py",)


def test_a_directory_selection_is_compared_as_a_whole_root(
    baseline_src: Path, tmp_path: Path
) -> None:
    """The committed ``Onyx`` workload selects a package directory.

    Before the per-language split this was a normal comparison.  Reintroducing
    a suffix check that rejects the selection aborts with an unhandled
    traceback, so this cell would fail rather than report a comparison.
    """

    root = _source_root(tmp_path, name="onyx-checkout")
    package = root / "instrument_reader"
    package.mkdir()
    (package / "__init__.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, root.name, selection="instrument_reader")),
        "--root",
        str(root),
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert "roots_compared=1 roots_present=1 roots_requested=1" in result.stdout
    assert f"root={root.resolve()} summary:" in result.stdout


def test_optional_root_with_a_directory_selection_compares(
    baseline_src: Path, tmp_path: Path
) -> None:
    """A present optional root keyed to the committed Onyx workload compares."""

    present = _source_root(tmp_path)
    onyx = _source_root(tmp_path, name="onyx-checkout")
    package = onyx / "instrument_reader"
    package.mkdir()
    (package / "__init__.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    queries = json.loads(_workload_file(tmp_path, present.name).read_text(encoding="utf-8"))
    onyx_workload = json.loads(
        (SCRIPT.with_name("equivalence_queries.json")).read_text(encoding="utf-8")
    )["Onyx"]
    # The committed selection is the suffix-less package directory.
    assert onyx_workload["selection"] == "instrument_reader"
    queries["Onyx"] = {
        "selection": onyx_workload["selection"],
        "queries": [item for item in onyx_workload["queries"] if item["command"] == "definitions"],
    }
    workload = tmp_path / "keyed-onyx.json"
    workload.write_text(json.dumps(queries), encoding="utf-8")
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(workload),
        "--root",
        str(present),
        "--optional-root",
        str(onyx),
        "--workload",
        "Onyx",
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"root={onyx.resolve()} summary:" in result.stdout
    assert "roots_compared=2 roots_present=2 roots_requested=2" in result.stdout


def test_drift_row_survives_a_scratch_path_that_collides_with_the_old_placeholders(
    baseline_src: Path, tmp_path: Path
) -> None:
    """Paths reach the drift snippet as argv, so ``ROOT``/``GRAPH`` text and a
    quote inside the scratch directory cannot rewrite the snippet."""

    present = _source_root(tmp_path)
    scratch = tmp_path / "ROOT'GRAPH"
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, present.name)),
        "--root",
        str(present),
        "--scratch",
        str(scratch),
    )
    assert result.returncode == 0, result.stderr
    assert "artifact=Drift: IDENTICAL" in result.stdout


@pytest.mark.parametrize(
    ("major", "minor", "refused"),
    [
        (3, 8, True),
        (3, 9, True),
        (3, 10, True),
        (3, 11, False),
        (3, 100, False),
        (4, 0, False),
    ],
)
def test_import_probe_runs_from_each_root_and_refuses_an_old_interpreter(
    harness: types.ModuleType,
    baseline_src: Path,
    monkeypatch: pytest.MonkeyPatch,
    major: int,
    minor: int,
    refused: bool,
) -> None:
    """The isolation proof must run in the comparison's own configuration
    (``cwd`` = the root) and reject interpreters that ignore PYTHONSAFEPATH."""

    seen: list[Path | None] = []
    real_run = harness._run

    def spy(side, args, *, cwd=None, timeout=300):
        seen.append(cwd)
        return real_run(side, args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(harness, "_run", spy)
    side = harness.Side("branch", ROOT / "src")
    assert harness._check_import(side, cwd=baseline_src) is None
    assert seen == [baseline_src]

    def reported_python(side, args, *, cwd=None, timeout=300):
        location = str(ROOT / "src" / "minotaur" / "__init__.py").encode()
        return harness.Completed(0, f"{major} {minor}\n".encode() + location, b"")

    monkeypatch.setattr(harness, "_run", reported_python)
    error = harness._check_import(side, cwd=baseline_src)
    if refused:
        assert error is not None and "PYTHONSAFEPATH" in error and "3.11+" in error
    else:
        assert error is None


def test_an_empty_human_result_is_vacuous_for_an_answering_query(
    baseline_src: Path, tmp_path: Path
) -> None:
    """Matching ``no definitions`` output is not an answered query."""

    root = _source_root(tmp_path)
    queries = _workload_file(
        tmp_path,
        root.name,
        queries=[
            {
                "name": "definitions-empty",
                "command": "definitions",
                "args": ["this symbol cannot exist"],
                "variants": {},
            }
        ],
    )
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(queries),
        "--root",
        str(root),
    )
    assert result.returncode == 1
    assert "query=definitions-empty" in result.stderr
    assert "VACUOUS expected=ok" in result.stderr


def test_an_expected_empty_result_must_be_the_command_literal(
    baseline_src: Path, tmp_path: Path
) -> None:
    root = _source_root(tmp_path)
    queries = _workload_file(
        tmp_path,
        root.name,
        queries=[
            {
                "name": "callers-empty",
                "command": "callers",
                "args": ["app.main"],
                "expect": "empty",
                "variants": {"no_refresh": True},
            }
        ],
    )
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(queries),
        "--root",
        str(root),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "queries_answered=0/1" in result.stdout


def test_visualize_failure_on_both_sides_is_not_comparable(
    harness: types.ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def successful_analyze(side, root, output, *args, **kwargs):
        output.write_bytes(b"graph")
        return harness.Completed(0, b"", b"")

    monkeypatch.setattr(harness, "_analyze", successful_analyze)
    monkeypatch.setattr(
        harness, "_run", lambda *args, **kwargs: harness.Completed(3, b"", b"broken")
    )
    monkeypatch.setattr(harness, "_compare_queries", lambda *args: True)
    monkeypatch.setattr(harness, "_compare_drift", lambda *args: True)
    root = tmp_path / "root"
    ok, tally = harness._compare_root(
        (
            harness.Side("baseline", tmp_path / "baseline"),
            harness.Side("branch", tmp_path / "branch"),
        ),
        root,
        harness.RootQueries(selection=".", queries=[]),
        tmp_path / "scratch",
    )
    output = capsys.readouterr().out
    assert not ok
    assert f"root={root} analyze: IDENTICAL" in output
    assert f"root={root} visualize: FAILED not comparable" in output
    assert tally.comparisons == 3


def test_partial_optional_workload_bindings_are_rejected(harness: types.ModuleType) -> None:
    arguments = harness._parser().parse_args(
        [
            "--baseline-src",
            "/baseline",
            "--branch-src",
            "/branch",
            "--optional-root",
            "/first",
            "--optional-root",
            "/second",
            "--workload",
            "One",
        ]
    )
    with pytest.raises(ValueError, match="1 --workload keys for 2 optional roots"):
        harness._root_list(arguments, harness.Side("baseline", Path("/baseline")))


def test_scenario_symbol_comes_from_the_root_workload(harness: types.ModuleType) -> None:
    workload = harness.RootQueries(
        selection="pkg",
        queries=[
            {"command": "callers", "args": ["pkg.f"]},
            {"command": "definitions", "args": ["bad"], "expect": "error"},
            {"command": "definitions", "args": ["entry"]},
        ],
    )
    assert harness._scenario_symbol(workload) == "entry"
    with pytest.raises(ValueError, match="no answering 'definitions' query"):
        harness._scenario_symbol(harness.RootQueries(selection="pkg", queries=[]))


def test_child_environment_is_scrubbed_and_pinned(
    harness: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERAGE_PROCESS_START", "/nowhere/.coveragerc")
    monkeypatch.setenv("COVERAGE_FILE", "/nowhere/.coverage")
    env = harness._env(Path("/src"))
    assert not any(key.startswith("COVERAGE_") for key in env)
    assert env["PYTHONHASHSEED"] == "0"
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["PYTHONPATH"] == "/src"


def test_committed_workloads_cover_every_default_root_with_real_error_controls() -> None:
    """Every default root needs a committed workload or it can never be
    compared; the Python roots' controls must include a genuine
    argument-validation error path, and the JavaScript and SQL roots must carry
    their own answering query classes (``R-02``)."""

    data = json.loads((SCRIPT.with_name("equivalence_queries.json")).read_text(encoding="utf-8"))
    assert set(data) == {"equivalence_root", "src", "Onyx", "javascript", "sql"}
    assert data["src"]["selection"] == "minotaur/cli.py"
    for key in PYTHON_ROOT_KEYS:
        workload = data[key]
        names = {item["name"] for item in workload["queries"]}
        assert "definitions-invalid" not in names, key
        errors = {item["name"] for item in workload["queries"] if item.get("expect") == "error"}
        assert {"context-malformed-site", "impact-negative-depth"} <= errors, key
        empties = {item["name"] for item in workload["queries"] if item.get("expect") == "empty"}
        assert {"definitions-no-match", "callers-zero"} <= empties, key
        assert (FIXTURE_ROOT if key == "equivalence_root" else ROOT / "src").is_dir(), key

    # The JavaScript and SQL roots reuse committed analyzer-semantics samples
    # read-only and each carry the four query classes (``R-02``).
    for key, root in (("javascript", JAVASCRIPT_ROOT), ("sql", SQL_ROOT)):
        workload = data[key]
        commands = {item["command"] for item in workload["queries"]}
        assert {"definitions", "callers", "unreferenced", "diff"} <= commands, key
        assert (root / workload["selection"]).is_file(), key
    javascript_diff = next(
        item for item in data["javascript"]["queries"] if item["command"] == "diff"
    )
    assert javascript_diff["expect"] == "difference"
    sql_diff = next(item for item in data["sql"]["queries"] if item["command"] == "diff")
    assert sql_diff["expect"] == "empty"

    onyx = data["Onyx"]["queries"]
    callers_zero = next(item for item in onyx if item["name"] == "callers-zero")
    assert callers_zero["args"] == ["analysis_io.resolve_analysis_h5_path"]
    assert callers_zero["expect"] == "empty"
    for name in ("unreferenced-graph", "unreferenced-text"):
        item = next(item for item in onyx if item["name"] == name)
        assert item["args"][0] == "admin_tool"


def test_a_run_that_compares_nothing_fails(baseline_src: Path, tmp_path: Path) -> None:
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, "unused")),
        "--optional-root",
        str(tmp_path / "onyx"),
        "--root",
        str(tmp_path / "onyx"),
    )
    assert result.returncode == 1
    assert "no artifact comparisons were performed" in result.stderr


def test_scenarios_refuse_a_root_with_no_python_to_mutate(
    baseline_src: Path, tmp_path: Path
) -> None:
    """The old substrate workaround hid that root (1) carried no sources."""

    root = tmp_path / "artifact-root"
    root.mkdir()
    (root / "README.md").write_text("artifact only\n", encoding="utf-8")
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, root.name, selection=".")),
        "--root",
        str(root),
        "--scenarios",
    )
    assert result.returncode == 1
    assert "no Python source to mutate" in result.stderr


def test_scenarios_cover_freshness_and_both_sidecar_trust_states(
    baseline_src: Path, tmp_path: Path
) -> None:
    """R-08 makes trusted and untrusted loads diverge, so both are compared.

    Steps (i)/(j)/(k) delete the stamp, corrupt the stamp and force
    ``--validate``: without them every query in the harness ran on the trusted
    path and the skipped node-ID check was never exercised.
    """

    root = _source_root(tmp_path)
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, root.name)),
        "--root",
        str(root),
        "--scenarios",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for letter in "abcdefgijk":
        assert f"step={letter}: IDENTICAL" in result.stdout
        assert f"step={letter} graph SHA-256: IDENTICAL" in result.stdout
    assert "step=h-a copies=a" in result.stdout
    assert "step=h-f copies=f" in result.stdout
    assert "step=f graph SHA-256 before/after: IDENTICAL" in result.stdout
    assert "step=g graph SHA-256 before/after: IDENTICAL" in result.stdout
    for letter in "de":
        assert f"step={letter}: IDENTICAL (2, b''," in result.stdout
        assert f"step={letter} graph SHA-256 before/after: IDENTICAL" in result.stdout


def test_scenarios_refresh_partial_deletion_and_rename(
    harness: types.ModuleType,
    baseline_src: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _source_root(tmp_path)
    (root / "other.py").write_text("other = True\n", encoding="utf-8")
    workload = harness._load_queries(_workload_file(tmp_path, root.name))[root.name]
    query = harness._scenario_query
    observed: dict[tuple[str, str], tuple[int, bytes, bytes]] = {}

    def observe(side, root, graph, no_refresh, **kwargs):
        before = graph.read_bytes()
        result = query(side, root, graph, no_refresh, **kwargs)
        letter = graph.stem.split("-")[0]
        if letter in ("d", "e"):
            observed[letter, side.name] = (result.returncode, before, graph.read_bytes())
        return result

    monkeypatch.setattr(harness, "_scenario_query", observe)
    assert harness._run_scenarios(
        (harness.Side("baseline", baseline_src), harness.Side("branch", ROOT / "src")),
        [(root, workload)],
        tmp_path / "scenarios",
    )
    output = capsys.readouterr().out
    for letter in "de":
        assert f"step={letter}: IDENTICAL" in output
        for side in ("baseline", "branch"):
            exit_code, before, after = observed[letter, side]
            assert exit_code == 0
            assert after != before
            paths = {
                node["path"] for node in json.loads(after)["nodes"] if node["node_class"] == "file"
            }
            assert paths == ({"other.py"} if letter == "d" else {"other.py", "app_renamed.py"})


def test_sidecar_scenarios_reject_a_step_that_did_not_set_up_its_stamp(
    harness: types.ModuleType,
    baseline_src: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The scenario setup itself must be observable, not only its query output."""

    workload = harness._load_queries(SCRIPT.with_name("equivalence_queries.json"))[
        FIXTURE_ROOT.name
    ]
    monkeypatch.setattr(harness, "_apply_sidecar_step", lambda letter, graph: None)
    ok = harness._run_scenarios(
        (harness.Side("baseline", baseline_src), harness.Side("branch", ROOT / "src")),
        [(FIXTURE_ROOT, workload)],
        tmp_path / "scenarios",
    )
    assert not ok
    assert "step=i sidecar setup" in capsys.readouterr().err


def test_sidecar_truth_check_reports_a_false_stamp(
    harness: types.ModuleType,
    baseline_src: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stamp that no longer matches its graph must be reported FAILED.

    Setting up the stamp is not enough: the query must leave it truthful, or a
    corrupted sidecar would silently pass the trust comparison.
    """

    workload = harness._load_queries(SCRIPT.with_name("equivalence_queries.json"))[
        FIXTURE_ROOT.name
    ]
    query = harness._scenario_query

    def corrupt(side, root, graph, no_refresh, **kwargs):
        result = query(side, root, graph, no_refresh, **kwargs)
        if graph.stem.split("-")[0] == "k":
            stamp = graph.with_name(graph.name + ".sha256")
            stamp.write_text(f"{'0' * 64}  {graph.name}\n", encoding="utf-8")
        return result

    monkeypatch.setattr(harness, "_scenario_query", corrupt)
    ok = harness._run_scenarios(
        (harness.Side("baseline", baseline_src), harness.Side("branch", ROOT / "src")),
        [(FIXTURE_ROOT, workload)],
        tmp_path / "scenarios",
    )
    assert not ok
    errors = capsys.readouterr().err
    assert "step=k sidecar" in errors
    assert "FAILED" in errors


def test_scenario_step_that_produces_no_answer_fails(
    harness: types.ModuleType,
    baseline_src: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two identical ``no definitions`` outputs are not an answer.

    The guard is what stops a freshness walk from passing while every step
    compared the same empty result.
    """

    root = _source_root(tmp_path)
    workload = harness._load_queries(
        _workload_file(
            tmp_path,
            root.name,
            queries=[
                {
                    "name": "definitions-missing",
                    "command": "definitions",
                    "args": ["not a valid symbol"],
                    "variants": {"no_refresh": True},
                }
            ],
        )
    )[root.name]
    ok = harness._run_scenarios(
        (harness.Side("baseline", baseline_src), harness.Side("branch", ROOT / "src")),
        [(root, workload)],
        tmp_path / "scenarios",
    )
    assert not ok
    assert "query produced no answer" in capsys.readouterr().err


def test_sidecar_steps_actually_change_the_stamp(harness: types.ModuleType, tmp_path: Path) -> None:
    graph = tmp_path / "graph.json"
    graph.write_text("{}", encoding="utf-8")
    stamp = tmp_path / "graph.json.sha256"
    stamp.write_text("original\n", encoding="utf-8")
    harness._apply_sidecar_step("i", graph)
    assert not stamp.exists()
    stamp.write_text("original\n", encoding="utf-8")
    harness._apply_sidecar_step("j", graph)
    assert stamp.read_text(encoding="utf-8").startswith("0" * 64)
    harness._apply_sidecar_step("k", graph)
    assert stamp.read_text(encoding="utf-8").startswith("0" * 64)


def test_both_scratch_roots_normalise_to_one_placeholder(harness: types.ModuleType) -> None:
    """Side-specific placeholders made any path-echoing diagnostic differ."""

    baseline_scratch = Path("/scratch/baseline")
    branch_scratch = Path("/scratch/branch")
    left = harness.Completed(0, b"", b"could not read /scratch/baseline/graph.json")
    right = harness.Completed(0, b"", b"could not read /scratch/branch/graph.json")
    assert harness._compare_processes("row", left, right, baseline_scratch, branch_scratch)


def test_ordinary_stderr_is_still_compared_byte_for_byte(harness: types.ModuleType) -> None:
    """Removing the refresh-delta rule leaves ordinary stderr exact (``R-05``)."""

    baseline = harness.Completed(
        0,
        b"answer\n",
        b"minotaur: refreshed graph (1 drifted paths)\nminotaur: stale: app.py (content changed)\n",
    )
    branch = harness.Completed(
        0,
        b"answer\n",
        b"minotaur: refreshing graph (1 drifted paths)\n"
        b"minotaur: stale: app.py (content changed)\n",
    )
    assert not harness._compare_processes("ordinary comparison", baseline, branch)
    assert harness._compare_processes("identical comparison", baseline, baseline)


def test_dead_refresh_delta_rule_and_constants_are_removed(harness: types.ModuleType) -> None:
    """The dead rule, parameter and constants are gone, not merely unused."""

    for name in (
        "_normalise_refresh_attempt_delta",
        "BASELINE_REFRESH_PREFIX",
        "BRANCH_REFRESH_PREFIX",
    ):
        assert not hasattr(harness, name), name
    parameters = inspect.signature(harness._compare_processes).parameters
    assert "allow_refresh_attempt_delta" not in parameters


def test_output_is_compared_as_bytes_not_decoded_text(harness: types.ModuleType) -> None:
    """Text mode would translate newlines and hide a real byte difference."""

    left = harness.Completed(0, b"result\r\n", b"")
    right = harness.Completed(0, b"result\n", b"")
    assert not harness._compare_processes("row", left, right, Path("/scratch"))


def test_a_timed_out_side_is_a_named_failure_not_a_traceback(
    harness: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    side = harness.Side("branch", ROOT / "src")
    result = harness._run(side, ["-c", "import time; time.sleep(5)"], timeout=0.5)
    assert result.timed_out
    assert not harness._compare_processes("row", result, result, Path("/scratch"))
    assert "row: FAILED timeout" in capsys.readouterr().out


def test_a_failed_drift_subprocess_is_reported_not_crashed(
    harness: types.ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A side that could not compute Drift has no value to compare against."""

    broken = tmp_path / "empty-src"
    broken.mkdir()
    sides = (harness.Side("baseline", broken), harness.Side("branch", ROOT / "src"))
    tally = harness.Tally()
    assert not harness._compare_drift(sides, tmp_path, tmp_path / "graph.json", tally)
    out = capsys.readouterr().out
    assert "artifact=Drift baseline: subprocess failed" in out
    assert "artifact=Drift: FAILED not comparable" in out
    assert tally.comparisons == 0


def test_definitions_many_query_is_the_bare_main_name() -> None:
    """Only the Python roots define ``main``; JavaScript and SQL roots do not."""

    workloads = json.loads((ROOT / "scripts/equivalence_queries.json").read_text())
    for key in PYTHON_ROOT_KEYS:
        definitions = next(
            entry for entry in workloads[key]["queries"] if entry["name"] == "definitions-many"
        )
        assert definitions["args"] == ["main"], key
    for key in ("javascript", "sql"):
        names = {entry["name"] for entry in workloads[key]["queries"]}
        assert "definitions-many" not in names, key


def test_root_two_import_is_isolated_from_a_shadowing_top_level_package(
    harness: types.ModuleType,
    baseline_src: Path,
) -> None:
    """Root (2)'s working directory cannot shadow the branch's own package."""

    probe = harness._run(
        harness.Side("branch", ROOT / "src"),
        ["-c", "import minotaur; print(minotaur.__file__)"],
        cwd=baseline_src,
    )
    assert probe.returncode == 0
    assert str((ROOT / "src" / "minotaur" / "__init__.py").resolve()) in probe.stdout_text


# The frozen pin that predates the reviewed viewer-asset change. It is the
# control for the advance proof below, not a second equivalence authority.
PRE_ADVANCE_BASELINE = "ec5e90e5623700c8797e1cdc2f51f27fdc7000f4"


def _is_ancestor(older: str, newer: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", older, newer],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )


def _worktree_side(tmp_path: Path, label: str, commit: str, clone: Path) -> Path:
    checkout = tmp_path / f"checkout-{label}"
    added = subprocess.run(
        ["git", "-C", str(clone), "worktree", "add", "--detach", str(checkout), commit],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode:
        raise RuntimeError(f"could not create worktree for {commit}: {added.stderr}")
    return checkout / "src"


def _remove_worktree(src: Path, clone: Path) -> None:
    subprocess.run(
        ["git", "-C", str(clone), "worktree", "remove", "--force", str(src.parent)],
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(clone), "worktree", "prune"],
        capture_output=True,
        text=True,
        check=False,
    )


def _registered_worktrees(repository: Path) -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        line[len("worktree ") :]
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ")
    ]


def test_harness_worktrees_register_in_the_session_clone_not_the_developer_repo(
    session_clone: Path, baseline_src: Path, tmp_path: Path
) -> None:
    """Worktrees never touch the user's ``.git`` (``D-11``, ``F-09``).

    The developer checkout's registrations must be unchanged before, during
    and after a harness worktree is created, while the clone's own list grows.
    A GitHub-hosted checkout is its repository's only worktree, so the same
    observation reduces to the developer list showing just its primary.
    """

    developer_before = _registered_worktrees(ROOT)
    clone_before = _registered_worktrees(session_clone)
    assert str(ROOT) in developer_before
    assert str(session_clone) in clone_before
    # The baseline worktree is materialised by the fixture, from the clone.
    assert str(baseline_src.parent) in clone_before
    assert str(baseline_src.parent) not in developer_before

    side = _worktree_side(tmp_path, "clone-proof", BASELINE_COMMIT, session_clone)
    try:
        clone_during = _registered_worktrees(session_clone)
        assert _registered_worktrees(ROOT) == developer_before
        assert len(clone_during) == len(clone_before) + 1
        assert str(side.parent) in clone_during
    finally:
        _remove_worktree(side, session_clone)
    assert _registered_worktrees(ROOT) == developer_before
    assert str(side.parent) not in _registered_worktrees(session_clone)


# Fixture files in a declared non-UTF-8 source encoding, which interpreters
# older than declaration-aware decoding cannot read.
DECLARED_ENCODING_FIXTURES = ("workflow/declared_encoding.py",)


def _pre_declared_encoding_fixture_root(tmp_path: Path) -> Path:
    """Copy the fixture root without files the historical pins cannot decode.

    The historical baseline-advance comparisons run interpreters that predate
    declared source encodings; on the declared-encoding sample both sides stop
    with a read error, so they analyze the rest of the root instead.
    """

    root = tmp_path / "pre-declared-encoding" / FIXTURE_ROOT.name
    shutil.copytree(FIXTURE_ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
    for relative in DECLARED_ENCODING_FIXTURES:
        (root / relative).unlink()
    return root


def test_equivalence_baseline_advance_is_necessary_and_sufficient(
    session_clone: Path, tmp_path: Path
) -> None:
    """``C-10``: the reviewed viewer change is the only reason the pin moved.

    Reverting only ``VIEWER_ADVANCE_COMMIT`` must reproduce the intentional
    ``visualize HTML`` difference while the graph, Drift, and query rows stay
    identical, and the advanced pin must compare cleanly. The pair makes the
    advance falsifiable instead of an unreviewed normalization.
    """

    assert _is_ancestor(PRE_ADVANCE_BASELINE, VIEWER_ADVANCE_COMMIT)

    workload_path, _ = _non_diff_workload(tmp_path, exclude_names=frozenset({"callers-qualified"}))
    fixture_root = _pre_declared_encoding_fixture_root(tmp_path)

    reverted_side = _worktree_side(tmp_path, "reverted", PRE_ADVANCE_BASELINE, session_clone)
    advanced_side = _worktree_side(tmp_path, "advanced", VIEWER_ADVANCE_COMMIT, session_clone)
    frozen_side = _worktree_side(tmp_path, "pre-stamp", PRE_STAMP_COMMIT, session_clone)
    try:
        reverted = _run(
            "--baseline-src",
            str(reverted_side),
            "--branch-src",
            str(frozen_side),
            "--queries",
            str(workload_path),
            "--root",
            str(fixture_root),
        )
        assert reverted.returncode == 1, reverted.stdout + reverted.stderr
        assert "artifact=analyze graph SHA-256: IDENTICAL" in reverted.stdout
        assert "artifact=Drift: IDENTICAL" in reverted.stdout
        assert "FAILED" not in reverted.stdout
        differing = [line for line in reverted.stdout.splitlines() if line.endswith(": DIFFERENT")]
        assert differing, "reverting the pin must not silently pass"
        assert all("artifact=visualize HTML SHA-256: DIFFERENT" in line for line in differing), (
            differing
        )

        advanced = _run(
            "--baseline-src",
            str(advanced_side),
            "--branch-src",
            str(frozen_side),
            "--queries",
            str(workload_path),
            "--root",
            str(fixture_root),
        )
        assert advanced.returncode == 0, advanced.stdout + advanced.stderr
        assert "DIFFERENT" not in advanced.stdout
    finally:
        _remove_worktree(reverted_side, session_clone)
        _remove_worktree(advanced_side, session_clone)
        _remove_worktree(frozen_side, session_clone)


def test_navigation_baseline_advance_preserves_exact_query_comparison(
    session_clone: Path, tmp_path: Path
) -> None:
    """The pin accounts for caller kinds while other query bytes stay equal."""

    assert _is_ancestor(VIEWER_ADVANCE_COMMIT, BASELINE_COMMIT)
    workload_path, _ = _non_diff_workload(tmp_path)
    fixture_root = _pre_declared_encoding_fixture_root(tmp_path)
    old_side = _worktree_side(tmp_path, "old-navigation", VIEWER_ADVANCE_COMMIT, session_clone)
    new_side = _worktree_side(tmp_path, "new-navigation", BASELINE_COMMIT, session_clone)
    frozen_side = _worktree_side(tmp_path, "pre-stamp", PRE_STAMP_COMMIT, session_clone)
    try:
        old = _run(
            "--baseline-src",
            str(old_side),
            "--branch-src",
            str(frozen_side),
            "--queries",
            str(workload_path),
            "--root",
            str(fixture_root),
        )
        assert old.returncode == 1, old.stdout + old.stderr
        differing = [line for line in old.stdout.splitlines() if line.endswith(": DIFFERENT")]
        assert len(differing) == 4
        assert all("query=callers-qualified" in line for line in differing), differing

        current = _run(
            "--baseline-src",
            str(new_side),
            "--branch-src",
            str(ROOT / "src"),
            "--queries",
            str(workload_path),
            "--root",
            str(FIXTURE_ROOT),
        )
        assert current.returncode == 0, current.stdout + current.stderr
        assert "DIFFERENT" not in current.stdout
    finally:
        _remove_worktree(old_side, session_clone)
        _remove_worktree(new_side, session_clone)
        _remove_worktree(frozen_side, session_clone)
