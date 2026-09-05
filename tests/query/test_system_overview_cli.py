"""Natural CLI coverage for the all-systems repository overview."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.query.index import GraphIndex


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _declare(root: Path, name: str, files: list[str]) -> None:
    directory = root / "docs" / "systems" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "system.toml").write_text(
        f'schema_version = 1\nname = "{name}"\nfiles = {json.dumps(files)}\n',
        encoding="utf-8",
    )


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    root = _repo(tmp_path)
    _write(root, "orders/mod.py", "def order():\n    return 1\n")
    _write(root, "billing/svc.py", "def charge():\n    return 1\n")
    _write(root, "loose.py", "def loose():\n    return 1\n")
    _declare(root, "orders", ["orders/mod.py", "orders/missing.py"])
    _declare(root, "billing", ["billing/svc.py"])
    graph = root / "graph.json"
    assert cli.main(["analyze", "--root", str(root), "--output", str(graph), str(root)]) == 0
    return root, graph


def _systems(
    capsys: pytest.CaptureFixture[str], root: Path, graph: Path, *extra: str
) -> tuple[int, str, str]:
    status = cli.main(["query", "systems", "--graph", str(graph), "--root", str(root), *extra])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_systems_compact_json_has_exact_inventory_and_coverage_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, graph = _tree(tmp_path)

    status, out, err = _systems(capsys, root, graph, "--json")

    assert status == 0
    assert err == "minotaur: warning: orders/missing.py (listed by system orders)\n"
    payload = json.loads(out)
    assert set(payload) == {"query", "refreshed", "stale", "results", "coverage"}
    assert payload["query"] == "systems"
    assert payload["refreshed"] is False
    assert payload["stale"] == []
    assert payload["results"] == [
        {
            "name": "billing",
            "declared_files": {
                "scope": "declared_system_files",
                "total": 1,
                "represented": 1,
                "absent": 0,
            },
        },
        {
            "name": "orders",
            "declared_files": {
                "scope": "declared_system_files",
                "total": 2,
                "represented": 1,
                "absent": 1,
            },
        },
    ]
    assert payload["coverage"] == {
        "selection": {"status": "recorded", "targets": ["."]},
        "graph_files": {"scope": "final_graph_file_nodes", "count": 3},
        "declared_files": {
            "scope": "all_declared_system_files",
            "total": 3,
            "represented": 2,
            "absent": 1,
        },
        "unassigned_files": {
            "scope": "final_graph_file_node_derived_paths",
            "count": 1,
        },
        "recorded_unresolved_references": {
            "scope": "all_declared_system_files",
            "count": 0,
        },
        "source_diagnostics": {"status": "unavailable"},
    }


def test_systems_text_details_are_canonical_and_empty_connections_are_explicit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, graph = _tree(tmp_path)

    status, out, err = _systems(capsys, root, graph, "--details")

    assert status == 0
    assert err == "minotaur: warning: orders/missing.py (listed by system orders)\n"
    assert out == (
        'coverage {"declared_files":{"absent":1,"represented":2,"scope":"all_declared_'
        'system_files","total":3},'
        '"graph_files":{"count":3,"scope":"final_graph_file_nodes"},'
        '"recorded_unresolved_references":{"count":0,"scope":"all_declared_system_files"},'
        '"selection":{"status":"recorded","targets":["."]},'
        '"source_diagnostics":{"status":"unavailable"},'
        '"unassigned_files":{"count":1,"paths":["loose.py"],"scope":"final_graph_file_node_derived_paths"}}\n'
        "billing  declared 1  represented 1  absent 0\n"
        "orders  declared 2  represented 1  absent 1\n"
        'declared_files {"billing":["billing/svc.py"],"orders":["orders/missing.py",'
        '"orders/mod.py"]}\n'
        "connections []\n"
    )


def test_systems_builds_one_shared_index_for_compact_and_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, graph = _tree(tmp_path)
    original = GraphIndex.build.__func__
    builds: list[object] = []

    def counted(cls: type[GraphIndex], document: object) -> GraphIndex:
        builds.append(document)
        return original(cls, document)  # type: ignore[arg-type]

    monkeypatch.setattr(GraphIndex, "build", classmethod(counted))
    for details in ((), ("--details",)):
        builds.clear()
        status, _, _ = _systems(capsys, root, graph, *details)
        assert status == 0
        assert len(builds) == 1


def test_systems_strict_load_rejects_malformed_declaration_before_refresh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, graph = _tree(tmp_path)
    original_graph = graph.read_bytes()
    _write(root, "orders/mod.py", "def order():\n    return 2\n")
    definition = root / "docs" / "systems" / "orders" / "system.toml"
    definition.write_text(
        'schema_version = 1\nname = "orders"\nfiles = ["orders/mod.py"]\nunknown = true\n',
        encoding="utf-8",
    )

    status, out, err = _systems(capsys, root, graph)

    assert status == 2
    assert out == ""
    assert "unknown system field: unknown" in err
    assert "refreshed graph" not in err
    assert "minotaur: stale:" not in err
    assert graph.read_bytes() == original_graph


def test_systems_refresh_and_no_refresh_report_distinct_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, graph = _tree(tmp_path)
    original_graph = graph.read_bytes()
    _write(root, "orders/mod.py", "def order():\n    return 2\n")

    status, out, err = _systems(capsys, root, graph, "--json")

    assert status == 0
    assert "minotaur: refreshed graph" in err
    assert "minotaur: stale: orders/mod.py" in err
    assert json.loads(out)["coverage"]["source_diagnostics"] == {
        "status": "observed_on_refresh",
        "count": 0,
    }

    graph.write_bytes(original_graph)
    status, out, err = _systems(capsys, root, graph, "--json", "--no-refresh")

    assert status == 0
    assert "minotaur: stale: orders/mod.py" in err
    assert "refreshed graph" not in err
    assert json.loads(out)["coverage"]["source_diagnostics"] == {"status": "unavailable"}
