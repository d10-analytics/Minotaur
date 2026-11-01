"""Behavioral proof of safe text output for graph-derived strings.

Hostile characters enter through real fixture repositories analyzed by
``analyze``; every claim is answered through the real ``query`` stdout
(``capsys``), never by calling the helper directly except for the one
parametrized ``escape_text`` semantics test. The committed-graph route is
exercised by hand-edited graphs queried ``--no-refresh``: label edits are
legal because labels carry no identity, and edits that change identity
inputs recompute the node id the way the model itself does.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.query.diff import DiffResult, Relocation, render_text
from minotaur.query.render import dump_json, escape_text

pytestmark = pytest.mark.skipif(os.name != "posix", reason="hostile file names are POSIX only")

#: Characters whose raw stdout appearance the escape rule forbids; ``\n`` is
#: the template line terminator and is allowed between lines.
HOSTILE = "\x1b\x9b\u202e\t\r\x7f\x0b\x0c\u0085"

CSI_FILE = "c1\x9bcsi.py"
ESC_FILE = "esc\x1bx.py"
RTL_FILE = "rtl\u202eevil.py"


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _analyze(root: Path, output: Path) -> None:
    assert cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)]) == 0


def _assert_safe_lines(output: str, expected: list[str]) -> None:
    lines = output.splitlines()
    assert lines == expected
    for line in lines:
        _assert_no_hostile(line)


def _assert_no_hostile(text: str) -> None:
    for character in HOSTILE:
        assert character not in text


def _assert_canonical_json(out: str) -> None:
    assert out == dump_json(json.loads(out))


# ---------------------------------------------------------------------------
# Helper semantics (AC-01)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\nb", "a\\nb"),
        ("\x1b[31m", "\\x1b[31m"),
        ("\x9b", "\\x9b"),
        ("\u202e", "\\u202e"),
        ("\t", "\\t"),
        ("\r", "\\r"),
        ("\x7f", "\\x7f"),
        ("\u2028", "\\u2028"),
        ("café x\\y 'q\"", "café x\\y 'q\""),
        ("plain text", "plain text"),
    ],
)
def test_escape_text_spells_unprintables_and_keeps_printables(raw: str, expected: str) -> None:
    escaped = escape_text(raw)
    assert escaped == expected
    # Escaped output is fully printable, so re-running the helper is a no-op.
    assert escape_text(escaped) == escaped


# ---------------------------------------------------------------------------
# Record queries (AC-02; AC-07 for these inputs)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hostile_repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """An analyzed repository whose analyzed file names carry hostile characters."""
    root = tmp_path_factory.mktemp("hostile_repo")
    source = root / "src"
    _write(source, "nl\nname.py", "def target():\n    return 1\n\n\ndef unused():\n    return 2\n")
    _write(source, ESC_FILE, "def escfn():\n\tx = 1  # \x1b[2J\u202e\n\treturn 3\n")
    _write(source, CSI_FILE, "def csi_caller():\n    return target()\n")
    _write(source, RTL_FILE, "def rtl_unused():\n    return 4\n")
    _write(
        source, "café x.py", "def friendly():\n    return 5\n\n\ndef site():\n    unknown.zzz()\n"
    )
    graph = root / "graph.json"
    _analyze(source, graph)
    return {"root": root, "source": source, "graph": graph}


def _add_calls_edge(raw: dict, source_label: str, target_label: str, path: str) -> None:
    def node_id(label: str) -> object:
        return next(node["id"] for node in raw["nodes"] if node.get("label") == label)

    raw["relationships"].append(
        {
            "kind": "calls",
            "source": node_id(source_label),
            "target": node_id(target_label),
            "evidence": [
                {
                    "provenance": "imported-graph",
                    "producer": {"name": "test-fixture"},
                    "locations": [
                        {
                            "path": path,
                            "range": {
                                "start": {"line": 1, "character": 11},
                                "end": {"line": 1, "character": 17},
                            },
                        }
                    ],
                }
            ],
        }
    )


def _relabel_unresolved(raw: dict, label: str, new_reference: str) -> None:
    """Give an unresolved reference a hostile spelling, recomputing its id."""
    node = next(n for n in raw["nodes"] if n.get("label") == label)
    old_id = node["id"]
    node["reference_text"] = new_reference
    location = Location.from_dict(node["location"])  # type: ignore[arg-type]
    new_id = compute_node_id(
        NodeIdentity.from_dict(node["identity"]),  # type: ignore[arg-type]
        node_class=node["node_class"],
        reference_text=new_reference,
        location=location,
    )
    node["id"] = new_id
    node["label"] = new_reference
    for relationship in raw["relationships"]:
        if relationship["source"] == old_id:
            relationship["source"] = new_id
        if relationship["target"] == old_id:
            relationship["target"] = new_id


@pytest.fixture(scope="module")
def hostile_edited_graph(hostile_repo: dict) -> Path:
    """The same analyzed graph with one added call and one hostile reference label."""
    raw = json.loads(hostile_repo["graph"].read_text(encoding="utf-8"))
    _add_calls_edge(raw, "c1\x9bcsi.csi_caller", "nl\nname.target", CSI_FILE)
    _relabel_unresolved(raw, "unknown.zzz", "esc\x1bx.target")
    edited = hostile_repo["root"] / "graph_edited.json"
    edited.write_text(json.dumps(raw), encoding="utf-8")
    return edited


def test_definitions_and_unreferenced_escape_hostile_paths(
    hostile_repo: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = str(hostile_repo["graph"])
    source = str(hostile_repo["source"])

    assert cli.main(["query", "definitions", "target", "--graph", graph, "--root", source]) == 0
    captured = capsys.readouterr()
    _assert_safe_lines(captured.out, ["nl\\nname.py:1  nl\\nname.target  function"])
    assert captured.err == ""

    assert cli.main(["query", "unreferenced", "--graph", graph, "--root", source]) == 0
    captured = capsys.readouterr()
    _assert_safe_lines(
        captured.out,
        [
            "c1\\x9bcsi.py:1  c1\\x9bcsi.csi_caller  function",
            "café x.py:1  café x.friendly  function",
            "café x.py:5  café x.site  function",
            "esc\\x1bx.py:1  esc\\x1bx.escfn  function",
            "nl\\nname.py:1  nl\\nname.target  function",
            "nl\\nname.py:5  nl\\nname.unused  function",
            "rtl\\u202eevil.py:1  rtl\\u202eevil.rtl_unused  function",
        ],
    )
    assert captured.err == ""


def test_callers_and_impact_escape_hand_edited_labels(
    hostile_repo: dict, hostile_edited_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = [
        "--graph",
        str(hostile_edited_graph),
        "--root",
        str(hostile_repo["source"]),
        "--no-refresh",
    ]

    assert cli.main(["query", "callers", "nl\nname.target", *common]) == 0
    captured = capsys.readouterr()
    _assert_safe_lines(
        captured.out,
        [
            "c1\\x9bcsi.py:2:12  c1\\x9bcsi.csi_caller [calls]",
            "c1\\x9bcsi.py:2:12  target [references] [unresolved]",
            "café x.py:6:5  esc\\x1bx.target [references] [unresolved]",
        ],
    )
    assert captured.err == ""

    assert cli.main(["query", "impact", "nl\nname.target", *common]) == 0
    captured = capsys.readouterr()
    _assert_safe_lines(
        captured.out,
        [
            "depth 0: nl\\nname.target",
            "depth 1: c1\\x9bcsi.csi_caller",
        ],
    )
    assert captured.err == ""


# ---------------------------------------------------------------------------
# Graph diff (AC-03; AC-07 for these inputs)
# ---------------------------------------------------------------------------


def _relocate_symbol(raw: dict, label: str, path: str, line: int) -> None:
    """Move one symbol node to another file, preserving its label.

    A committed graph can carry any schema-valid fact, so the moved label
    keeps ``post.delta`` while its location claims ``c1\x9bcsi.py``; the id
    is recomputed the way the model itself does.
    """
    node = next(n for n in raw["nodes"] if n.get("label") == label)
    old_id = node["id"]
    location = json.loads(json.dumps(node["location"]))
    location["path"] = path
    location["range"]["start"]["line"] = line
    location["range"]["end"]["line"] = line
    new_id = compute_node_id(
        NodeIdentity.from_dict(node["identity"]),  # type: ignore[arg-type]
        node_class=node["node_class"],
        symbol_kind=node.get("symbol_kind"),
        path=path,
        location=Location.from_dict(location),
    )
    node["id"] = new_id
    node["location"] = location
    for relationship in raw["relationships"]:
        if relationship["source"] == old_id:
            relationship["source"] = new_id
        if relationship["target"] == old_id:
            relationship["target"] = new_id


@pytest.fixture(scope="module")
def diff_graphs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Two hostile graphs: NEW adds symbols and a calls relationship between
    hostile module-derived labels, and OLD carries a calls relationship that
    NEW removes; NEW relocates one symbol inside its file and OLD relocates
    another across files."""
    root = tmp_path_factory.mktemp("diff_repo")
    old_root, new_root = root / "old", root / "new"
    _write(old_root, "one.py", "def alpha():\n    return 1\n\n\ndef beta():\n    return alpha()\n")
    _write(
        old_root,
        ESC_FILE,
        "def first():\n    return 1\n\n\ndef removed_helper():\n    return first()\n",
    )
    _write(old_root, "post.py", "def delta():\n    return 1\n")
    _write(old_root, "two.py", "def gamma():\n    return 1\n")
    _write(
        new_root,
        "one.py",
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 42\n"
        "\n\ndef extra_caller():\n    return alpha()\n",
    )
    _write(new_root, ESC_FILE, "# shifted\ndef first():\n    return 1\n")
    _write(new_root, "post.py", "def delta():\n    return 1\n")
    _write(new_root, CSI_FILE, "x = 1\n")
    _write(new_root, "two.py", "x = 1\n")
    _write(
        new_root,
        RTL_FILE,
        "def added_evil():\n    return 1\n\n\ndef outer():\n    return helper2()\n"
        "\n\ndef helper2():\n    return 1\n",
    )
    old_graph, new_graph = root / "old.json", root / "new.json"
    _analyze(old_root, old_graph)
    _analyze(new_root, new_graph)
    raw = json.loads(old_graph.read_text(encoding="utf-8"))
    _relocate_symbol(raw, "post.delta", CSI_FILE, 0)
    old_graph.write_text(json.dumps(raw), encoding="utf-8")
    return old_graph, new_graph


