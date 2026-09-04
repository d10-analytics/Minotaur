"""End-to-end behavior for the language-neutral analysis CLI."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model import loading
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


def _distribution_is_installed() -> bool:
    """Report whether this interpreter has the Minotaur distribution installed."""
    try:
        metadata.distribution("minotaur")
    except metadata.PackageNotFoundError:
        return False
    return True


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
        "generated_by": {"name": "minotaur"},
        "extensions": {"minotaur": {"selection": ["."]}},
        "nodes": [],
        "relationships": [],
    }


def test_non_empty_python_selection_keeps_python_producer(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write(root, "app.py", "value = 1\n")
    output = tmp_path / "graph.json"

    completed = _run(root, output, root)

    assert completed.returncode == 0, completed.stderr
    graph = json.loads(output.read_text(encoding="utf-8"))
    assert graph["generated_by"]["name"] == "minotaur-python"


def test_javascript_selection_dispatches_and_mixed_selection_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    _write(root, "app.js", "export function app() {}\n")
    output = tmp_path / "javascript.json"

    javascript = _run(root, output, root)

    assert javascript.returncode == 0, javascript.stderr
    graph = json.loads(output.read_text(encoding="utf-8"))
    validated = load_graph_file(output).document
    assert graph["generated_by"]["name"] == "minotaur-javascript"
    assert validated.generated_by.name == "minotaur-javascript"
    assert any(node.path == "app.js" for node in validated.nodes)
    assert {node["path"] for node in graph["nodes"] if node["node_class"] == "file"} == {"app.js"}

    _write(root, "helper.py", "def helper():\n    return 1\n")
    mixed_status = cli.main(
        [
            "analyze",
            "--root",
            str(root),
            "--output",
            str(tmp_path / "mixed.json"),
            str(root),
        ]
    )

    assert mixed_status == 2
    assert capsys.readouterr().err.strip() == (
        "minotaur: error: selected files require unsupported multi-interpreter graph composition"
    )


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
    console_script = Path(sys.executable).with_name("minotaur")
    if os.name == "nt" and not console_script.is_file():
        console_script = console_script.with_suffix(".exe")
    if not console_script.is_file():
        # When the running interpreter *is* the environment minotaur is
        # installed into, the console script must exist beside it; a skip
        # there would silently retire the only entry-point parity proof.
        if _distribution_is_installed():
            pytest.fail(
                f"minotaur is installed in {sys.executable}'s environment"
                f" but {console_script} is missing"
            )
        pytest.skip("minotaur console script is not installed")
    console = subprocess.run(
        [
            str(console_script),
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


def test_distribution_predicate_distinguishes_an_editable_install_from_an_absent_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console-script parity guard must follow installation metadata, not import paths."""

    monkeypatch.setattr(metadata, "distribution", lambda name: object())
    assert _distribution_is_installed()

    def missing_distribution(name: str) -> metadata.Distribution:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "distribution", missing_distribution)
    assert not _distribution_is_installed()


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


def test_analyze_skips_after_unselected_commit_and_keeps_original_git_snapshot(
    tmp_path: Path,
) -> None:
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
    first_bytes = output.read_bytes()
    first_sidecar = stamp_path(output).read_bytes()
    first_mtime = output.stat().st_mtime_ns

    unselected.write_text("value = 2\n", encoding="utf-8")
    assert _git(root, "add", "unselected.py").returncode == 0
    assert _git(root, "commit", "-m", "unselected change").returncode == 0
    expected_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    second = _run(root, output, selected)
    second_graph = json.loads(output.read_text(encoding="utf-8"))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first_commit != expected_commit
    assert second_graph["source_control"]["commit"] == first_commit
    assert output.read_bytes() == first_bytes
    assert stamp_path(output).read_bytes() == first_sidecar
    assert output.stat().st_mtime_ns == first_mtime
    assert "graph is up to date, skipping analysis" in second.stderr


def test_analyze_skips_after_branch_change_and_keeps_original_git_snapshot(
    tmp_path: Path,
) -> None:
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
    first_sidecar = stamp_path(output).read_bytes()
    first_mtime = output.stat().st_mtime_ns
    original_branch = first_graph["source_control"]["branch"]

    assert _git(root, "switch", "-c", "alternate").returncode == 0
    second = _run(root, output, selected)
    second_graph = json.loads(output.read_text(encoding="utf-8"))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert original_branch != "alternate"
    assert output.read_bytes() == first_bytes
    assert stamp_path(output).read_bytes() == first_sidecar
    assert output.stat().st_mtime_ns == first_mtime
    assert second_graph["source_control"]["branch"] == original_branch
    assert "graph is up to date, skipping analysis" in second.stderr


def test_analyze_refreshes_git_snapshot_after_selected_content_change(tmp_path: Path) -> None:
    root = _config_repo(tmp_path, "source")
    selected = _write(root, "selected.py", "value = 1\n")
    _write_config(
        root,
        _MINOTAUR_CONFIG + 'root = "."\ngraph = "graph.json"\ntargets = ["selected.py"]\n',
    )
    assert _git(root, "add", "selected.py", ".minotaur.toml").returncode == 0
    assert _git(root, "commit", "-m", "initial").returncode == 0

    first = _run_in(root, "analyze")
    first_graph = json.loads((root / "graph.json").read_text(encoding="utf-8"))
    first_commit = first_graph["source_control"]["commit"]
    selected.write_text("value = 2\n", encoding="utf-8")
    assert _git(root, "add", "selected.py").returncode == 0
    assert _git(root, "commit", "-m", "selected change").returncode == 0
    expected_commit = _git(root, "rev-parse", "HEAD").stdout.strip()

    changed = _run_in(root, "analyze")
    changed_graph = json.loads((root / "graph.json").read_text(encoding="utf-8"))

    assert first.returncode == 0, first.stderr
    assert changed.returncode == 0, changed.stderr
    assert first_commit != expected_commit
    assert changed_graph["source_control"]["commit"] == expected_commit
    assert "graph is up to date, skipping analysis" not in changed.stderr


