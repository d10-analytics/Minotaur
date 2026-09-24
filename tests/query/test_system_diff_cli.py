"""Public composition tests for configured ``query diff --systems``."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.evidence import Evidence, Rule
from minotaur.graph_model.loading import graph_digest, stamp_path
from minotaur.graph_model.provenance import NodeClass, Provenance
from minotaur.language_interpreter.contract import AnalysisResult


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Minotaur Tests")
    return root


def _configured_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "api.py").write_text("def receive():\n    return 1\n", encoding="utf-8")
    (root / "consumer.py").write_text(
        "from app.api import receive\n\ndef consume():\n    return receive()\n", encoding="utf-8"
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "app"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "App"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    return root


def _configured_sql_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "schema.sql").write_text("CREATE TABLE base (id int)\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["src"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "sql"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "sql"\nfiles = ["src/schema.sql"]\n',
        encoding="utf-8",
    )
    return root


def _state(root: Path) -> dict[str, object]:
    """Capture every comparison input plus Git index and porcelain state."""
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    files: dict[str, object] = {}
    for value in sorted(set(tracked)):
        path = root / value
        if not os.path.lexists(path):
            files[value] = None
        elif path.is_symlink():
            files[value] = ("symlink", os.readlink(path))
        elif path.is_file():
            files[value] = path.read_bytes()
        else:
            files[value] = "directory"
    return {
        "files": files,
        "index": subprocess.run(
            ["git", "ls-files", "--stage"], cwd=root, text=True, capture_output=True, check=True
        ).stdout,
        "status": subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout,
    }


def _assert_state(root: Path, before: dict[str, object]) -> None:
    assert _state(root) == before


def _serialized_file_boundary_fixture(payload: dict[str, object], label: str) -> dict[str, object]:
    """Keep one valid source-to-file boundary while changing its file label."""
    nodes = payload["nodes"]
    relationships = payload["relationships"]
    assert isinstance(nodes, list)
    assert isinstance(relationships, list)
    file_node = next(
        node
        for node in nodes
        if isinstance(node, dict)
        and node.get("node_class") == NodeClass.FILE.value
        and node.get("path") == "app/api.py"
    )
    source_id = next(
        node["id"]
        for node in nodes
        if isinstance(node, dict) and node.get("label") == "consumer.consume"
    )
    target_id = next(
        node["id"]
        for node in nodes
        if isinstance(node, dict) and node.get("label") == "app.api.receive"
    )
    assert isinstance(file_node, dict)
    file_id = file_node["id"]
    relation = next(
        relationship
        for relationship in relationships
        if isinstance(relationship, dict)
        and relationship.get("kind") == "calls"
        and relationship.get("source") == source_id
        and relationship.get("target") == target_id
    )
    assert isinstance(relation, dict)
    result = dict(payload)
    result["nodes"] = [
        {**node, "label": label} if node.get("id") == file_id else node for node in nodes
    ]
    result["relationships"] = [{**relation, "target": file_id}]
    return result


def _controlled_file_boundary_result(
    result: AnalysisResult, *, label: str, evidence_only: bool = False
) -> AnalysisResult:
    """Supply one valid current graph for the approved FILE_PATH proof row."""
    nodes = result.document.nodes
    file_node = next(
        node for node in nodes if node.node_class is NodeClass.FILE and node.path == "app/api.py"
    )
    source_id = next(node.id for node in nodes if node.label == "consumer.consume")
    target_id = next(node.id for node in nodes if node.label == "app.api.receive")
    relationship = next(
        relationship
        for relationship in result.document.relationships
        if relationship.kind == "calls"
        and relationship.source == source_id
        and relationship.target == target_id
    )
    if evidence_only:
        evidence = relationship.evidence + (
            Evidence(Provenance.CURATED_RULE, rule=Rule("controlled-proof")),
        )
        relationship = replace(relationship, evidence=evidence)
    file_node = replace(file_node, label=label)
    updated_nodes = tuple(
        replace(node, label=label) if node.id == file_node.id else node for node in nodes
    )
    document = replace(
        result.document,
        nodes=updated_nodes,
        relationships=(replace(relationship, target=file_node.id),),
    )
    return replace(result, document=document)


def _commit_file_boundary_graph(root: Path, *, label: str) -> None:
    graph = root / "graph.json"
    payload = json.loads(graph.read_text(encoding="utf-8"))
    transformed = _serialized_file_boundary_fixture(payload, label)
    content = json.dumps(transformed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    graph.write_bytes(content)
    stamp_path(graph).write_bytes((graph_digest(content) + "\n").encode("ascii"))
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "file boundary fixture")


def test_systems_help_does_not_parse_malformed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / ".minotaur.toml").write_text("not = [valid", encoding="utf-8")
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["query", "diff", "--systems", "--help"])

    assert excinfo.value.code == 0
    assert "usage: minotaur query diff" in capsys.readouterr().out


def test_systems_mode_requires_config_and_keeps_stdout_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    monkeypatch.chdir(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "current input" in captured.err
    assert "config" in captured.err


def test_systems_grammar_requires_zero_or_two_revisions_and_rejects_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    with pytest.raises(SystemExit) as scope_error:
        cli.main(["query", "diff", "--systems", "--scope", "App"])
    assert scope_error.value.code == 2
    assert "unrecognized arguments: --scope" in capsys.readouterr().err

    assert cli.main(["query", "diff", "--systems", "HEAD"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "zero or exactly two revisions" in captured.err

    assert cli.main(["query", "diff", "--systems", "missing-a", "missing-b"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before" in captured.err
    assert "missing-a" in captured.err


@pytest.mark.parametrize("override", ("--graph", "--root"))
def test_systems_route_rejects_unused_graph_and_root_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    override: str,
) -> None:
    """AC-07: the systems route never silently accepts an unused graph/root."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["query", "diff", "--systems", override, "unused.json"])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"unrecognized arguments: {override}" in captured.err


