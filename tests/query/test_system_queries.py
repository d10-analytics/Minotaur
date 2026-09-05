"""Behavioral coverage for the system boundary queries (AC-05..AC-13).

Every cell drives natural ``cli.main`` invocations (with ``capsys``) over real
Python trees under ``tmp_path`` repositories -- cross-module ``calls`` /
``references`` / ``imports``, internal and same-file edges, unlisted files,
unknown systems, a system without consumers, freshness states around invalid
declarations, listed-but-absent warnings, and config/config-less routing.
Only the genuinely path-less ``external`` endpoint (F-04) is exercised on an
in-memory node set: no shipped interpreter emits one from Python source.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Evidence, Producer
from minotaur.graph_model.identity import IdentityBasis, NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node, NodeClass
from minotaur.graph_model.provenance import CoordinateEncoding, Provenance
from minotaur.graph_model.relationship import Relationship
from minotaur.query import system as system_query
from minotaur.query.index import GraphIndex
from minotaur.system import System, load_systems, resolve_system

_DOCS = "docs/systems"


def _projection_symbol(label: str, path: str, line: int) -> Node:
    location = Location(path, Range(Position(line, 0), Position(line, 1)))
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, "test")
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.SYMBOL.value,
        symbol_kind="function",
        location=location,
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind="function",
        location=location,
    )


def _projection_file(path: str) -> Node:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, "test")
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.FILE.value, path=path),
        identity=identity,
        node_class=NodeClass.FILE,
        label=path,
        path=path,
    )


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    """A Git work tree whose root is the config-discovery stop point."""
    root = tmp_path / name
    root.mkdir()
    assert (
        subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True, check=False).returncode
        == 0
    )
    return root


def _declare(root: Path, name: str, files: list[str], systems_dir: str = _DOCS) -> Path:
    directory = root / systems_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    definition = directory / "system.toml"
    definition.write_text(
        f'schema_version = 1\nname = "{name}"\nfiles = {json.dumps(files)}\n',
        encoding="utf-8",
    )
    return definition


def _analyze(root: Path) -> Path:
    output = root / "graph.json"
    status = cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])
    assert status == 0
    return output


def _orders_tree(root: Path) -> None:
    """orders/mod.py (order, cancel) plus a no_system consumer calling order."""
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n\ndef cancel():\n    pass\n")
    _write(
        root,
        "use.py",
        "from orders.mod import order\n\ndef caller():\n    order()\n",
    )
    _declare(root, "orders", ["orders/mod.py"])


def _query(
    capsys: pytest.CaptureFixture[str],
    graph: Path,
    root: Path,
    name: str,
    system_name: str,
    *extra: str,
) -> tuple[int, str, str]:
    status = cli.main(
        ["query", name, system_name, "--graph", str(graph), "--root", str(root), *extra]
    )
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _assert_system_text(out: str, summary: str) -> dict[str, object]:
    """Assert the coverage prefix while keeping legacy summary bytes pinned."""
    coverage_line, separator, remainder = out.partition("\n")
    assert separator == "\n"
    assert coverage_line.startswith("coverage ")
    coverage = json.loads(coverage_line.removeprefix("coverage "))
    assert remainder == summary
    return coverage


# ---------------------------------------------------------------------------
# AC-05: surface -- one record per exposed in-scope symbol, never internal,
# same-file, or module-import exposure.
# ---------------------------------------------------------------------------


def test_surface_reports_only_symbols_outside_files_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n\ndef cancel():\n    pass\n")
    # Cross-file *internal* edge: audit.py is listed by orders too.
    _write(
        root,
        "orders/audit.py",
        "from orders.mod import order\n\ndef audit():\n    return order()\n",
    )
    # Outside consumers: one calls order, one only imports cancel's module.
    _write(
        root,
        "mix.py",
        "from orders.mod import order\n\ndef caller():\n    order()\n",
    )
    _write(
        root,
        "importer_only.py",
        "from orders.mod import cancel\n\ndef nothing():\n    return 42\n",
    )
    _declare(root, "orders", ["orders/mod.py", "orders/audit.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, err = _query(capsys, graph, root, "surface", "orders")
    assert status == 0
    assert err == ""
    coverage = _assert_system_text(out, "orders/mod.py  orders.mod.order  calls\n")
    assert coverage["declared_files"]["total"] == 2
    # The internal cross-file call from audit.py and the module-layer imports
    # from importer_only.py expose nothing (AC-05).
    assert "audit" not in out
    assert "cancel" not in out

    status, out, _ = _query(capsys, graph, root, "surface", "orders", "--json")
    assert status == 0
    payload = json.loads(out)
    assert payload["query"] == "surface"
    assert payload["refreshed"] is False
    assert payload["stale"] == []
    assert payload["results"] == [
        {
            "category": "system: orders",
            "kinds": ["calls"],
            "path": "orders/mod.py",
            "symbol": "orders.mod.order",
        }
    ]
    assert payload["coverage"]["source_diagnostics"] == {"status": "unavailable"}


def test_surface_import_only_consumer_exposes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-05: importing the system's module is not surface."""
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(root, "importer_only.py", "import orders.mod\n\ndef nothing():\n    return 42\n")
    _declare(root, "orders", ["orders/mod.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, _ = _query(capsys, graph, root, "surface", "orders")
    assert status == 0
    _assert_system_text(out, "no exposed symbols\n")

    status, out, _ = _query(capsys, graph, root, "surface", "orders", "--json")
    assert status == 0
    assert json.loads(out)["results"] == []


def test_surface_one_row_per_symbol_aggregates_reaching_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-05: one record per symbol; calls and references aggregate as kinds."""
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(
        root,
        "mix.py",
        "from orders.mod import order\n"
        "\n"
        "def caller():\n"
        "    order()\n"
        "\n"
        "def grab():\n"
        "    return order\n",
    )
    _declare(root, "orders", ["orders/mod.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, _ = _query(capsys, graph, root, "surface", "orders", "--json")
    assert status == 0
    payload = json.loads(out)
    assert payload["results"] == [
        {
            "category": "system: orders",
            "kinds": ["calls", "references"],
            "path": "orders/mod.py",
            "symbol": "orders.mod.order",
        }
    ]
    assert set(payload) == {"query", "refreshed", "results", "stale", "coverage"}
    assert payload["coverage"]["source_diagnostics"] == {"status": "unavailable"}


# ---------------------------------------------------------------------------
# AC-06: consumers -- one row per outside file, distinct kinds, in-scope
# targets as detail; module-layer imports count even when calls never resolve.
# ---------------------------------------------------------------------------


def test_consumers_one_row_per_outside_file_with_distinct_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n\ndef cancel():\n    pass\n")
    _write(root, "billing/__init__.py", "")
    _write(
        root,
        "billing/svc.py",
        "from orders.mod import order\n\ndef ship():\n    return order()\n",
    )
    _write(
        root,
        "use.py",
        "from orders.mod import order\n"
        "import billing.svc\n"
        "\n"
        "def caller():\n"
        "    order()\n"
        "    billing.svc.ship()\n",
    )
    _declare(root, "orders", ["orders/mod.py"])
    _declare(root, "billing", ["billing/svc.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, err = _query(capsys, graph, root, "consumers", "orders")
    assert status == 0
    assert err == ""
    _assert_system_text(
        out,
        "billing/svc.py (system: billing)  calls: orders.mod.order (orders/mod.py); "
        "imports: orders.mod.order (orders/mod.py)\n"
        "use.py (no_system)  calls: orders.mod.order (orders/mod.py); "
        "imports: orders.mod.order (orders/mod.py)\n",
    )

    status, out, _ = _query(capsys, graph, root, "consumers", "orders", "--json")
    assert status == 0
    payload = json.loads(out)
    assert payload["query"] == "consumers"
    assert payload["results"] == [
        {
            "category": "system: billing",
            "file": "billing/svc.py",
            "kinds": ["calls", "imports"],
            "targets": [
                {"kind": "calls", "label": "orders.mod.order", "path": "orders/mod.py"},
                {"kind": "imports", "label": "orders.mod.order", "path": "orders/mod.py"},
            ],
        },
        {
            "category": "no_system",
            "file": "use.py",
            "kinds": ["calls", "imports"],
            "targets": [
                {"kind": "calls", "label": "orders.mod.order", "path": "orders/mod.py"},
                {"kind": "imports", "label": "orders.mod.order", "path": "orders/mod.py"},
            ],
        },
    ]


def test_module_layer_import_alone_makes_a_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-06: an outside module that only imports a system module is a consumer
    through ``imports`` even when its calls never resolve."""
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(
        root,
        "onlyimports.py",
        "import orders.mod\n\ndef try_call():\n    return missing()\n",
    )
    _declare(root, "orders", ["orders/mod.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, _ = _query(capsys, graph, root, "consumers", "orders", "--json")
    assert status == 0
    payload = json.loads(out)
    assert payload["results"] == [
        {
            "category": "no_system",
            "file": "onlyimports.py",
            "kinds": ["imports"],
            "targets": [{"kind": "imports", "label": "orders.mod", "path": "orders/mod.py"}],
        }
    ]


# ---------------------------------------------------------------------------
# AC-07: system-deps -- one row per target category with sorted nested detail.
# ---------------------------------------------------------------------------


def test_system_deps_rows_per_category_with_sorted_nested_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    for sub in ("orders", "billing", "lib"):
        _write(root, f"{sub}/__init__.py", "")
    _write(
        root,
        "orders/mod.py",
        "from billing.svc import ship\n"
        "import lib.util\n"
        "\n"
        "def order():\n"
        "    ship()\n"
        "    lib.util.helper()\n",
    )
    _write(root, "billing/svc.py", "def ship():\n    pass\n")
    _write(root, "lib/util.py", "def helper():\n    pass\n")
    _declare(root, "orders", ["orders/mod.py"])
    _declare(root, "billing", ["billing/svc.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, err = _query(capsys, graph, root, "system-deps", "orders")
    assert status == 0
    assert err == ""
    _assert_system_text(
        out,
        "no_system  imports: lib.util (lib/util.py); references: lib.util (lib/util.py)\n"
        "system: billing  calls: billing.svc.ship (billing/svc.py); "
        "imports: billing.svc.ship (billing/svc.py)\n",
    )

    status, out, _ = _query(capsys, graph, root, "system-deps", "orders", "--json")
    assert status == 0
    payload = json.loads(out)
    assert payload["results"] == [
        {
            "category": "no_system",
            "targets": [
                {"kind": "imports", "label": "lib.util", "path": "lib/util.py"},
                {"kind": "references", "label": "lib.util", "path": "lib/util.py"},
            ],
        },
        {
            "category": "system: billing",
            "targets": [
                {"kind": "calls", "label": "billing.svc.ship", "path": "billing/svc.py"},
                {"kind": "imports", "label": "billing.svc.ship", "path": "billing/svc.py"},
            ],
        },
    ]


def test_external_category_from_a_pathless_upstream_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-04/AC-07: a path-less upstream target is an explicit ``external`` row.

    No shipped interpreter emits a path-less upstream node from Python source
    (F-04), so the external cell exercises the producers and renderers over an
    in-memory digest-correct node added to a naturally analyzed graph.
    """
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _declare(root, "orders", ["orders/mod.py"])
    graph_path = _analyze(root)
    monkeypatch.chdir(root)

    from minotaur.graph_model.loading import load_graph_file

    systems = load_systems(root / _DOCS)
    target = resolve_system(systems, "orders")
    document = load_graph_file(graph_path).document
    order_node = next(node for node in document.nodes if node.label == "orders.mod.order")
    identity = NodeIdentity(
        IdentityBasis.UPSTREAM_IDENTIFIER, "test-fixture", upstream_identifier="gateway.ship"
    )
    upstream = Node(
        id=compute_node_id(identity, node_class=NodeClass.SYMBOL.value, symbol_kind="function"),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label="gateway.ship",
        symbol_kind="function",
    )
    extended = dataclasses.replace(
        document,
        nodes=document.nodes + (upstream,),
        relationships=document.relationships
        + (
            Relationship(
                source=order_node.id,
                target=upstream.id,
                kind="calls",
                evidence=(
                    Evidence(
                        provenance=Provenance.IMPORTED_GRAPH,
                        producer=Producer(name="test-fixture"),
                    ),
                ),
            ),
        ),
    )
    records = system_query.system_deps(systems, GraphIndex.build(extended), target)
    assert len(records) == 1
    record = records[0]
    assert record.category == "external"
    assert record.to_dict() == {
        "category": "external",
        "targets": [{"kind": "calls", "label": "gateway.ship"}],
    }
    assert system_query.render_system_deps_text(records) == "external  calls: gateway.ship\n"
    # The genuinely path-less endpoint stays path-less end to end.
    assert "node:sha256:" not in system_query.render_system_deps_text(records)


# ---------------------------------------------------------------------------
# AC-08: deterministic, stable, node-ID-free text and JSON output.
# ---------------------------------------------------------------------------


def test_system_queries_json_is_deterministic_and_hides_graph_internals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(
        root,
        "mix.py",
        "from orders.mod import order\n\ndef caller():\n    order()\n",
    )
    _declare(root, "orders", ["orders/mod.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    pairs = (("surface", "orders"), ("consumers", "orders"), ("system-deps", "orders"))
    for name, system_name in pairs:
        first = _query(capsys, graph, root, name, system_name, "--json")
        second = _query(capsys, graph, root, name, system_name, "--json")
        assert first[0] == second[0] == 0
        assert first[1] == second[1]
        assert "node:sha256:" not in first[1]
        payload = json.loads(first[1])
        assert set(payload) == {"query", "refreshed", "results", "stale", "coverage"}
        for record in payload["results"]:
            if name == "surface":
                assert set(record) == {"category", "kinds", "path", "symbol"}
            elif name == "consumers":
                assert set(record) == {"category", "file", "kinds", "targets"}
            else:
                assert set(record) == {"category", "targets"}
            for target in record.get("targets", []):
                assert set(target) == {"kind", "label", "path"}
                assert "sha256" not in target["label"]


def test_second_call_site_in_visible_consumer_changes_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(
        root,
        "use.py",
        "from orders.mod import order\n\ndef caller():\n    order()\n",
    )
    _declare(root, "orders", ["orders/mod.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    baseline = _query(capsys, graph, root, "consumers", "orders", "--json")
    assert baseline[0] == 0

    # A second call site inside the already-visible consumer file.
    _write(
        root,
        "use.py",
        "from orders.mod import order\n\ndef caller():\n    order()\n    order()\n",
    )
    _analyze(root)
    second = _query(capsys, graph, root, "consumers", "orders", "--json")
    assert second[0] == 0
    assert second[1] == baseline[1]


# ---------------------------------------------------------------------------
# AC-09 / AC-10: unknown names are exit-2 resolution errors; a defined system
# without consumers is a successful empty answer.
# ---------------------------------------------------------------------------


def test_unknown_system_exits_two_naming_nearest_loaded_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(root, "billing/__init__.py", "")
    _write(root, "billing/svc.py", "def ship():\n    pass\n")
    _declare(root, "orders", ["orders/mod.py"])
    _declare(root, "billing", ["billing/svc.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    for name in ("surface", "consumers", "system-deps"):
        status = cli.main(
            ["query", name, "oder", "--graph", str(graph), "--root", str(root), "--json"]
        )
        captured = capsys.readouterr()
        assert status == 2
        assert captured.out == ""
        assert captured.err == ("minotaur: error: unknown system: oder; nearest systems: orders\n")


def test_defined_system_with_no_consumers_is_a_successful_empty_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-10: ``no consumers`` / empty JSON at exit 0, never an error."""
    root = _repo(tmp_path)
    _write(root, "solo/__init__.py", "")
    _write(root, "solo/core.py", "def core():\n    pass\n")
    _write(root, "solo/inner.py", "from solo.core import core\n\ndef inner():\n    return core()\n")
    _declare(root, "solo", ["solo/core.py", "solo/inner.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, err = _query(capsys, graph, root, "consumers", "solo")
    assert status == 0
    assert err == ""
    _assert_system_text(out, "no consumers\n")

    status, out, _ = _query(capsys, graph, root, "consumers", "solo", "--json")
    assert status == 0
    assert json.loads(out)["results"] == []


# ---------------------------------------------------------------------------
# AC-11: listed-but-absent files warn per file, deterministically, at exit 0.
# ---------------------------------------------------------------------------


def test_listed_but_absent_files_warn_once_per_file_and_answer_fully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(
        root,
        "use.py",
        "from orders.mod import order\n\ndef caller():\n    order()\n",
    )
    _declare(root, "orders", ["orders/mod.py", "orders/missing.py", "orders/notes.md"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, err = _query(capsys, graph, root, "consumers", "orders")
    assert status == 0
    _assert_system_text(
        out,
        "use.py (no_system)  calls: orders.mod.order (orders/mod.py); "
        "imports: orders.mod.order (orders/mod.py)\n",
    )
    assert err == (
        "minotaur: warning: orders/missing.py (listed by system orders)\n"
        "minotaur: warning: orders/notes.md (listed by system orders)\n"
    )

    # The JSON envelope is identical with the same warnings on stderr.
    status, out, err = _query(capsys, graph, root, "consumers", "orders", "--json")
    assert status == 0
    assert json.loads(out)["results"]
    assert err == (
        "minotaur: warning: orders/missing.py (listed by system orders)\n"
        "minotaur: warning: orders/notes.md (listed by system orders)\n"
    )


# ---------------------------------------------------------------------------
# AC-12: the strict system-tree load runs in _query's system path, before any
# freshness refresh, on every freshness state; invalid declarations exit 2
# with the file-attributed load error, no answer, and no graph rewrite.  A
# valid declaration refreshes normally and loaded systems add nothing to the
# serialized graph (AR-04).
# ---------------------------------------------------------------------------


def _orders_with_consumer(root: Path) -> None:
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    return 1\n")
    _write(
        root,
        "use.py",
        "from orders.mod import order\n\ndef caller():\n    return order()\n",
    )
    _declare(root, "orders", ["orders/mod.py"])


def test_invalid_declaration_beside_clean_graph_exits_two_no_answer_no_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _orders_with_consumer(root)
    graph = _analyze(root)
    monkeypatch.chdir(root)
    original_bytes = graph.read_bytes()

    definition = _declare(root, "orders", ["orders/mod.py"])
    definition.write_text(
        'schema_version = 99\nname = "orders"\nfiles = ["orders/mod.py"]\n', encoding="utf-8"
    )

    status, out, err = _query(capsys, graph, root, "consumers", "orders")
    assert status == 2
    assert out == ""
    assert "unsupported schema_version: 99 (expected 1)" in err
    assert str(definition) in err
    assert "unknown system: orders" not in err
    assert "refreshed graph" not in err
    assert graph.read_bytes() == original_bytes


def test_invalid_declaration_beside_drifted_graph_exits_two_without_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _orders_with_consumer(root)
    graph = _analyze(root)
    monkeypatch.chdir(root)
    original_bytes = graph.read_bytes()

    # Drift the source so a refresh would otherwise rewrite the graph.
    _write(root, "orders/mod.py", "def order():\n    return 2\n")
    definition = _declare(root, "orders", ["orders/mod.py"])
    definition.write_text(
        'schema_version = 1\nname = "orders"\nfiles = ["orders/mod.py"]\nexpectations = ["x"]\n',
        encoding="utf-8",
    )

    status, out, err = _query(capsys, graph, root, "consumers", "orders")
    assert status == 2
    assert out == ""
    assert "unknown system field: expectations" in err
    assert str(definition) in err
    assert "unknown system: orders" not in err
    assert "refreshed graph" not in err
    assert graph.read_bytes() == original_bytes

    # Same drifted graph, --no-refresh: the strict load still precedes the
    # freshness refusal and still reports the declaration, never stale lines.
    status, out, err = _query(capsys, graph, root, "consumers", "orders", "--no-refresh")
    assert status == 2
    assert out == ""
    assert "unknown system field: expectations" in err
    assert "minotaur: stale:" not in err
    assert "refreshed graph" not in err
    assert graph.read_bytes() == original_bytes


def test_surface_clean_graph_beside_invalid_declaration_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The strict load also runs on the surface/system-deps paths, not only the
    consumers path, and on a graph whose freshness would otherwise be clean."""
    root = _repo(tmp_path)
    _orders_with_consumer(root)
    graph = _analyze(root)
    monkeypatch.chdir(root)
    original_bytes = graph.read_bytes()

    definition = _declare(root, "orders", ["orders/mod.py"])
    definition.write_text('schema_version = 1\nname = "orders"\nfiles = []\n', encoding="utf-8")

    for name in ("surface", "system-deps"):
        status, out, err = _query(capsys, graph, root, name, "orders")
        assert status == 2
        assert out == ""
        assert "system files must not be empty" in err
        assert str(definition) in err
        assert graph.read_bytes() == original_bytes


def test_valid_declaration_refresh_matches_non_system_query_and_adds_no_graph_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-12/AR-04: a valid declaration beside a drifted graph refreshes and
    answers normally, and the rewritten bytes equal the bytes the same drift
    produces for a non-system graph query -- loaded systems demonstrably add
    no node or relationship to the serialized graph on the write path."""
    root = _repo(tmp_path)
    _orders_with_consumer(root)
    graph = _analyze(root)
    monkeypatch.chdir(root)
    state_a_bytes = graph.read_bytes()

    _write(root, "orders/mod.py", "def order():\n    return 5\n")
    status, out, err = _query(capsys, graph, root, "consumers", "orders")
    assert status == 0
    assert "refreshed graph" in err
    assert "minotaur: stale: orders/mod.py" in err
    assert "use.py (no_system)" in out
    refreshed_by_system_query = graph.read_bytes()
    assert refreshed_by_system_query != state_a_bytes

    # Restore the drifted state's graph file and refresh through a non-system
    # graph query instead: the rewritten bytes must be identical.
    graph.write_bytes(state_a_bytes)
    callers_status = cli.main(
        [
            "query",
            "callers",
            "orders.mod.order",
            "--graph",
            str(graph),
            "--root",
            str(root),
        ]
    )
    captured = capsys.readouterr()
    assert callers_status == 0
    assert "refreshed graph" in captured.err
    refreshed_by_callers = graph.read_bytes()
    assert refreshed_by_callers == refreshed_by_system_query


# ---------------------------------------------------------------------------
# AC-13: config-consuming routing -- from a nested cwd inside a config tree
# the queries answer from config graph/root/systems_dir with no flags, and
# with explicit --graph/--root and no config from <root>/docs/systems.
# ---------------------------------------------------------------------------


def test_config_tree_system_query_answers_from_nested_cwd_with_no_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(
        root,
        "mix.py",
        "from orders.mod import order\n\ndef caller():\n    order()\n",
    )
    # systems_dir is config-sourced ("declared"): if the query defaulted to
    # docs/systems it would find no system and exit 2.
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "g.json"\n'
        'targets = ["orders", "mix.py"]\nsystems_dir = "declared"\n',
        encoding="utf-8",
    )
    _declare(root, "orders", ["orders/mod.py"], systems_dir="declared")
    nested = root / "deep" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert cli.main(["analyze"]) == 0
    capsys.readouterr()  # Drain analyze output; the query output is asserted next.

    status = cli.main(["query", "consumers", "orders"])
    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == ""
    _assert_system_text(
        captured.out,
        "mix.py (no_system)  calls: orders.mod.order (orders/mod.py); "
        "imports: orders.mod.order (orders/mod.py)\n",
    )
    assert (root / "g.json").exists()


def test_config_less_explicit_graph_root_answers_from_default_docs_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-13/D-11: with explicit --graph/--root and no located config, the
    systems tree resolves to <root>/docs/systems by default."""
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(
        root,
        "use.py",
        "from orders.mod import order\n\ndef caller():\n    order()\n",
    )
    _declare(root, "orders", ["orders/mod.py"])  # Default docs/systems location.
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, err = _query(capsys, graph, root, "consumers", "orders")
    assert status == 0
    assert err == ""
    _assert_system_text(
        out,
        "use.py (no_system)  calls: orders.mod.order (orders/mod.py); "
        "imports: orders.mod.order (orders/mod.py)\n",
    )


# ---------------------------------------------------------------------------
# Reviewer-added cells (adversarial pass): participant-keying across distinct
# outside files, the system-deps internal-edge/empty form, and the D-09
# diagnosis-after-refresh ordering.
# ---------------------------------------------------------------------------


def test_surface_one_row_per_symbol_across_distinct_outside_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-05: rows key on the exposed symbol, never the calling file -- two
    distinct outside files both calling ``order`` still yield exactly one
    surface record, and a new outside caller changes no record set."""
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(
        root,
        "a.py",
        "from orders.mod import order\n\ndef fa():\n    order()\n",
    )
    _write(
        root,
        "b.py",
        "from orders.mod import order\n\ndef fb():\n    order()\n",
    )
    _declare(root, "orders", ["orders/mod.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, err = _query(capsys, graph, root, "surface", "orders", "--json")
    assert status == 0
    assert err == ""
    assert json.loads(out)["results"] == [
        {
            "category": "system: orders",
            "kinds": ["calls"],
            "path": "orders/mod.py",
            "symbol": "orders.mod.order",
        }
    ]


def test_system_deps_internal_edges_are_never_a_dependency_and_empty_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-07/R-07: within-system and same-file edges are internal and never a
    dependency row; the empty result answers ``no dependencies`` / empty JSON
    at exit 0."""
    root = _repo(tmp_path)
    _write(root, "solo/__init__.py", "")
    _write(root, "solo/core.py", "def core():\n    pass\n")
    _write(
        root,
        "solo/inner.py",
        "from solo.core import core\n\ndef inner():\n    return core()\n",
    )
    _declare(root, "solo", ["solo/core.py", "solo/inner.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, err = _query(capsys, graph, root, "system-deps", "solo")
    assert status == 0
    assert err == ""
    _assert_system_text(out, "no dependencies\n")

    status, out, _ = _query(capsys, graph, root, "system-deps", "solo", "--json")
    assert status == 0
    assert json.loads(out)["results"] == []


def test_absent_warning_is_computed_against_the_final_index_after_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-09/AC-11: the listed-but-absent diagnosis runs after refresh against
    the final index -- a declared file created between analyze and query is
    analyzed by the drift refresh, so no stale warning may survive on the
    refresh path.  A diagnosis computed from the pre-refresh document would
    keep warning about the now-analyzed file."""
    root = _repo(tmp_path)
    _orders_with_consumer(root)
    _declare(root, "orders", ["orders/mod.py", "orders/new.py"])  # new.py absent on disk yet.
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, err = _query(capsys, graph, root, "consumers", "orders")
    assert status == 0
    assert "minotaur: warning: orders/new.py (listed by system orders)\n" in err

    # Create the declared file and drift mod.py: the refresh must analyze
    # new.py and the warning must not survive on the refreshed answer.
    _write(root, "orders/new.py", "def new():\n    pass\n")
    _write(root, "orders/mod.py", "def order():\n    return 2\n")
    status, out, err = _query(capsys, graph, root, "consumers", "orders")
    assert status == 0
    assert "refreshed graph" in err
    assert "warning:" not in err
    assert "use.py (no_system)" in out


def test_system_cli_composes_coverage_and_details_for_each_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-03: all system siblings expose one composed report contract."""
    root = _repo(tmp_path)
    _orders_with_consumer(root)
    graph = _analyze(root)
    monkeypatch.chdir(root)

    for name in ("surface", "consumers", "system-deps"):
        status, default_out, err = _query(capsys, graph, root, name, "orders", "--json")
        assert status == 0
        assert err == ""
        default_payload = json.loads(default_out)
        assert set(default_payload) == {"query", "refreshed", "results", "stale", "coverage"}
        assert "relationships" not in default_payload
        assert default_payload["coverage"]["source_diagnostics"] == {"status": "unavailable"}

        status, details_out, err = _query(
            capsys, graph, root, name, "orders", "--json", "--details"
        )
        assert status == 0
        assert err == ""
        details_payload = json.loads(details_out)
        assert details_payload["coverage"] == default_payload["coverage"]
        assert isinstance(details_payload["relationships"], list)

        status, details_text, err = _query(capsys, graph, root, name, "orders", "--details")
        assert status == 0
        assert err == ""
        lines = details_text.splitlines()
        assert json.loads(lines[0].removeprefix("coverage ")) == details_payload["coverage"]
        relationship_line = next(line for line in lines if line.startswith("relationships "))
        assert (
            json.loads(relationship_line.removeprefix("relationships "))
            == details_payload["relationships"]
        )


def test_system_cli_refresh_composes_zero_and_positive_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-04: refresh diagnostics are taken from the final invocation."""
    root = _repo(tmp_path)
    _orders_with_consumer(root)
    graph = _analyze(root)
    monkeypatch.chdir(root)

    _write(root, "orders/mod.py", "def order():\n    return 2\n")
    status, out, err = _query(capsys, graph, root, "surface", "orders", "--json")
    assert status == 0
    assert "refreshed graph" in err
    payload = json.loads(out)
    assert payload["refreshed"] is True
    assert payload["coverage"]["source_diagnostics"] == {
        "status": "observed_on_refresh",
        "count": 0,
    }

    _write(root, "broken.py", "def broken(\n")
    status, out, err = _query(capsys, graph, root, "consumers", "orders", "--json")
    assert status == 1
    assert "refreshed graph" in err
    payload = json.loads(out)
    assert payload["refreshed"] is True
    assert payload["coverage"]["source_diagnostics"]["status"] == "observed_on_refresh"
    assert payload["coverage"]["source_diagnostics"]["count"] > 0


def test_system_cli_no_refresh_keeps_saved_coverage_and_unavailable_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-03/AC-04: stale answers remain graph-only when refresh is disabled."""
    root = _repo(tmp_path)
    _orders_with_consumer(root)
    graph = _analyze(root)
    monkeypatch.chdir(root)
    _write(root, "orders/mod.py", "def order():\n    return 8\n")

    status, out, err = _query(capsys, graph, root, "consumers", "orders", "--json", "--no-refresh")
    assert status == 0
    assert "minotaur: stale: orders/mod.py" in err
    payload = json.loads(out)
    assert payload["refreshed"] is False
    assert payload["stale"] == ["orders/mod.py"]
    assert payload["coverage"]["source_diagnostics"] == {"status": "unavailable"}


def test_reporting_snapshot_projects_coverage_and_details_from_canonical_owner() -> None:
    target_node = _projection_symbol("orders.mod.order", "orders/mod.py", 0)
    source_node = _projection_symbol("use.call", "use.py", 2)
    target_file = _projection_file("orders/mod.py")
    source_file = _projection_file("use.py")
    evidence = Evidence(
        provenance=Provenance.STATIC_ANALYSIS,
        producer=Producer("test", "1"),
        locations=(
            Location("use.py", Range(Position(4, 2), Position(4, 7))),
            Location("use.py", Range(Position(1, 0), Position(1, 2))),
        ),
        extensions={},
    )
    relationship = Relationship(
        source=source_node.id,
        target=target_node.id,
        kind="calls",
        evidence=(evidence,),
        extensions={},
    )
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(target_node, source_node, target_file, source_file),
        relationships=(relationship,),
        extensions={"minotaur": {"selection": [".", "orders/mod.py"]}},
    )
    snapshot = system_query.ReportingSnapshot.prepare(
        document, (System("orders", ("orders/mod.py",)),)
    )

    report = snapshot.report("surface", "orders", details=True)
    assert isinstance(report, system_query.SystemReport)
    assert isinstance(report.results[0], system_query.SurfaceRecord)
    assert report.coverage.to_dict() == {
        "selection": {"status": "recorded", "targets": [".", "orders/mod.py"]},
        "graph_files": {"scope": "final_graph_file_nodes", "count": 2},
        "declared_files": {
            "scope": "selected_system_declared_files",
            "total": 1,
            "represented": 1,
            "absent": 0,
        },
        "recorded_unresolved_references": {
            "scope": "selected_system_declared_files",
            "count": 0,
        },
        "source_diagnostics": {"status": "unavailable"},
    }
    assert report.relationships is not None
    detail = report.relationships[0].to_dict()
    assert detail["source"]["id"] == source_node.id
    assert detail["source"]["node_class"] == "symbol"
    assert detail["source"]["path"] == {"status": "recorded", "value": "use.py"}
    assert detail["source"]["location"]["range"] == {
        "start": {"line": 3, "column": 1},
        "end": {"line": 3, "column": 2},
        "end_exclusive": True,
    }
    assert detail["relationship_extensions"] == {"status": "recorded", "value": {}}
    assert detail["evidence"][0]["producer"] == {
        "status": "recorded",
        "name": "test",
        "version": {"status": "recorded", "value": "1"},
    }
    assert [site["range"]["start"]["line"] for site in detail["evidence"][0]["sites"]] == [2, 5]

    composed = system_query.compose_system_query(
        report, system_query.QueryInvocation(False, (), None)
    )
    assert composed.report is report
    payload = composed.to_dict()
    assert set(payload) == {"query", "refreshed", "results", "stale", "coverage", "relationships"}
    payload["coverage"]["declared_files"]["total"] = 99
    assert report.coverage.to_dict()["declared_files"]["total"] == 1


def test_reporting_snapshot_reuses_index_and_records_unavailable_selection() -> None:
    target = _projection_symbol("target", "target.py", 0)
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_16,
        nodes=(target,),
        extensions={"minotaur": {"selection": "target.py"}},
    )
    snapshot = system_query.ReportingSnapshot.prepare(document, (System("s", ("target.py",)),))
    first = snapshot.report("consumers", "s")
    second = snapshot.report("system-deps", "s")
    detailed_empty = snapshot.report("consumers", "s", details=True)
    assert first.coverage.selection == {"status": "unavailable"}
    assert first.relationships is None
    assert detailed_empty.relationships == ()
    assert snapshot.index is snapshot.index
    assert second.coverage.source_diagnostics == {"status": "unavailable"}
    with pytest.raises(ValueError, match="unknown system query"):
        snapshot.report("other", "s")


def test_reporting_snapshot_direct_query_variants_and_invocation_errors() -> None:
    """Direct consumers receive typed immutable reports.

    This intentionally bypasses CLI rendering so a renderer cannot mask a
    missing query variant, details shape, or invocation validation rule.
    """
    target_node = _projection_symbol("target", "target.py", 0)
    source_node = _projection_symbol("caller", "use.py", 0)
    dependency_node = _projection_symbol("dependency", "other.py", 0)
    target_file = _projection_file("target.py")
    source_file = _projection_file("use.py")
    dependency_file = _projection_file("other.py")
    evidence = Evidence(provenance=Provenance.STATIC_ANALYSIS)
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(
            target_node,
            source_node,
            dependency_node,
            target_file,
            source_file,
            dependency_file,
        ),
        relationships=(
            Relationship(
                source=source_node.id,
                target=target_node.id,
                kind="calls",
                evidence=(evidence,),
            ),
            Relationship(
                source=target_node.id,
                target=dependency_node.id,
                kind="references",
                evidence=(evidence,),
            ),
        ),
    )
    snapshot = system_query.ReportingSnapshot.prepare(document, [System("s", ("target.py",))])

    surface = snapshot.report("surface", "s")
    consumers = snapshot.report("consumers", "s")
    dependencies = snapshot.report("system-deps", "s")
    assert isinstance(surface.results, tuple)
    assert isinstance(surface.results[0], system_query.SurfaceRecord)
    assert isinstance(consumers.results, tuple)
    assert isinstance(consumers.results[0], system_query.ConsumersRecord)
    assert isinstance(dependencies.results, tuple)
    assert isinstance(dependencies.results[0], system_query.SystemDepsRecord)
    assert surface.relationships is None
    surface_details = snapshot.report("surface", "s", details=True)
    assert isinstance(surface_details.relationships, tuple)
    assert len(surface_details.relationships) == 1

    with pytest.raises(ValueError, match="unknown system query"):
        snapshot.report("unknown", "s")
    with pytest.raises(ValueError, match="unknown system"):
        snapshot.report("surface", "missing")

    with pytest.raises(ValueError, match="non-negative integer"):
        system_query.QueryInvocation(True, (), -1)
    with pytest.raises(ValueError, match="non-negative integer"):
        system_query.QueryInvocation(True, (), True)
    with pytest.raises(ValueError, match="disagree"):
        system_query.QueryInvocation(False, (), 0)
    with pytest.raises(ValueError, match="disagree"):
        system_query.QueryInvocation(True, (), None)

    invocation = system_query.QueryInvocation(True, ("z.py", "a.py"), 0)
    composed = system_query.compose_system_query(surface, invocation)
    assert composed.report is surface
    assert composed.invocation.stale == ("a.py", "z.py")
    assert composed.to_dict()["coverage"]["source_diagnostics"] == {
        "status": "observed_on_refresh",
        "count": 0,
    }

    observed = dataclasses.replace(
        surface.coverage,
        source_diagnostics={"status": "observed_on_refresh", "count": 1},
    )
    observed_report = dataclasses.replace(surface, coverage=observed)
    with pytest.raises(ValueError, match="snapshot report source diagnostics"):
        system_query.compose_system_query(
            observed_report, system_query.QueryInvocation(False, (), None)
        )
