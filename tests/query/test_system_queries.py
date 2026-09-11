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
        "no_system  calls: lib.util.helper (lib/util.py); imports: lib.util (lib/util.py)\n"
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
                {"kind": "calls", "label": "lib.util.helper", "path": "lib/util.py"},
                {"kind": "imports", "label": "lib.util", "path": "lib/util.py"},
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


def test_absent_warning_only_names_files_of_selected_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-03: diagnostics are scoped to the requested system, not the tree."""
    root = _repo(tmp_path)
    _write(root, "orders/__init__.py", "")
    _write(root, "orders/mod.py", "def order():\n    pass\n")
    _write(root, "billing/__init__.py", "")
    _write(root, "billing/svc.py", "def charge():\n    pass\n")
    _write(root, "use.py", "from orders.mod import order\n\ndef caller():\n    order()\n")
    _declare(root, "orders", ["orders/mod.py", "orders/missing.py"])
    _declare(root, "billing", ["billing/svc.py", "billing/missing.py"])
    graph = _analyze(root)
    monkeypatch.chdir(root)

    status, out, err = _query(capsys, graph, root, "consumers", "orders")
    assert status == 0
    assert out.startswith("coverage ")
    assert err == "minotaur: warning: orders/missing.py (listed by system orders)\n"
    assert "billing/missing.py" not in err


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
    assert detailed_empty.row_relationships == {}
    with pytest.raises(TypeError):
        detailed_empty.row_relationships[()] = ()  # type: ignore[index]
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


def _row_reporting_fixture() -> tuple[GraphDocument, tuple[System, ...]]:
    order_a = _projection_symbol("orders.a", "orders/a.py", 0)
    order_b = _projection_symbol("orders.b", "orders/b.py", 0)
    caller_a = _projection_symbol("callers.a", "callers/a.py", 0)
    caller_b = _projection_symbol("callers.b", "callers/b.py", 0)
    caller_c = _projection_symbol("callers.c", "callers/c.py", 0)
    billing = _projection_symbol("billing.ship", "billing.py", 0)
    loose = _projection_symbol("loose.value", "loose.py", 0)
    external = _projection_upstream("gateway.ship")
    evidence = Evidence(provenance=Provenance.STATIC_ANALYSIS)

    def edge(source: Node, target: Node, kind: str) -> Relationship:
        return Relationship(source=source.id, target=target.id, kind=kind, evidence=(evidence,))

    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(caller_c, order_b, external, billing, caller_a, loose, order_a, caller_b),
        relationships=(
            edge(order_b, external, "imports"),
            edge(caller_b, order_b, "imports"),
            edge(order_a, billing, "calls"),
            edge(caller_c, order_b, "calls"),
            edge(order_a, order_b, "calls"),
            edge(caller_a, order_a, "calls"),
            edge(order_b, loose, "references"),
            edge(caller_b, order_b, "references"),
        ),
    )
    systems = (System("orders", ("orders/a.py", "orders/b.py")), System("billing", ("billing.py",)))
    return document, systems


def test_detailed_reports_attach_exact_row_contributors_and_preserve_order() -> None:
    document, systems = _row_reporting_fixture()
    snapshot = system_query.ReportingSnapshot.prepare(document, systems)

    surface = snapshot.report("surface", "orders", details=True)
    assert [record.path for record in surface.results] == ["orders/a.py", "orders/b.py"]
    assert surface.relationships is not None
    assert [
        (item.source.id, item.target.id, item.kind) for item in surface.relationships
    ] == sorted((item.source.id, item.target.id, item.kind) for item in surface.relationships)
    assert list(surface.row_relationships or {}) == [
        ("orders/a.py", "orders.a"),
        ("orders/b.py", "orders.b"),
    ]
    assert [item.kind for item in surface.row_relationships[("orders/a.py", "orders.a")]] == [
        "calls"
    ]
    b_details = surface.row_relationships[("orders/b.py", "orders.b")]
    assert [(item.source.id, item.target.id, item.kind) for item in b_details] == sorted(
        (item.source.id, item.target.id, item.kind) for item in b_details
    )

    consumers = snapshot.report("consumers", "orders", details=True)
    assert [record.file for record in consumers.results] == [
        "callers/a.py",
        "callers/b.py",
        "callers/c.py",
    ]
    assert list(consumers.row_relationships or {}) == [
        ("callers/a.py",),
        ("callers/b.py",),
        ("callers/c.py",),
    ]
    assert [item.kind for item in consumers.row_relationships[("callers/b.py",)]] == [
        "imports",
        "references",
    ]

    dependencies = snapshot.report("system-deps", "orders", details=True)
    assert [record.category for record in dependencies.results] == [
        "external",
        "no_system",
        "system: billing",
    ]
    assert list(dependencies.row_relationships or {}) == [
        ("external",),
        ("no_system",),
        ("system: billing",),
    ]
    assert [item.kind for item in dependencies.row_relationships[("external",)]] == ["imports"]
    assert [item.kind for item in dependencies.row_relationships[("no_system",)]] == ["references"]
    assert [item.kind for item in dependencies.row_relationships[("system: billing",)]] == ["calls"]
    assert "row_relationships" not in dependencies.to_dict()


def test_row_maps_preserve_exact_edges_and_pathless_inbound_rules() -> None:
    document, systems = _row_reporting_fixture()
    nodes = {node.label: node for node in document.nodes}
    pathless = nodes["gateway.ship"]
    target = nodes["orders.b"]
    evidence = Evidence(provenance=Provenance.STATIC_ANALYSIS)
    document = dataclasses.replace(
        document,
        relationships=document.relationships
        + (
            Relationship(
                source=pathless.id,
                target=target.id,
                kind="calls",
                evidence=(evidence,),
            ),
        ),
    )
    snapshot = system_query.ReportingSnapshot.prepare(document, systems)

    def identities(
        report: system_query.SystemReport[object],
    ) -> dict[tuple[str, ...], tuple[tuple[str, str, str], ...]]:
        assert report.row_relationships is not None
        return {
            key: tuple((item.source.id, item.target.id, item.kind) for item in values)
            for key, values in report.row_relationships.items()
        }

    surface = snapshot.report("surface", "orders", details=True)
    surface_ids = identities(surface)
    assert set(surface_ids) == {
        ("orders/a.py", "orders.a"),
        ("orders/b.py", "orders.b"),
    }
    assert surface_ids[("orders/a.py", "orders.a")] == (
        (nodes["callers.a"].id, nodes["orders.a"].id, "calls"),
    )
    assert surface_ids[("orders/b.py", "orders.b")] == tuple(
        sorted(
            (
                (nodes["callers.b"].id, nodes["orders.b"].id, "references"),
                (nodes["callers.c"].id, nodes["orders.b"].id, "calls"),
                (pathless.id, nodes["orders.b"].id, "calls"),
            )
        )
    )

    consumers = snapshot.report("consumers", "orders", details=True)
    consumer_ids = identities(consumers)
    assert consumer_ids[("callers/b.py",)] == tuple(
        sorted(
            (
                (nodes["callers.b"].id, nodes["orders.b"].id, "imports"),
                (nodes["callers.b"].id, nodes["orders.b"].id, "references"),
            )
        )
    )
    assert all(pathless.id not in edge for edges in consumer_ids.values() for edge in edges)


def test_row_contributor_outputs_are_independent_of_input_order() -> None:
    document, systems = _row_reporting_fixture()
    permuted = dataclasses.replace(
        document,
        nodes=tuple(reversed(document.nodes)),
        relationships=tuple(reversed(document.relationships)),
    )
    first = system_query.ReportingSnapshot.prepare(document, systems)
    second = system_query.ReportingSnapshot.prepare(permuted, systems)

    def fingerprint(report: system_query.SystemReport[object]) -> tuple[object, object]:
        assert report.row_relationships is not None
        row_values = tuple(
            (
                key,
                tuple((item.source.id, item.target.id, item.kind) for item in values),
            )
            for key, values in report.row_relationships.items()
        )
        return report.to_dict(), row_values

    for query in ("surface", "consumers", "system-deps"):
        assert fingerprint(first.report(query, "orders", details=True)) == fingerprint(
            second.report(query, "orders", details=True)
        )


def test_row_relationships_are_copied_immutable_and_constructor_validated() -> None:
    document, systems = _row_reporting_fixture()
    snapshot = system_query.ReportingSnapshot.prepare(document, systems)
    detailed = snapshot.report("surface", "orders", details=True)
    assert detailed.row_relationships is not None
    supplied = dict(detailed.row_relationships)
    rebuilt = system_query.SystemReport(
        query=detailed.query,
        system_name=detailed.system_name,
        results=detailed.results,
        coverage=detailed.coverage,
        relationships=detailed.relationships,
        row_relationships=supplied,
    )
    supplied.clear()
    assert list(rebuilt.row_relationships or {}) == list(detailed.row_relationships)
    assert all(isinstance(values, tuple) for values in rebuilt.row_relationships.values())
    with pytest.raises(TypeError):
        detailed.row_relationships[("orders/a.py", "orders.a")] = ()  # type: ignore[index]

    with pytest.raises(ValueError, match="requires detailed"):
        system_query.SystemReport(
            query="surface",
            system_name="orders",
            results=tuple(detailed.results),
            coverage=detailed.coverage,
            row_relationships={},
        )
    with pytest.raises(ValueError, match="unexpected row key"):
        system_query.SystemReport(
            query="surface",
            system_name="orders",
            results=detailed.results,
            coverage=detailed.coverage,
            relationships=detailed.relationships,
            row_relationships={("wrong.py", "wrong"): ()},
        )
    with pytest.raises(ValueError, match="cover every report result"):
        system_query.SystemReport(
            query="surface",
            system_name="orders",
            results=detailed.results,
            coverage=detailed.coverage,
            relationships=detailed.relationships,
            row_relationships={next(iter(detailed.row_relationships)): ()},
        )
    with pytest.raises(ValueError, match="keys must be tuples"):
        system_query.SystemReport(
            query="surface",
            system_name="orders",
            results=detailed.results,
            coverage=detailed.coverage,
            relationships=detailed.relationships,
            row_relationships={"orders/a.py": ()},  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="RelationshipDetail tuples"):
        system_query.SystemReport(
            query="surface",
            system_name="orders",
            results=detailed.results,
            coverage=detailed.coverage,
            relationships=detailed.relationships,
            row_relationships={
                key: (object(),) if key == next(iter(detailed.row_relationships)) else values
                for key, values in detailed.row_relationships.items()
            },
        )
    with pytest.raises(ValueError, match="partition"):
        system_query.SystemReport(
            query="surface",
            system_name="orders",
            results=detailed.results,
            coverage=detailed.coverage,
            relationships=detailed.relationships,
            row_relationships={
                key: detailed.row_relationships[next(iter(detailed.row_relationships))]
                if key != next(iter(detailed.row_relationships))
                else detailed.row_relationships[key]
                for key in detailed.row_relationships
            },
        )
    with pytest.raises(ValueError, match="partition"):
        system_query.SystemReport(
            query="surface",
            system_name="orders",
            results=detailed.results,
            coverage=detailed.coverage,
            relationships=detailed.relationships[:-1],
            row_relationships=detailed.row_relationships,
        )


def test_report_selection_and_detail_projection_are_shared_and_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, systems = _row_reporting_fixture()
    snapshot = system_query.ReportingSnapshot.prepare(document, systems)
    target = resolve_system(systems, "orders")
    sentinel = system_query.SurfaceRecord(
        category="system: orders", kinds=("calls",), path="sentinel.py", symbol="sentinel"
    )
    real_selector = system_query._select_report
    selection = system_query._ReportSelection(
        records_by_key={("sentinel.py", "sentinel"): sentinel},
        relationships_by_key={("sentinel.py", "sentinel"): ()},
    )
    monkeypatch.setattr(system_query, "_select_report", lambda *_args: selection)
    assert system_query.surface(systems, snapshot.index, target) == (sentinel,)
    assert snapshot.report("surface", "orders").results == (sentinel,)
    monkeypatch.setattr(system_query, "_select_report", real_selector)

    real_detail = system_query._relationship_detail

    def distinctive_detail(
        document: GraphDocument,
        relationship: Relationship,
        source_node: Node,
        target_node: Node,
    ) -> system_query.RelationshipDetail:
        detail = real_detail(document, relationship, source_node, target_node)
        source = dataclasses.replace(detail.source, label="canonical-owner")
        return dataclasses.replace(detail, source=source)

    monkeypatch.setattr(system_query, "_relationship_detail", distinctive_detail)
    detailed = snapshot.report("surface", "orders", details=True)
    assert detailed.row_relationships is not None
    assert any(
        item.source.label == "canonical-owner"
        for values in detailed.row_relationships.values()
        for item in values
    )

    def fail_detail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("compact reports must not project relationship details")

    monkeypatch.setattr(system_query, "_relationship_detail", fail_detail)
    assert snapshot.report("surface", "orders").relationships is None
    with pytest.raises(AssertionError, match="compact reports"):
        snapshot.report("surface", "orders", details=True)


def _projection_file_with_namespace(path: str, namespace: str) -> Node:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, namespace)
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.FILE.value, path=path),
        identity=identity,
        node_class=NodeClass.FILE,
        label=path,
        path=path,
    )


def _projection_upstream(label: str) -> Node:
    identity = NodeIdentity(
        IdentityBasis.UPSTREAM_IDENTIFIER, "external", upstream_identifier=label
    )
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.SYMBOL.value, symbol_kind="function"),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind="function",
    )


def test_reporting_snapshot_all_systems_preserves_file_universes_and_detaches_json() -> None:
    named_a = _projection_symbol("a.entry", "a.py", 0)
    named_b = _projection_symbol("b.entry", "b.py", 0)
    unassigned_symbol = _projection_symbol("loose.entry", "loose.py", 0)
    unassigned_file = _projection_file("loose.py")
    duplicate_one = _projection_file_with_namespace("shared.py", "one")
    duplicate_two = _projection_file_with_namespace("shared.py", "two")
    location = Location("located.py", Range(Position(0, 0), Position(0, 1)))
    located_identity = NodeIdentity(IdentityBasis.FILE_PATH, "located")
    located_file = Node(
        id=compute_node_id(
            located_identity,
            node_class=NodeClass.FILE.value,
            path="wrong.py",
        ),
        identity=located_identity,
        node_class=NodeClass.FILE,
        label="wrong.py",
        path="wrong.py",
        location=location,
    )
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(
            named_a,
            named_b,
            unassigned_symbol,
            unassigned_file,
            duplicate_one,
            duplicate_two,
            located_file,
        ),
        extensions={"minotaur": {"selection": ["."]}},
    )
    snapshot = system_query.ReportingSnapshot.prepare(
        document,
        (System("b", ("b.py",)), System("a", ("a.py", "absent.py"))),
    )

    report = snapshot.all_systems_report(details=True)
    assert isinstance(report, system_query.SystemsReport)
    payload = report.to_dict()
    assert [item["name"] for item in payload["results"]] == ["a", "b"]
    assert payload["results"][0]["declared_files"] == {
        "absent": 1,
        "paths": ["a.py", "absent.py"],
        "represented": 1,
        "scope": "declared_system_files",
        "total": 2,
    }
    assert payload["coverage"] == {
        "declared_files": {
            "absent": 1,
            "represented": 2,
            "scope": "all_declared_system_files",
            "total": 3,
        },
        "graph_files": {"count": 4, "scope": "final_graph_file_nodes"},
        "recorded_unresolved_references": {
            "count": 0,
            "scope": "all_declared_system_files",
        },
        "selection": {"status": "recorded", "targets": ["."]},
        "source_diagnostics": {"status": "unavailable"},
        "unassigned_files": {
            "count": 3,
            "paths": ["located.py", "loose.py", "shared.py"],
            "scope": "final_graph_file_node_derived_paths",
        },
    }
    payload["results"][0]["declared_files"]["total"] = 999
    payload["coverage"]["unassigned_files"]["paths"].append("mutated.py")
    assert report.to_dict()["results"][0]["declared_files"]["total"] == 2
    assert report.to_dict()["coverage"]["unassigned_files"]["paths"] == [
        "located.py",
        "loose.py",
        "shared.py",
    ]
    composed = system_query.compose_system_query(report, system_query.QueryInvocation(True, (), 0))
    assert composed.to_dict()["query"] == "systems"
    assert composed.to_dict()["coverage"]["source_diagnostics"] == {
        "status": "observed_on_refresh",
        "count": 0,
    }
    assert "connections" in composed.to_dict()

    reassigned = system_query.ReportingSnapshot.prepare(
        document,
        (System("b", ("b.py",)), System("a", ("a.py", "absent.py", "loose.py"))),
    )
    reassigned_payload = reassigned.all_systems_report().to_dict()
    assert reassigned_payload["results"][0]["declared_files"] == {
        "absent": 1,
        "represented": 2,
        "scope": "declared_system_files",
        "total": 3,
    }
    assert reassigned_payload["coverage"]["unassigned_files"] == {
        "count": 2,
        "scope": "final_graph_file_node_derived_paths",
    }


def test_reporting_snapshot_all_systems_connections_group_named_boundaries() -> None:
    a = _projection_symbol("a.entry", "a.py", 0)
    b = _projection_symbol("b.entry", "b.py", 0)
    no_system = _projection_symbol("loose.entry", "loose.py", 0)
    external = _projection_upstream("external.entry")
    evidence = Evidence(provenance=Provenance.STATIC_ANALYSIS)

    def edge(source: Node, target: Node, kind: str) -> Relationship:
        return Relationship(source=source.id, target=target.id, kind=kind, evidence=(evidence,))

    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(a, b, no_system, external),
        relationships=(
            edge(a, b, "calls"),
            edge(a, no_system, "references"),
            edge(no_system, a, "imports"),
            edge(a, external, "calls"),
            edge(external, a, "references"),
            edge(a, a, "calls"),
            edge(no_system, no_system, "calls"),
            edge(no_system, external, "calls"),
            edge(external, no_system, "calls"),
            edge(external, external, "calls"),
        ),
    )
    systems = (System("a", ("a.py",)), System("b", ("b.py",)))
    report = system_query.ReportingSnapshot.prepare(document, systems).all_systems_report(
        details=True
    )
    assert report.connections is not None
    assert [
        (row.source_category, row.target_category, row.kinds, len(row.relationships))
        for row in report.connections
    ] == [
        ("external", "system: a", ("references",), 1),
        ("no_system", "system: a", ("imports",), 1),
        ("system: a", "external", ("calls",), 1),
        ("system: a", "no_system", ("references",), 1),
        ("system: a", "system: b", ("calls",), 1),
    ]
    assert all(
        not (row.source_category == "system: a" and row.target_category == "system: a")
        for row in report.connections
    )
    detail = report.connections[0].relationships[0].to_dict()
    assert detail["source"]["id"] == external.id
    assert detail["source"]["path"] == {"status": "unavailable"}
    assert detail["evidence"][0]["sites"] == []

    reassigned = system_query.ReportingSnapshot.prepare(
        document, (System("a", ("a.py", "loose.py")), System("b", ("b.py",)))
    ).all_systems_report(details=True)
    assert reassigned.connections is not None
    assert [(row.source_category, row.target_category) for row in reassigned.connections] == [
        ("external", "system: a"),
        ("system: a", "external"),
        ("system: a", "system: b"),
    ]


def test_reporting_snapshot_all_systems_keeps_zero_inventory_and_empty_connections() -> None:
    document = GraphDocument(coordinate_encoding=CoordinateEncoding.UTF_8)
    report = system_query.ReportingSnapshot.prepare(
        document, (System("empty", ("empty.py",)),)
    ).all_systems_report(details=True)

    assert report.to_dict() == {
        "query": "systems",
        "results": [
            {
                "name": "empty",
                "declared_files": {
                    "absent": 1,
                    "paths": ["empty.py"],
                    "represented": 0,
                    "scope": "declared_system_files",
                    "total": 1,
                },
            }
        ],
        "coverage": {
            "declared_files": {
                "absent": 1,
                "represented": 0,
                "scope": "all_declared_system_files",
                "total": 1,
            },
            "graph_files": {"count": 0, "scope": "final_graph_file_nodes"},
            "recorded_unresolved_references": {
                "count": 0,
                "scope": "all_declared_system_files",
            },
            "selection": {"status": "unavailable"},
            "source_diagnostics": {"status": "unavailable"},
            "unassigned_files": {
                "count": 0,
                "paths": [],
                "scope": "final_graph_file_node_derived_paths",
            },
        },
        "connections": [],
    }


def test_reporting_snapshot_connections_preserve_sites_and_group_kinds() -> None:
    source = _projection_symbol("a.entry", "a.py", 0)
    target = _projection_symbol("loose.entry", "loose.py", 0)
    first_site = Location("calls.py", Range(Position(1, 2), Position(1, 5)))
    second_site = Location("calls.py", Range(Position(0, 4), Position(0, 7)))
    evidence = Evidence(
        provenance=Provenance.STATIC_ANALYSIS,
        producer=Producer("analyzer", "1"),
        locations=(first_site, second_site),
        extensions={"trace": {"source": "fixture"}},
    )
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(source, target),
        relationships=(
            Relationship(
                source=source.id,
                target=target.id,
                kind="references",
                evidence=(evidence,),
                extensions={"edge": {"confidence": 1}},
            ),
            Relationship(
                source=source.id,
                target=target.id,
                kind="calls",
                evidence=(evidence,),
            ),
        ),
    )

    report = system_query.ReportingSnapshot.prepare(
        document, (System("a", ("a.py",)),)
    ).all_systems_report(details=True)
    assert report.connections is not None
    assert len(report.connections) == 1
    connection = report.connections[0]
    assert (connection.source_category, connection.target_category) == (
        "system: a",
        "no_system",
    )
    assert connection.kinds == ("calls", "references")
    assert [item.kind for item in connection.relationships] == ["calls", "references"]

    calls, references = connection.relationships
    assert calls.evidence[0].to_dict()["sites"] == [
        {
            "coordinate_encoding": "utf-8",
            "path": "calls.py",
            "range": {
                "end": {"column": 8, "line": 1},
                "start": {"column": 5, "line": 1},
                "end_exclusive": True,
            },
        },
        {
            "coordinate_encoding": "utf-8",
            "path": "calls.py",
            "range": {
                "end": {"column": 6, "line": 2},
                "start": {"column": 3, "line": 2},
                "end_exclusive": True,
            },
        },
    ]
    assert references.relationship_extensions == {
        "status": "recorded",
        "value": {"edge": {"confidence": 1}},
    }
    assert calls.evidence[0].evidence_extensions == {
        "status": "recorded",
        "value": {"trace": {"source": "fixture"}},
    }
    detached = report.to_dict()
    detached["connections"][0]["relationships"][0]["evidence"][0]["sites"].clear()
    assert len(report.connections[0].relationships[0].evidence[0].sites) == 2


def test_reporting_snapshot_reassignment_updates_inventory_and_connections_together() -> None:
    source = _projection_symbol("a.entry", "a.py", 0)
    loose = _projection_symbol("loose.entry", "loose.py", 0)
    loose_file = _projection_file("loose.py")
    relationship = Relationship(
        source=source.id,
        target=loose.id,
        kind="calls",
        evidence=(Evidence(provenance=Provenance.STATIC_ANALYSIS),),
    )
    document = GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=(source, loose, loose_file),
        relationships=(relationship,),
    )

    before = system_query.ReportingSnapshot.prepare(
        document, (System("a", ("a.py",)),)
    ).all_systems_report(details=True)
    assert before.to_dict()["results"][0]["declared_files"] == {
        "absent": 0,
        "paths": ["a.py"],
        "represented": 1,
        "scope": "declared_system_files",
        "total": 1,
    }
    assert before.to_dict()["coverage"]["unassigned_files"] == {
        "count": 1,
        "paths": ["loose.py"],
        "scope": "final_graph_file_node_derived_paths",
    }
    assert [(row.source_category, row.target_category) for row in before.connections or ()] == [
        ("system: a", "no_system")
    ]

    after = system_query.ReportingSnapshot.prepare(
        document, (System("a", ("a.py", "loose.py")),)
    ).all_systems_report(details=True)
    assert after.to_dict()["results"][0]["declared_files"] == {
        "absent": 0,
        "paths": ["a.py", "loose.py"],
        "represented": 2,
        "scope": "declared_system_files",
        "total": 2,
    }
    assert after.to_dict()["coverage"]["unassigned_files"] == {
        "count": 0,
        "paths": [],
        "scope": "final_graph_file_node_derived_paths",
    }
    assert after.connections == ()
