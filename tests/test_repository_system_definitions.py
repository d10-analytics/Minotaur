"""Public configured proof of the repository system map (AC-05)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import graph_digest, load_graph_file, stamp_path
from minotaur.graph_model.node import NodeClass

ROOT = Path(__file__).parents[1]
SOURCE_ROOT = ROOT / "src"
SYSTEMS_ROOT = ROOT / "docs" / "systems"
CONFIG = ROOT / ".minotaur.toml"

_TARGETS = (
    "minotaur/__init__.py",
    "minotaur/__main__.py",
    "minotaur/cli.py",
    "minotaur/comparison.py",
    "minotaur/config.py",
    "minotaur/git.py",
    "minotaur/graph_model",
    "minotaur/graph_visualizer/__init__.py",
    "minotaur/graph_visualizer/contract.py",
    "minotaur/graph_visualizer/html/render.py",
    "minotaur/graph_visualizer/presentation.py",
    "minotaur/graph_visualizer/source.py",
    "minotaur/language_interpreter",
    "minotaur/query",
    "minotaur/source.py",
    "minotaur/system.py",
)

_SYSTEM_FILES = {
    "analysis-javascript": [
        "minotaur/language_interpreter/javascript/__init__.py",
        "minotaur/language_interpreter/javascript/interpreter.py",
    ],
    "analysis-platform": [
        "minotaur/language_interpreter/__init__.py",
        "minotaur/language_interpreter/accumulation.py",
        "minotaur/language_interpreter/contract.py",
        "minotaur/language_interpreter/emission.py",
        "minotaur/language_interpreter/exclusions.py",
        "minotaur/language_interpreter/paths.py",
        "minotaur/language_interpreter/reading.py",
        "minotaur/language_interpreter/registry.py",
        "minotaur/language_interpreter/selection.py",
        "minotaur/language_interpreter/source_text.py",
        "minotaur/language_interpreter/workspace.py",
    ],
    "analysis-python": [
        "minotaur/language_interpreter/python/__init__.py",
        "minotaur/language_interpreter/python/binding_flow.py",
        "minotaur/language_interpreter/python/discovery.py",
        "minotaur/language_interpreter/python/interpreter.py",
    ],
    "command-interface": [
        "minotaur/__init__.py",
        "minotaur/__main__.py",
        "minotaur/cli.py",
    ],
    "graph-contract": [
        "minotaur/graph_model/__init__.py",
        "minotaur/graph_model/_parsing.py",
        "minotaur/graph_model/document.py",
        "minotaur/graph_model/evidence.py",
        "minotaur/graph_model/identity.py",
        "minotaur/graph_model/loading.py",
        "minotaur/graph_model/location.py",
        "minotaur/graph_model/node.py",
        "minotaur/graph_model/provenance.py",
        "minotaur/graph_model/relationship.py",
        "minotaur/graph_model/serialization.py",
        "minotaur/graph_model/slicing.py",
        "minotaur/graph_model/validation.py",
    ],
    "project-acquisition": [
        "minotaur/comparison.py",
        "minotaur/config.py",
        "minotaur/git.py",
    ],
    "query-and-system-reporting": [
        "minotaur/query/__init__.py",
        "minotaur/query/context.py",
        "minotaur/query/correspondence.py",
        "minotaur/query/diff.py",
        "minotaur/query/freshness.py",
        "minotaur/query/impact.py",
        "minotaur/query/index.py",
        "minotaur/query/render.py",
        "minotaur/query/symbols.py",
        "minotaur/query/system.py",
        "minotaur/query/system_diff.py",
        "minotaur/query/system_diff_view.py",
        "minotaur/query/unreferenced.py",
        "minotaur/system.py",
    ],
    "source-presentation": [
        "minotaur/graph_visualizer/__init__.py",
        "minotaur/graph_visualizer/contract.py",
        "minotaur/graph_visualizer/html/render.py",
        "minotaur/graph_visualizer/presentation.py",
        "minotaur/graph_visualizer/source.py",
        "minotaur/source.py",
    ],
}

_CONNECTIONS = [
    (
        "system: analysis-javascript",
        "system: analysis-platform",
        ("calls", "imports", "references"),
    ),
    (
        "system: analysis-javascript",
        "system: graph-contract",
        ("calls", "imports", "references"),
    ),
    (
        "system: analysis-platform",
        "system: graph-contract",
        ("calls", "imports", "references"),
    ),
    (
        "system: analysis-python",
        "system: analysis-platform",
        ("calls", "imports", "references"),
    ),
    (
        "system: analysis-python",
        "system: graph-contract",
        ("calls", "imports", "references"),
    ),
    (
        "system: command-interface",
        "system: analysis-platform",
        ("calls", "imports", "references"),
    ),
    (
        "system: command-interface",
        "system: graph-contract",
        ("calls", "imports", "references"),
    ),
    (
        "system: command-interface",
        "system: project-acquisition",
        ("calls", "imports", "references"),
    ),
    (
        "system: command-interface",
        "system: query-and-system-reporting",
        ("calls", "imports", "references"),
    ),
    ("system: command-interface", "system: source-presentation", ("calls", "imports")),
    (
        "system: project-acquisition",
        "system: analysis-platform",
        ("calls", "imports", "references"),
    ),
    (
        "system: project-acquisition",
        "system: graph-contract",
        ("calls", "imports", "references"),
    ),
    (
        "system: project-acquisition",
        "system: query-and-system-reporting",
        ("calls", "imports", "references"),
    ),
    (
        "system: query-and-system-reporting",
        "system: analysis-platform",
        ("calls", "imports", "references"),
    ),
    (
        "system: query-and-system-reporting",
        "system: graph-contract",
        ("calls", "imports", "references"),
    ),
    ("system: query-and-system-reporting", "system: project-acquisition", ("calls", "imports")),
    ("system: query-and-system-reporting", "system: source-presentation", ("calls", "imports")),
]


def _copy_systems(destination: Path) -> Path:
    shutil.copytree(SYSTEMS_ROOT, destination)
    return destination


def _write_config(
    destination: Path,
    *,
    graph: Path,
    systems: Path,
    targets: tuple[str, ...] = _TARGETS,
) -> Path:
    destination.write_text(
        "[minotaur]\n"
        "schema_version = 1\n"
        f"root = {json.dumps(str(SOURCE_ROOT))}\n"
        f"graph = {json.dumps(str(graph))}\n"
        f"systems_dir = {json.dumps(str(systems))}\n"
        f"targets = {json.dumps(list(targets))}\n",
        encoding="utf-8",
    )
    return destination


def _query_systems(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, str, str]:
    status = cli.main(["query", "systems", *arguments])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _analyze_real_source_to(graph: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create a disposable graph from the checkout's configured source set."""
    monkeypatch.chdir(ROOT)
    assert cli.main(["analyze", "--output", str(graph)]) == 0