def test_analyze_force_refreshes_git_snapshot(tmp_path: Path) -> None:
    root = _config_repo(tmp_path, "source")
    selected = _write(root, "selected.py", "value = 1\n")
    _write_config(
        root,
        _MINOTAUR_CONFIG + 'root = "."\ngraph = "graph.json"\ntargets = ["selected.py"]\n',
    )
    assert _git(root, "add", "selected.py", ".minotaur.toml").returncode == 0
    assert _git(root, "commit", "-m", "initial").returncode == 0
    first = _run_in(root, "analyze")
    first_bytes = (root / "graph.json").read_bytes()
    first_commit = json.loads(first_bytes)["source_control"]["commit"]

    selected.write_text("value = 2\n", encoding="utf-8")
    assert _git(root, "add", "selected.py").returncode == 0
    assert _git(root, "commit", "-m", "force source change").returncode == 0
    expected_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    forced = _run_in(root, "analyze", "--force")
    forced_graph = json.loads((root / "graph.json").read_text(encoding="utf-8"))

    assert first.returncode == 0, first.stderr
    assert forced.returncode == 0, forced.stderr
    assert first_commit != expected_commit
    assert forced_graph["source_control"]["commit"] == expected_commit
    assert (root / "graph.json").read_bytes() != first_bytes


def test_analyze_clean_skip_runs_no_git_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    selected = _write(root, "selected.py", "value = 1\n")
    output = tmp_path / "graph.json"
    config = _write_config(
        root,
        _MINOTAUR_CONFIG + 'root = "."\ngraph = "../graph.json"\ntargets = ["selected.py"]\n',
    )
    assert _run(root, output, selected).returncode == 0

    calls: list[object] = []

    def fail_probe(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        raise AssertionError("clean analyze skip must not probe Git")

    monkeypatch.setattr(cli.subprocess, "run", fail_probe)
    status = cli.main(
        [
            "analyze",
            "--config",
            str(config),
            "--root",
            str(root),
            "--output",
            str(output),
            str(selected),
        ]
    )

    assert status == 0
    assert calls == []


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


def test_written_files_respect_process_umask(tmp_path: Path) -> None:
    """L-1: mkstemp's 0600 mode is widened to the process umask on replace."""
    root = tmp_path / "source"
    _write(root, "app.py", "def app():\n    return 1\n")
    output = tmp_path / "graph.json"

    old_umask = os.umask(0o022)
    try:
        status = cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])
    finally:
        os.umask(old_umask)

    assert status == 0
    expected_mode = 0o666 & ~0o022
    assert stat.S_IMODE(output.stat().st_mode) == expected_mode
    assert stat.S_IMODE(stamp_path(output).stat().st_mode) == expected_mode


def test_sidecar_replace_failure_cleans_up_temp_file_and_preserves_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-2: a failed sidecar os.replace leaves no temp file and keeps the graph."""
    root = tmp_path / "source"
    _write(root, "app.py", "def app():\n    return 1\n")
    output = tmp_path / "graph.json"
    sidecar = stamp_path(output)
    original_replace = cli.os.replace

    def fail_only_for_sidecar(source: Path, destination: Path) -> None:
        if Path(destination) == sidecar:
            raise OSError("simulated sidecar replace failure")
        original_replace(source, destination)

    monkeypatch.setattr(cli.os, "replace", fail_only_for_sidecar)

    status = cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])

    assert status == 2
    assert output.exists()
    # The graph is intact and parseable; only the sidecar write failed.
    json.loads(output.read_text(encoding="utf-8"))
    assert not sidecar.exists()
    assert list(tmp_path.glob(f".{sidecar.name}.*")) == []


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


def _symlinked_graph(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (source_root, symlink graph path, real graph path) for a symlink output."""
    root = tmp_path / "source"
    _write(root, "app.py", "def app():\n    return 1\n")
    real_directory = tmp_path / "build"
    real_directory.mkdir()
    real = real_directory / "graph.json"
    link = tmp_path / "graph.json"
    link.symlink_to(real)
    return root, link, real


def test_analyze_through_symlink_stamps_beside_the_link_not_the_resolved_file(
    tmp_path: Path,
) -> None:
    """H-1: the sidecar follows the caller path so the next read is a trusted load."""
    root, link, real = _symlinked_graph(tmp_path)

    status = cli.main(["analyze", "--root", str(root), "--output", str(link), str(root)])
    assert status == 0

    # The graph itself went through the symlink to the real file.
    assert link.is_symlink()
    assert real.is_file()
    graph_bytes = real.read_bytes()
    assert graph_bytes.startswith(b"{")

    # The sidecar sits beside the link — the path every reader derives from.
    link_sidecar = stamp_path(link)
    assert link_sidecar.exists()
    assert not stamp_path(real).exists()
    expected = hashlib.sha256(graph_bytes).hexdigest()
    assert link_sidecar.read_text(encoding="ascii").strip() == expected

    # And so the next read through the link skips schema validation.
    assert load_graph_file(link).validated is False


def test_query_refresh_through_symlink_updates_the_sidecar_beside_the_link(
    tmp_path: Path, capsys: object
) -> None:
    """H-1: the refresh write path stamps the caller path too, not the resolved one."""
    root, link, real = _symlinked_graph(tmp_path)
    assert cli.main(["analyze", "--root", str(root), "--output", str(link), str(root)]) == 0
    original_bytes = real.read_bytes()

    # Drift the source so the query refreshes (and rewrites) the graph.
    _write(root, "app.py", "def app():\n    return 2\n")
    capsys.readouterr()  # type: ignore[attr-defined]
    status = cli.main(["query", "definitions", "--graph", str(link), "--root", str(root), "app"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert status == 0
    assert "refreshed graph" in captured.err

    assert link.is_symlink()
    new_bytes = real.read_bytes()
    assert new_bytes != original_bytes
    expected = hashlib.sha256(new_bytes).hexdigest()
    assert stamp_path(link).read_text(encoding="ascii").strip() == expected
    assert load_graph_file(link).validated is False


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
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    # Command exits 2.
    assert status == 2

    # M-3: the diagnostic names the sidecar path and states the graph was
    # already written, instead of leaking the raw mkstemp temporary-file
    # errno on stderr.
    assert f"could not write graph stamp {stamp_path(output)}" in captured.err
    assert "the graph itself was written" in captured.err

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


def _unstamped_graph(tmp_path: Path) -> tuple[Path, Path]:
    """Create a small analyzed graph and then remove its sidecar stamp.

    Returns (graph_path, source_root).
    """
    root = tmp_path / "src"
    _write(root, "a.py", "x = 1\n")
    output = tmp_path / "graph.json"
    assert cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)]) == 0
    sidecar = stamp_path(output)
    assert sidecar.exists()
    sidecar.unlink()
    return output, root


def _stamped_graph(tmp_path: Path) -> Path:
    """Create a small analyzed graph with a matching sidecar stamp."""
    root = tmp_path / "src"
    _write(root, "a.py", "x = 1\n")
    output = tmp_path / "graph.json"
    assert cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)]) == 0
    assert stamp_path(output).exists()
    return output