def test_systems_composes_real_acquisition_comparison_and_rendering_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    before = (graph.read_bytes(), sidecar.read_bytes())
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    (root / "consumer.py").write_text(
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
        encoding="utf-8",
    )
    before_state = _state(root)

    status = cli.main(["query", "diff", "--systems", "--system=App", "--details", "--validate"])
    captured = capsys.readouterr()

    assert status == 1
    assert captured.out.startswith("surface added: App app/api.py.app.api.send\n")
    assert "consumer changed: App <- consumer.py" in captured.out
    assert "old evidence: unavailable" in captured.out
    assert "new evidence: [" in captured.out
    assert graph.read_bytes() == before[0]
    assert sidecar.read_bytes() == before[1]
    _assert_state(root, before_state)


def test_system_filter_unknown_name_is_error_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--system", "Ap"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown system: Ap" in captured.err


def test_systems_json_status_is_typed_and_context_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    before = _state(root)

    status = cli.main(["query", "diff", "--systems", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert status == 0
    assert '"changed":false' in captured.out
    assert '"coverage":{"new":' in captured.out
    comparison = payload["comparison"]
    assert comparison["schema_version"] == 1
    assert comparison["selected_system"] is None
    assert comparison["before"]["kind"] == "commit"
    assert comparison["after"]["kind"] == "working-tree"
    assert captured.err == ""
    _assert_state(root, before)


def test_systems_combined_json_details_and_validation_keep_typed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    (root / "consumer.py").write_text(
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
        encoding="utf-8",
    )
    before = _state(root)

    status = cli.main(
        ["query", "diff", "--systems", "--system=App", "--json", "--details", "--validate"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert status == 1
    assert payload["changed"] is True
    assert payload["exit_code"] == 1
    assert payload["boundary_changes"]
    assert payload["boundary_changes"][0]["new"]["relationships"]
    _assert_state(root, before)
    assert captured.err == ""


@pytest.mark.parametrize("flag", ("--system", "--details"))
def test_systems_only_flags_are_rejected_without_systems_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["query", "diff", "--system=App"] if flag == "--system" else ["query", "diff", flag]
        )

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments" in captured.err


def test_systems_linked_config_fails_before_historical_read_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    before = (graph.read_bytes(), sidecar.read_bytes())
    alias = root / "config-alias.toml"
    alias.symlink_to(root / ".minotaur.toml")
    before_index = subprocess.run(
        ["git", "ls-files", "--stage"], cwd=root, text=True, capture_output=True, check=True
    ).stdout

    assert cli.main(["query", "diff", "--systems", "--config", str(alias)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "config-alias.toml" in captured.err
    assert "symbolic link" in captured.err
    assert (graph.read_bytes(), sidecar.read_bytes()) == before
    assert (
        subprocess.run(
            ["git", "ls-files", "--stage"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        == before_index
    )
    assert (
        subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        == "?? config-alias.toml\n"
    )


def test_systems_stale_committed_graph_does_not_change_source_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    graph = root / "graph.json"
    corrupt = b'{"schema_version":1}'
    graph.write_bytes(corrupt)
    stamp_path(graph).write_bytes((graph_digest(corrupt) + "\n").encode("ascii"))
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "invalid historical graph")
    committed = (graph.read_bytes(), stamp_path(graph).read_bytes())

    # The saved graph is inert: unchanged source still reports equality even
    # though the committed graph is unreadable.
    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert captured.err == ""

    # A source edit is detected from source rather than from the saved graph.
    (root / "app" / "unrelated.py").write_text("value = 1\n", encoding="utf-8")
    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is True
    assert any(node["status"] == "added" for node in payload["comparison"]["nodes"])
    assert captured.err == ""
    assert (graph.read_bytes(), stamp_path(graph).read_bytes()) == committed


def test_unaffected_system_filter_returns_zero_when_complete_result_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    (root / "other.py").write_text("def other():\n    return 1\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py", "other.py"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "other"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "Other"\nfiles = ["other.py"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )

    assert cli.main(["query", "diff", "--systems", "--system", "Other"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("no system differences\n")
    assert "old coverage:" in captured.out
    assert "new coverage:" in captured.out
    assert captured.err == ""

    # The scoped status is zero while the retained context still reports the
    # complete comparison as changed.
    assert cli.main(["query", "diff", "--systems", "--system", "Other", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert payload["selected_system"] == "Other"
    assert payload["comparison"]["changed"] is True


@pytest.mark.parametrize(
    "config_args",
    [
        ("--config", ".minotaur.toml"),
        ("--config=.minotaur.toml",),
        ("--config", "missing.toml", "--config", ".minotaur.toml"),
    ],
)
def test_systems_config_forms_preserve_last_value_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config_args: tuple[str, ...],
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    (root / "consumer.py").write_text(
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
        encoding="utf-8",
    )

    assert cli.main(["query", "diff", "--systems", *config_args]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("surface added: App")
    assert captured.err == ""


def test_systems_nested_dotdot_config_route_normalizes_before_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "nested").mkdir()

    assert cli.main(["query", "diff", "--systems", "--config", "nested/../.minotaur.toml"]) == 0
    captured = capsys.readouterr()
    assert "no system differences" in captured.out
    assert captured.err == ""


def test_plain_diff_accepts_linked_explicit_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    alias = root / "config-alias.toml"
    alias.symlink_to(root / ".minotaur.toml")

    assert cli.main(["query", "diff", "--config", str(alias)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "no changes\n"
    assert captured.err == ""


def test_systems_without_git_fails_as_current_input_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "nongit"
    root.mkdir()
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["app.py"]\n',
        encoding="utf-8",
    )
    (root / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "current input" in captured.err
    assert "Git" in captured.err


def test_systems_unborn_git_fails_as_historical_input_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["app.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before Git input" in captured.err
    assert "HEAD" in captured.err


def test_invalid_unselected_current_definition_fails_before_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    (root / "other.py").write_text("def other():\n    return 1\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py", "other.py"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "other"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "Other"\nfiles = ["other.py"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (definition / "system.toml").write_text(
        "schema_version = 1\nname = [invalid\n", encoding="utf-8"
    )
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--system", "App"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "other/system.toml" in captured.err
    assert "invalid current system definitions" in captured.err
    _assert_state(root, before)


def test_current_source_diagnostic_outside_selected_system_is_global_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "consumer.py").write_text("def broken(:\n", encoding="utf-8")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--system", "App"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "consumer.py" in captured.err
    assert "captured source analysis produced diagnostics" in captured.err
    _assert_state(root, before)


def test_systems_diff_renders_sql_warning_and_rejects_sql_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_sql_repo(tmp_path)
    monkeypatch.chdir(root)
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["src"]\n[minotaur.sql]\nview_depth_threshold = 3\n',
        encoding="utf-8",
    )
    warning_source = (
        "CREATE TABLE base (id int)\nGO\n"
        "CREATE VIEW v1 AS SELECT * FROM base\nGO\n"
        "CREATE VIEW v2 AS SELECT * FROM v1\n"
    )
    (root / "src" / "schema.sql").write_text(warning_source, encoding="utf-8")
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["src"]\n[minotaur.sql]\nview_depth_threshold = 1\n',
        encoding="utf-8",
    )

    status = cli.main(["query", "diff", "--systems", "--json"])
    captured = capsys.readouterr()
    assert status == 0
    assert "view-depth-warning" in captured.err
    assert json.loads(captured.out)["changed"] is False

    (root / "src" / "schema.sql").write_text(
        warning_source + "\nGO\nCREATE TABLE broken (\n", encoding="utf-8"
    )
    status = cli.main(["query", "diff", "--systems", "--json"])
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert "captured source analysis produced diagnostics" in captured.err


@pytest.mark.parametrize("route", ("root", "target", "systems"))
@pytest.mark.parametrize("nested", (False, True))
def test_systems_current_link_and_nested_routes_fail_before_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    route: str,
    nested: bool,
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    graph = root / "graph.json"
    if nested:
        (root / "nested").symlink_to(root / "app")
        route_value = "nested/.."
    else:
        (root / "route-link").symlink_to(root / "app")
        route_value = "route-link"
    config = (root / ".minotaur.toml").read_text(encoding="utf-8")
    if route == "root":
        config = config.replace('root = "."', f'root = "{route_value}"')
    elif route == "target":
        config = config.replace('"app"', f'"{route_value}/app"')
    else:
        config += f'systems_dir = "{route_value}/docs/systems"\n'
    (root / ".minotaur.toml").write_text(config, encoding="utf-8")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "current input" in captured.err
    assert graph.read_bytes() == before["files"]["graph.json"]
    _assert_state(root, before)


def test_systems_historical_link_is_rejected_after_current_route_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    config_bytes = (root / ".minotaur.toml").read_bytes()
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "historical.toml").write_bytes(config_bytes)
    (root / ".minotaur.toml").unlink()
    (root / ".minotaur.toml").symlink_to("historical.toml")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "historical link")
    (root / ".minotaur.toml").unlink()
    (root / ".minotaur.toml").write_bytes(config_bytes)
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before revision" in captured.err
    assert "symbolic link" in captured.err
    _assert_state(root, before)


def test_systems_historical_gitlink_is_rejected_after_current_route_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    config_bytes = (root / ".minotaur.toml").read_bytes()
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()
    _git(root, "rm", "--cached", "-q", ".minotaur.toml")
    result = subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"160000,{commit},.minotaur.toml"],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    _git(root, "commit", "-qm", "historical gitlink")
    (root / ".minotaur.toml").write_bytes(config_bytes)
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before revision" in captured.err
    assert "Gitlink" in captured.err
    _assert_state(root, before)


def test_systems_pinned_read_failure_is_attributed_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    before = _state(root)
    from minotaur.git import PinnedCommit

    original = PinnedCommit.read_blob

    def fail_read(self: PinnedCommit, relative: str) -> bytes:
        if relative == "graph.json":
            raise OSError("simulated pinned read failure")
        return original(self, relative)

    monkeypatch.setattr(PinnedCommit, "read_blob", fail_read)
    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before revision" in captured.err
    assert "graph.json" in captured.err
    _assert_state(root, before)


def test_systems_target_absent_on_both_sides_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    config = (root / ".minotaur.toml").read_text(encoding="utf-8")
    (root / ".minotaur.toml").write_text(config.replace('"app"', '"missing.py"'), encoding="utf-8")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "absent on both comparison sides" in captured.err
    assert "missing.py" in captured.err
    _assert_state(root, before)


def _file_target_repo(tmp_path: Path) -> Path:
    root = _configured_repo(tmp_path)
    config = (root / ".minotaur.toml").read_text(encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        config.replace(
            'targets = ["app", "consumer.py"]', 'targets = ["app/api.py", "consumer.py"]'
        ),
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize("delete_all", (False, True))
def test_systems_deleted_committed_targets_keep_selection_context_and_no_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    delete_all: bool,
) -> None:
    root = _file_target_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "api.py").unlink()
    if delete_all:
        (root / "consumer.py").unlink()
    before = _state(root)

    status = cli.main(["query", "diff", "--systems", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert status == 1
    assert payload["changed"] is True
    assert payload["selection"]["old"]["targets"] == ["app/api.py", "consumer.py"]
    assert payload["selection"]["new"]["targets"] == ["app/api.py", "consumer.py"]
    _assert_state(root, before)


def test_systems_added_and_deleted_systems_are_public_structural_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    added_root = _configured_repo(tmp_path / "added")
    monkeypatch.chdir(added_root)
    assert cli.main(["analyze"]) == 0
    _git(added_root, "add", ".")
    _git(added_root, "commit", "-qm", "baseline")
    (added_root / "docs" / "systems" / "other").mkdir(parents=True)
    (added_root / "docs" / "systems" / "other" / "system.toml").write_text(
        'schema_version = 1\nname = "Other"\nfiles = ["missing.py"]\n', encoding="utf-8"
    )
    before_added = _state(added_root)
    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    added_output = capsys.readouterr()
    added_payload = json.loads(added_output.out)
    assert added_payload["added_systems"] == ["Other"]
    assert added_payload["changed"] is True
    _assert_state(added_root, before_added)

    deleted_root = _configured_repo(tmp_path / "deleted")
    monkeypatch.chdir(deleted_root)
    assert cli.main(["analyze"]) == 0
    _git(deleted_root, "add", ".")
    _git(deleted_root, "commit", "-qm", "baseline")
    definition = deleted_root / "docs" / "systems" / "app" / "system.toml"
    definition.unlink()
    before_deleted = _state(deleted_root)
    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    deleted_output = capsys.readouterr()
    deleted_payload = json.loads(deleted_output.out)
    assert deleted_payload["removed_systems"] == ["App"]
    assert deleted_payload["changed"] is True
    _assert_state(deleted_root, before_deleted)


def test_systems_public_route_composes_matched_file_endpoint_label_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compose both sides' stored endpoint records for the FILE_PATH row.

    The analyzer cannot naturally produce two labels for one file path, so a
    controlled producer supplies the Before and After graph shapes in analysis
    order. Both sides still come from captured source; the committed graph is
    inert.
    """
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _commit_file_boundary_graph(root, label="legacy-api.py")

    original_producer = cli._produce_selection
    labels = ["legacy-api.py", "current-api.py"]

    def controlled_producer(*args: object, **kwargs: object) -> object:
        workspace, selection, result = original_producer(*args, **kwargs)  # type: ignore[arg-type]
        if not labels:
            labels.extend(["legacy-api.py", "current-api.py"])
        label = labels.pop(0)
        return (
            workspace,
            selection,
            _controlled_file_boundary_result(result, label=label),
        )

    monkeypatch.setattr(cli, "_produce_selection", controlled_producer)
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--details"]) == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    boundary = "boundary endpoint: no_system.consumer.consume -> App.current-api.py (calls)"
    assert lines.count(boundary) == 1
    assert "boundary added:" not in captured.out
    assert "boundary removed:" not in captured.out
    index = lines.index(boundary)
    old = json.loads(lines[index + 1].removeprefix("old: "))
    new = json.loads(lines[index + 2].removeprefix("new: "))
    old_evidence = json.loads(lines[index + 3].removeprefix("old evidence: "))
    new_evidence = json.loads(lines[index + 4].removeprefix("new evidence: "))
    assert old["target_endpoint"]["label"] == "legacy-api.py"
    assert new["target_endpoint"]["label"] == "current-api.py"
    assert old["target_endpoint"]["node_class"] == new["target_endpoint"]["node_class"] == "file"
    assert (
        old["target_endpoint"]["path"]
        == new["target_endpoint"]["path"]
        == {
            "status": "recorded",
            "value": "app/api.py",
        }
    )
    assert old["source_membership"] == new["source_membership"] == "no_system"
    assert old["target_membership"] == new["target_membership"] == "system: App"
    assert old_evidence[0]["evidence"] == new_evidence[0]["evidence"]
    assert old_evidence[0]["evidence"][0]["provenance"] == "static-analysis"
    assert old_evidence[0]["evidence"][0]["sites"][0]["path"] == "consumer.py"
    _assert_state(root, before)

    assert cli.main(["query", "diff", "--systems", "--json", "--details"]) == 1
    json_output = capsys.readouterr()
    payload = json.loads(json_output.out)
    assert payload["changed"] is True
    assert payload["exit_code"] == 1
    assert payload["boundary_changes"]
    assert [change["kind"] for change in payload["boundary_changes"]] == ["endpoint"]
    change = payload["boundary_changes"][0]
    assert change["old"]["target_endpoint"]["label"] == "legacy-api.py"
    assert change["new"]["target_endpoint"]["label"] == "current-api.py"
    assert (
        change["old"]["target_endpoint"]["node_class"]
        == change["new"]["target_endpoint"]["node_class"]
        == "file"
    )
    assert (
        change["old"]["target_endpoint"]["path"]
        == change["new"]["target_endpoint"]["path"]
        == {"status": "recorded", "value": "app/api.py"}
    )
    assert (
        change["old"]["relationships"][0]["target"]["id"]
        == change["new"]["relationships"][0]["target"]["id"]
    )
    assert change["old"]["source_membership"] == change["new"]["source_membership"] == "no_system"
    assert change["old"]["target_membership"] == change["new"]["target_membership"] == "system: App"
    assert (
        change["old"]["relationships"][0]["evidence"]
        == change["new"]["relationships"][0]["evidence"]
    )
    assert change["old"]["relationships"][0]["evidence"][0]["provenance"] == "static-analysis"
    assert change["old"]["relationships"][0]["evidence"][0]["sites"][0]["path"] == "consumer.py"
    _assert_state(root, before)


def test_systems_public_route_composes_file_endpoint_evidence_only_as_status_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The After side keeps the matched endpoint while adding evidence only."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _commit_file_boundary_graph(root, label="legacy-api.py")

    original_producer = cli._produce_selection
    calls = 0
    observed_evidence: list[tuple[str, ...]] = []

    def controlled_producer(*args: object, **kwargs: object) -> object:
        nonlocal calls
        workspace, selection, result = original_producer(*args, **kwargs)  # type: ignore[arg-type]
        calls += 1
        controlled = _controlled_file_boundary_result(
            result, label="legacy-api.py", evidence_only=calls > 1
        )
        observed_evidence.append(
            tuple(
                evidence.provenance.value
                for evidence in controlled.document.relationships[0].evidence
            )
        )
        return (
            workspace,
            selection,
            controlled,
        )

    monkeypatch.setattr(cli, "_produce_selection", controlled_producer)
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--json", "--details"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert payload["exit_code"] == 0
    assert payload["boundary_changes"] == []
    assert payload["surface_changes"] == []
    assert payload["consumer_changes"] == []
    assert payload["dependency_changes"] == []
    assert captured.err == ""
    assert observed_evidence == [("static-analysis",), ("static-analysis", "curated-rule")]
    _assert_state(root, before)


def test_systems_untracked_source_addition_is_a_detected_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "unrelated.py").write_text("value = 1\n", encoding="utf-8")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is True
    assert (
        payload["coverage"]["new"]["graph_files"]["count"]
        > payload["coverage"]["old"]["graph_files"]["count"]
    )
    added = [
        node
        for node in payload["comparison"]["nodes"]
        if node["status"] == "added" and isinstance(node["after"], dict)
    ]
    assert any(node["after"]["node"].get("path") == "app/unrelated.py" for node in added)
    # Genuinely changed captured content still changes the logical digest.
    assert (
        payload["comparison"]["before"]["source_digest"]
        != payload["comparison"]["after"]["source_digest"]
    )
    _assert_state(root, before)


def test_systems_keeps_absent_current_graph_and_sidecar_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    graph = root / "graph.json"
    graph.unlink()
    stamp_path(graph).unlink()
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert captured.err == ""
    assert before["files"]["graph.json"] is None  # type: ignore[index]
    assert before["files"]["graph.json.sha256"] is None  # type: ignore[index]
    _assert_state(root, before)


def test_systems_evidence_only_graph_change_is_status_zero_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    graph = root / "graph.json"
    payload = json.loads(graph.read_text(encoding="utf-8"))
    for relationship in payload["relationships"]:
        if relationship["kind"] == "calls":
            alternate = dict(relationship["evidence"][0])
            alternate["provenance"] = "curated-rule"
            alternate["rule"] = {"id": "alternate"}
            relationship["evidence"].append(alternate)
            break
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    graph.write_bytes(content)
    stamp_path(graph).write_bytes((graph_digest(content) + "\n").encode("ascii"))
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "evidence-only historical change")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert captured.err == ""
    _assert_state(root, before)


def test_systems_status_comes_from_typed_result_when_renderer_is_neutral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    (root / "consumer.py").write_text(
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.system_diff_view, "render_text", lambda *_args, **_kwargs: "neutral\n")

    assert cli.main(["query", "diff", "--systems", "--system=App"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "neutral\n"
    assert captured.err == ""


def test_systems_public_route_accepts_supported_unresolved_origin_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / "app").mkdir()
    (root / "app" / "api.py").write_text(
        "from missing import receive\n\ndef caller():\n    return receive()\n", encoding="utf-8"
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["app"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "app"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "App"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "unresolved chain")

    assert cli.main(["query", "diff", "--systems", "--system=App", "--validate"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("no system differences\n")
    assert captured.err == ""


@pytest.mark.parametrize(
    ("before", "after"),
    (
        (None, "system: App"),
        ("system: App", None),
        ("system: App", "Ordinary"),
        ("Ordinary", "system: App"),
    ),
)
def test_literal_owner_graph_absent_membership(tmp_path, monkeypatch, capsys, before, after):
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    names = ("system: App", "Ordinary", "Unrelated")

    def definitions(owner):
        for index, name in enumerate(names):
            directory = root / "docs" / "systems" / ("app" if index == 0 else str(index))
            directory.mkdir(exist_ok=True)
            files = ["app/api.py"] if index == 0 else [f"placeholder{index}.py"]
            if name == owner:
                files.append("absent.py")
            (directory / "system.toml").write_text(
                f"schema_version = 1\nname = {json.dumps(name)}\nfiles = {json.dumps(files)}\n"
            )

    definitions(before)
    assert cli.main(["analyze"]) == 0
    capsys.readouterr()
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    definitions(after)
    for selected in (None, *names):
        arguments = ["query", "diff", "--systems"]
        if selected is not None:
            arguments += ["--system", selected]
        expected = int(selected is None or selected in (before, after))
        assert cli.main([*arguments, "--json"]) == expected
        payload = json.loads(capsys.readouterr().out)
        changes = payload["membership_changes"]
        assert changes == (
            [
                {
                    "domain": "membership",
                    "kind": "changed",
                    "key": ["absent.py"],
                    "old": {"file": "absent.py", "system": before},
                    "new": {"file": "absent.py", "system": after},
                    "involved_systems": sorted(
                        {name for name in (before, after) if name is not None}
                    ),
                }
            ]
            if expected
            else []
        )
        for flags in ([], ["--details"]):
            assert cli.main([*arguments, *flags]) == expected
            capsys.readouterr()


def _embedded_comparison_payload(html: str) -> dict[str, object]:
    """Extract the inert comparison payload from a rendered report."""
    prefix = '<script id="minotaur-presentation" type="application/json">'
    payload = json.loads(html.split(prefix, 1)[1].split("</script>", 1)[0])
    comparison = payload["comparison"]
    assert isinstance(comparison, dict)
    return comparison


def _record_identity(record: dict[str, object]) -> object:
    return record.get("id") or record.get("relationship_id")


def _comparison_fingerprint(records: object) -> list[dict[str, object]]:
    """Project the fields that both public outputs must store identically."""
    assert isinstance(records, list)
    return [
        {
            "id": _record_identity(record),
            "status": record.get("status"),
            "reasons": list(record.get("reasons") or []),
            "involved_systems": list(record.get("involved_systems") or []),
            "before": record.get("before"),
            "after": record.get("after"),
        }
        for record in records
        if isinstance(record, dict)
    ]


def _head_sha(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


_SIDE_RECORD_KEYS = frozenset(
    {"kind", "requested_revision", "commit", "config_path", "root", "targets", "source_digest"}
)


def _assert_side_record(record: object) -> dict[str, object]:
    """Require the exact captured-side shape and a hex content digest."""
    assert isinstance(record, dict)
    assert set(record) == set(_SIDE_RECORD_KEYS)
    assert isinstance(record["source_digest"], str)
    assert len(record["source_digest"]) == 64
    assert all(character in "0123456789abcdef" for character in record["source_digest"])
    assert isinstance(record["targets"], list)
    return record


def test_systems_two_revision_grammar_compares_pinned_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    base = _head_sha(root)
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "changed")
    head = _head_sha(root)

    assert cli.main(["query", "diff", "--systems", base, "HEAD", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is True
    assert payload["revisions"] == {"old": f"{base} · {base[:7]}", "new": f"HEAD · {head[:7]}"}
    assert payload["comparison"]["changed"] is True

    assert cli.main(["query", "diff", "--systems", "HEAD", "HEAD", "--json"]) == 0
    identical = json.loads(capsys.readouterr().out)
    assert identical["changed"] is False
    assert identical["comparison"]["changed"] is False

    # Captured-side records identify both pinned revisions and are stable for
    # the same repository state.
    assert cli.main(["query", "diff", "--systems", base, "HEAD", "--json"]) == 1
    context = json.loads(capsys.readouterr().out)["comparison"]
    before_record = _assert_side_record(context["before"])
    after_record = _assert_side_record(context["after"])
    assert context["schema_version"] == 1
    assert before_record["kind"] == "commit"
    assert before_record["requested_revision"] == base
    assert before_record["commit"] == base
    assert before_record["config_path"] == ".minotaur.toml"
    assert before_record["root"] == "."
    assert before_record["targets"] == ["app", "consumer.py"]
    assert after_record["kind"] == "commit"
    assert after_record["requested_revision"] == "HEAD"
    assert after_record["commit"] == head
    assert after_record["targets"] == ["app", "consumer.py"]
    assert before_record["source_digest"] != after_record["source_digest"]
    assert context["before"] == payload["comparison"]["before"]
    assert context["after"] == payload["comparison"]["after"]


def test_systems_working_tree_after_has_no_commit_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    head = _head_sha(root)

    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["revisions"]["new"] == "Working tree at report generation"
    assert payload["revisions"]["old"].startswith("HEAD · ")
    assert head[:7] in payload["revisions"]["old"]
    assert payload["selected_system"] is None

    # The Before side is a real commit; the working-tree After side carries no
    # invented commit identity, so a fabricated SHA or a dropped field fails.
    comparison = payload["comparison"]
    assert comparison["schema_version"] == 1
    assert comparison["before"]["kind"] == "commit"
    assert comparison["before"]["requested_revision"] == "HEAD"
    assert comparison["before"]["commit"] == head
    assert comparison["after"]["kind"] == "working-tree"
    assert comparison["after"]["requested_revision"] is None
    assert comparison["after"]["commit"] is None
    # A clean working tree is logically identical to the captured commit, so
    # the logical manifest digest is identical. The manifest normalizes modes
    # to the exec/non-exec model, so this no longer varies with the process
    # umask; a difference here would leak filesystem permission bits.
    assert comparison["before"]["source_digest"] == comparison["after"]["source_digest"]


def test_systems_json_and_html_describe_the_same_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One changed invocation proves JSON and the embedded payload agree."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    head = _head_sha(root)
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    (root / "consumer.py").write_text(
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
        encoding="utf-8",
    )
    report = root / "comparison.html"

    # A selected system scopes the top-level status while the complete context
    # stays in both outputs, so a viewer-side or divergent projection fails.
    status = cli.main(
        ["query", "diff", "--systems", "--system", "App", "--json", "--html", str(report)]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    embedded = _embedded_comparison_payload(report.read_text(encoding="utf-8"))
    context = payload["comparison"]

    assert status == 1
    assert captured.err == ""
    assert payload["selected_system"] == "App"
    assert context["selected_system"] == "App"
    assert context["changed"] is True
    assert embedded["changed"] is True
    assert context["revisions"] == embedded["revisions"]
    for key in ("nodes", "relationships", "call_changes"):
        assert _comparison_fingerprint(context[key]) == _comparison_fingerprint(embedded[key])
    assert context["limitations"] == embedded["limitations"]
    assert any(
        record["status"] != "unchanged"
        for record in _comparison_fingerprint(context["nodes"])
        if record["status"] is not None
    )

    # The schema-versioned captured-side records are present and identical in
    # both public outputs, so a removed or divergent field fails this proof.
    assert context["schema_version"] == 1
    assert embedded["schema_version"] == 1
    assert context["before"] == embedded["before"]
    assert context["after"] == embedded["after"]
    before_record = _assert_side_record(context["before"])
    after_record = _assert_side_record(context["after"])
    assert before_record["kind"] == "commit"
    assert before_record["requested_revision"] == "HEAD"
    assert before_record["commit"] == head
    assert before_record["config_path"] == ".minotaur.toml"
    assert before_record["root"] == "."
    assert before_record["targets"] == ["app", "consumer.py"]
    assert after_record["kind"] == "working-tree"
    assert after_record["requested_revision"] is None
    assert after_record["commit"] is None
    assert after_record["config_path"] == ".minotaur.toml"
    assert after_record["root"] == "."
    assert after_record["targets"] == ["app", "consumer.py"]
    assert before_record["source_digest"] != after_record["source_digest"]


def test_systems_html_unchanged_pair_reports_zero_without_touching_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    report = root / "comparison.html"
    inputs = {
        ".minotaur.toml": (root / ".minotaur.toml").read_bytes(),
        "graph.json": graph.read_bytes(),
        "graph.json.sha256": sidecar.read_bytes(),
        "app/api.py": (root / "app" / "api.py").read_bytes(),
        "docs/systems/app/system.toml": (
            root / "docs" / "systems" / "app" / "system.toml"
        ).read_bytes(),
    }

    assert cli.main(["query", "diff", "--systems", "--html", str(report)]) == 0
    captured = capsys.readouterr()
    assert "no system differences" in captured.out
    assert captured.err == ""
    assert report.exists()
    for relative, content in inputs.items():
        assert (root / relative).read_bytes() == content


def test_systems_html_refuses_existing_report_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    report = root / "comparison.html"
    report.write_text("existing report", encoding="utf-8")

    assert cli.main(["query", "diff", "--systems", "--html", str(report)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already exists" in captured.err
    assert "--force" in captured.err
    assert report.read_text(encoding="utf-8") == "existing report"


def test_systems_force_replaces_report_and_requires_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--force"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--force requires --html" in captured.err

    report = root / "comparison.html"
    report.write_text("existing report", encoding="utf-8")
    assert cli.main(["query", "diff", "--systems", "--html", str(report), "--force"]) == 0
    assert "existing report" not in report.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "alias",
    ("source", "config", "graph", "definition", "git-metadata"),
)
def test_systems_force_refuses_protected_output_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    alias: str,
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    target = {
        "source": root / "app" / "api.py",
        "config": root / ".minotaur.toml",
        "graph": root / "graph.json",
        "definition": root / "docs" / "systems" / "app" / "system.toml",
        "git-metadata": root / ".git" / "config",
    }[alias]
    original = target.read_bytes()

    assert cli.main(["query", "diff", "--systems", "--html", str(target), "--force"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "comparison input" in captured.err or "protected" in captured.err
    assert target.read_bytes() == original


def test_systems_force_refuses_hardlink_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    source = root / "app" / "api.py"
    link = root / "comparison.html"
    os.link(source, link)
    original = source.read_bytes()

    assert cli.main(["query", "diff", "--systems", "--html", str(link), "--force"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hard-links" in captured.err
    assert source.read_bytes() == original


def test_systems_output_directory_and_missing_parent_are_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--html", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "directory" in captured.err

    assert cli.main(["query", "diff", "--systems", "--html", str(root / "missing" / "r.html")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "parent directory does not exist" in captured.err


def test_systems_output_write_failure_preserves_existing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    report = root / "comparison.html"
    report.write_text("existing report", encoding="utf-8")

    def fail_write(output: Path, content: bytes) -> None:
        raise OSError("simulated report write failure")

    monkeypatch.setattr(cli, "_write_atomically", fail_write)
    assert cli.main(["query", "diff", "--systems", "--html", str(report), "--force", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "simulated report write failure" in captured.err
    assert report.read_text(encoding="utf-8") == "existing report"


def test_systems_output_created_during_comparison_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    report = root / "comparison.html"
    original_producer = cli._produce_selection

    def racing_producer(*args: object, **kwargs: object) -> object:
        result = original_producer(*args, **kwargs)  # type: ignore[arg-type]
        report.write_text("concurrent report", encoding="utf-8")
        return result

    monkeypatch.setattr(cli, "_produce_selection", racing_producer)
    assert cli.main(["query", "diff", "--systems", "--html", str(report)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "created during comparison" in captured.err
    assert report.read_text(encoding="utf-8") == "concurrent report"


def test_systems_side_config_overrides_select_each_side_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    (root / "alt.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py"]\nsystems_dir = "alt-systems"\n',
        encoding="utf-8",
    )
    alternative = root / "alt-systems" / "other"
    alternative.mkdir(parents=True)
    (alternative / "system.toml").write_text(
        'schema_version = 1\nname = "Other"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--after-config", "alt.toml", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["old_system_names"] == ["App"]
    assert payload["new_system_names"] == ["Other"]
    assert captured.err == ""
    _assert_state(root, before)


def test_systems_config_route_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--config", str(root / ".minotaur.toml")]) == 0
    capsys.readouterr()

    outside = tmp_path / "outside.toml"
    outside.write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "g.json"\ntargets = ["app"]\n',
        encoding="utf-8",
    )
    assert cli.main(["query", "diff", "--systems", "--config", str(outside)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "outside the invoking repository" in captured.err

    assert (
        cli.main(["query", "diff", "--systems", "--before-config", str(root / ".minotaur.toml")])
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "repository-relative" in captured.err


def test_systems_call_expression_only_change_is_status_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    base = _head_sha(root)
    (root / "consumer.py").write_text(
        "from app.api import receive\n\ndef consume():\n    return receive(2)\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "call change")

    assert cli.main(["query", "diff", "--systems", base, "HEAD", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is True
    assert all(node["status"] == "unchanged" for node in payload["comparison"]["nodes"])
    assert all(
        relationship["status"] == "unchanged"
        for relationship in payload["comparison"]["relationships"]
    )
    assert any(item["status"] == "changed" for item in payload["comparison"]["call_changes"])


def test_systems_moved_call_is_not_a_detected_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-10/V-06: a call that only shifts line is not a structural change."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    base = _head_sha(root)
    # Insert a blank line before the same call: the normalized expression is
    # byte-identical, only its evidence location moves.
    (root / "consumer.py").write_text(
        "from app.api import receive\n\ndef consume():\n\n    return receive()\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "shift only")

    assert cli.main(["query", "diff", "--systems", base, "HEAD", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is False
    assert payload["exit_code"] == 0
    assert all(node["status"] == "unchanged" for node in payload["comparison"]["nodes"])
    assert all(
        relationship["status"] == "unchanged"
        for relationship in payload["comparison"]["relationships"]
    )
    assert payload["comparison"]["call_changes"]
    assert all(item["status"] == "unchanged" for item in payload["comparison"]["call_changes"])


def test_systems_duplicate_call_adds_multiplicity_without_expression_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pure duplicate add is a multiplicity change, not an expression edit."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    base = _head_sha(root)
    (root / "consumer.py").write_text(
        "from app.api import receive\n\ndef consume():\n    receive()\n    return receive()\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "duplicate call")

    assert cli.main(["query", "diff", "--systems", base, "HEAD", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    changed = [
        item for item in payload["comparison"]["call_changes"] if item["status"] != "unchanged"
    ]
    assert len(changed) == 1
    assert changed[0]["reasons"] == ["multiplicity_changed"]


def test_systems_large_report_warning_keeps_json_stdout_parseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    report = root / "comparison.html"
    monkeypatch.setattr(cli, "_LARGE_ARTIFACT_BYTES", 10)

    assert cli.main(["query", "diff", "--systems", "--json", "--html", str(report)]) == 0
    captured = capsys.readouterr()
    assert "exceeds 10 MiB" in captured.err
    assert json.loads(captured.out)["changed"] is False


def test_systems_config_route_keeps_working_directory_meaning_from_nested_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--config", "../.minotaur.toml"]) == 0
    captured = capsys.readouterr()
    assert "no system differences" in captured.out
    assert captured.err == ""


def _embedded_presentation(html: str) -> dict[str, object]:
    """Extract the complete inert presentation payload from a rendered report."""
    prefix = '<script id="minotaur-presentation" type="application/json">'
    payload = json.loads(html.split(prefix, 1)[1].split("</script>", 1)[0])
    assert isinstance(payload, dict)
    return payload


def test_systems_per_side_target_present_on_opposite_side_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C-01: a target absent on its own side is allowed when the coordinate
    exists on the opposite captured side, even if only one side declares it.

    ``b.py`` is committed at HEAD but selected only by the After config and
    deleted from the working tree, so it is a deletion representable from the
    Before tree rather than an absent-on-both-sides error.
    """
    root = _repo(tmp_path)
    (root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["a.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "b.py").unlink()
    (root / "alt.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["b.py"]\n',
        encoding="utf-8",
    )

    assert cli.main(["query", "diff", "--systems", "--after-config", "alt.toml", "--json"]) == 1
    captured = capsys.readouterr()
    assert "absent on both comparison sides" not in captured.err
    assert json.loads(captured.out)["changed"] is True


def test_systems_captured_call_excerpts_resolve_under_the_side_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C-07: captured excerpt bytes resolve under each side's configured root.

    With ``root = "src"`` the graph stores root-relative locations, so captured
    bytes must be read below ``<capture>/src`` or every call excerpt degrades to
    unavailable even though the exact captured source exists.
    """
    root = _repo(tmp_path)
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "api.py").write_text("def receive():\n    return 1\n", encoding="utf-8")
    (root / "src" / "consumer.py").write_text(
        "from pkg.api import receive\n\ndef consume():\n    return receive()\n",
        encoding="utf-8",
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "src"\ngraph = "graph.json"\n'
        'targets = ["pkg", "consumer.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "src" / "consumer.py").write_text(
        "from pkg.api import receive\n\ndef consume():\n    return receive(2)\n",
        encoding="utf-8",
    )
    report = root / "comparison.html"

    status = cli.main(["query", "diff", "--systems", "--json", "--html", str(report)])
    capsys.readouterr()
    assert status == 1
    excerpts = _embedded_presentation(report.read_text(encoding="utf-8"))["excerpts"]
    assert isinstance(excerpts, dict)
    for side in ("before", "after"):
        rendered = excerpts[side]["paths"]["consumer.py"]
        assert rendered["status"] == "available"
        assert "receive" in json.dumps(rendered["spans"])


def test_systems_json_and_html_agree_under_a_configured_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-02/C-07: one sub-root invocation proves both public outputs describe
    the same captured sides and that captured excerpts resolve below each
    side's configured root rather than the snapshot root."""
    root = _repo(tmp_path)
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "api.py").write_text("def receive():\n    return 1\n", encoding="utf-8")
    (root / "src" / "consumer.py").write_text(
        "from pkg.api import receive\n\ndef consume():\n    return receive()\n",
        encoding="utf-8",
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "src"\ngraph = "graph.json"\n'
        'targets = ["pkg", "consumer.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    head = _head_sha(root)
    (root / "src" / "consumer.py").write_text(
        "from pkg.api import receive\n\ndef consume():\n    return receive(2)\n",
        encoding="utf-8",
    )
    report = root / "comparison.html"

    assert cli.main(["query", "diff", "--systems", "--json", "--html", str(report)]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    presentation = _embedded_presentation(report.read_text(encoding="utf-8"))
    context = payload["comparison"]

    assert context["changed"] is True
    assert context["before"] == presentation["comparison"]["before"]
    assert context["after"] == presentation["comparison"]["after"]
    assert context["before"]["root"] == "src"
    assert context["before"]["config_path"] == ".minotaur.toml"
    assert context["before"]["targets"] == ["src/consumer.py", "src/pkg"]
    assert context["before"]["commit"] == head
    assert context["after"]["kind"] == "working-tree"
    assert context["after"]["commit"] is None
    assert context["before"]["source_digest"] != context["after"]["source_digest"]
    for key in ("nodes", "relationships", "call_changes"):
        assert _comparison_fingerprint(context[key]) == _comparison_fingerprint(
            presentation["comparison"][key]
        )
    excerpts = presentation["excerpts"]
    assert isinstance(excerpts, dict)
    for side in ("before", "after"):
        rendered = excerpts[side]["paths"]["consumer.py"]
        assert rendered["status"] == "available"
        assert "receive" in json.dumps(rendered["spans"])


def test_systems_json_and_html_agree_for_one_side_target_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-02/C-01: a target declared by only one side is admitted when its
    coordinate exists on the opposite captured tree, and both public outputs
    describe the same admitted sides."""
    root = _repo(tmp_path)
    (root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["a.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "b.py").unlink()
    (root / "alt.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["b.py"]\n',
        encoding="utf-8",
    )
    report = root / "comparison.html"

    assert (
        cli.main(
            [
                "query",
                "diff",
                "--systems",
                "--after-config",
                "alt.toml",
                "--json",
                "--html",
                str(report),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "absent on both comparison sides" not in captured.err
    payload = json.loads(captured.out)
    presentation = _embedded_presentation(report.read_text(encoding="utf-8"))
    context = payload["comparison"]

    assert context["before"]["targets"] == ["a.py"]
    assert context["after"]["targets"] == ["b.py"]
    assert context["before"]["config_path"] == ".minotaur.toml"
    assert context["after"]["config_path"] == "alt.toml"
    assert context["before"] == presentation["comparison"]["before"]
    assert context["after"] == presentation["comparison"]["after"]
    assert context["changed"] is True


def test_capture_present_admits_only_regular_files_and_directories(tmp_path: Path) -> None:
    """C-01: a non-regular opposite entry is not evidence of a deletion."""
    root = tmp_path / "capture"
    root.mkdir()
    (root / "file.py").write_text("value = 1\n", encoding="utf-8")
    (root / "package").mkdir()
    (root / "link").symlink_to("file.py")
    (root / "dirlink").symlink_to("package")

    assert cli._capture_present(root, "file.py") is True
    assert cli._capture_present(root, "package") is True
    assert cli._capture_present(root, ".") is True
    assert cli._capture_present(root, "missing.py") is False
    assert cli._capture_present(root, "link") is False
    assert cli._capture_present(root, "dirlink") is False

    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "pipe")
        assert cli._capture_present(root, "pipe") is False