def test_root_discovered_system_map_has_exact_manifest_and_observed_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Analyze from the located config, then query the public JSON overview."""
    monkeypatch.chdir(ROOT)
    graph = tmp_path / "repository-systems.json"

    assert cli.main(["analyze", "--output", str(graph)]) == 0
    status, output, error = _query_systems(
        capsys, "--graph", str(graph), "--no-refresh", "--details", "--json"
    )

    assert status == 0
    assert error == ""
    payload = json.loads(output)
    assert payload["query"] == "systems"
    assert payload["refreshed"] is False
    assert payload["stale"] == []
    assert [record["name"] for record in payload["results"]] == sorted(_SYSTEM_FILES)
    assert [record["declared_files"] for record in payload["results"]] == [
        {
            "scope": "declared_system_files",
            "total": len(_SYSTEM_FILES[name]),
            "represented": len(_SYSTEM_FILES[name]),
            "absent": 0,
            "paths": _SYSTEM_FILES[name],
        }
        for name in sorted(_SYSTEM_FILES)
    ]

    coverage = payload["coverage"]
    assert coverage["selection"] == {
        "status": "recorded",
        "targets": sorted(_TARGETS),
    }
    assert coverage["graph_files"] == {"scope": "final_graph_file_nodes", "count": 56}
    assert coverage["declared_files"] == {
        "scope": "all_declared_system_files",
        "total": 56,
        "represented": 56,
        "absent": 0,
    }
    assert coverage["unassigned_files"] == {
        "scope": "final_graph_file_node_derived_paths",
        "count": 0,
        "paths": [],
    }
    assert len(payload["connections"]) == 17
    assert [
        (item["source_category"], item["target_category"], tuple(item["kinds"]))
        for item in payload["connections"]
    ] == _CONNECTIONS
    assert all(item["relationships"] for item in payload["connections"])


def test_configured_analysis_without_output_writes_only_isolated_root_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The no-output configured path resolves its graph relative to the copied root."""
    project = tmp_path / "isolated-project"
    project.mkdir()
    shutil.copy2(CONFIG, project / ".minotaur.toml")
    shutil.copy2(ROOT / ".gitignore", project / ".gitignore")
    shutil.copytree(SOURCE_ROOT, project / "src")
    shutil.copytree(SYSTEMS_ROOT, project / "docs" / "systems")

    checkout_graph = ROOT / "minotaur-system-definitions.json"
    checkout_sidecar = stamp_path(checkout_graph)
    checkout_before = tuple(
        path.read_bytes() if path.exists() else None for path in (checkout_graph, checkout_sidecar)
    )
    monkeypatch.chdir(project)
    assert cli.main(["analyze"]) == 0
    graph = project / "minotaur-system-definitions.json"
    sidecar = stamp_path(graph)
    assert graph.is_file()
    assert sidecar.is_file()
    graph_bytes = graph.read_bytes()
    loaded = load_graph_file(graph)
    assert sum(node.node_class is NodeClass.FILE for node in loaded.document.nodes) == 56
    assert sidecar.read_text(encoding="ascii").strip() == graph_digest(graph_bytes)
    checkout_after = tuple(
        path.read_bytes() if path.exists() else None for path in (checkout_graph, checkout_sidecar)
    )
    assert checkout_after == checkout_before