class TestValidateFlag:
    """AC-06: --validate forces schema pass at every user-facing graph read."""

    def test_query_definitions_validate_forces_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = _stamped_graph(tmp_path)
        root = tmp_path / "src"
        monkeypatch.setattr(
            "minotaur.graph_model.loading._validate_wire_shape",
            lambda raw: (_ for _ in ()).throw(AssertionError("schema forced")),
        )
        assert (
            cli.main(["query", "definitions", "--graph", str(output), "--root", str(root), "a"])
            == 0
        )
        with pytest.raises(AssertionError, match="schema forced"):
            cli.main(
                [
                    "query",
                    "definitions",
                    "--graph",
                    str(output),
                    "--root",
                    str(root),
                    "--validate",
                    "--no-refresh",
                    "a",
                ]
            )

    def test_query_context_validate_forces_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = _stamped_graph(tmp_path)
        root = tmp_path / "src"
        monkeypatch.setattr(
            "minotaur.graph_model.loading._validate_wire_shape",
            lambda raw: (_ for _ in ()).throw(AssertionError("schema forced")),
        )
        assert cli.main(
            ["query", "context", "--graph", str(output), "--root", str(root), "--site", "a.py:1"]
        ) in {0, 1}
        with pytest.raises(AssertionError, match="schema forced"):
            cli.main(
                [
                    "query",
                    "context",
                    "--graph",
                    str(output),
                    "--root",
                    str(root),
                    "--validate",
                    "--site",
                    "a.py:1",
                ]
            )

    def test_query_diff_validate_forces_schema_on_old(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = _stamped_graph(tmp_path)
        monkeypatch.setattr(
            "minotaur.graph_model.loading._validate_wire_shape",
            lambda raw: (_ for _ in ()).throw(AssertionError("schema forced")),
        )
        assert cli.main(["query", "diff", str(output), str(output)]) == 0
        with pytest.raises(AssertionError, match="schema forced"):
            cli.main(["query", "diff", "--validate", str(output), str(output)])

    def test_query_diff_validate_forces_schema_on_new(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output_old = _stamped_graph(tmp_path)
        root2 = tmp_path / "src2"
        _write(root2, "b.py", "y = 2\n")
        output_new = tmp_path / "graph2.json"
        assert (
            cli.main(["analyze", "--root", str(root2), "--output", str(output_new), str(root2)])
            == 0
        )
        call_count = 0
        mod = __import__("minotaur.graph_model.loading", fromlist=["_validate_wire_shape"])
        original_validate = mod._validate_wire_shape

        def fail_on_second(raw: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise AssertionError("schema forced on NEW")
            original_validate(raw)

        monkeypatch.setattr("minotaur.graph_model.loading._validate_wire_shape", fail_on_second)
        # L-4: without --validate, both graphs are stamped, so the trusted
        # load path never calls the broken schema seam and the command
        # succeeds despite the monkeypatch.
        assert cli.main(["query", "diff", str(output_old), str(output_new)]) == 0
        with pytest.raises(AssertionError, match="schema forced on NEW"):
            cli.main(["query", "diff", "--validate", str(output_old), str(output_new)])

    def test_query_callers_validate_forces_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L-4: --validate is threaded for `query callers` too."""
        output = _stamped_graph(tmp_path)
        root = tmp_path / "src"
        monkeypatch.setattr(
            "minotaur.graph_model.loading._validate_wire_shape",
            lambda raw: (_ for _ in ()).throw(AssertionError("schema forced")),
        )
        assert cli.main(["query", "callers", "--graph", str(output), "--root", str(root), "a"]) == 0
        with pytest.raises(AssertionError, match="schema forced"):
            cli.main(
                [
                    "query",
                    "callers",
                    "--graph",
                    str(output),
                    "--root",
                    str(root),
                    "--validate",
                    "--no-refresh",
                    "a",
                ]
            )

    def test_query_impact_validate_forces_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L-4: --validate is threaded for `query impact` too."""
        output = _stamped_graph(tmp_path)
        root = tmp_path / "src"
        monkeypatch.setattr(
            "minotaur.graph_model.loading._validate_wire_shape",
            lambda raw: (_ for _ in ()).throw(AssertionError("schema forced")),
        )
        assert cli.main(["query", "impact", "--graph", str(output), "--root", str(root), "a"]) == 0
        with pytest.raises(AssertionError, match="schema forced"):
            cli.main(
                [
                    "query",
                    "impact",
                    "--graph",
                    str(output),
                    "--root",
                    str(root),
                    "--validate",
                    "--no-refresh",
                    "a",
                ]
            )

    def test_query_unreferenced_validate_forces_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L-4: --validate is threaded for `query unreferenced` too."""
        output = _stamped_graph(tmp_path)
        root = tmp_path / "src"
        monkeypatch.setattr(
            "minotaur.graph_model.loading._validate_wire_shape",
            lambda raw: (_ for _ in ()).throw(AssertionError("schema forced")),
        )
        assert cli.main(["query", "unreferenced", "--graph", str(output), "--root", str(root)]) == 0
        with pytest.raises(AssertionError, match="schema forced"):
            cli.main(
                [
                    "query",
                    "unreferenced",
                    "--graph",
                    str(output),
                    "--root",
                    str(root),
                    "--validate",
                    "--no-refresh",
                ]
            )

    def test_visualize_validate_forces_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = _stamped_graph(tmp_path)
        html_out = tmp_path / "viz.html"
        monkeypatch.setattr(
            "minotaur.graph_model.loading._validate_wire_shape",
            lambda raw: (_ for _ in ()).throw(AssertionError("schema forced")),
        )
        result = cli.main(["visualize", "--input", str(output), "--output", str(html_out)])
        assert result == 0
        html_out2 = tmp_path / "viz2.html"
        with pytest.raises(AssertionError, match="schema forced"):
            cli.main(
                ["visualize", "--input", str(output), "--output", str(html_out2), "--validate"]
            )


class TestStampAfterValidation:
    """AC-18: user-facing commands that fully validate a graph leave a sidecar."""

    def _assert_sidecar_matches_bytes(self, graph_path: Path) -> None:
        """Verify the sidecar contains the digest of the graph bytes on disk."""
        sidecar = stamp_path(graph_path)
        assert sidecar.exists(), f"sidecar not created for {graph_path}"
        expected = hashlib.sha256(graph_path.read_bytes()).hexdigest()
        assert sidecar.read_text(encoding="ascii").strip() == expected

    def test_query_definitions_stamps_unstamped_graph(self, tmp_path: Path) -> None:
        """AC-18 (a): query definitions exits 0 and leaves correct sidecar."""
        output, root = _unstamped_graph(tmp_path)
        status = cli.main(
            ["query", "definitions", "--graph", str(output), "--root", str(root), "a"]
        )
        assert status == 0
        self._assert_sidecar_matches_bytes(output)

    def test_query_context_stamps_unstamped_graph(self, tmp_path: Path) -> None:
        """AC-18 (a): query context exits 0 and leaves correct sidecar."""
        output, root = _unstamped_graph(tmp_path)
        status = cli.main(
            [
                "query",
                "context",
                "--graph",
                str(output),
                "--root",
                str(root),
                "--site",
                "a.py:1",
            ]
        )
        assert status == 0
        self._assert_sidecar_matches_bytes(output)

    def test_query_diff_stamps_both_graphs(self, tmp_path: Path) -> None:
        """AC-18 (a): query diff exits 0 and stamps OLD and NEW."""
        old_path, _ = _unstamped_graph(tmp_path)
        # Create a second unstamped graph in a subdirectory.
        root2 = tmp_path / "src2"
        _write(root2, "b.py", "y = 2\n")
        new_path = tmp_path / "graph2.json"
        assert (
            cli.main(["analyze", "--root", str(root2), "--output", str(new_path), str(root2)]) == 0
        )
        stamp_path(new_path).unlink()
        assert not stamp_path(old_path).exists()
        assert not stamp_path(new_path).exists()

        status = cli.main(["query", "diff", str(old_path), str(new_path)])
        assert status == 0
        self._assert_sidecar_matches_bytes(old_path)
        self._assert_sidecar_matches_bytes(new_path)

    def test_visualize_stamps_unstamped_graph(self, tmp_path: Path) -> None:
        """AC-18 (a): visualize exits 0 and leaves correct sidecar."""
        output, _ = _unstamped_graph(tmp_path)
        html_out = tmp_path / "viz.html"
        status = cli.main(["visualize", "--input", str(output), "--output", str(html_out)])
        assert status == 0
        self._assert_sidecar_matches_bytes(output)

    def test_visualize_output_preflight_refusal_creates_no_sidecar(self, tmp_path: Path) -> None:
        """M-4: a refused --output write must not still stamp the input graph."""
        output, _ = _unstamped_graph(tmp_path)
        html_out = tmp_path / "viz.html"
        html_out.write_text("old", encoding="utf-8")

        status = cli.main(["visualize", "--input", str(output), "--output", str(html_out)])

        assert status == 2
        assert not stamp_path(output).exists()

    def test_validate_flag_also_stamps(self, tmp_path: Path) -> None:
        """AC-18 (b): --validate also leaves a matching sidecar."""
        output, root = _unstamped_graph(tmp_path)
        status = cli.main(
            [
                "query",
                "definitions",
                "--graph",
                str(output),
                "--root",
                str(root),
                "--validate",
                "--no-refresh",
                "a",
            ]
        )
        assert status == 0
        self._assert_sidecar_matches_bytes(output)

    def test_stamped_graph_skips_stamp_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-18 (c): a stamped graph triggers no sidecar write.

        With _write_atomically monkeypatched to raise AssertionError, the
        query still exits 0, proving the write path is never entered when
        the stamp already matches (validated is False).
        """
        output = _stamped_graph(tmp_path)
        root = tmp_path / "src"
        monkeypatch.setattr(
            cli,
            "_write_atomically",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("_write_atomically must not be called")
            ),
        )
        status = cli.main(
            [
                "query",
                "definitions",
                "--graph",
                str(output),
                "--root",
                str(root),
                "--no-refresh",
                "a",
            ]
        )
        assert status == 0

    def test_stamp_write_failure_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
    ) -> None:
        """AC-18 (d): OSError during stamp write does not fail the command."""
        output, root = _unstamped_graph(tmp_path)
        query_args = [
            "query",
            "definitions",
            "--graph",
            str(output),
            "--root",
            str(root),
            "--no-refresh",
            "a",
        ]

        # L-5: run the same query without failure injection first, so the
        # normal answer can be compared against the swallowed-failure run.
        baseline_status = cli.main(query_args)
        baseline_out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert baseline_status == 0
        assert baseline_out
        # The baseline run stamped the graph; remove it so the failure-
        # injected run below exercises the same unstamped starting state.
        stamp_path(output).unlink()

        monkeypatch.setattr(
            cli,
            "_write_atomically",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated write failure")),
        )
        status = cli.main(query_args)
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert status == 0
        assert not stamp_path(output).exists()
        assert captured.out == baseline_out

    def test_analyze_clean_skip_does_not_stamp(self, tmp_path: Path) -> None:
        """AC-18 (e): analyze's clean-skip probe leaves no sidecar."""
        root = tmp_path / "src"
        _write(root, "a.py", "x = 1\n")
        output = tmp_path / "graph.json"
        assert cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)]) == 0
        # Remove the sidecar that analyze's write path created.
        stamp_path(output).unlink()
        # Re-run analyze: the graph is clean, so it hits the skip path.
        status = cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])
        assert status == 0
        assert not stamp_path(output).exists()

    def test_stamp_records_loaded_digest_not_current_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-18 (f): sidecar records digest of originally loaded bytes.

        The graph file is replaced on disk between the load and the stamp
        write. The sidecar must still equal the digest of the original bytes.
        """
        output, root = _unstamped_graph(tmp_path)
        original_bytes = output.read_bytes()
        original_digest = hashlib.sha256(original_bytes).hexdigest()

        original_load = cli.load_graph_file

        def load_then_replace(path: Path, **kwargs: object) -> object:
            result = original_load(path, **kwargs)
            # Replace the file on disk after reading it.
            path.write_bytes(b'{"replaced": true}')
            return result

        monkeypatch.setattr(cli, "load_graph_file", load_then_replace)
        status = cli.main(
            [
                "query",
                "definitions",
                "--graph",
                str(output),
                "--root",
                str(root),
                "--no-refresh",
                "a",
            ]
        )
        assert status == 0
        sidecar = stamp_path(output)
        assert sidecar.exists()
        assert sidecar.read_text(encoding="ascii").strip() == original_digest
        # The sidecar does NOT match the replaced file.
        replaced_digest = hashlib.sha256(output.read_bytes()).hexdigest()
        assert replaced_digest != original_digest


# ---------------------------------------------------------------------------
# Project configuration through the CLI (T02: AC-01..AC-05, AC-07..AC-10,
# AC-13). Discovery-sensitive tests run from cwds inside tmp_path Git
# repositories so the D-07 boundary makes them independent of anything above
# the temporary repo; in-process runs monkeypatch.chdir into the same repos.
# ---------------------------------------------------------------------------

_MINOTAUR_CONFIG = "[minotaur]\nschema_version = 1\n"


def _config_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A Git work tree whose root is the discovery stop point for its tests."""
    root = tmp_path / name
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Minotaur Tests").returncode == 0
    return root


