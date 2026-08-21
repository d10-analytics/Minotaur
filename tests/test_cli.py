"""End-to-end behavior for the language-neutral analysis CLI."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import load_graph_file, stamp_path


def _write(root: Path, path: str, source: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def _run(
    root: Path, output: Path, *targets: Path, force: bool = False
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "minotaur",
        "analyze",
        "--root",
        str(root),
        "--output",
        str(output),
    ]
    if force:
        command.append("--force")
    command.extend(str(target) for target in targets)
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _paths(output: Path) -> set[str]:
    graph = json.loads(output.read_text(encoding="utf-8"))
    return {node["path"] for node in graph["nodes"] if node["node_class"] == "file"}


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def test_selected_mixed_targets_are_deduplicated_and_leave_unselected_imports_unresolved(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    app = _write(root, "selected/app.py", "from other import helper\nhelper()\n")
    _write(root, "selected/readme.txt", "not source\n")
    _write(root, "other.py", "def helper():\n    return 1\n")
    output = tmp_path / "graph.json"

    completed = _run(root, output, root / "selected", app)

    assert completed.returncode == 0, completed.stderr
    assert _paths(output) == {"selected/app.py"}
    graph = json.loads(output.read_text(encoding="utf-8"))
    assert {
        node["reference_text"]
        for node in graph["nodes"]
        if node["node_class"] == "unresolved-reference"
    } == {"helper", "other.helper"}


def test_unsupported_explicit_file_fails_but_recursive_unsupported_files_are_ignored(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    _write(root, "supported.py", "pass\n")
    unsupported = _write(root, "notes.txt", "text\n")
    output = tmp_path / "graph.json"

    clean = _run(root, output, root)
    unsupported_result = _run(root, tmp_path / "rejected.json", unsupported)

    assert clean.returncode == 0, clean.stderr
    assert _paths(output) == {"supported.py"}
    assert unsupported_result.returncode == 2
    assert "unsupported source file" in unsupported_result.stderr
    assert not (tmp_path / "rejected.json").exists()


def test_valid_selection_with_no_registered_files_writes_empty_canonical_graph(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    _write(root, "notes.txt", "not Python\n")
    output = tmp_path / "graph.json"

    completed = _run(root, output, root)

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "coordinate_encoding": "utf-8",
        "format": "minotaur-graph",
        "format_version": "0.1.0",
        "generated_by": {"name": "minotaur-python"},
        "extensions": {"minotaur": {"selection": ["."]}},
        "nodes": [],
        "relationships": [],
    }


def test_selection_containment_exclusions_and_direct_overrides(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write(root, "visible.py", "pass\n")
    _write(root, ".hidden/hidden.py", "pass\n")
    _write(root, ".venv/internal.py", "pass\n")
    outside = _write(tmp_path, "outside.py", "pass\n")
    escaping = root / "escape.py"
    escaping.symlink_to(outside)

    ordinary_output = tmp_path / "ordinary.json"
    hidden_output = tmp_path / "hidden.json"
    venv_output = tmp_path / "venv.json"
    ordinary = _run(root, ordinary_output, root)
    explicit_hidden = _run(root, hidden_output, root / ".hidden")
    explicit_venv = _run(root, venv_output, root / ".venv")
    outside_result = _run(root, tmp_path / "outside.json", outside)
    escape_result = _run(root, tmp_path / "escape.json", escaping)

    assert ordinary.returncode == 0, ordinary.stderr
    assert _paths(ordinary_output) == {"visible.py"}
    assert explicit_hidden.returncode == 0, explicit_hidden.stderr
    assert _paths(hidden_output) == {".hidden/hidden.py"}
    assert explicit_venv.returncode == 0, explicit_venv.stderr
    assert _paths(venv_output) == {".venv/internal.py"}
    assert outside_result.returncode == 2
    assert escape_result.returncode == 2
    assert not (tmp_path / "outside.json").exists()
    assert not (tmp_path / "escape.json").exists()


def test_diagnostics_write_partial_graph_and_report_source_location(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write(root, "valid.py", "def good():\n    return 1\n")
    _write(root, "broken.py", "def incomplete(:\n")
    output = tmp_path / "graph.json"

    completed = _run(root, output, root)

    assert completed.returncode == 1
    assert "broken.py:0:" in completed.stderr
    assert "parse-error" in completed.stderr
    assert _paths(output) == {"valid.py"}
    assert output.read_bytes() == output.read_bytes().rstrip(b"\n")


def test_output_preflight_and_module_console_entry_points_match(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "pass\n")
    output = tmp_path / "graph.json"
    output.write_text("old", encoding="utf-8")

    refused = _run(root, output, root)
    collision = _run(root, source, source, force=True)
    replaced = _run(root, output, root, force=True)
    console_script = shutil.which("minotaur")
    if console_script is None:
        pytest.skip("minotaur console script is not installed")
    console = subprocess.run(
        [
            console_script,
            "analyze",
            "--root",
            str(root),
            "--output",
            str(tmp_path / "console.json"),
            str(root),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert refused.returncode == 2
    assert "pass --force" in refused.stderr
    assert collision.returncode == 2
    assert "also a selected source" in collision.stderr
    assert replaced.returncode == 0, replaced.stderr
    assert console.returncode == 0, console.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == json.loads(
        (tmp_path / "console.json").read_text(encoding="utf-8")
    )


def test_analyze_skips_clean_graph_and_rewrites_after_content_drift(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write(root, "app.py", "def app():\n    return 1\n")
    output = tmp_path / "graph.json"

    first = _run(root, output, root)
    before = output.stat().st_mtime_ns
    clean = _run(root, output, root)

    assert first.returncode == 0, first.stderr
    assert clean.returncode == 0
    assert "graph is up to date, skipping analysis" in clean.stderr
    assert output.stat().st_mtime_ns == before

    _write(root, "app.py", "def app():\n    return 2\n")
    changed = _run(root, output, root)

    assert changed.returncode == 0, changed.stderr
    assert output.stat().st_mtime_ns != before


def test_analyze_skips_clean_graph_with_duplicate_targets(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source = _write(root, "app.py", "def app():\n    return 1\n")
    output = tmp_path / "graph.json"

    first = _run(root, output, root, source, source)
    before = output.stat().st_mtime_ns
    repeated = _run(root, output, root, source, source)

    assert first.returncode == 0, first.stderr
    assert repeated.returncode == 0, repeated.stderr
    assert "graph is up to date, skipping analysis" in repeated.stderr
    assert output.stat().st_mtime_ns == before


def test_analyze_records_git_snapshot_and_omits_it_for_non_git_root(tmp_path: Path) -> None:
    git_root = tmp_path / "git-source"
    _write(git_root, "app.py", "def app():\n    return 1\n")
    assert _git(git_root, "init").returncode == 0
    assert _git(git_root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(git_root, "config", "user.name", "Minotaur Tests").returncode == 0
    assert _git(git_root, "add", "app.py").returncode == 0
    assert _git(git_root, "commit", "-m", "initial").returncode == 0
    expected_commit = _git(git_root, "rev-parse", "HEAD").stdout.strip()
    expected_branch = _git(git_root, "branch", "--show-current").stdout.strip()

    git_output = tmp_path / "git-graph.json"
    git_result = _run(git_root, git_output, git_root)

    non_git_root = tmp_path / "plain-source"
    _write(non_git_root, "app.py", "def app():\n    return 1\n")
    non_git_output = tmp_path / "plain-graph.json"
    non_git_result = _run(non_git_root, non_git_output, non_git_root)

    assert git_result.returncode == 0, git_result.stderr
    git_graph = json.loads(git_output.read_text(encoding="utf-8"))
    assert git_graph["source_control"] == {
        "system": "git",
        "commit": expected_commit,
        "branch": expected_branch,
    }
    assert non_git_result.returncode == 0, non_git_result.stderr
    non_git_graph = json.loads(non_git_output.read_text(encoding="utf-8"))
    assert "source_control" not in non_git_graph


def test_analyze_refreshes_git_snapshot_after_unselected_commit(tmp_path: Path) -> None:
    root = tmp_path / "source"
    selected = _write(root, "selected.py", "value = 1\n")
    unselected = _write(root, "unselected.py", "value = 1\n")
    assert _git(root, "init").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Minotaur Tests").returncode == 0
    assert _git(root, "add", "selected.py", "unselected.py").returncode == 0
    assert _git(root, "commit", "-m", "initial").returncode == 0

    output = tmp_path / "graph.json"
    first = _run(root, output, selected)
    first_graph = json.loads(output.read_text(encoding="utf-8"))
    first_commit = first_graph["source_control"]["commit"]

    unselected.write_text("value = 2\n", encoding="utf-8")
    assert _git(root, "add", "unselected.py").returncode == 0
    assert _git(root, "commit", "-m", "unselected change").returncode == 0
    expected_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    second = _run(root, output, selected)
    second_graph = json.loads(output.read_text(encoding="utf-8"))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first_commit != expected_commit
    assert second_graph["source_control"]["commit"] == expected_commit
    assert "graph is up to date, skipping analysis" not in second.stderr


def test_analyze_refreshes_git_snapshot_after_branch_change(tmp_path: Path) -> None:
    root = tmp_path / "source"
    selected = _write(root, "selected.py", "value = 1\n")
    assert _git(root, "init").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Minotaur Tests").returncode == 0
    assert _git(root, "add", "selected.py").returncode == 0
    assert _git(root, "commit", "-m", "initial").returncode == 0

    output = tmp_path / "graph.json"
    first = _run(root, output, selected)
    first_graph = json.loads(output.read_text(encoding="utf-8"))
    first_bytes = output.read_bytes()
    original_branch = first_graph["source_control"]["branch"]

    assert _git(root, "switch", "-c", "alternate").returncode == 0
    second = _run(root, output, selected)
    second_graph = json.loads(output.read_text(encoding="utf-8"))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert original_branch != "alternate"
    assert output.read_bytes() != first_bytes
    assert second_graph["source_control"]["branch"] == "alternate"
    assert "graph is up to date, skipping analysis" not in second.stderr


def test_analyze_ignores_git_probe_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    _write(root, "app.py", "value = 1\n")
    output = tmp_path / "graph.json"

    calls = 0

    def fail_git(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, stdout="true\n", stderr="")
        raise OSError("git unavailable")

    monkeypatch.setattr(cli.subprocess, "run", fail_git)
    result = cli._analyze_selection(root, (root,), output, False)

    assert calls == 3
    assert result.document.source_control is None
    assert output.exists()


def test_analyze_force_rewrites_clean_graph(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write(root, "app.py", "def app():\n    return 1\n")
    output = tmp_path / "graph.json"

    first = _run(root, output, root)
    before = output.stat().st_mtime_ns
    time.sleep(0.01)
    forced = _run(root, output, root, force=True)

    assert first.returncode == 0, first.stderr
    assert forced.returncode == 0, forced.stderr
    assert "graph is up to date, skipping analysis" not in forced.stderr
    assert output.stat().st_mtime_ns != before


def test_atomic_output_failure_preserves_old_graph_and_removes_its_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "graph.json"
    output.write_bytes(b"old graph")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(cli.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replacement failure"):
        cli._write_atomically(output, b"new graph")

    assert output.read_bytes() == b"old graph"
    assert list(tmp_path.glob(".graph.json.*")) == []


def test_analyze_writes_sidecar_matching_graph_bytes_and_query_refresh_updates_it(
    tmp_path: Path, capsys: object
) -> None:
    """AC-03: sidecar matches graph after analyze, and again after query refresh."""
    root = tmp_path / "source"
    _write(root, "app.py", "def app():\n    return 1\n")
    output = tmp_path / "graph.json"

    status = cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])
    assert status == 0

    # Sidecar exists and matches the graph bytes.
    graph_bytes = output.read_bytes()
    sidecar = stamp_path(output)
    assert sidecar.exists()
    expected_digest = hashlib.sha256(graph_bytes).hexdigest()
    assert sidecar.read_text(encoding="ascii").strip() == expected_digest

    # Drift the source so a query refresh rewrites the graph.
    _write(root, "app.py", "def app():\n    return 2\n")
    capsys.readouterr()  # type: ignore[attr-defined]
    status = cli.main(
        [
            "query",
            "definitions",
            "--graph",
            str(output),
            "--root",
            str(root),
            "app",
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert status == 0
    assert "refreshed graph" in captured.err

    # After refresh, the sidecar matches the new graph bytes.
    new_graph_bytes = output.read_bytes()
    assert new_graph_bytes != graph_bytes
    new_expected_digest = hashlib.sha256(new_graph_bytes).hexdigest()
    assert sidecar.read_text(encoding="ascii").strip() == new_expected_digest


def test_stamp_write_failure_exits_two_and_leaves_mismatched_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
) -> None:
    """AC-04 (ii): stamp-write failure leaves graph on disk with stale sidecar."""
    root = tmp_path / "source"
    _write(root, "app.py", "def app():\n    return 1\n")
    output = tmp_path / "graph.json"

    # First analyze: creates graph + sidecar.
    status = cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])
    assert status == 0
    old_sidecar_content = stamp_path(output).read_text(encoding="ascii").strip()

    # Drift the source.
    _write(root, "app.py", "def app():\n    return 2\n")

    # Monkeypatch _write_atomically to fail only on the second call (the stamp).
    original_write = cli._write_atomically
    call_count = 0

    def fail_on_stamp(path: Path, content: bytes) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("simulated stamp write failure")
        original_write(path, content)

    monkeypatch.setattr(cli, "_write_atomically", fail_on_stamp)
    capsys.readouterr()  # type: ignore[attr-defined]

    status = cli.main(
        [
            "analyze",
            "--root",
            str(root),
            "--output",
            str(output),
            "--force",
            str(root),
        ]
    )

    # Command exits 2.
    assert status == 2

    # The new graph bytes are on disk (the first _write_atomically succeeded).
    new_graph_bytes = output.read_bytes()
    new_digest = hashlib.sha256(new_graph_bytes).hexdigest()
    # The sidecar on disk does not match the new graph bytes.
    sidecar_content = stamp_path(output).read_text(encoding="ascii").strip()
    assert sidecar_content == old_sidecar_content
    assert sidecar_content != new_digest

    # A subsequent load_graph_file takes the full-validation path.
    loaded = load_graph_file(output)
    assert loaded.validated is True


def test_analyze_warns_when_imports_only_resolve_under_a_different_root(
    tmp_path: Path, capsys: object
) -> None:
    # A src/ layout analyzed from the repository root: every cross-module
    # import names `pkg.*` while module labels carry the `src.` prefix.
    _write(tmp_path, "src/pkg/__init__.py", "")
    _write(tmp_path, "src/pkg/a.py", "def helper():\n    return 1\n")
    _write(tmp_path, "src/pkg/b.py", "from pkg.a import helper\nimport numpy\nhelper()\n")

    status = cli.main(
        [
            "analyze",
            "--root",
            str(tmp_path),
            "--output",
            str(tmp_path / "g.json"),
            str(tmp_path / "src/pkg"),
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert status == 0
    # numpy is unresolved but not root-mismatched, so the ratio is 1 of 2.
    assert "warning: 50% of imports (1 of 2) only resolve with a different root" in captured.err
    assert f"pass --root {tmp_path.resolve() / 'src'}" in captured.err
    graph = json.loads((tmp_path / "g.json").read_text(encoding="utf-8"))
    assert graph["extensions"]["minotaur-python"] == {
        "import_root_hint": "src",
        "imports_resolved": 0,
        "imports_root_mismatched": 1,
        "imports_unresolved": 2,
    }

    status = cli.main(
        [
            "analyze",
            "--root",
            str(tmp_path / "src"),
            "--output",
            str(tmp_path / "g2.json"),
            "--force",
            str(tmp_path / "src/pkg"),
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert status == 0
    assert "warning" not in captured.err
    graph = json.loads((tmp_path / "g2.json").read_text(encoding="utf-8"))
    assert graph["extensions"]["minotaur-python"] == {
        "imports_resolved": 1,
        "imports_root_mismatched": 0,
        "imports_unresolved": 1,
    }


def test_missing_target_error_explains_working_directory_resolution(
    tmp_path: Path, capsys: object
) -> None:

    status = cli.main(
        ["analyze", "--root", str(tmp_path), "--output", str(tmp_path / "g.json"), "nope"]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert status == 2
    assert "target does not exist: nope" in captured.err
    assert f"resolved from the current directory {Path.cwd()}, not from --root" in captured.err
