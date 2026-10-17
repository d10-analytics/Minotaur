"""Public ``analyze`` proof that Python decoding honours PEP 263 declarations."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.loading import load_graph_file
from minotaur.graph_model.location import Position
from minotaur.graph_model.node import Node, NodeClass
from minotaur.graph_model.provenance import CoordinateEncoding
from minotaur.language_interpreter.registry import default_registry
from minotaur.source_encoding import decode_python_source, is_python_source_path

_SIBLING = "sibling.py"
_SIBLING_BYTES = b"def helper():\n    return 'caf\xc3\xa9'\n"


def _analyze(
    root: Path, output: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, GraphDocument, list[str]]:
    status = cli.main(["analyze", "--root", str(root), "--output", str(output), str(root)])
    error = capsys.readouterr().err
    return status, load_graph_file(output).document, error.splitlines()


def _root(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    for name, content in files.items():
        (root / name).write_bytes(content)
    return root


def _file_nodes(document: GraphDocument) -> dict[str, Node]:
    return {
        node.path: node
        for node in document.nodes
        if node.node_class is NodeClass.FILE and node.path is not None
    }


def _symbols(document: GraphDocument) -> dict[str, Node]:
    return {
        node.label: node
        for node in document.nodes
        if node.node_class is NodeClass.SYMBOL and node.label is not None
    }


def _call_starts(document: GraphDocument, path: str) -> list[Position]:
    return [
        location.range.start
        for relationship in document.relationships
        if relationship.kind == "calls"
        for evidence in relationship.evidence
        for location in evidence.locations
        if location.path == path
    ]


# ---------------------------------------------------------------------------
# Declared encodings are honoured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declaration", "literal", "character"),
    [
        # "é" is two UTF-8 bytes but one latin-1 byte, so a byte-offset
        # position would be 9.
        ("latin-1", b"\xe9", 10),
        # cp1252 0x80 is "€" (three UTF-8 bytes); a hard-coded latin-1 decode
        # would produce U+0080 (two UTF-8 bytes) and report 10.
        ("cp1252", b"\x80", 11),
    ],
)
def test_declared_encoding_file_is_analyzed_with_utf8_positions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    declaration: str,
    literal: bytes,
    character: int,
) -> None:
    content = (
        f"# -*- coding: {declaration} -*-\ndef f():\n    pass\n".encode("ascii")
        + b'x = "'
        + literal
        + b'"; f()\n'
    )
    with pytest.raises(UnicodeDecodeError):
        content.decode("utf-8")
    root = _root(tmp_path, {_SIBLING: _SIBLING_BYTES, "declared.py": content})

    status, document, errors = _analyze(root, tmp_path / "graph.json", capsys)

    assert status == 0
    assert errors == []
    files = _file_nodes(document)
    assert set(files) == {_SIBLING, "declared.py"}
    extensions = files["declared.py"].extensions
    assert extensions is not None
    assert extensions["minotaur-python"]["content_sha256"] == hashlib.sha256(content).hexdigest()
    symbols = _symbols(document)
    assert symbols["declared"].symbol_kind == "module"
    assert symbols["declared.f"].symbol_kind == "function"
    assert _call_starts(document, "declared.py") == [Position(3, character)]
    assert document.coordinate_encoding is CoordinateEncoding.UTF_8


def test_bom_file_without_declaration_is_analyzed_without_its_bom(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A retained BOM would make the text unparsable, and an ASCII check that
    # encodes with ``utf-8-sig`` would reject the file before parsing.
    content = b"\xef\xbb\xbfdef f():\n    pass\nf()\n"
    root = _root(tmp_path, {_SIBLING: _SIBLING_BYTES, "bom.py": content})

    status, document, errors = _analyze(root, tmp_path / "graph.json", capsys)

    assert status == 0
    assert errors == []
    assert set(_file_nodes(document)) == {_SIBLING, "bom.py"}
    function = _symbols(document)["bom.f"]
    assert function.location is not None
    assert function.location.range.start == Position(0, 0)
    assert _call_starts(document, "bom.py") == [Position(2, 0)]


def test_cr_only_declared_encoding_file_is_analyzed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Read as one LF-terminated line, the declaration and the latin-1 byte
    # share a line and detection reports an invalid declaration.
    content = b'# -*- coding: latin-1 -*-\rdef f():\r    pass\rx = "\xe9"; f()\r'
    root = _root(tmp_path, {_SIBLING: _SIBLING_BYTES, "declared.py": content})

    status, document, errors = _analyze(root, tmp_path / "graph.json", capsys)

    assert status == 0
    assert errors == []
    assert set(_file_nodes(document)) == {_SIBLING, "declared.py"}
    assert _call_starts(document, "declared.py") == [Position(3, 10)]


def test_cr_only_utf8_file_with_later_encoding_text_is_analyzed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Read as one LF-terminated line, ``encoding=enc`` on a later line is
    # mistaken for a declaration of the unknown codec ``enc``.
    content = b"# helper module\rdef read(p, enc):\r    return open(p, encoding=enc).read()\r"
    root = _root(tmp_path, {_SIBLING: _SIBLING_BYTES, "helper.py": content})

    status, document, errors = _analyze(root, tmp_path / "graph.json", capsys)

    assert status == 0
    assert errors == []
    assert set(_file_nodes(document)) == {_SIBLING, "helper.py"}
    assert _symbols(document)["helper.read"].symbol_kind == "function"


@pytest.mark.parametrize(
    ("first", "second", "newline"),
    [
        pytest.param(b"# M\xf3dulo", b"# -*- coding: latin-1 -*-", b"\n", id="comment-line"),
        pytest.param(b"#!/usr/bin/env python \xf3", b"# coding=cp1252", b"\n", id="shebang"),
        pytest.param(b"# M\xf3dulo", b"# -*- coding: latin-1 -*-", b"\r\n", id="crlf"),
        pytest.param(b"# M\xf3dulo", b"# -*- coding: latin-1 -*-", b"\r", id="cr-only"),
        pytest.param(b"", b"# coding: latin-1 \xf3", b"\n", id="declaration-line"),
    ],
)
def test_declaration_beside_non_utf8_bytes_is_honoured(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    first: bytes,
    second: bytes,
    newline: bytes,
) -> None:
    # The first two lines are not UTF-8, but the declaration on one of them
    # is ASCII, and the compiler finds it in the raw bytes.
    content = newline.join([first, second, b"def f():", b"    pass", b'x = "\xe9"; f()', b""])
    with pytest.raises(UnicodeDecodeError):
        (first + second).decode("utf-8")
    compile(content, "declared.py", "exec")
    root = _root(tmp_path, {_SIBLING: _SIBLING_BYTES, "declared.py": content})

    status, document, errors = _analyze(root, tmp_path / "graph.json", capsys)

    assert status == 0
    assert errors == []
    files = _file_nodes(document)
    assert set(files) == {_SIBLING, "declared.py"}
    extensions = files["declared.py"].extensions
    assert extensions is not None
    assert extensions["minotaur-python"]["content_sha256"] == hashlib.sha256(content).hexdigest()
    symbols = _symbols(document)
    assert symbols["declared"].symbol_kind == "module"
    assert symbols["declared.f"].symbol_kind == "function"
    assert _call_starts(document, "declared.py") == [Position(4, 10)]


def test_declaration_beside_non_utf8_bytes_decodes_as_the_compiler_does() -> None:
    content = b"# M\xf3dulo\n# -*- coding: latin-1 -*-\nx = '\xe9'\n"
    namespace: dict[str, object] = {}

    exec(compile(content, "declared.py", "exec"), namespace)

    assert decode_python_source(content) == content.decode("latin-1")
    assert namespace["x"] == "\xe9"


# ---------------------------------------------------------------------------
# Declarations that cannot be honoured are isolated source-read errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param(b"# coding: bogus-codec\nx = 1\n", "unknown encoding", id="unknown-codec"),
        pytest.param(
            b"\xef\xbb\xbf# coding: latin-1\nx = 1\n", "encoding problem", id="bom-conflict"
        ),
        pytest.param(
            b"# coding: caf\xe9\nx = 1\n",
            "invalid or missing encoding declaration",
            id="non-ascii-name",
        ),
        pytest.param(b"# coding: rot13\nx = 1\n", "not a text encoding", id="non-text-codec"),
        pytest.param(b"# coding: utf-16\nx = 12\n", "not ASCII-compatible", id="utf-16"),
        pytest.param(b'# coding: cp1252\nx = "\x81"\n', "can't decode", id="undecodable"),
        pytest.param(b"\xff", "invalid or missing encoding declaration", id="undeclared"),
    ],
)
def test_unhonourable_declaration_is_one_source_read_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes, message: str
) -> None:
    root = _root(tmp_path, {_SIBLING: _SIBLING_BYTES, "broken.py": content})

    status, document, errors = _analyze(root, tmp_path / "graph.json", capsys)

    assert status == 1
    assert set(_file_nodes(document)) == {_SIBLING}
    assert len(errors) == 1
    assert errors[0].startswith("broken.py: source-read-error: ")
    assert errors[0].endswith(" [severity=error]")
    assert message in errors[0]
    assert "parse-error" not in errors[0]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param(
            b"\xef\xbb\xbf# M\xf3dulo\n# coding: latin-1\nx = 1\n",
            "invalid or missing encoding declaration",
            id="bom-conflict",
        ),
        pytest.param(
            b"# M\xf3dulo\n# coding: caf\xe9\nx = 1\n",
            "invalid or missing encoding declaration",
            id="non-ascii-name",
        ),
        pytest.param(
            b"# M\xf3dulo\n# coding: bogus-codec\nx = 1\n",
            "invalid or missing encoding declaration",
            id="unknown-codec",
        ),
        pytest.param(b"# M\xf3dulo\n# coding: rot13\nx = 1\n", "not a text encoding", id="rot13"),
        pytest.param(
            b"# M\xf3dulo\n# coding: utf-16\nx = 12\n", "not ASCII-compatible", id="utf-16"
        ),
        pytest.param(
            b'# M\xf3dulo\n# coding: cp1252\nx = "\x81"\n', "can't decode", id="undecodable"
        ),
        pytest.param(
            b"# M\xf3dulo\n# coding: utf-8\nx = 1\n",
            "invalid or missing encoding declaration",
            id="utf-8-declaration",
        ),
        pytest.param(
            b"# M\xf3dulo\n# plain comment\nx = 1\n",
            "invalid or missing encoding declaration",
            id="undeclared",
        ),
        pytest.param(
            b"x = '\xf3'\n# coding: latin-1\n",
            "invalid or missing encoding declaration",
            id="declaration-after-code",
        ),
    ],
)
def test_non_utf8_first_line_leaves_unhonourable_declarations_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes, message: str
) -> None:
    root = _root(tmp_path, {_SIBLING: _SIBLING_BYTES, "broken.py": content})

    status, document, errors = _analyze(root, tmp_path / "graph.json", capsys)

    assert status == 1
    assert set(_file_nodes(document)) == {_SIBLING}
    assert len(errors) == 1
    assert errors[0].startswith("broken.py: source-read-error: ")
    assert message in errors[0]
    assert "parse-error" not in errors[0]


def test_utf16_declaration_case_needs_the_ascii_compatibility_check() -> None:
    # The utf-16 case decodes without error, so only the ASCII check turns it
    # into a source-read error instead of a parse error.
    content = b"# coding: utf-16\nx = 12\n"

    assert len(content) % 2 == 0
    decoded = content.decode("utf-16")
    with pytest.raises(SyntaxError):
        compile(decoded, "broken.py", "exec")


# ---------------------------------------------------------------------------
# Other languages keep their UTF-8 decoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["app.js", "query.sql"])
def test_other_languages_ignore_python_encoding_declarations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    content = b'# -*- coding: latin-1 -*-\nvalue = "caf\xe9";\n'
    root = _root(tmp_path, {name: content})

    status, document, errors = _analyze(root, tmp_path / "graph.json", capsys)

    assert status == 1
    assert _file_nodes(document) == {}
    assert len(errors) == 1
    assert errors[0].startswith(f"{name}: source-read-error: ")


# ---------------------------------------------------------------------------
# Python file recognition follows the interpreter registry
# ---------------------------------------------------------------------------


def test_python_source_path_matches_registered_python_extensions() -> None:
    registrations = default_registry().registrations
    assert any(registration.namespace == "minotaur-python" for registration in registrations)
    assert any(registration.namespace != "minotaur-python" for registration in registrations)

    for registration in registrations:
        expected = registration.namespace == "minotaur-python"
        for extension in (registration.extension.lower(), registration.extension.upper()):
            assert is_python_source_path(f"package/module{extension}") is expected, extension
