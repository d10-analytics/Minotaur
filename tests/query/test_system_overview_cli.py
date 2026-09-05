"""Natural CLI coverage for the all-systems repository overview."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.identity import IdentityBasis, NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node, NodeClass
from minotaur.graph_model.provenance import CoordinateEncoding
from minotaur.graph_model.serialization import serialize
from minotaur.query.index import GraphIndex


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
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
    assert out == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
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
    assert graph.read_bytes() == original_graph


def test_systems_cli_distinguishes_empty_tree_zero_node_and_symbol_only_graphs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty_root = _repo(tmp_path, "empty")
    _write(empty_root, "loose.py", "value = 1\n")
    empty_graph = empty_root / "graph.json"
    assert (
        cli.main(
            ["analyze", "--root", str(empty_root), "--output", str(empty_graph), str(empty_root)]
        )
        == 0
    )
    status, out, err = _systems(capsys, empty_root, empty_graph, "--details", "--json")
    assert status == 0
    assert err == ""
    empty_payload = json.loads(out)
    assert empty_payload["results"] == []
    assert empty_payload["coverage"]["graph_files"] == {
        "scope": "final_graph_file_nodes",
        "count": 1,
    }
    assert empty_payload["coverage"]["unassigned_files"] == {
        "scope": "final_graph_file_node_derived_paths",
        "count": 1,
        "paths": ["loose.py"],
    }
    assert empty_payload["connections"] == []

    zero_root = _repo(tmp_path, "zero")
    _declare(zero_root, "empty", ["empty.py"])
    zero_graph = zero_root / "graph.json"
    assert (
        cli.main(["analyze", "--root", str(zero_root), "--output", str(zero_graph), str(zero_root)])
        == 0
    )
    status, out, err = _systems(capsys, zero_root, zero_graph, "--details", "--json")
    assert status == 0
    assert err == "minotaur: warning: empty.py (listed by system empty)\n"
    zero_payload = json.loads(out)
    assert zero_payload["results"][0]["declared_files"] == {
        "scope": "declared_system_files",
        "total": 1,
        "represented": 0,
        "absent": 1,
        "paths": ["empty.py"],
    }
    assert zero_payload["coverage"]["graph_files"] == {
        "scope": "final_graph_file_nodes",
        "count": 0,
    }
    assert zero_payload["connections"] == []

    symbol_root = _repo(tmp_path, "symbol")
    _declare(symbol_root, "orders", ["orders/mod.py"])
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, "fixture")
    location = Location("orders/mod.py", Range(Position(0, 0), Position(0, 1)))
    symbol = Node(
        id=compute_node_id(
            identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind="function",
            location=location,
        ),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label="orders.mod.order",
        symbol_kind="function",
        location=location,
    )
    symbol_graph = symbol_root / "graph.json"
    symbol_graph.write_bytes(
        serialize(
            GraphDocument(
                coordinate_encoding=CoordinateEncoding.UTF_8,
                nodes=(symbol,),
            )
        )
    )
    status, out, err = _systems(capsys, symbol_root, symbol_graph, "--details", "--json")
    assert status == 0
    assert err == ""
    symbol_payload = json.loads(out)
    assert symbol_payload["coverage"]["selection"] == {"status": "unavailable"}
    assert symbol_payload["results"][0]["declared_files"]["represented"] == 1
    assert symbol_payload["coverage"]["graph_files"]["count"] == 0
    assert symbol_payload["coverage"]["unassigned_files"] == {
        "scope": "final_graph_file_node_derived_paths",
        "count": 0,
        "paths": [],
    }


def test_systems_config_discovery_matches_explicit_graph_and_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repo(tmp_path)
    _write(root, "orders/mod.py", "def order():\n    return 1\n")
    _declare(root, "orders", ["orders/mod.py"])
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["."]\n',
        encoding="utf-8",
    )
    graph = root / "graph.json"
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    capsys.readouterr()
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)

    status = cli.main(["query", "systems", "--details", "--json"])
    discovered = capsys.readouterr()
    assert status == 0
    status, explicit_out, explicit_err = _systems(capsys, root, graph, "--details", "--json")
    assert status == 0
    discovered_out, discovered_err = discovered.out, discovered.err
    assert discovered_out == explicit_out
    assert discovered_err == explicit_err


def test_systems_details_json_routes_named_boundary_connections(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _write(root, "orders/mod.py", "def order():\n    return 1\n")
    _write(
        root,
        "use.py",
        "from orders.mod import order\n\ndef caller():\n    return order()\n",
    )
    _declare(root, "orders", ["orders/mod.py"])
    graph = root / "graph.json"
    assert cli.main(["analyze", "--root", str(root), "--output", str(graph), str(root)]) == 0

    status, out, err = _systems(capsys, root, graph, "--details", "--json")

    assert status == 0
    assert err == ""
    connections = json.loads(out)["connections"]
    assert len(connections) == 1
    assert connections[0]["source_category"] == "no_system"
    assert connections[0]["target_category"] == "system: orders"
    assert connections[0]["kinds"] == ["calls", "imports"]
    assert len(connections[0]["relationships"]) == 2
    assert {
        "source",
        "target",
        "kind",
        "relationship_extensions",
        "evidence",
    } == set(connections[0]["relationships"][0])
