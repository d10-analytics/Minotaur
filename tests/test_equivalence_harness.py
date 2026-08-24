"""Behavioral proofs for the baseline/branch equivalence instrument."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_equivalence.py"
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


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def _query_file(tmp_path: Path) -> Path:
    queries = tmp_path / "queries.json"
    queries.write_text("[]\n", encoding="utf-8")
    return queries


def _source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
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
        str(_query_file(tmp_path)),
        "--root",
        str(root),
        "--self-test",
    )
    assert result.returncode == 0, result.stderr
    assert "analyze graph SHA-256: DIFFERENT" in result.stdout
    assert "self-test: PASS" in result.stdout
    assert "self-test mode: provenance guard skipped" in result.stderr


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


def test_scenarios_materialize_root_without_source_and_retain_a_f_copies(
    baseline_src: Path, tmp_path: Path
) -> None:
    root = tmp_path / "artifact-root"
    root.mkdir()
    (root / "README.md").write_text("artifact only\n", encoding="utf-8")
    queries = _query_file(tmp_path)
    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(queries),
        "--root",
        str(root),
        "--scenarios",
    )
    assert result.returncode == 0, result.stderr
    assert "step=h-a copies=a" in result.stdout
    assert "step=h-f copies=f" in result.stdout
    assert "step=f graph SHA-256 before/after: IDENTICAL" in result.stdout
    assert "step=g graph SHA-256 before/after: IDENTICAL" in result.stdout


def test_definitions_many_query_is_the_bare_main_name() -> None:
    entries = json.loads((ROOT / "scripts/equivalence_queries.json").read_text())
    definitions = next(entry for entry in entries if entry["name"] == "definitions-many")
    assert definitions["args"] == ["main"]


def test_root_two_import_isolated_from_top_level_package_and_self_test_detects_divergence(
    baseline_src: Path,
    tmp_path: Path,
) -> None:
    loader = importlib.util.spec_from_file_location("equivalence_harness", SCRIPT)
    assert loader is not None and loader.loader is not None
    harness = importlib.util.module_from_spec(loader)
    sys.modules["equivalence_harness"] = harness
    loader.loader.exec_module(harness)
    probe = harness._run(
        harness.Side("branch", ROOT / "src"),
        ["-c", "import minotaur; print(minotaur.__file__)"],
        cwd=baseline_src,
    )
    assert probe.returncode == 0
    assert str((ROOT / "src" / "minotaur" / "__init__.py").resolve()) in probe.stdout

    result = _run(
        "--baseline-src",
        str(baseline_src),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_query_file(tmp_path)),
        "--root",
        str(baseline_src),
        "--self-test",
    )
    assert result.returncode == 0, result.stderr
    assert "artifact=analyze graph SHA-256: DIFFERENT" in result.stdout
    assert "self-test: PASS" in result.stdout
