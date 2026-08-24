"""Behavioral proofs for the baseline/branch equivalence instrument."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_equivalence.py"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "equivalence_root"
# The specification Baseline: the commit every hot-path change is measured and
# compared against.  The harness's provenance guard refuses plain copies and
# refuses two clean worktrees sharing a HEAD, so the baseline side must be a
# real worktree pinned to a commit other than the branch under test.
BASELINE_COMMIT = "fb63689"


@pytest.fixture(scope="session")
def baseline_src(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Materialise a throwaway git worktree of the Baseline commit."""

    checkout = tmp_path_factory.mktemp("equivalence-baseline") / "checkout"
    added = subprocess.run(
        ["git", "-C", str(ROOT), "worktree", "add", "--detach", str(checkout), BASELINE_COMMIT],
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
            ["git", "-C", str(ROOT), "worktree", "remove", "--force", str(checkout)],
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


def test_self_test_rejects_byte_injection_and_accepts_clean_copy(
    baseline_src: Path, tmp_path: Path
) -> None:
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
        "--self-test",
    )
    assert result.returncode == 0, result.stderr
    assert "analyze graph SHA-256: DIFFERENT" in result.stdout
    assert "self-test: PASS" in result.stdout
    assert "self-test mode: provenance guard skipped" in result.stderr


def test_self_test_requires_each_injection_to_produce_its_own_differing_row(
    baseline_src: Path, tmp_path: Path
) -> None:
    """Graph bytes, HTML bytes and query stdout are each proven separately.

    A single injection would let a harness that had stopped comparing HTML or
    query output still print PASS.
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
        "--self-test",
    )
    assert result.returncode == 0, result.stderr
    for name in ("serialize", "visualize", "query"):
        assert f"self-test: injection {name} detected" in result.stdout
    differing = [line for line in result.stdout.splitlines() if line.endswith(": DIFFERENT")]
    assert any("artifact=analyze graph SHA-256" in line for line in differing)
    assert any("artifact=visualize HTML SHA-256" in line for line in differing)
    assert any("query=" in line for line in differing)


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
    baseline_src: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "same-head"
    added = subprocess.run(
        [
            "git",
            "-C",
            str(baseline_src.parent),
            "worktree",
            "add",
            "--detach",
            str(worktree),
            "HEAD",
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
            ["git", "-C", str(baseline_src.parent), "worktree", "remove", "--force", str(worktree)],
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


def test_every_non_control_query_answers_on_the_committed_fixture_root(
    baseline_src: Path,
) -> None:
    """The whole committed workload really runs, on the real fixture root."""

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
    assert int(answered) == int(expected) - 2, summary  # two negative controls
    assert "DIFFERENT" not in result.stdout


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
    workloads = json.loads((ROOT / "scripts/equivalence_queries.json").read_text())
    for key, workload in workloads.items():
        definitions = next(
            entry for entry in workload["queries"] if entry["name"] == "definitions-many"
        )
        assert definitions["args"] == ["main"], key


def test_root_two_import_isolated_from_top_level_package_and_self_test_detects_divergence(
    harness: types.ModuleType,
    baseline_src: Path,
    tmp_path: Path,
) -> None:
    probe = harness._run(
        harness.Side("branch", ROOT / "src"),
        ["-c", "import minotaur; print(minotaur.__file__)"],
        cwd=baseline_src,
    )
    assert probe.returncode == 0
    assert str((ROOT / "src" / "minotaur" / "__init__.py").resolve()) in probe.stdout_text

    root = _source_root(tmp_path, name="root-two-probe")
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_workload_file(tmp_path, root.name)),
        "--root",
        str(root),
        "--self-test",
    )
    assert result.returncode == 0, result.stderr
    assert "artifact=analyze graph SHA-256: DIFFERENT" in result.stdout
    assert "self-test: PASS" in result.stdout
