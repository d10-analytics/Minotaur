"""Behavioral coverage for the graph-loading boundary.

AC-12: ``LoadedGraph.canonical`` is a lazy cached property.  The query path
never accesses it, so monkeypatching ``canonicalize`` to raise does not
affect ``query`` on a clean or ``--no-refresh`` graph, but ``visualize``
(which reads ``.canonical``) propagates the error.

AC-01: A stamped load skips the JSON-schema pass; a flipped byte raises.
AC-02: Every non-matching sidecar state falls back to full validation.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from test_graph_model_serialization import (
    _collect_loadable_graph_fixtures,
    _oracle_serialize,
)

from minotaur import cli
from minotaur.graph_model.loading import (
    GraphLoadError,
    _validate_wire_shape,
    graph_digest,
    load_graph_bytes,
    load_graph_file,
    stamp_path,
)
from minotaur.graph_model.serialization import canonicalize, serialize

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root: Path, path: str, source: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def _analyze(root: Path, graph_path: Path, *targets: Path) -> None:
    """Run ``analyze`` to produce a graph JSON at *graph_path*."""
    args = [
        "analyze",
        "--root",
        str(root),
        "--output",
        str(graph_path),
        "--force",
        *(str(t) for t in targets),
    ]
    status = cli.main(args)
    assert status == 0, f"analyze failed with exit {status}"


def _symbol_name(graph_path: Path) -> str:
    """Return the label of the first symbol node in the graph."""
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    for node in graph["nodes"]:
        if node["node_class"] == "symbol":
            return node["label"]
    raise AssertionError("graph has no symbol nodes")


# ---------------------------------------------------------------------------
# AC-12 proof
# ---------------------------------------------------------------------------


class TestLazyCanonical:
    """Prove that ``LoadedGraph.canonical`` is computed lazily (AC-12)."""

    @pytest.fixture()
    def workspace(self, tmp_path: Path) -> tuple[Path, Path, str]:
        """Analyze a minimal source tree and return (root, graph_path, symbol)."""
        root = tmp_path / "repo"
        root.mkdir()
        source = _write(root, "mod.py", "def helper():\n    return 1\n")
        graph_path = tmp_path / "graph.json"
        _analyze(root, graph_path, source)
        symbol = _symbol_name(graph_path)
        return root, graph_path, symbol

    def test_clean_graph_query_does_not_canonicalize(
        self,
        workspace: tuple[Path, Path, str],
        capsys: object,
    ) -> None:
        """A query on a clean graph exits 0 even when canonicalize would raise."""
        root, graph_path, symbol = workspace
        with patch(
            "minotaur.graph_model.loading.canonicalize",
            side_effect=AssertionError("canonicalize must not be called"),
        ):
            status = cli.main(
                [
                    "query",
                    "definitions",
                    symbol,
                    "--graph",
                    str(graph_path),
                    "--root",
                    str(root),
                ]
            )
        assert status == 0

    def test_no_refresh_query_does_not_canonicalize(
        self,
        workspace: tuple[Path, Path, str],
        capsys: object,
    ) -> None:
        """A --no-refresh query on a drifted graph exits 0 without canonicalize."""
        root, graph_path, symbol = workspace
        # Introduce drift: modify the source after analysis.
        time.sleep(0.05)  # ensure mtime advances
        _write(root, "mod.py", "def helper():\n    return 2\n")
        with patch(
            "minotaur.graph_model.loading.canonicalize",
            side_effect=AssertionError("canonicalize must not be called"),
        ):
            status = cli.main(
                [
                    "query",
                    "definitions",
                    symbol,
                    "--graph",
                    str(graph_path),
                    "--root",
                    str(root),
                    "--no-refresh",
                ]
            )
        # Exit 1 is acceptable here: it means "answered, but graph is stale".
        assert status in (0, 1)

    def test_visualize_accesses_canonical(
        self,
        workspace: tuple[Path, Path, str],
    ) -> None:
        """``visualize`` reads ``.canonical``, so a poisoned canonicalize propagates."""
        _root, graph_path, _symbol = workspace
        html_path = graph_path.with_suffix(".html")
        with (
            patch(
                "minotaur.graph_model.loading.canonicalize",
                side_effect=AssertionError("canonicalize called"),
            ),
            pytest.raises(AssertionError, match="canonicalize called"),
        ):
            cli.main(
                [
                    "visualize",
                    "--input",
                    str(graph_path),
                    "--output",
                    str(html_path),
                    "--force",
                ]
            )

    def test_canonical_value_matches_eager_computation(self) -> None:
        """The cached property returns the same value as an eager call would."""
        from minotaur.graph_model.serialization import canonicalize

        path = Path(__file__).parents[1] / "examples/synthetic-graphs/small-workflow.json"
        content = path.read_bytes()
        loaded = load_graph_bytes(content)
        expected = canonicalize(loaded.document)
        assert loaded.canonical == expected

    def test_cached_property_is_computed_once(self) -> None:
        """Repeated reads return the same object without recomputing."""
        path = Path(__file__).parents[1] / "examples/synthetic-graphs/small-workflow.json"
        loaded = load_graph_bytes(path.read_bytes())
        first = loaded.canonical
        second = loaded.canonical
        assert first is second

    def test_dataclass_eq_compares_document_only(self) -> None:
        """Dropping ``canonical`` from fields means equality uses ``document`` only."""
        path = Path(__file__).parents[1] / "examples/synthetic-graphs/small-workflow.json"
        content = path.read_bytes()
        a = load_graph_bytes(content)
        b = load_graph_bytes(content)
        # Both have the same document but independent cached-property state.
        assert a == b
        # Access canonical on only one side — equality still holds.
        _ = a.canonical
        assert a == b


# ---------------------------------------------------------------------------
# AC-14 and AC-15 proof: the decoder boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_path",
    _collect_loadable_graph_fixtures(),
    ids=lambda path: str(path.relative_to(Path(__file__).parents[1])),
)
def test_strict_load_fixtures_round_trip_through_oracle(fixture_path: Path) -> None:
    """Every strict-load fixture uses the one decoder and canonical serializer."""
    content = fixture_path.read_bytes()
    loaded = load_graph_bytes(content)
    assert serialize(loaded.document) == _oracle_serialize(canonicalize(loaded.document))


def test_python_workflow_graph_round_trips_to_its_committed_bytes() -> None:
    """The committed canonical workflow graph remains byte-stable after loading."""
    path = Path(__file__).parents[1] / "examples/python-workflow/minotaur-graph.json"
    loaded = load_graph_bytes(path.read_bytes())
    assert serialize(loaded.document) == path.read_bytes()


def _workflow_with_extension_literal(literal: str) -> bytes:
    """Return a valid graph document with a caller-supplied JSON literal."""
    path = Path(__file__).parents[1] / "examples/synthetic-graphs/small-workflow.json"
    text = path.read_text(encoding="utf-8")
    marker = '"relationships":'
    extension = '"extensions":{"test":{"value":' + literal + "}},"
    return text.replace(marker, extension + marker, 1).encode()


@pytest.mark.parametrize(
    "content",
    [
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":"\\ud800"}',
        b"[" * 1100 + b"]" * 1100,
    ],
    ids=["nan", "infinity", "negative-infinity", "lone-surrogate", "nesting-depth"],
)
def test_orjson_rejections_surface_as_graph_load_errors(content: bytes) -> None:
    """Every form the decoder itself refuses becomes one GraphLoadError.

    The 1024-level nesting limit is `orjson`-specific and has no
    standard-library equivalent; it must surface through the same boundary
    rather than escaping as a raw decoder or recursion error.
    """
    with pytest.raises(GraphLoadError, match=r"^graph input is not valid JSON: "):
        load_graph_bytes(content)


@pytest.mark.parametrize(
    ("depth", "skip_schema", "outcome"),
    [
        (100, False, "loads"),
        (150, False, "nests deeper than the loader supports"),
        (495, True, "loads"),
        (900, True, "loads"),
        (1010, True, "nests deeper than the loader supports"),
        (1100, True, "graph input is not valid JSON: "),
    ],
    ids=[
        "untrusted-100",
        "untrusted-150-schema-bound",
        "trusted-495",
        "trusted-900",
        "trusted-1010-freeze-bound",
        "trusted-1100-decoder-bound",
    ],
)
def test_extension_nesting_bounds_surface_as_load_errors(
    depth: int, skip_schema: bool, outcome: str
) -> None:
    """Every nesting bound is a ``GraphLoadError``, never a raw traceback.

    Three bounds exist and none is the decoder's for realistic input: the
    recursive ``extensionValue`` schema introduced by this spec bounds the
    full-validation path at roughly 120 levels (main accepted deeper documents
    because its schema never descended into extension contents); the extension
    freeze bounds the trusted path just short of 1000; orjson's fixed 1024
    limit is only reached on the trusted path.  ``load_graph_bytes`` converts
    the interpreter's ``RecursionError`` into the load boundary's own error.
    The exact cliffs depend on the surrounding call stack, so the parameters
    sit well inside each region rather than on its edge.
    """
    literal = "{" + '"n":{' * depth + "}" * depth + "}"
    content = _workflow_with_extension_literal(literal)
    if outcome == "loads":
        loaded = load_graph_bytes(content, _skip_schema=skip_schema)
        assert loaded.document.extensions is not None
        return
    with pytest.raises(GraphLoadError, match=re.escape(outcome)):
        load_graph_bytes(content, _skip_schema=skip_schema)


def _workflow_with_position_line(literal: str) -> bytes:
    """Return the workflow graph with its first `"line"` value replaced."""
    path = Path(__file__).parents[1] / "examples/synthetic-graphs/small-workflow.json"
    text = path.read_text(encoding="utf-8")
    marker = '"line":'
    index = text.index(marker) + len(marker)
    end = index
    while text[end] not in ",}":
        end += 1
    return (text[:index] + literal + text[end:]).encode()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            _workflow_with_extension_literal(str(2**70)),
            r"extension value at /test/value must be an integer, got float: ",
        ),
        (
            _workflow_with_position_line(str(2**70)),
            r"'line' must be an integer, got float: ",
        ),
    ],
    ids=["extension-value", "position-line"],
)
def test_integers_beyond_64_bits_are_rejected_by_the_model_layer(
    content: bytes, expected: str
) -> None:
    """An oversized integer literal is still rejected — one layer further in.

    `orjson` decodes such a literal as a float rather than raising, so the
    loader no longer walks the decoded document looking for it. Every place a
    v1 document may carry a number is guarded by the model layer instead, so
    the document is rejected with that layer's message rather than
    "graph input is not valid JSON: integer out of range".
    """
    with pytest.raises(GraphLoadError, match=expected):
        load_graph_bytes(content)


def test_loading_performs_no_recursive_walk_over_the_decoded_document() -> None:
    """The out-of-range integer walk is gone, not merely unused (H-1/M-3)."""
    source = (Path(__file__).parents[1] / "src/minotaur/graph_model/loading.py").read_text()
    assert "_contains_out_of_range_integer" not in source
    assert "integer out of range" not in source


def test_invalid_utf8_message_is_unchanged() -> None:
    with pytest.raises(GraphLoadError, match=r"^graph input is not valid UTF-8: "):
        load_graph_bytes(b"\xff")


def test_json_array_message_is_unchanged() -> None:
    with pytest.raises(GraphLoadError, match="^graph input must contain a JSON object$"):
        load_graph_bytes(b"[]")


def test_load_boundary_uses_orjson_without_a_decoder_fallback() -> None:
    source = (Path(__file__).parents[1] / "src/minotaur/graph_model/loading.py").read_text()
    decode_start = source.index("raw: Any =")
    decode_end = source.index("if not isinstance(raw, dict):", decode_start)
    assert "orjson.loads(decoded)" in source[decode_start:decode_end]
    assert "raw: Any = json.loads" not in source[decode_start:decode_end]
    assert "json.loads(text)" in source


def test_orjson_is_a_declared_runtime_dependency() -> None:
    # A text match rather than ``tomllib``: that module is 3.11+, and importing
    # it would drop this whole module from a run on the declared 3.10 floor.
    text = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert re.search(r'^\s*"orjson>=[\d.]+",?\s*$', dependencies, re.MULTILINE)


# ---------------------------------------------------------------------------
# Helpers for stamp-aware tests
# ---------------------------------------------------------------------------

_EXAMPLE_GRAPH = Path(__file__).parents[1] / "examples/synthetic-graphs/small-workflow.json"


def _stamped_graph(tmp_path: Path) -> tuple[Path, bytes]:
    """Copy the example graph into *tmp_path* and write a correct sidecar."""
    data = _EXAMPLE_GRAPH.read_bytes()
    graph_path = tmp_path / "graph.json"
    graph_path.write_bytes(data)
    stamp_path(graph_path).write_text(graph_digest(data) + "\n", encoding="utf-8")
    return graph_path, data


def _stamped_single_node_graph(tmp_path: Path, *, alter_id: bool) -> Path:
    """Create an isolated graph for the sidecar trust-risk proof.

    The altered node has no relationship endpoint, so a trusted load reaches
    the node-ID decision instead of failing first on dangling structure. The
    sidecar is regenerated after the edit to model the documented trust risk.
    """
    raw = json.loads(_EXAMPLE_GRAPH.read_text(encoding="utf-8"))
    raw["nodes"] = [raw["nodes"][0]]
    raw["relationships"] = []
    if alter_id:
        raw["nodes"][0]["id"] = "node:sha256:" + "0" * 64
    data = json.dumps(raw, separators=(",", ":")).encode("utf-8")
    graph_path = tmp_path / "single-node.json"
    graph_path.write_bytes(data)
    stamp_path(graph_path).write_text(graph_digest(data) + "\n", encoding="utf-8")
    return graph_path


# ---------------------------------------------------------------------------
# AC-01 proof
# ---------------------------------------------------------------------------


class TestStampAwareLoader:
    """Prove that a matching sidecar skips the schema pass (AC-01)."""

    def test_stamped_load_skips_schema(self, tmp_path: Path) -> None:
        """A matching sidecar suppresses the schema pass."""
        graph_path, data = _stamped_graph(tmp_path)
        with patch(
            "minotaur.graph_model.loading._validate_wire_shape",
            side_effect=AssertionError("schema seam must not be called"),
        ):
            loaded = load_graph_file(graph_path)
        assert loaded.validated is False
        assert loaded.digest == hashlib.sha256(data).hexdigest()

    def test_flipped_byte_raises(self, tmp_path: Path) -> None:
        """Flipping one graph byte makes the digest mismatch, running the seam."""
        graph_path, data = _stamped_graph(tmp_path)
        # Re-serialize with a trivial change that keeps the JSON valid
        # but produces different bytes (different digest).
        raw = json.loads(data)
        raw["_tampered"] = True
        graph_path.write_bytes(json.dumps(raw).encode())
        # The sidecar still has the OLD digest, which no longer matches.
        with (
            patch(
                "minotaur.graph_model.loading._validate_wire_shape",
                side_effect=AssertionError("schema seam was called"),
            ),
            pytest.raises(AssertionError, match="schema seam was called"),
        ):
            load_graph_file(graph_path)

    def test_validate_flag_forces_schema(self, tmp_path: Path) -> None:
        """``validate=True`` runs the schema pass even when the sidecar matches."""
        graph_path, data = _stamped_graph(tmp_path)
        loaded = load_graph_file(graph_path, validate=True)
        assert loaded.validated is True
        assert loaded.digest == hashlib.sha256(data).hexdigest()

    def test_trusted_load_skips_only_node_id_verification(self, tmp_path: Path) -> None:
        """A matching stamp skips IDs; missing stamps and ``--validate`` do not."""
        graph_path, _data = _stamped_graph(tmp_path)

        with patch(
            "minotaur.graph_model.validation.verify_node_id",
            side_effect=AssertionError("node-ID verification was called"),
        ):
            trusted = load_graph_file(graph_path)
            assert trusted.validated is False

            stamp_path(graph_path).unlink()
            with pytest.raises(AssertionError, match="node-ID verification was called"):
                load_graph_file(graph_path)

            stamp_path(graph_path).write_text(
                graph_digest(graph_path.read_bytes()) + "\n", encoding="utf-8"
            )
            with pytest.raises(AssertionError, match="node-ID verification was called"):
                load_graph_file(graph_path, validate=True)

    def test_regenerated_sidecar_accepts_altered_id_until_validate(self, tmp_path: Path) -> None:
        """The documented trusted-sidecar risk is executable and bounded."""
        graph_path = _stamped_single_node_graph(tmp_path, alter_id=True)

        trusted = load_graph_file(graph_path)
        assert trusted.validated is False

        with pytest.raises(GraphLoadError, match="does not match the digest recomputed"):
            load_graph_file(graph_path, validate=True)

    def test_load_graph_bytes_always_validates(self) -> None:
        """``load_graph_bytes`` runs the schema seam even for stamped bytes."""
        data = _EXAMPLE_GRAPH.read_bytes()
        seam_called = False
        original = _validate_wire_shape

        def tracking_seam(raw: dict[str, object]) -> None:
            nonlocal seam_called
            seam_called = True
            original(raw)

        with patch(
            "minotaur.graph_model.loading._validate_wire_shape",
            side_effect=tracking_seam,
        ):
            loaded = load_graph_bytes(data)
        assert seam_called
        assert loaded.validated is True
        assert loaded.digest == hashlib.sha256(data).hexdigest()

    def test_trusted_load_keeps_relationship_endpoint_validation(self, tmp_path: Path) -> None:
        """A matching stamp does not suppress non-ID semantic checks."""
        raw = {
            "format": "minotaur-graph",
            "format_version": "0.1.0",
            "coordinate_encoding": "utf-8",
            "nodes": [],
            "relationships": [
                {
                    "source": f"node:sha256:{'a' * 64}",
                    "target": f"node:sha256:{'b' * 64}",
                    "kind": "contains",
                    "evidence": [{"provenance": "static-analysis"}],
                }
            ],
        }
        data = json.dumps(raw).encode("utf-8")
        graph_path = tmp_path / "dangling-trusted.json"
        graph_path.write_bytes(data)
        stamp_path(graph_path).write_text(graph_digest(data) + "\n", encoding="utf-8")

        with pytest.raises(
            GraphLoadError,
            match="graph semantic validation failed:.*relationship source",
        ):
            load_graph_file(graph_path)

    def test_trusted_load_keeps_duplicate_node_validation(self, tmp_path: Path) -> None:
        """Skipping digest recomputation does not suppress duplicate detection."""
        raw = json.loads(_EXAMPLE_GRAPH.read_text(encoding="utf-8"))
        raw["nodes"][1]["id"] = raw["nodes"][0]["id"]
        data = json.dumps(raw, separators=(",", ":")).encode("utf-8")
        graph_path = tmp_path / "duplicate-node-trusted.json"
        graph_path.write_bytes(data)
        stamp_path(graph_path).write_text(graph_digest(data) + "\n", encoding="utf-8")

        with pytest.raises(
            GraphLoadError,
            match="graph semantic validation failed:.*node id .* already declared",
        ):
            load_graph_file(graph_path)

    def test_trusted_load_keeps_location_validation(self, tmp_path: Path) -> None:
        """Skipping digest recomputation does not suppress range validation."""
        raw = json.loads(_EXAMPLE_GRAPH.read_text(encoding="utf-8"))
        raw["nodes"][0]["location"]["range"] = {
            "start": {"line": 3, "character": 0},
            "end": {"line": 2, "character": 0},
        }
        data = json.dumps(raw, separators=(",", ":")).encode("utf-8")
        graph_path = tmp_path / "reversed-location-trusted.json"
        graph_path.write_bytes(data)
        stamp_path(graph_path).write_text(graph_digest(data) + "\n", encoding="utf-8")

        with pytest.raises(
            GraphLoadError,
            match="graph semantic validation failed:.*range end .* precedes start",
        ):
            load_graph_file(graph_path)

    def test_validate_flag_keeps_schema_validation_active(self, tmp_path: Path) -> None:
        """The explicit full-validation escape hatch still runs the schema seam."""
        graph_path, _data = _stamped_graph(tmp_path)
        with (
            patch(
                "minotaur.graph_model.loading._validate_wire_shape",
                side_effect=AssertionError("schema seam must run under --validate"),
            ),
            pytest.raises(AssertionError, match="schema seam must run under --validate"),
        ):
            load_graph_file(graph_path, validate=True)


# ---------------------------------------------------------------------------
# AC-02 proof
# ---------------------------------------------------------------------------

_SIDECAR_STATES: list[tuple[str, object]] = []


def _make_sidecar_states() -> list[tuple[str, object]]:
    """Build the parametrized sidecar-state table.

    Each entry is ``(id_string, setup_callable)`` where the callable takes
    ``(graph_path, correct_digest)`` and arranges the sidecar.
    """

    def absent(gp: Path, _d: str) -> None:
        # No sidecar file at all.
        sp = stamp_path(gp)
        if sp.exists():
            sp.unlink()

    def oserror_dir(gp: Path, _d: str) -> None:
        # A directory at the sidecar path triggers OSError on open.
        sp = stamp_path(gp)
        sp.mkdir(parents=True, exist_ok=True)

    def empty(gp: Path, _d: str) -> None:
        stamp_path(gp).write_text("", encoding="utf-8")

    def whitespace(gp: Path, _d: str) -> None:
        stamp_path(gp).write_text("   \n\t  \n", encoding="utf-8")

    def non_hex_64(gp: Path, _d: str) -> None:
        stamp_path(gp).write_text("g" * 64 + "\n", encoding="utf-8")

    def uppercase(gp: Path, d: str) -> None:
        stamp_path(gp).write_text(d.upper() + "\n", encoding="utf-8")

    def sha256sum_form(gp: Path, d: str) -> None:
        stamp_path(gp).write_text(f"{d}  graph.json\n", encoding="utf-8")

    def wrong_digest(gp: Path, d: str) -> None:
        wrong = d[:-1] + ("0" if d[-1] != "0" else "1")
        stamp_path(gp).write_text(wrong + "\n", encoding="utf-8")

    def junk_8k(gp: Path, d: str) -> None:
        # First 64 bytes are the correct digest, but the file is 8 KiB.
        stamp_path(gp).write_text(d + "x" * (8192 - 64) + "\n", encoding="utf-8")

    def non_utf8(gp: Path, _d: str) -> None:
        stamp_path(gp).write_bytes(b"\xff\xfe" + b"\x80" * 62)

    return [
        ("absent", absent),
        ("oserror_dir", oserror_dir),
        ("empty", empty),
        ("whitespace", whitespace),
        ("non_hex_64", non_hex_64),
        ("uppercase", uppercase),
        ("sha256sum_form", sha256sum_form),
        ("wrong_digest", wrong_digest),
        ("junk_8k", junk_8k),
        ("non_utf8", non_utf8),
    ]


_SIDECAR_STATES = _make_sidecar_states()


class TestSidecarFallback:
    """Prove every non-matching sidecar state falls back to full validation (AC-02)."""

    @pytest.fixture()
    def valid_graph(self, tmp_path: Path) -> tuple[Path, bytes, str]:
        """Copy the example graph into tmp_path and return (path, data, digest)."""
        data = _EXAMPLE_GRAPH.read_bytes()
        gp = tmp_path / "graph.json"
        gp.write_bytes(data)
        return gp, data, graph_digest(data)

    @pytest.mark.parametrize(
        "state_id, setup",
        _SIDECAR_STATES,
        ids=[s[0] for s in _SIDECAR_STATES],
    )
    def test_fallback_to_full_validation(
        self,
        valid_graph: tuple[Path, bytes, str],
        state_id: str,
        setup: object,
    ) -> None:
        """Non-matching sidecar loads succeed with validated=True."""
        graph_path, data, correct_digest = valid_graph
        # Set up the sidecar state.
        setup(graph_path, correct_digest)  # type: ignore[operator]

        seam_called = False
        original = _validate_wire_shape

        def tracking_seam(raw: dict[str, object]) -> None:
            nonlocal seam_called
            seam_called = True
            original(raw)

        with patch(
            "minotaur.graph_model.loading._validate_wire_shape",
            side_effect=tracking_seam,
        ):
            loaded = load_graph_file(graph_path)

        assert seam_called, f"schema seam not called for sidecar state {state_id!r}"
        assert loaded.validated is True
        assert loaded.digest == correct_digest

    @pytest.mark.parametrize(
        "state_id, setup",
        _SIDECAR_STATES,
        ids=[s[0] for s in _SIDECAR_STATES],
    )
    def test_fallback_rejects_invalid_graph(
        self,
        tmp_path: Path,
        state_id: str,
        setup: object,
    ) -> None:
        """Non-matching sidecar with a schema-invalid graph raises GraphLoadError
        with the same message ``load_graph_bytes`` produces on a fully-validating
        path (AC-02: "the same message as on main")."""
        invalid = {"not": "a valid graph"}
        graph_path = tmp_path / "graph.json"
        data = json.dumps(invalid).encode()
        graph_path.write_bytes(data)
        digest = graph_digest(data)
        setup(graph_path, digest)  # type: ignore[operator]

        with pytest.raises(GraphLoadError) as always_validated:
            load_graph_bytes(data)
        expected_message = str(always_validated.value)
        # Pin a stable substring so a reworded schema error is still caught
        # even if both the fallback and reference paths changed together.
        assert "'format' is a required property" in expected_message

        with pytest.raises(GraphLoadError) as fallback:
            load_graph_file(graph_path)
        assert str(fallback.value) == expected_message

    @pytest.mark.parametrize(
        "state_id, setup",
        _SIDECAR_STATES,
        ids=[s[0] for s in _SIDECAR_STATES],
    )
    def test_fallback_rejects_semantically_invalid_graph(
        self,
        tmp_path: Path,
        state_id: str,
        setup: object,
    ) -> None:
        """Non-matching sidecar with a schema-valid but semantically invalid
        graph (a relationship endpoint that names no declared node) raises
        GraphLoadError with the same message ``load_graph_bytes`` produces on
        a fully-validating path (AC-02), covering the ``validate_document``
        seam as well as the ``_validate_wire_shape`` seam above."""
        invalid = {
            "format": "minotaur-graph",
            "format_version": "0.1.0",
            "coordinate_encoding": "utf-8",
            "nodes": [],
            "relationships": [
                {
                    "source": f"node:sha256:{'a' * 64}",
                    "target": f"node:sha256:{'b' * 64}",
                    "kind": "contains",
                    "evidence": [{"provenance": "static-analysis"}],
                }
            ],
        }
        graph_path = tmp_path / "graph.json"
        data = json.dumps(invalid).encode()
        graph_path.write_bytes(data)
        digest = graph_digest(data)
        setup(graph_path, digest)  # type: ignore[operator]

        with pytest.raises(GraphLoadError) as always_validated:
            load_graph_bytes(data)
        expected_message = str(always_validated.value)
        assert "relationship source" in expected_message
        assert "does not identify a declared node" in expected_message

        with pytest.raises(GraphLoadError) as fallback:
            load_graph_file(graph_path)
        assert str(fallback.value) == expected_message

    def test_load_graph_bytes_still_validates_stamped_bytes(self) -> None:
        """``load_graph_bytes`` with exact stamped-graph bytes runs the schema."""
        data = _EXAMPLE_GRAPH.read_bytes()
        seam_called = False
        original = _validate_wire_shape

        def tracking_seam(raw: dict[str, object]) -> None:
            nonlocal seam_called
            seam_called = True
            original(raw)

        with patch(
            "minotaur.graph_model.loading._validate_wire_shape",
            side_effect=tracking_seam,
        ):
            loaded = load_graph_bytes(data)

        assert seam_called
        assert loaded.validated is True
        assert loaded.digest == graph_digest(data)


# ---------------------------------------------------------------------------
# Adversarial reviewer tests (T01)
# ---------------------------------------------------------------------------


class TestStampTrustBoundaryAdversarial:
    """Adversarial challenges to the stamp trust boundary (reviewer T01)."""

    def test_load_graph_file_is_read_only(self, tmp_path: Path) -> None:
        """D-14: load_graph_file never writes any file, including the sidecar.

        A valid graph loaded without a sidecar must not create the sidecar
        as a side effect.  A valid graph loaded WITH a matching sidecar must
        not modify the sidecar.
        """
        data = _EXAMPLE_GRAPH.read_bytes()
        graph_path = tmp_path / "graph.json"
        graph_path.write_bytes(data)

        before_files = set(tmp_path.iterdir())
        load_graph_file(graph_path)
        after_files = set(tmp_path.iterdir())
        assert before_files == after_files, "load_graph_file created a file"

        # Now add a sidecar and verify it is not modified.
        sidecar = stamp_path(graph_path)
        sidecar_content = graph_digest(data) + "\n"
        sidecar.write_text(sidecar_content, encoding="utf-8")
        sidecar_mtime = sidecar.stat().st_mtime_ns
        load_graph_file(graph_path)
        assert sidecar.read_text(encoding="utf-8") == sidecar_content
        assert sidecar.stat().st_mtime_ns == sidecar_mtime

    def test_sidecar_digest_split_at_4k_boundary(self, tmp_path: Path) -> None:
        """A sidecar where the correct digest straddles the 4096-byte read
        boundary must NOT match.  The reader reads at most 4096 bytes, so
        if the digest starts at byte 4090, only 6 of its 64 hex chars are
        read, and the stripped result cannot equal the full digest.
        """
        data = _EXAMPLE_GRAPH.read_bytes()
        graph_path = tmp_path / "graph.json"
        graph_path.write_bytes(data)
        digest = graph_digest(data)

        # Place whitespace padding so the digest starts at byte 4090.
        padding = " " * 4090
        stamp_path(graph_path).write_text(padding + digest + "\n", encoding="utf-8")

        seam_called = False
        original = _validate_wire_shape

        def tracking_seam(raw: dict[str, object]) -> None:
            nonlocal seam_called
            seam_called = True
            original(raw)

        with patch(
            "minotaur.graph_model.loading._validate_wire_shape",
            side_effect=tracking_seam,
        ):
            loaded = load_graph_file(graph_path)

        assert seam_called, "digest straddling 4K boundary must not skip schema"
        assert loaded.validated is True

    def test_sidecar_correct_digest_within_4k_with_whitespace_padding(self, tmp_path: Path) -> None:
        """A sidecar with the correct digest followed by whitespace that
        fits within 4096 bytes matches after .strip() -- this is the
        expected behavior per D-12 (strip + exact equality).  Verify the
        trust path activates.
        """
        data = _EXAMPLE_GRAPH.read_bytes()
        graph_path = tmp_path / "graph.json"
        graph_path.write_bytes(data)
        digest = graph_digest(data)

        # 64 hex chars + enough spaces + newline, all within 4096 bytes.
        padding = " " * (4096 - 64 - 1)
        stamp_path(graph_path).write_text(digest + padding + "\n", encoding="utf-8")

        with patch(
            "minotaur.graph_model.loading._validate_wire_shape",
            side_effect=AssertionError("schema seam must not be called"),
        ):
            loaded = load_graph_file(graph_path)

        assert loaded.validated is False
        assert loaded.digest == digest

    def test_sidecar_with_embedded_null_byte(self, tmp_path: Path) -> None:
        """A sidecar containing the correct digest followed by a NUL byte
        must NOT match.  str.strip() does not remove NUL bytes, so the
        stripped content is ``digest + chr(0)`` which differs from ``digest``.
        """
        data = _EXAMPLE_GRAPH.read_bytes()
        graph_path = tmp_path / "graph.json"
        graph_path.write_bytes(data)
        digest = graph_digest(data)

        stamp_path(graph_path).write_bytes((digest + "\x00\n").encode("utf-8"))

        seam_called = False
        original = _validate_wire_shape

        def tracking_seam(raw: dict[str, object]) -> None:
            nonlocal seam_called
            seam_called = True
            original(raw)

        with patch(
            "minotaur.graph_model.loading._validate_wire_shape",
            side_effect=tracking_seam,
        ):
            loaded = load_graph_file(graph_path)

        assert seam_called, "embedded NUL byte must cause fallback to full validation"
        assert loaded.validated is True
