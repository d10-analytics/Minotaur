"""Behavioral coverage for UTF-8 BOM handling in Python source files."""

from __future__ import annotations

from pathlib import Path

from minotaur.graph_model.provenance import NodeClass
from minotaur.language_interpreter.contract import AnalysisResult
from minotaur.language_interpreter.python import analyze_python_workspace


def _node_shape(result: AnalysisResult) -> list[tuple[object, ...]]:
    document = result.document
    return [
        (
            node.id,
            node.identity,
            node.node_class,
            node.label,
            node.symbol_kind,
            node.language,
            node.location,
            node.path,
            node.reference_text,
        )
        for node in document.nodes
    ]


def _content_hash(result: AnalysisResult) -> str:
    document = result.document
    file_node = next(node for node in document.nodes if node.node_class is NodeClass.FILE)
    assert file_node.extensions is not None
    digest = file_node.extensions["minotaur-python"]["content_sha256"]
    assert isinstance(digest, str)
    return digest


def test_python_bom_preserves_graph_facts_but_hashes_raw_bytes(tmp_path: Path) -> None:
    source = b"class Example:\n    def run(self):\n        return 1\n"
    plain_root = tmp_path / "plain"
    bom_root = tmp_path / "bom"
    plain_root.mkdir()
    bom_root.mkdir()
    (plain_root / "app.py").write_bytes(source)
    (bom_root / "app.py").write_bytes(b"\xef\xbb\xbf" + source)

    plain = analyze_python_workspace(plain_root)
    bom = analyze_python_workspace(bom_root)

    assert plain.diagnostics == ()
    assert bom.diagnostics == ()
    assert _node_shape(plain) == _node_shape(bom)
    assert plain.document.relationships == bom.document.relationships
    assert _content_hash(plain) != _content_hash(bom)