def test_diff_escapes_hostile_graph_changes(
    diff_graphs: tuple, capsys: pytest.CaptureFixture[str]
) -> None:
    old_graph, new_graph = diff_graphs
    assert cli.main(["query", "diff", str(old_graph), str(new_graph)]) == 1
    captured = capsys.readouterr()
    _assert_safe_lines(
        captured.out,
        [
            "+ one.extra_caller",
            "+ rtl\\u202eevil.added_evil",
            "+ rtl\\u202eevil.helper2",
            "+ rtl\\u202eevil.outer",
            "- esc\\x1bx.removed_helper",
            "- two.gamma",
            "~ esc\\x1bx.first (relocated esc\\x1bx.py:1→2)",
            "~ post.delta (relocated c1\\x9bcsi.py:1→post.py:1)",
            "+ calls one.extra_caller → one.alpha",
            "+ calls rtl\\u202eevil.outer → rtl\\u202eevil.helper2",
            "- calls esc\\x1bx.removed_helper → esc\\x1bx.first",
            "- calls one.beta → one.alpha",
        ],
    )
    assert captured.err == ""


# ---------------------------------------------------------------------------
# Context query (AC-04; AC-07 for these inputs)
# ---------------------------------------------------------------------------


def test_context_keeps_real_tab_and_escapes_header_and_source(
    hostile_repo: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = str(hostile_repo["graph"])
    source = str(hostile_repo["source"])
    assert (
        cli.main(
            ["query", "context", "--site", "esc\x1bx.py:2", "--graph", graph, "--root", source]
        )
        == 0
    )
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 4
    assert lines[0] == "esc\\x1bx.py:1-3"
    assert lines[1] == "  1: def escfn():"
    assert lines[2] == "> 2: \tx = 1  # \\x1b[2J\\u202e"
    assert lines[3] == "  3: \treturn 3"
    # The tab stays a real tab; ESC and U+202E never reach stdout raw.
    assert "\t" in lines[2]
    assert "\t" in lines[3]
    assert "\x1b" not in captured.out
    assert "\u202e" not in captured.out
    assert captured.err == ""


# ---------------------------------------------------------------------------
# System queries (AC-05)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def systems_repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """A repository whose declared system name and consumer file are hostile."""
    root = tmp_path_factory.mktemp("systems_repo")
    app = root / "app"
    _write(app, "__init__.py", "")
    _write(app, "api.py", "def offered():\n    return 1\n")
    _write(
        app,
        CSI_FILE,
        "from consumer import external\n\ndef inside():\n    return external()\n",
    )
    _write(
        root,
        "consumer.py",
        "def external():\n"
        "    return 1\n\nfrom app.api import offered\n\ndef consume():\n    return offered()\n",
    )
    _write(
        root,
        RTL_FILE,
        "from app.api import offered\n\ndef evilconsume():\n    return offered()\n",
    )
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py", "rtl\\u202eevil.py"]\n',
    )
    zone = root / "docs" / "systems" / "zone"
    zone.mkdir(parents=True)
    (zone / "system.toml").write_text(
        'schema_version = 1\nname = "zone\u202e"\nfiles = ["app/c1\u009bcsi.py", "app/api.py"]\n',
        encoding="utf-8",
    )
    plain = root / "docs" / "systems" / "plain"
    plain.mkdir(parents=True)
    (plain / "system.toml").write_text(
        'schema_version = 1\nname = "plain"\nfiles = ["consumer.py"]\n', encoding="utf-8"
    )
    graph = root / "graph.json"
    _analyze(root, graph)
    return {"root": root, "graph": graph, "edited": root / "graph_edited.json"}