@pytest.mark.parametrize("variant", ["unknown", "overlap"])
def test_invalid_copied_declarations_fail_before_any_graph_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variant: str,
) -> None:
    """Strict public loading rejects malformed and overlapping declaration sets."""
    systems = _copy_systems(tmp_path / "systems")
    if variant == "unknown":
        definition = systems / "command-interface" / "system.toml"
        definition.write_text(
            definition.read_text(encoding="utf-8") + "unknown = true\n", encoding="utf-8"
        )
    else:
        definition = systems / "project-acquisition" / "system.toml"
        definition.write_text(
            definition.read_text(encoding="utf-8").replace(
                '  "minotaur/git.py",', '  "minotaur/git.py",\n  "minotaur/cli.py",'
            ),
            encoding="utf-8",
        )
    graph = tmp_path / "graph.json"
    _analyze_real_source_to(graph, monkeypatch)
    original = graph.read_bytes()
    config = _write_config(tmp_path / "config.toml", graph=graph, systems=systems)

    monkeypatch.chdir(tmp_path)
    status, output, error = _query_systems(
        capsys, "--config", str(config), "--no-refresh", "--details", "--json"
    )

    assert status == 2
    assert output == ""
    assert error
    assert graph.read_bytes() == original
    if variant == "unknown":
        assert "unknown system field: unknown" in error
    else:
        assert "file listed in two systems: minotaur/cli.py" in error


def test_declaration_omission_reports_the_graph_file_as_unassigned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Removing one literal declaration does not hide the analyzed file."""
    systems = _copy_systems(tmp_path / "systems")
    definition = systems / "command-interface" / "system.toml"
    definition.write_text(
        definition.read_text(encoding="utf-8").replace('  "minotaur/cli.py",\n', ""),
        encoding="utf-8",
    )
    graph = tmp_path / "graph.json"
    _analyze_real_source_to(graph, monkeypatch)
    config = _write_config(tmp_path / "config.toml", graph=graph, systems=systems)

    monkeypatch.chdir(tmp_path)
    status, output, error = _query_systems(
        capsys, "--config", str(config), "--no-refresh", "--details", "--json"
    )

    assert status == 0
    assert error == ""
    payload = json.loads(output)
    assert payload["coverage"]["graph_files"]["count"] == 56
    assert payload["coverage"]["declared_files"] == {
        "scope": "all_declared_system_files",
        "total": 55,
        "represented": 55,
        "absent": 0,
    }
    assert payload["coverage"]["unassigned_files"] == {
        "scope": "final_graph_file_node_derived_paths",
        "count": 1,
        "paths": ["minotaur/cli.py"],
    }
    command_interface = next(
        item for item in payload["results"] if item["name"] == "command-interface"
    )
    assert command_interface["declared_files"] == {
        "scope": "declared_system_files",
        "total": 2,
        "represented": 2,
        "absent": 0,
        "paths": ["minotaur/__init__.py", "minotaur/__main__.py"],
    }


def test_target_omission_reports_one_declared_file_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A config that omits one target keeps it declared but absent from the graph."""
    systems = _copy_systems(tmp_path / "systems")
    graph = tmp_path / "graph.json"
    config = _write_config(
        tmp_path / "config.toml",
        graph=graph,
        systems=systems,
        targets=tuple(target for target in _TARGETS if target != "minotaur/cli.py"),
    )

    monkeypatch.chdir(tmp_path)
    assert cli.main(["analyze", "--config", str(config)]) == 0
    status, output, error = _query_systems(
        capsys, "--config", str(config), "--no-refresh", "--details", "--json"
    )

    assert status == 0
    assert error == "minotaur: warning: minotaur/cli.py (listed by system command-interface)\n"
    payload = json.loads(output)
    assert payload["coverage"]["graph_files"] == {
        "scope": "final_graph_file_nodes",
        "count": 55,
    }
    assert payload["coverage"]["declared_files"] == {
        "scope": "all_declared_system_files",
        "total": 56,
        "represented": 55,
        "absent": 1,
    }
    assert payload["coverage"]["unassigned_files"] == {
        "scope": "final_graph_file_node_derived_paths",
        "count": 0,
        "paths": [],
    }
    command_interface = next(
        item for item in payload["results"] if item["name"] == "command-interface"
    )
    assert command_interface["declared_files"]["absent"] == 1
    assert command_interface["declared_files"]["represented"] == 2
    assert command_interface["declared_files"]["paths"] == _SYSTEM_FILES["command-interface"]
    assert payload["coverage"]["selection"] == {
        "status": "recorded",
        "targets": sorted(target for target in _TARGETS if target != "minotaur/cli.py"),
    }