def _write_config(root: Path, body: str) -> Path:
    config = root / ".minotaur.toml"
    config.write_text(body, encoding="utf-8")
    return config


def _run_in(cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run one CLI invocation from ``cwd`` (subprocess inherits the env)."""
    return subprocess.run(
        [sys.executable, "-m", "minotaur", *argv],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _file_paths(graph: dict[str, object]) -> set[str]:
    return {node["path"] for node in graph["nodes"] if node["node_class"] == "file"}


def _scope_project(tmp_path: Path, *, files: str = "src/auth/api.py") -> Path:
    root = _config_repo(tmp_path, "scope-project")
    _write(root, "src/auth/api.py", "from external import helper\nhelper()\n")
    _write(root, "src/other.py", "def other():\n    return 1\n")
    systems = root / "docs" / "systems" / "auth"
    systems.mkdir(parents=True)
    (systems / "system.toml").write_text(
        'schema_version = 1\nname = "auth"\nfiles = ['
        + ", ".join(f'"{entry}"' for entry in files.split(","))
        + "]\n",
        encoding="utf-8",
    )
    _write_config(
        root,
        _MINOTAUR_CONFIG
        + 'root = "."\ngraph = "graph.json"\ntargets = ["src"]\nsystems_dir = "docs/systems"\n',
    )
    assert _git(root, "add", ".").returncode == 0
    assert _git(root, "commit", "-m", "scope fixtures").returncode == 0
    return root


def test_analyze_scope_writes_truthful_schema_graph_and_sidecar(tmp_path: Path) -> None:
    root = _scope_project(tmp_path)
    completed = _run_in(root, "analyze", "--scope", "auth")
    output = root / "docs" / "systems" / "auth" / "graph.json"

    assert completed.returncode == 0, completed.stderr
    assert output.exists()
    graph = json.loads(output.read_text(encoding="utf-8"))
    assert _file_paths(graph) == {"src/auth/api.py"}
    unresolved = [node for node in graph["nodes"] if node["node_class"] == "unresolved-reference"]
    assert unresolved
    assert all(node["location"]["path"] == "src/auth/api.py" for node in unresolved)
    assert graph["source_control"]["commit"] == _git(root, "rev-parse", "HEAD").stdout.strip()
    assert (
        stamp_path(output).read_text(encoding="ascii").strip()
        == hashlib.sha256(output.read_bytes()).hexdigest()
    )
    loaded = load_graph_file(output)
    assert loaded.document.to_dict() == graph


def test_analyze_scope_has_whole_repo_skip_refresh_and_force_lifecycle(tmp_path: Path) -> None:
    root = _scope_project(tmp_path)
    output = root / "docs" / "systems" / "auth" / "graph.json"
    assert _run_in(root, "analyze", "--scope", "auth").returncode == 0
    original = output.read_bytes()
    original_sidecar = stamp_path(output).read_bytes()
    original_mtime = output.stat().st_mtime_ns

    _write(root, "unselected.py", "value = 2\n")
    assert _git(root, "add", "unselected.py").returncode == 0
    assert _git(root, "commit", "-m", "unselected change").returncode == 0
    skipped = _run_in(root, "analyze", "--scope", "auth")
    assert skipped.returncode == 0, skipped.stderr
    assert "graph is up to date, skipping analysis" in skipped.stderr
    assert output.read_bytes() == original
    assert stamp_path(output).read_bytes() == original_sidecar
    assert output.stat().st_mtime_ns == original_mtime

    _write(root, "src/auth/api.py", "from external import helper\nhelper()\nvalue = 3\n")
    assert _git(root, "add", "src/auth/api.py").returncode == 0
    assert _git(root, "commit", "-m", "selected change").returncode == 0
    expected_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    refreshed = _run_in(root, "analyze", "--scope", "auth")
    assert refreshed.returncode == 0, refreshed.stderr
    assert (
        json.loads(output.read_text(encoding="utf-8"))["source_control"]["commit"]
        == expected_commit
    )

    forced_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    forced = _run_in(root, "analyze", "--scope", "auth", "--force")
    assert forced.returncode == 0, forced.stderr
    assert (
        json.loads(output.read_text(encoding="utf-8"))["source_control"]["commit"] == forced_commit
    )


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("--scope", "aut"), "unknown system: aut"),
        (("--scope", "auth", "extra.py"), "--scope cannot be combined with positional targets"),
        (("--scope", "auth", "--output", "other.json"), "--scope cannot be combined with --output"),
    ],
)
def test_analyze_scope_rejects_unknown_and_conflicting_invocations(
    tmp_path: Path, args: tuple[str, ...], expected: str
) -> None:
    root = _scope_project(tmp_path)
    completed = _run_in(root, "analyze", *args)
    assert completed.returncode == 2
    assert expected in completed.stderr


def test_analyze_scope_rejects_duplicate_missing_and_unsupported_declarations(
    tmp_path: Path,
) -> None:
    root = _scope_project(tmp_path, files="src/auth/missing.py")
    missing = _run_in(root, "analyze", "--scope", "auth")
    assert missing.returncode == 2
    assert "target does not exist" in missing.stderr

    second = root / "docs" / "systems" / "second"
    second.mkdir()
    (second / "system.toml").write_text(
        'schema_version = 1\nname = "auth"\nfiles = ["src/other.py"]\n', encoding="utf-8"
    )
    duplicate = _run_in(root, "analyze", "--scope", "auth")
    assert duplicate.returncode == 2
    assert "duplicate system name: auth" in duplicate.stderr

    (root / "docs" / "systems" / "second" / "system.toml").unlink()
    (root / "docs" / "systems" / "second").rmdir()
    (root / "docs" / "systems" / "auth" / "system.toml").write_text(
        'schema_version = 1\nname = "auth"\nfiles = ["src/notes.txt"]\n', encoding="utf-8"
    )
    _write(root, "src/notes.txt", "not source\n")
    unsupported = _run_in(root, "analyze", "--scope", "auth")
    assert unsupported.returncode == 2
    assert "unsupported source file" in unsupported.stderr


def test_analyze_from_nested_directory_uses_discovered_config_only(
    tmp_path: Path,
) -> None:
    """AC-01: no flags plus a nested cwd discovers the config and its targets."""
    root = _config_repo(tmp_path)
    _write(root, "app/one.py", "def one():\n    return 1\n")
    _write(root, "app/two.py", "def two():\n    return 2\n")
    _write_config(
        root,
        _MINOTAUR_CONFIG + 'root = "."\ngraph = "minotaur-graph.json"\ntargets = ["app"]\n',
    )
    nested = root / "work"
    nested.mkdir()

    completed = _run_in(nested, "analyze")

    graph_path = root / "minotaur-graph.json"
    assert completed.returncode == 0, completed.stderr
    assert graph_path.exists()
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert _file_paths(graph) == {"app/one.py", "app/two.py"}


def test_analyze_explicit_output_and_targets_override_only_their_fields(
    tmp_path: Path,
) -> None:
    """AC-02: an explicit --output and positionals override per field."""
    root = _config_repo(tmp_path)
    _write(root, "app/one.py", "def one():\n    return 1\n")
    _write(root, "extra.py", "def extra():\n    return 1\n")
    _write_config(
        root,
        _MINOTAUR_CONFIG + 'root = "."\ngraph = "configured.json"\ntargets = ["app"]\n',
    )

    completed = _run_in(root, "analyze", "--output", "out.json", "extra.py")

    assert completed.returncode == 0, completed.stderr
    assert not (root / "configured.json").exists()
    graph = json.loads((root / "out.json").read_text(encoding="utf-8"))
    assert _file_paths(graph) == {"extra.py"}
    assert graph["extensions"]["minotaur"]["selection"] == ["extra.py"]


def test_analyze_explicit_root_override_reanchors_only_selection(
    tmp_path: Path,
) -> None:
    """AC-02: an explicit --root changes selection anchoring, not the graph."""
    repo = _config_repo(tmp_path)
    project = repo / "project"
    _write(project, "src/app.py", "def app():\n    return 1\n")
    _write_config(
        project,
        _MINOTAUR_CONFIG + 'root = "."\ngraph = "g.json"\ntargets = ["src"]\n',
    )

    completed = _run_in(project, "analyze", "--root", str(repo))

    assert completed.returncode == 0, completed.stderr
    graph = json.loads((project / "g.json").read_text(encoding="utf-8"))
    assert _file_paths(graph) == {"project/src/app.py"}


def test_no_config_usage_error_writes_no_graph_or_stamp(tmp_path: Path) -> None:
    """AC-03: no config keeps argparse's exact usage error and writes nothing."""
    root = _config_repo(tmp_path)

    completed = _run_in(root, "analyze", "--root", str(root))

    assert completed.returncode == 2
    assert "the following arguments are required" in completed.stderr
    assert list(root.glob("*.json")) == []
    assert list(root.glob("*.sha256")) == []


def test_resolver_double_observes_each_consumer_once_never_diff_or_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-04(1): one resolver call per config-consuming trigger; none for help/diff."""
    root = _config_repo(tmp_path)
    _write(root, "src/app.py", "value = 1\n\ndef app():\n    return value\n")
    _write_config(
        root,
        _MINOTAUR_CONFIG + 'root = "."\ngraph = "g.json"\ntargets = ["src"]\n',
    )
    nested = root / "work"
    nested.mkdir()
    monkeypatch.chdir(nested)
    # Prime the configured graph while the double is not yet active.
    assert cli.main(["analyze"]) == 0

    original = cli.resolve_config
    calls: list[dict[str, object]] = []

    def recording(start: Path, **kwargs: object) -> object:
        calls.append(kwargs)
        return original(start, **kwargs)

    monkeypatch.setattr(cli, "resolve_config", recording)

    with pytest.raises(SystemExit) as help_info:
        cli.main(["analyze", "--help"])
    assert help_info.value.code == 0
    assert calls == []
    graph = root / "g.json"
    assert cli.main(["query", "diff", str(graph), str(graph)]) == 0
    assert calls == []

    assert cli.main(["analyze"]) == 0
    assert len(calls) == 1
    calls.clear()
    assert cli.main(["query", "unreferenced"]) == 0
    assert len(calls) == 1
    calls.clear()
    assert cli.main(["query", "context", "--site", "src/app.py:1"]) == 0
    assert len(calls) == 1
    calls.clear()
    assert cli.main(["visualize", "--output", str(root / "view.html")]) == 0
    assert len(calls) == 1
    calls.clear()
    assert cli.main(["analyze", "--output", str(root / "mixed.json"), str(root / "src")]) == 0
    assert len(calls) == 1
    assert calls[0]["explicit_graph"] == Path(root / "mixed.json")
    assert calls[0]["explicit_targets"] == (Path(root / "src"),)


def test_no_module_other_than_config_imports_a_toml_parser() -> None:
    """AC-04(2): consumer-side TOML re-resolution fails this source-text check."""
    package = Path(__file__).parents[1] / "src" / "minotaur"
    offenders = []
    for module in sorted(package.rglob("*.py")):
        relative = module.relative_to(package)
        if relative == Path("config.py"):
            continue
        text = module.read_text(encoding="utf-8")
        if "import tomllib" in text or "import tomli" in text:
            offenders.append(str(module))
    assert offenders == []


def test_every_config_validation_violation_exits_two_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """AC-05: each R-05/R-06 failure exits 2, names the field, writes no graph."""
    cases: list[tuple[str, str, str]] = [
        (
            "unsupported-schema-version",
            '[minotaur]\nschema_version = 2\ntargets = ["src"]\n',
            "schema_version",
        ),
        (
            "unknown-field",
            _MINOTAUR_CONFIG + 'bogus = 1\ntargets = ["src"]\n',
            "bogus",
        ),
        (
            "wrong-targets-type",
            _MINOTAUR_CONFIG + 'targets = "src"\n',
            "list of strings",
        ),
        ("missing-schema-version", '[minotaur]\ntargets = ["src"]\n', "schema_version"),
        ("missing-targets", "[minotaur]\nschema_version = 1\n", "targets"),
        ("empty-targets", "[minotaur]\nschema_version = 1\ntargets = []\n", "not be empty"),
        (
            "escaping-target",
            _MINOTAUR_CONFIG + 'root = "."\ntargets = ["../escape"]\n',
            "escapes root",
        ),
        (
            "expectations-dir-field",
            _MINOTAUR_CONFIG + 'expectations_dir = "expect"\ntargets = ["src"]\n',
            "expectations_dir",
        ),
        (
            "server-settings-field",
            _MINOTAUR_CONFIG + 'server = {host = "x"}\ntargets = ["src"]\n',
            "server",
        ),
    ]
    for label, body, expected in cases:
        root = _config_repo(tmp_path, label)
        _write_config(root, body)
        completed = _run_in(root, "analyze")
        assert completed.returncode == 2, label
        assert expected in completed.stderr, label
        assert list(root.glob("*.json")) == [], label
        assert list(root.glob("*.sha256")) == [], label


def test_config_with_systems_dir_still_analyzes_at_exit_zero(tmp_path: Path) -> None:
    """AC-01: a located config carrying systems_dir is accepted and analyzes."""
    root = _config_repo(tmp_path)
    _write(root, "app/one.py", "def one():\n    return 1\n")
    _write_config(
        root,
        _MINOTAUR_CONFIG + 'root = "."\ngraph = "g.json"\ntargets = ["app"]\n'
        'systems_dir = "docs/systems"\n',
    )

    completed = _run_in(root, "analyze")

    assert completed.returncode == 0, completed.stderr
    assert (root / "g.json").is_file()


def test_nonexistent_explicit_config_exits_two_names_path_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """AC-05: a --config path that does not exist fails before parsing."""
    root = _config_repo(tmp_path)
    missing = root / "missing.toml"

    completed = _run_in(root, "analyze", "--config", str(missing))

    assert completed.returncode == 2
    assert str(missing) in completed.stderr
    assert list(root.glob("*.json")) == []
    assert list(root.glob("*.sha256")) == []


def test_fully_explicit_analyze_beside_invalid_config_exits_two_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """AC-07: a present config is validated even when every flag is explicit."""
    root = _config_repo(tmp_path)
    _write(root, "src/app.py", "def app():\n    return 1\n")
    _write_config(root, '[minotaur]\nschema_version = 99\ntargets = ["src"]\n')
    output = root / "out.json"

    completed = _run_in(root, "analyze", "--root", str(root), "--output", str(output), "src")

    assert completed.returncode == 2
    assert "schema_version" in completed.stderr
    assert not output.exists()
    assert not stamp_path(output).exists()


def test_visualize_input_defaults_and_excerpts_follow_config_root_existence(
    tmp_path: Path,
) -> None:
    """AC-08: config input default; source-root default only when root exists."""
    root = _config_repo(tmp_path)
    _write(root, "src/app.py", "value = 1\n\ndef app():\n    return value\n")
    explicit_graph = tmp_path / "explicit.json"
    primed = _run_in(root, "analyze", "--root", str(root), "--output", str(explicit_graph), "src")
    assert primed.returncode == 0, primed.stderr

    _write_config(root, _MINOTAUR_CONFIG + 'root = "."\ngraph = "g.json"\ntargets = ["src"]\n')
    with_root = _run_in(
        root, "visualize", "--input", str(explicit_graph), "--output", str(root / "with.html")
    )
    assert with_root.returncode == 0, with_root.stderr
    assert "def app" in (root / "with.html").read_text(encoding="utf-8")

    _write_config(root, _MINOTAUR_CONFIG + 'root = "absent"\ngraph = "g.json"\ntargets = ["src"]\n')
    without_root = _run_in(
        root, "visualize", "--input", str(explicit_graph), "--output", str(root / "without.html")
    )
    assert without_root.returncode == 0, without_root.stderr
    assert "def app" not in (root / "without.html").read_text(encoding="utf-8")


def test_visualize_requires_output_even_when_config_is_present(tmp_path: Path) -> None:
    """AC-08: --output stays required beside a config; usage error exits 2."""
    root = _config_repo(tmp_path)
    _write(root, "src/app.py", "def app():\n    return 1\n")
    _write_config(root, _MINOTAUR_CONFIG + 'root = "."\ngraph = "g.json"\ntargets = ["src"]\n')
    primed = _run_in(root, "analyze")
    assert primed.returncode == 0, primed.stderr

    completed = _run_in(root, "visualize")

    assert completed.returncode == 2
    assert "--output" in completed.stderr
    assert "the following arguments are required" in completed.stderr


def test_query_context_fills_graph_and_root_from_config_and_diff_stays_free(
    tmp_path: Path,
) -> None:
    """AC-09: config-only context answers; diff beside an invalid config answers."""
    root = _config_repo(tmp_path)
    _write(root, "src/app.py", "value = 1\n\ndef app():\n    return value\n")
    _write_config(root, _MINOTAUR_CONFIG + 'root = "."\ngraph = "g.json"\ntargets = ["src"]\n')
    nested = root / "nested"
    nested.mkdir()
    primed = _run_in(nested, "analyze")
    assert primed.returncode == 0, primed.stderr

    context_run = _run_in(nested, "query", "context", "--site", "src/app.py:3")
    assert context_run.returncode == 0, context_run.stderr
    assert "def app()" in context_run.stdout

    _write_config(root, "[minotaur]\nschema_version = 99\n")
    diff = _run_in(nested, "query", "diff", str(root / "g.json"), str(root / "g.json"))
    assert diff.returncode == 0, diff.stderr


def test_config_targets_do_not_leak_into_unreferenced_path_filters(
    tmp_path: Path,
) -> None:
    """AC-10: config targets stay analysis-only; query filters stay root-relative."""
    root = _config_repo(tmp_path)
    _write(root, "app/mod.py", "def mod_symbol():\n    return 1\n")
    _write(root, "extra/other.py", "def other_symbol():\n    return 1\n")
    _write_config(root, _MINOTAUR_CONFIG + 'root = "."\ngraph = "g.json"\ntargets = ["app"]\n')
    nested = root / "nested"
    nested.mkdir()
    primed = _run_in(root, "analyze", "--output", "g.json", "--force", "app", "extra")
    assert primed.returncode == 0, primed.stderr

    unfiltered = _run_in(nested, "query", "unreferenced")
    assert unfiltered.returncode == 0, unfiltered.stderr
    assert "mod_symbol" in unfiltered.stdout
    assert "other_symbol" in unfiltered.stdout

    filtered = _run_in(nested, "query", "unreferenced", "extra")
    assert filtered.returncode == 0, filtered.stderr
    assert "other_symbol" in filtered.stdout
    assert "mod_symbol" not in filtered.stdout


def test_config_graph_spelling_keeps_sidecar_trust_until_an_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-13: one canonical config graph spelling; explicit override revalidates safely."""
    root = _config_repo(tmp_path)
    _write(root, "src/app.py", "value = 1\n\ndef app():\n    return value\n")
    (root / "graphs").mkdir()
    _write_config(
        root,
        _MINOTAUR_CONFIG + 'root = "."\ngraph = "graphs/tracked.json"\ntargets = ["src"]\n',
    )
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert cli.main(["analyze"]) == 0
    tracked = (root / "graphs" / "tracked.json").resolve()
    assert tracked.exists()
    assert stamp_path(tracked).exists()

    original = loading._validate_wire_shape
    validated: list[str] = []

    def record(raw: object) -> None:
        validated.append("schema")
        original(raw)

    monkeypatch.setattr(loading, "_validate_wire_shape", record)

    assert cli.main(["query", "unreferenced"]) == 0
    assert validated == []

    alternate = root / "alternate.json"
    alternate.write_bytes(tracked.read_bytes())
    explicit = cli.main(["query", "unreferenced", "--graph", str(alternate), "--root", str(root)])
    assert explicit == 0
    assert validated == ["schema"]
    assert stamp_path(alternate).exists()


def test_visualize_without_input_renders_content_from_the_config_graph(
    tmp_path: Path,
) -> None:
    """AC-08: with --input omitted the config graph is the input and renders.

    The AC-04 resolver-double proof only asserts exit 0 for a config-input
    visualize; this asserts the rendered HTML actually carries content from the
    config graph (the file ``analyze`` wrote to the configured ``graph``), so a
    default that pointed at a different, existing graph would fail here.
    """
    root = _config_repo(tmp_path)
    _write(root, "src/app.py", "value = 1\n\ndef app():\n    return value\n")
    _write_config(root, _MINOTAUR_CONFIG + 'root = "."\ngraph = "g.json"\ntargets = ["src"]\n')
    primed = _run_in(root, "analyze")
    assert primed.returncode == 0, primed.stderr
    assert (root / "g.json").exists()

    completed = _run_in(root, "visualize", "--output", str(root / "config.html"))

    assert completed.returncode == 0, completed.stderr
    assert "def app" in (root / "config.html").read_text(encoding="utf-8")


def test_equals_form_missing_config_exits_two_beside_a_valid_walk_up_config(
    tmp_path: Path,
) -> None:
    """D-05/R-02: ``--config=PATH`` names the missing path and never falls back.

    The D-05 raw-argv scanner reads ``--config=VALUE`` with its own equals
    branch; if that branch broke, an invocation with a valid walk-up config
    present would silently analyze under the walk-up config instead of failing
    on the explicitly named missing file.  Asserting exit 2, the named path on
    stderr, and no graph or stamp written discriminates the two envelopes.
    """
    root = _config_repo(tmp_path)
    _write(root, "src/app.py", "def app():\n    return 1\n")
    _write_config(root, _MINOTAUR_CONFIG + 'root = "."\ngraph = "walkup.json"\ntargets = ["src"]\n')
    missing = root / "missing.toml"

    completed = _run_in(root, "analyze", f"--config={missing}")

    assert completed.returncode == 2
    assert str(missing) in completed.stderr
    assert not (root / "walkup.json").exists()
    assert list(root.glob("*.json")) == []
    assert list(root.glob("*.sha256")) == []


def test_equals_form_empty_config_value_exits_two_naming_the_option_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """An empty ``--config=`` value is a config error, not a nonexistent path.

    The D-05 raw-argv scanner turns ``--config=`` into an empty raw value;
    before the empty-value guard ``Path("")`` collapsed to the working
    directory and the locate phase reported ``config file does not exist: .``
    — naming a real directory instead of the empty value.  Asserting exit 2,
    stderr naming ``--config`` and the empty value (and never ``does not
    exist``), plus no graph or stamp written, pins the corrected diagnostic.
    """
    root = _config_repo(tmp_path)

    completed = _run_in(root, "analyze", "--config=")

    assert completed.returncode == 2
    assert "--config" in completed.stderr
    assert "''" in completed.stderr
    assert "does not exist" not in completed.stderr
    assert list(root.glob("*.json")) == []
    assert list(root.glob("*.sha256")) == []


def test_empty_config_value_never_falls_back_to_a_walk_up_config(tmp_path: Path) -> None:
    """An empty ``--config=`` stays an error even beside a valid walk-up config.

    An explicit ``--config`` with an empty value must never silently walk up
    to a discoverable ``.minotaur.toml`` and analyze under it: the walk-up
    config's graph must not be created and no fallback analysis may run.  If
    the scanner ever treated the empty value as "no explicit config", this
    invocation would succeed under the walk-up config and create
    ``walkup.json`` instead of exiting 2.
    """
    root = _config_repo(tmp_path)
    _write(root, "src/app.py", "def app():\n    return 1\n")
    _write_config(root, _MINOTAUR_CONFIG + 'root = "."\ngraph = "walkup.json"\ntargets = ["src"]\n')

    completed = _run_in(root, "analyze", "--config=")

    assert completed.returncode == 2
    assert "--config" in completed.stderr
    assert "''" in completed.stderr
    assert "does not exist" not in completed.stderr
    assert not (root / "walkup.json").exists()
    assert list(root.glob("*.json")) == []
    assert list(root.glob("*.sha256")) == []


def test_space_separated_empty_config_value_is_rejected_like_the_equals_form(
    tmp_path: Path,
) -> None:
    """``--config ""`` reaches the empty-value rejection via the space branch.

    The D-05 scanner consumes a following token for ``--config VALUE``; an
    empty token exercises that branch (not the ``--config=VALUE`` equals
    branch) and must land on the same exit-2 rejection naming the option and
    the empty value, with no fallback analysis under a walk-up config.
    """
    root = _config_repo(tmp_path)
    _write(root, "src/app.py", "def app():\n    return 1\n")
    _write_config(root, _MINOTAUR_CONFIG + 'root = "."\ngraph = "walkup.json"\ntargets = ["src"]\n')

    completed = _run_in(root, "analyze", "--config", "")

    assert completed.returncode == 2
    assert "--config" in completed.stderr
    assert "''" in completed.stderr
    assert "does not exist" not in completed.stderr
    assert not (root / "walkup.json").exists()
    assert list(root.glob("*.json")) == []
    assert list(root.glob("*.sha256")) == []