def _relabel_node(raw: dict, label: str, new_label: str) -> None:
    """Rename one symbol node's label; labels are not identity-bearing."""
    for node in raw["nodes"]:
        if node.get("label") == label:
            node["label"] = new_label
            return
    raise AssertionError(f"no node labelled {label!r}")


@pytest.fixture(scope="module")
def systems_edited_graph(systems_repo: dict) -> dict:
    """The systems graph, hand-edited along the committed-graph route: the
    in-scope offered symbol carries a hostile label, and its location claims
    the hostile-named zone member file ``app/c1\x9bcsi.py`` (the module stem is
    not a legal identifier, so no importer could reach it from source text;
    the committed graph still carries the fact), which is how the outside
    consumers and the plain system reach it."""
    raw = json.loads((systems_repo["root"] / "graph.json").read_text(encoding="utf-8"))
    _relabel_node(raw, "app.api.offered", "app.api.\u202eviolet")
    _relocate_symbol(raw, "app.api.\u202eviolet", "app/" + CSI_FILE, 0)
    systems_repo["edited"].write_text(json.dumps(raw), encoding="utf-8")
    return systems_repo


def _systems_run(
    systems_repo: dict, capsys: pytest.CaptureFixture[str], args: list, *, edited: bool
) -> str:
    """Answer one system query from the configured repository, via the real CLI."""
    root = systems_repo["root"]
    previous = os.getcwd()
    os.chdir(root)
    try:
        complete = list(args)
        if edited:
            complete += ["--graph", str(systems_repo["edited"]), "--no-refresh"]
        assert cli.main(complete) == 0
    finally:
        os.chdir(previous)
    return capsys.readouterr().out


