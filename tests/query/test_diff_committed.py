"""Behavioral coverage for committed-reference ``query diff`` mode."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model import loading
from minotaur.graph_model.loading import stamp_path


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the installed module from a real subprocess in ``cwd``."""
    return subprocess.run(
        [sys.executable, "-m", "minotaur", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _commit_all(root: Path, message: str = "fixture") -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-qm", message)


def _graph_fixture(tmp_path: Path, *, git_repo: bool = True) -> tuple[Path, Path, Path]:
    root = _repo(tmp_path) if git_repo else tmp_path / "project"
    if not git_repo:
        root.mkdir()
    _write_config(root)
    source = root / "app.py"
    source.write_text("def app():\n    return 1\n", encoding="utf-8")
    return root, source, root / "graph.json"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Minotaur Tests")
    return root


def _write_config(root: Path, *, targets: str = 'targets = ["app.py"]') -> None:
    (root / ".minotaur.toml").write_text(
        f'[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n{targets}\n',
        encoding="utf-8",
    )


def test_committed_mode_compares_head_graph_to_current_files_without_mutation(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    root = _repo(tmp_path)
    _write_config(root)
    source = root / "app.py"
    source.write_text("def app():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(root)  # type: ignore[attr-defined]
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    before_graph = graph.read_bytes()
    before_sidecar = sidecar.read_bytes()
    source.write_text("def app():\n    return 1\n\ndef added():\n    pass\n", encoding="utf-8")
    tracked_before = {
        relative: (root / relative).read_bytes()
        for relative in subprocess.run(
            ["git", "ls-files"], cwd=root, text=True, capture_output=True, check=True
        ).stdout.splitlines()
    }
    status = cli.main(["query", "diff"])
    captured = capsys.readouterr()

    assert status == 1
    assert "relocated" in captured.out or "no changes" not in captured.out
    assert graph.read_bytes() == before_graph
    assert sidecar.read_bytes() == before_sidecar
    tracked_after = {relative: (root / relative).read_bytes() for relative in tracked_before}
    assert tracked_after == tracked_before


def test_committed_mode_uses_head_when_dirty_disk_graph_is_mutated(
    tmp_path: Path, monkeypatch: object
) -> None:
    root = _repo(tmp_path)
    _write_config(root)
    source = root / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)  # type: ignore[attr-defined]
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    graph_before = graph.read_bytes()
    sidecar_before = sidecar.read_bytes()
    graph.write_bytes(b"not the committed graph")
    sidecar.write_bytes(b"0" * 64)
    source.write_text("value = 2\n\ndef added():\n    pass\n", encoding="utf-8")

    assert cli.main(["query", "diff"]) == 1
    assert graph.read_bytes() != graph_before
    assert sidecar.read_bytes() != sidecar_before


def test_committed_mode_scope_reads_graph_beside_definition_directory(
    tmp_path: Path, monkeypatch: object
) -> None:
    root = _repo(tmp_path)
    _write_config(root)
    source = root / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    definition_dir = root / "docs" / "systems" / "actual"
    definition_dir.mkdir(parents=True)
    (definition_dir / "system.toml").write_text(
        'schema_version = 1\nname = "declared"\nfiles = ["app.py"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)  # type: ignore[attr-defined]
    assert cli.main(["analyze", "--scope", "declared"]) == 0
    assert (definition_dir / "graph.json").exists()
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    source.write_text("value = 2\n\ndef added():\n    pass\n", encoding="utf-8")

    assert cli.main(["query", "diff", "--scope", "declared"]) == 1


def test_committed_mode_outside_git_uses_disk_graph_without_stamping(
    tmp_path: Path, monkeypatch: object
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _write_config(root)
    source = root / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)  # type: ignore[attr-defined]
    assert cli.main(["analyze"]) == 0
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    before = (graph.read_bytes(), sidecar.read_bytes())
    source.write_text("value = 2\n\ndef added():\n    pass\n", encoding="utf-8")

    assert cli.main(["query", "diff"]) == 1
    assert (graph.read_bytes(), sidecar.read_bytes()) == before


def test_config_free_and_config_located_diff_help_expose_their_own_grammar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    free = tmp_path / "free"
    free.mkdir()
    monkeypatch.chdir(free)
    with pytest.raises(SystemExit) as free_help:
        cli.main(["query", "diff", "--help"])
    free_output = capsys.readouterr().out
    free_help_text = " ".join(free_output.split())
    assert free_help.value.code == 0
    assert "OLD NEW" in free_output
    assert "--validate" in free_output
    assert "--scope NAME" not in free_output
    assert "--config CONFIG" not in free_output
    assert "1 means structures differ" in free_help_text
    assert "caller decides the consequence" in free_help_text

    configured = tmp_path / "configured"
    configured.mkdir()
    _write_config(configured)
    monkeypatch.chdir(configured)
    with pytest.raises(SystemExit) as located_help:
        cli.main(["query", "diff", "--help"])
    located_output = capsys.readouterr().out
    located_help_text = " ".join(located_output.split())
    assert located_help.value.code == 0
    assert "[OLD] [NEW]" in located_output
    assert "--scope NAME" in located_output
    assert "--config CONFIG" in located_output
    assert "0 means structures are identical" in located_help_text
    assert "1 means structures differ" in located_help_text
    assert "caller decides the consequence" in located_help_text


def test_config_free_bare_and_mixed_diff_grammar_is_refused_without_reading_graph(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    # Invalid JSON makes a disk read observable if mode classification starts
    # loading before argparse rejects the strict grammar.
    (root / "bad.json").write_text("not-json", encoding="utf-8")
    bare = _run(root, "query", "diff")
    one = _run(root, "query", "diff", "bad.json")
    mixed = _run(root, "query", "diff", "bad.json", "--scope", "app")
    assert bare.returncode == 2
    assert "the following arguments are required" in bare.stderr
    assert "OLD" in bare.stderr and "NEW" in bare.stderr
    assert one.returncode == 2
    assert "the following arguments are required" in one.stderr
    assert mixed.returncode == 2
    assert "unrecognized arguments: --scope" in mixed.stderr


def test_explicit_diff_remains_config_free_beside_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _, graph = _graph_fixture(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    (root / ".minotaur.toml").write_text("[minotaur]\nschema_version = 99\n", encoding="utf-8")
    assert cli.main(["query", "diff", str(graph), str(graph)]) == 0
    assert capsys.readouterr().err == ""


def test_config_located_diff_rejects_unknown_and_duplicate_scopes(
    tmp_path: Path,
) -> None:
    root, source, _ = _graph_fixture(tmp_path)
    (root / "outside.py").write_text("value = 1\n", encoding="utf-8")
    systems = root / "docs" / "systems"
    for directory, name in (("actual", "declared"), ("other", "other")):
        target = systems / directory
        target.mkdir(parents=True)
        listed_file = "app.py" if name == "declared" else "outside.py"
        (target / "system.toml").write_text(
            f'schema_version = 1\nname = "{name}"\nfiles = ["{listed_file}"]\n',
            encoding="utf-8",
        )
    # Keep source in scope and make definitions part of HEAD, but no graph is
    # needed: scope resolution must fail before the committed artifact load.
    _commit_all(root)
    unknown = _run(root, "query", "diff", "--scope", "declard")
    assert unknown.returncode == 2
    assert "unknown system: declard" in unknown.stderr
    assert "declared" in unknown.stderr
    duplicate = root / "docs" / "systems" / "duplicate"
    duplicate.mkdir()
    (duplicate / "system.toml").write_text(
        'schema_version = 1\nname = "declared"\nfiles = ["app.py"]\n', encoding="utf-8"
    )
    duplicate_result = _run(root, "query", "diff", "--scope", "declared")
    assert duplicate_result.returncode == 2
    assert "duplicate system name: declared" in duplicate_result.stderr
    assert source.read_text(encoding="utf-8").startswith("def app")


def test_committed_mode_requires_both_head_artifacts_and_never_reads_dirty_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, source, graph = _graph_fixture(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _commit_all(root)
    sidecar = stamp_path(graph)
    graph_bytes = graph.read_bytes()
    sidecar_bytes = sidecar.read_bytes()
    # An untracked dirty graph cannot satisfy the strict HEAD pair when the
    # committed graph is absent.
    _git(root, "rm", "-q", str(graph.relative_to(root)), str(sidecar.relative_to(root)))
    _commit_all(root, "remove graph pair")
    graph.write_bytes(graph_bytes)
    sidecar.write_bytes(sidecar_bytes)
    missing_graph = _run(root, "query", "diff")
    assert missing_graph.returncode == 2
    assert "committed graph is absent at HEAD" in missing_graph.stderr
    _git(root, "add", str(graph.relative_to(root)))
    _git(root, "commit", "-qm", "graph without sidecar")
    missing_sidecar = _run(root, "query", "diff")
    assert missing_sidecar.returncode == 2
    assert "committed graph sidecar is absent at HEAD" in missing_sidecar.stderr


def test_committed_mismatched_head_sidecar_forces_full_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, source, graph = _graph_fixture(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    sidecar = stamp_path(graph)
    sidecar.write_text("0" * 64 + "\n", encoding="ascii")
    committed_bytes = (graph.read_bytes(), sidecar.read_bytes())
    _commit_all(root)
    calls: list[dict[str, object]] = []
    original = loading._validate_wire_shape

    def recording(raw: dict[str, object]) -> None:
        calls.append(raw)
        original(raw)

    monkeypatch.setattr(loading, "_validate_wire_shape", recording)
    result = cli.main(["query", "diff"])
    assert result == 0
    assert calls, "mismatched HEAD sidecar must take the full validation path"
    assert (graph.read_bytes(), sidecar.read_bytes()) == committed_bytes
    assert source.read_text(encoding="utf-8").startswith("def app")


def test_probe_unavailable_fallback_reads_disk_without_stamping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, source, graph = _graph_fixture(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    before = (graph.read_bytes(), stamp_path(graph).read_bytes())
    _commit_all(root)
    source.write_text("def app():\n    return 2\n\ndef changed():\n    pass\n", encoding="utf-8")

    monkeypatch.setattr("minotaur.git.run_git", lambda *args, **kwargs: None)
    assert cli.main(["query", "diff"]) == 1
    assert (graph.read_bytes(), stamp_path(graph).read_bytes()) == before


def test_committed_relocation_is_exit_one_and_json_has_only_diff_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, source, graph = _graph_fixture(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _commit_all(root)
    source.write_text("\n\ndef app():\n    return 1\n", encoding="utf-8")
    assert cli.main(["query", "diff", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["added"] == []
    assert payload["removed"] == []
    assert payload["relationships_added"] == []
    assert payload["relationships_removed"] == []
    assert [item["symbol"] for item in payload["relocated"]] == ["app.app"]
    assert set(payload) == {
        "query",
        "added",
        "removed",
        "relocated",
        "relationships_added",
        "relationships_removed",
    }


def test_scope_ignores_out_of_scope_edit_and_json_matches_explicit_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, source, graph = _graph_fixture(tmp_path)
    outside = root / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    definition = root / "docs" / "systems" / "app"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "app"\nfiles = ["app.py"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze", "--scope", "app"]) == 0
    scoped_graph = definition / "graph.json"
    _commit_all(root)
    outside.write_text("value = 2\n", encoding="utf-8")
    assert cli.main(["query", "diff", "--scope", "app", "--json"]) == 0
    committed_payload = json.loads(capsys.readouterr().out)
    assert committed_payload == {
        "query": "diff",
        "added": [],
        "removed": [],
        "relocated": [],
        "relationships_added": [],
        "relationships_removed": [],
    }
    explicit_graph = tmp_path / "explicit.json"
    explicit_graph.write_bytes(scoped_graph.read_bytes())
    assert cli.main(["query", "diff", str(scoped_graph), str(explicit_graph), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == committed_payload
    assert source.read_text(encoding="utf-8").startswith("def app")


def test_diagnostic_is_stderr_only_and_does_not_change_structure_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, source, graph = _graph_fixture(tmp_path)
    source.write_text("def app(:\n", encoding="utf-8")
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 1
    capsys.readouterr()
    _commit_all(root)
    status = cli.main(["query", "diff", "--json"])
    captured = capsys.readouterr()
    assert status == 0
    assert "parse-error" in captured.err
    payload = json.loads(captured.out)
    assert payload["added"] == []
    assert payload["removed"] == []
    assert payload["relocated"] == []
