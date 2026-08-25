"""Behavioral coverage for unreferenced-symbol queries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minotaur import cli


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _analyze(root: Path, output: Path) -> int:
    return cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])


def test_unreferenced_excludes_called_callback_test_and_dunder_names(
    tmp_path: Path, capsys: object
) -> None:
    _write(
        tmp_path,
        "fixture.py",
        "def called():\n"
        "    pass\n\n"
        "def callback_only():\n"
        "    pass\n\n"
        "def test_fixture():\n"
        "    pass\n\n"
        "def __repr__():\n"
        "    pass\n\n"
        "def orphan():\n"
        "    pass\n\n"
        "def register(callback):\n"
        "    pass\n\n"
        "def use():\n"
        "    called()\n"
        "    register(callback_only)\n",
    )
    _write(
        tmp_path,
        "caller.py",
        "from fixture import called, use\ncalled()\nuse()\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "unreferenced",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert status == 0
    assert captured.out == "fixture.py:13  fixture.orphan  function\n"
    assert "callback_only" not in captured.out
    assert "called" not in captured.out
    assert "test_fixture" not in captured.out
    assert "__repr__" not in captured.out


def test_unreferenced_text_fallback_tags_string_mentions_and_supports_paths_and_excludes(
    tmp_path: Path, capsys: object
) -> None:
    _write(
        tmp_path,
        "one.py",
        "def orphan():\n    pass\n\nmessage = 'orphan'\n\ndef excluded():\n    pass\n",
    )
    _write(tmp_path, "two.py", "def other_orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "unreferenced",
            "one.py",
            "--exclude",
            "excluded",
            "--text-fallback",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert status == 0
    assert captured.out == "one.py:1  one.orphan  function [text-mention]\n"
    assert "other_orphan" not in captured.out
    assert "excluded" not in captured.out

    exclusions = tmp_path / "exclude.json"
    exclusions.write_text(json.dumps({"fixture": ["other_orphan"]}), encoding="utf-8")
    status = cli.main(
        [
            "query",
            "unreferenced",
            "--exclude-file",
            str(exclusions),
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert [result["symbol"] for result in payload["results"]] == [
        "one.orphan",
        "one.excluded",
    ]


def test_unreferenced_no_refresh_uses_deleted_graph_path_without_text_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "deleted.py"
    _write(
        tmp_path,
        "deleted.py",
        "def orphan():\n    pass\n\nmessage = 'orphan'\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0
    before = graph.read_bytes()
    source.unlink()

    status = cli.main(
        [
            "query",
            "unreferenced",
            "deleted.py",
            "--text-fallback",
            "--no-refresh",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "deleted.py:1  deleted.orphan  function\n"
    assert "text-mention" not in captured.out
    assert "minotaur: stale: deleted.py" in captured.err
    assert graph.read_bytes() == before


def test_unreferenced_no_refresh_uses_graph_path_when_source_is_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "unreadable.py"
    _write(tmp_path, "unreadable.py", "def orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0
    source.unlink()
    source.mkdir()

    status = cli.main(
        [
            "query",
            "unreferenced",
            "unreadable.py",
            "--text-fallback",
            "--no-refresh",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "unreadable.py:1  unreadable.orphan  function\n"
    assert "minotaur: stale: unreadable.py" in captured.err


def test_unreferenced_no_refresh_root_path_filters_saved_graph_without_filesystem_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "present.py"
    _write(tmp_path, "present.py", "def orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0
    source.unlink()

    status = cli.main(
        [
            "query",
            "unreferenced",
            ".",
            "--no-refresh",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "present.py:1  present.orphan  function\n"
    assert "minotaur: stale: present.py" in captured.err


def test_unreferenced_clean_graph_still_validates_missing_query_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "present.py", "def orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "unreferenced",
            "missing.py",
            "--no-refresh",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "query path does not exist: missing.py" in captured.err


def test_unreferenced_stale_graph_still_rejects_query_path_escape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "present.py"
    _write(tmp_path, "present.py", "def orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0
    source.unlink()

    status = cli.main(
        [
            "query",
            "unreferenced",
            "../outside.py",
            "--no-refresh",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "query path escapes root: ../outside.py" in captured.err


def test_unreferenced_excludes_symbols_used_only_in_a_class_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A class body executes at definition time, so a factory or callable used
    # only there is live code; before class bodies were visited both targets
    # below were reported as dead.
    _write(
        tmp_path,
        "config.py",
        "from dataclasses import dataclass, field\n\n"
        "def make_config():\n    return {}\n\n"
        "def helper():\n    return 1\n\n"
        "def orphan():\n    pass\n\n"
        "@dataclass\n"
        "class Cfg:\n"
        "    data: dict = field(default_factory=make_config)\n"
        "    handler = staticmethod(helper)\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(["query", "unreferenced", "--graph", str(graph), "--root", str(tmp_path)])
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "config.py:9  config.orphan  function\n"


def test_unreferenced_excludes_symbols_used_only_in_a_signature(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A default argument and an annotation are dependencies an agent must see
    # before deleting the symbol; before signatures were traversed, both
    # `default_cb` and `Handler` were reported as dead code.
    _write(
        tmp_path,
        "app.py",
        "def default_cb():\n    return 0\n\n"
        "class Handler:\n    pass\n\n"
        "def orphan():\n    pass\n\n"
        "def top(cb=default_cb, handler: Handler = None):\n"
        "    return cb, handler\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(["query", "unreferenced", "--graph", str(graph), "--root", str(tmp_path)])
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "app.py:7  app.orphan  function\napp.py:10  app.top  function\n"


def test_unreferenced_counts_module_scope_call_and_callback_registration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Module-scope statements are attributed to the module node, which is also
    # the ``contains`` container of every top-level function. While that
    # container was treated as the symbol's own definition, an assignment from
    # a call, a callback handed to a registrar, and a bare alias were all
    # discarded and these three live functions were reported dead.
    _write(
        tmp_path,
        "wiring.py",
        "def create_app():\n    return 1\n\n"
        "def cleanup():\n    pass\n\n"
        "def handler():\n    pass\n\n"
        "def orphan():\n    pass\n\n"
        "def register(callback):\n    pass\n\n"
        "app = create_app()\n"
        "register(cleanup)\n"
        "HOOKS = {'on_event': handler}\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(["query", "unreferenced", "--graph", str(graph), "--root", str(tmp_path)])
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "wiring.py:10  wiring.orphan  function\n"


def test_unreferenced_counts_a_call_from_a_sibling_method(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A method reached only from a sibling method of the same class must not be
    # reported: the call is attributed to the sibling, and no class-container
    # exclusion may swallow it.
    _write(
        tmp_path,
        "service.py",
        "class Service:\n"
        "    def outer(self):\n"
        "        return self.inner()\n\n"
        "    def inner(self):\n"
        "        return 1\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(["query", "unreferenced", "--graph", str(graph), "--root", str(tmp_path)])
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == (
        "service.py:1  service.Service  class\nservice.py:2  service.Service.outer  method\n"
    )


def test_unreferenced_text_fallback_ignores_same_name_definitions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Each definition of a name puts that name in the text once, so two
    # unreferenced methods named `render` used to vouch for each other and both
    # were tagged `[text-mention]`, hiding them from a hygiene pass.
    _write(
        tmp_path,
        "views.py",
        "class A:\n    def render(self):\n        return 1\n\n"
        "class B:\n    def render(self):\n        return 2\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "unreferenced",
            "--text-fallback",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == (
        "views.py:1  views.A  class\n"
        "views.py:2  views.A.render  method\n"
        "views.py:5  views.B  class\n"
        "views.py:6  views.B.render  method\n"
    )


def test_unreferenced_text_fallback_tags_every_symbol_sharing_a_mentioned_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fallback is keyed by bare name, not by symbol: one extra occurrence
    # of `render` beyond the two definitions tags both `A.render` and
    # `B.render`, because the text scan cannot say which class it meant.
    _write(
        tmp_path,
        "views.py",
        "class A:\n    def render(self):\n        return 1\n\n"
        "class B:\n    def render(self):\n        return 2\n\n"
        "DISPATCH = 'render'\n",
    )
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    status = cli.main(
        [
            "query",
            "unreferenced",
            "--text-fallback",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == (
        "views.py:1  views.A  class\n"
        "views.py:2  views.A.render  method [text-mention]\n"
        "views.py:5  views.B  class\n"
        "views.py:6  views.B.render  method [text-mention]\n"
    )


def test_unreferenced_exclude_file_reads_json_list_and_line_per_name_formats(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every documented exclude-file shape must suppress the same names.

    A JSON mapping may hold either a single name or a list per key, and the
    line format is the fallback taken when the file is not JSON at all -- so it
    is exercised with a file that fails ``json.loads`` outright rather than
    with a JSON string list.
    """
    _write(tmp_path, "one.py", "def orphan():\n    pass\n\n\ndef other_orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0

    for document in (["orphan"], {"reason": "orphan"}):
        listed = tmp_path / "exclude-list.json"
        listed.write_text(json.dumps(document), encoding="utf-8")
        assert (
            cli.main(
                [
                    "query",
                    "unreferenced",
                    "--exclude-file",
                    str(listed),
                    "--graph",
                    str(graph),
                    "--root",
                    str(tmp_path),
                ]
            )
            == 0
        )
        assert capsys.readouterr().out == "one.py:5  one.other_orphan  function\n"

    lines = tmp_path / "exclude.txt"
    lines.write_text("orphan\n\n  other_orphan  \n", encoding="utf-8")
    assert (
        cli.main(
            [
                "query",
                "unreferenced",
                "--exclude-file",
                str(lines),
                "--graph",
                str(graph),
                "--root",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == "no unreferenced symbols\n"


def test_unreferenced_exclude_file_rejects_a_json_document_that_is_not_a_list_or_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "one.py", "def orphan():\n    pass\n")
    graph = tmp_path / "graph.json"
    assert _analyze(tmp_path, graph) == 0
    scalar = tmp_path / "exclude.json"
    # Valid JSON, so the line fallback does not apply and the shape is an error
    # rather than a file silently read as one long name.
    scalar.write_text("3\n", encoding="utf-8")

    status = cli.main(
        [
            "query",
            "unreferenced",
            "--exclude-file",
            str(scalar),
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "exclude file must contain a JSON list or object of names" in captured.err


def test_unreferenced_exclude_pattern_matches_qualified_labels(
    tmp_path: Path, capsys: object
) -> None:
    _write(
        tmp_path,
        "suite.py",
        "class TestThing:\n    def check(self):\n        return 1\n\n"
        "class Widget:\n    def paintEvent(self, event):\n        return 2\n\n"
        "def orphan():\n    return 3\n",
    )
    graph = tmp_path / "graph.json"
    assert (
        cli.main(
            ["analyze", "--root", str(tmp_path), "--output", str(graph), str(tmp_path / "suite.py")]
        )
        == 0
    )
    capsys.readouterr()  # type: ignore[attr-defined]

    status = cli.main(
        [
            "query",
            "unreferenced",
            "--exclude-pattern",
            r"\.Test\w*(\.|$)",
            "--exclude-pattern",
            r"Event$",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert status == 0
    # The test class, its method, and the Qt-style override are excluded by
    # the caller's patterns; Minotaur itself knows nothing about pytest or Qt.
    # Widget stays: only its override matched, not the class itself.
    assert captured.out == ("suite.py:5  suite.Widget  class\nsuite.py:9  suite.orphan  function\n")

    status = cli.main(
        [
            "query",
            "unreferenced",
            "--exclude-pattern",
            "(unclosed",
            "--graph",
            str(graph),
            "--root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert status == 2
    assert "invalid --exclude-pattern '(unclosed'" in captured.err