def test_system_queries_escape_hostile_system_names_and_files(
    systems_repo: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    surface = _systems_run(systems_repo, capsys, ["query", "surface", "zone\u202e"], edited=False)
    _assert_safe_lines(surface, [surface.splitlines()[0], "app/api.py  app.api.offered  calls"])

    consumers = _systems_run(
        systems_repo, capsys, ["query", "consumers", "zone\u202e"], edited=False
    )
    _assert_safe_lines(
        consumers,
        [
            consumers.splitlines()[0],
            "consumer.py (system: plain)  calls: app.api.offered (app/api.py); "
            "imports: app.api.offered (app/api.py)",
            "rtl\\u202eevil.py (no_system)  calls: app.api.offered (app/api.py); "
            "imports: app.api.offered (app/api.py)",
        ],
    )

    deps_zone = _systems_run(
        systems_repo, capsys, ["query", "system-deps", "zone\u202e"], edited=False
    )
    _assert_safe_lines(
        deps_zone,
        [
            deps_zone.splitlines()[0],
            "system: plain  calls: consumer.external (consumer.py); "
            "imports: consumer.external (consumer.py)",
        ],
    )

    deps_plain = _systems_run(systems_repo, capsys, ["query", "system-deps", "plain"], edited=False)
    _assert_safe_lines(
        deps_plain,
        [
            deps_plain.splitlines()[0],
            "system: zone\\u202e  calls: app.api.offered (app/api.py); "
            "imports: app.api.offered (app/api.py)",
        ],
    )


def test_systems_overview_escapes_hostile_system_name(
    systems_repo: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _systems_run(systems_repo, capsys, ["query", "systems"], edited=False)
    _assert_safe_lines(
        out,
        [
            "coverage " + out.splitlines()[0][len("coverage ") :],
            "plain  declared 1  represented 1  absent 0",
            "zone\\u202e  declared 2  represented 2  absent 0",
        ],
    )


def test_system_queries_escape_hand_edited_labels(
    systems_repo: dict, systems_edited_graph: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    del systems_edited_graph
    surface = _systems_run(systems_repo, capsys, ["query", "surface", "zone\u202e"], edited=True)
    _assert_safe_lines(
        surface,
        [surface.splitlines()[0], "app/c1\\x9bcsi.py  app.api.\\u202eviolet  calls"],
    )

    consumers = _systems_run(
        systems_repo, capsys, ["query", "consumers", "zone\u202e"], edited=True
    )
    _assert_safe_lines(
        consumers,
        [
            consumers.splitlines()[0],
            "consumer.py (system: plain)  calls: app.api.\\u202eviolet (app/c1\\x9bcsi.py); "
            "imports: app.api.\\u202eviolet (app/c1\\x9bcsi.py)",
            "rtl\\u202eevil.py (no_system)  calls: app.api.\\u202eviolet (app/c1\\x9bcsi.py); "
            "imports: app.api.\\u202eviolet (app/c1\\x9bcsi.py)",
        ],
    )

    deps_plain = _systems_run(systems_repo, capsys, ["query", "system-deps", "plain"], edited=True)
    _assert_safe_lines(
        deps_plain,
        [
            deps_plain.splitlines()[0],
            "system: zone\\u202e  calls: app.api.\\u202eviolet (app/c1\\x9bcsi.py); "
            "imports: app.api.\\u202eviolet (app/c1\\x9bcsi.py)",
        ],
    )


# ---------------------------------------------------------------------------
# JSON paths stay raw and canonical (AC-07)
# ---------------------------------------------------------------------------


def _json_run(capsys: pytest.CaptureFixture[str], args: list, *, status: int = 0) -> tuple:
    assert cli.main([*args, "--json"]) == status
    out = capsys.readouterr().out
    return json.loads(out), out


def test_record_json_keeps_raw_hostile_strings(
    hostile_repo: dict, hostile_edited_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = str(hostile_repo["graph"])
    source = str(hostile_repo["source"])
    common = ["--graph", str(hostile_edited_graph), "--root", source, "--no-refresh"]

    payload, out = _json_run(
        capsys, ["query", "definitions", "target", "--graph", graph, "--root", source]
    )
    record = payload["results"][0]
    assert record["path"] == "nl\nname.py" and record["symbol"] == "nl\nname.target"
    _assert_canonical_json(out)

    payload, out = _json_run(capsys, ["query", "callers", "nl\nname.target", *common])
    resolved = payload["results"][0]
    assert resolved["caller"] == "c1\x9bcsi.csi_caller" and resolved["path"] == CSI_FILE
    unresolved = payload["results"][2]
    assert unresolved["unresolved"] is True
    assert unresolved["reference"] == "esc\x1bx.target"
    _assert_canonical_json(out)

    payload, out = _json_run(capsys, ["query", "impact", "nl\nname.target", *common])
    assert payload["results"][0]["symbol"] == "nl\nname.target"
    _assert_canonical_json(out)

    payload, out = _json_run(
        capsys,
        ["query", "context", "--site", "esc\x1bx.py:2", "--graph", graph, "--root", source],
    )
    record = payload["results"][0]
    assert record["path"] == ESC_FILE
    assert record["lines"][1]["text"] == "\tx = 1  # \x1b[2J\u202e"
    _assert_canonical_json(out)


def test_relocation_text_chooses_the_form_on_raw_paths() -> None:
    """The relocation in-file/cross-file form compares raw paths, not escapes.

    ``escape_text`` is not injective: two distinct raw paths -- one carrying
    real newline characters, one carrying the literal ``\\n`` spelling -- can
    escape to identical text, and output would then present a cross-file move
    as a one-file line-number shift. The v1 location schema rejects every
    backslash in a graph path, and escape_text is injective over the paths it
    admits, so this pair provably cannot exist in any schema-valid committed
    graph; the decision being pinned lives entirely in the relocation render,
    so the colliding to-path is attached to the location value here after
    construction (a value the model itself would never emit) and the line is
    asserted where ``tests/query/test_diff.py`` already pins relocation
    forms.
    """
    old_location = Location("a\nq\nz.py", Range(Position(0, 1), Position(0, 12)))
    new_location = Location("post.py", Range(Position(0, 1), Position(0, 12)))
    object.__setattr__(new_location, "path", "a\\nq\\nz.py")
    result = DiffResult(
        relocated=(
            Relocation(
                kind="function",
                symbol="two.gamma",
                old_location=old_location,
                new_location=new_location,
            ),
        )
    )
    text = render_text(result)
    # Cross-file form: the escaped spelling appears at both the from and the
    # to endpoint; a decision taken on the escaped texts would choose the
    # in-file form and print the path once, as ``…:1→1``.
    assert text == "~ two.gamma (relocated a\\nq\\nz.py:1→a\\nq\\nz.py:1)\n"
    assert text.count("a\\nq\\nz.py") == 2


def test_diff_json_keeps_raw_hostile_strings(
    diff_graphs: tuple, capsys: pytest.CaptureFixture[str]
) -> None:
    old_graph, new_graph = diff_graphs
    payload, out = _json_run(capsys, ["query", "diff", str(old_graph), str(new_graph)], status=1)
    assert {"kind": "function", "symbol": "rtl\u202eevil.added_evil"} in payload["added"]
    assert {"kind": "function", "symbol": "rtl\u202eevil.helper2"} in payload["added"]
    assert {"kind": "function", "symbol": "esc\x1bx.removed_helper"} in payload["removed"]
    same_path = next(item for item in payload["relocated"] if item["symbol"] == "esc\x1bx.first")
    assert same_path["from"] == {"path": ESC_FILE, "line": 1, "column": 1}
    assert same_path["to"] == {"path": ESC_FILE, "line": 2, "column": 1}
    cross_path = next(item for item in payload["relocated"] if item["symbol"] == "post.delta")
    assert cross_path["from"]["path"] == CSI_FILE
    assert cross_path["to"]["path"] == "post.py"
    assert {
        "kind": "calls",
        "source": "rtl\u202eevil.outer",
        "target": "rtl\u202eevil.helper2",
    } in payload["relationships_added"]
    assert {
        "kind": "calls",
        "source": "esc\x1bx.removed_helper",
        "target": "esc\x1bx.first",
    } in payload["relationships_removed"]
    _assert_canonical_json(out)
    _assert_no_hostile(out)


def test_system_json_keeps_raw_hostile_strings(
    systems_repo: dict, systems_edited_graph: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    del systems_edited_graph
    out = _systems_run(
        systems_repo, capsys, ["query", "surface", "zone\u202e", "--json"], edited=False
    )
    payload = json.loads(out)
    record = payload["results"][0]
    assert record["path"] == "app/api.py" and record["symbol"] == "app.api.offered"
    assert record["category"] == "system: zone\u202e"
    _assert_canonical_json(out)

    out = _systems_run(systems_repo, capsys, ["query", "systems", "--json"], edited=False)
    payload = json.loads(out)
    assert [record["name"] for record in payload["results"]] == ["plain", "zone\u202e"]
    _assert_canonical_json(out)

    # The hand-edited label and the relocated location survive as raw strings.
    raw_out = _systems_run(
        systems_repo, capsys, ["query", "surface", "zone\u202e", "--json"], edited=True
    )
    payload = json.loads(raw_out)
    assert payload["results"][0]["symbol"] == "app.api.\u202eviolet"
    assert payload["results"][0]["path"] == "app/" + CSI_FILE
    _assert_canonical_json(raw_out)
