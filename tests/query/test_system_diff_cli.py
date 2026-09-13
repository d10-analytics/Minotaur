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


def test_systems_grammar_rejects_scope_and_explicit_graphs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as scope_error:
        cli.main(["query", "diff", "--systems", "--scope", "App"])
    assert scope_error.value.code == 2
    assert "unrecognized arguments: --scope" in capsys.readouterr().err

    assert cli.main(["query", "diff", "old.json", "new.json", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot be combined" in captured.err


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

    assert status == 0
    assert '"changed":false' in captured.out
    assert '"coverage":{"new":' in captured.out
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


def test_systems_invalid_historical_graph_cannot_be_hidden_by_filter(
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
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--system", "App"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "historical Git input" in captured.err
    assert "graph.json" in captured.err
    _assert_state(root, before)


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
    assert "historical Git input" in captured.err
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
    assert "current source analysis produced diagnostics" in captured.err
    _assert_state(root, before)


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
    assert "historical Git input" in captured.err
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
    assert "historical Git input" in captured.err
    assert "gitlink" in captured.err
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
    assert "historical Git input" in captured.err
    assert "graph.json" in captured.err
    _assert_state(root, before)


def test_systems_old_selection_mismatch_fails_before_producer(
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

    def fail_producer(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("producer must not run before selection mismatch")

    monkeypatch.setattr(cli, "_produce_selection", fail_producer)
    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "current targets" in captured.err
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
    """Use the approved supplied graph only for the unreachable FILE_PATH row."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _commit_file_boundary_graph(root, label="legacy-api.py")

    original_producer = cli._produce_selection

    def controlled_producer(*args: object, **kwargs: object) -> object:
        workspace, selection, result = original_producer(*args, **kwargs)  # type: ignore[arg-type]
        return (
            workspace,
            selection,
            _controlled_file_boundary_result(result, label="current-api.py"),
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
    """The supplied graph keeps the matched endpoint while adding evidence only."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _commit_file_boundary_graph(root, label="legacy-api.py")

    original_producer = cli._produce_selection
    observed_evidence: list[tuple[str, ...]] = []

    def controlled_producer(*args: object, **kwargs: object) -> object:
        workspace, selection, result = original_producer(*args, **kwargs)  # type: ignore[arg-type]
        controlled = _controlled_file_boundary_result(
            result, label="legacy-api.py", evidence_only=True
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
    assert len(observed_evidence) == 1
    assert observed_evidence == [("static-analysis", "curated-rule")]
    _assert_state(root, before)


def test_systems_coverage_only_change_is_status_zero_with_old_new_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "unrelated.py").write_text("value = 1\n", encoding="utf-8")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert payload["coverage"]["old"] != payload["coverage"]["new"]
    assert (
        payload["coverage"]["new"]["graph_files"]["count"]
        > payload["coverage"]["old"]["graph_files"]["count"]
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
