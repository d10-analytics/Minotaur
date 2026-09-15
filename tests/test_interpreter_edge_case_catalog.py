"""Integrity checks for the cross-language interpreter edge-case catalog.

These checks validate the catalog's document contract and its traceability
links. They intentionally do not run an interpreter or assert graph behavior;
those facts belong to the natural proofs linked by each catalog row.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from minotaur.language_interpreter.registry import default_registry

ROOT = Path(__file__).parents[1]
CATALOG = ROOT / "docs/concepts/interpreter-edge-cases.md"
NAVIGATION = {
    ROOT / "docs/concepts/structural-analysis-contract.md": "interpreter-edge-cases.md",
    ROOT / "docs/guides/analyze-python.md": "../concepts/interpreter-edge-cases.md",
    ROOT / "docs/guides/analyze-javascript.md": "../concepts/interpreter-edge-cases.md",
    ROOT / "docs/guides/create-a-language-interpreter.md": "../concepts/interpreter-edge-cases.md",
}
CASE_IDS = (
    "EDGE-BIND-001",
    "EDGE-BIND-002",
    "EDGE-BIND-003",
    "EDGE-BIND-004",
    "EDGE-DECL-001",
    "EDGE-DECL-002",
)
STATUS_MATRIX = {
    "EDGE-BIND-001": {"minotaur-python": "SUPPORTED", "minotaur-javascript": "NOT_APPLICABLE"},
    "EDGE-BIND-002": {"minotaur-python": "SUPPORTED", "minotaur-javascript": "NOT_APPLICABLE"},
    "EDGE-BIND-003": {"minotaur-python": "SUPPORTED", "minotaur-javascript": "NOT_APPLICABLE"},
    "EDGE-BIND-004": {"minotaur-python": "SUPPORTED", "minotaur-javascript": "NOT_APPLICABLE"},
    "EDGE-DECL-001": {"minotaur-python": "SUPPORTED", "minotaur-javascript": "SUPPORTED"},
    "EDGE-DECL-002": {"minotaur-python": "UNSUPPORTED", "minotaur-javascript": "UNSUPPORTED"},
}
CLOSED_STATUSES = {"SUPPORTED", "PARTIAL", "UNSUPPORTED", "NOT_APPLICABLE"}
CASE_HEADING = re.compile(r"^###\s+(EDGE-[A-Z]+-\d{3})\s+—\s+(.+)$")
LANGUAGE_HEADING = re.compile(r"^####\s+(.+?)\s+—\s+([a-z0-9-]+)$")
FIELD = re.compile(
    r"^(Question|Status|Example|Expected graph facts|Owner|Marker|Proof|Reason):\s*(.+)$"
)
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
MARKER_STATUS = re.compile(
    r"\b(?:supports|excludes|rejects|unsupported|partial|not\s+applicable)\b",
    re.IGNORECASE,
)


class CatalogError(ValueError):
    """Raised when catalog text cannot satisfy its integrity contract."""


def _parse_catalog(text: str) -> list[dict[str, object]]:
    """Parse the fixed heading and labelled-field shape without graph logic."""
    lines = text.splitlines()
    starts: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        if line.startswith("### "):
            match = CASE_HEADING.match(line)
            if match is None:
                raise CatalogError(f"invalid case heading at line {index + 1}")
            starts.append((index, match.group(1), match.group(2)))
    if not starts:
        raise CatalogError("catalog has no case headings")

    seen: set[str] = set()
    previous_by_category: dict[str, int] = {}
    cases: list[dict[str, object]] = []
    for position, (start, case_id, title) in enumerate(starts):
        if case_id in seen:
            raise CatalogError(f"duplicate case identifier: {case_id}")
        seen.add(case_id)
        category, number_text = case_id.removeprefix("EDGE-").rsplit("-", 1)
        number = int(number_text)
        if category in previous_by_category and number <= previous_by_category[category]:
            raise CatalogError(f"case identifiers are not append-only: {case_id}")
        previous_by_category[category] = number
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        section = lines[start + 1 : end]
        questions = [line for line in section if line.startswith("Question:")]
        if len(questions) != 1 or not questions[0][len("Question:") :].strip():
            raise CatalogError(f"case requires exactly one Question field: {case_id}")
        row_starts = [
            (offset, match.group(1), match.group(2))
            for offset, line in enumerate(section)
            if (match := LANGUAGE_HEADING.match(line))
        ]
        if not row_starts:
            raise CatalogError(f"case has no language rows: {case_id}")
        rows: list[dict[str, object]] = []
        languages: set[str] = set()
        for row_position, (row_start, language, namespace) in enumerate(row_starts):
            if namespace in languages:
                raise CatalogError(f"duplicate language row: {case_id}/{namespace}")
            languages.add(namespace)
            row_end = (
                row_starts[row_position + 1][0]
                if row_position + 1 < len(row_starts)
                else len(section)
            )
            fields: dict[str, str] = {}
            for line in section[row_start + 1 : row_end]:
                if not line.strip():
                    continue
                match = FIELD.match(line)
                if match is None:
                    raise CatalogError(f"unlabelled row content: {case_id}/{namespace}")
                key, value = match.groups()
                if key in fields:
                    raise CatalogError(f"duplicate field {key}: {case_id}/{namespace}")
                fields[key] = value.strip()
            if "Status" not in fields or fields["Status"] not in CLOSED_STATUSES:
                raise CatalogError(f"invalid or missing status: {case_id}/{namespace}")
            if fields["Status"] == "NOT_APPLICABLE":
                if set(fields) != {"Status", "Reason"} or not fields.get("Reason"):
                    raise CatalogError(
                        f"NOT_APPLICABLE row must contain only Reason: {case_id}/{namespace}"
                    )
            else:
                required = {"Status", "Example", "Expected graph facts", "Owner", "Marker", "Proof"}
                if set(fields) != required or not all(fields[field] for field in required):
                    raise CatalogError(
                        f"applicable row has incomplete fields: {case_id}/{namespace}"
                    )
            rows.append({"language": language, "namespace": namespace, "fields": fields})
        cases.append({"id": case_id, "title": title, "rows": rows})
    return cases


def _resolve_link(document: Path, target: str) -> Path:
    """Resolve a repository-relative Markdown link and reject escapes."""
    parsed = urlsplit(target)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path.startswith("/")
    ):
        raise CatalogError(f"link is not a relative repository path: {target}")
    resolved = (document.parent / parsed.path).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as error:
        raise CatalogError(f"link escapes repository root: {target}") from error
    if not resolved.is_file():
        raise CatalogError(f"linked file does not exist: {target}")
    return resolved


def _linked_field(value: str, field: str, document: Path) -> tuple[str, Path]:
    matches = list(LINK.finditer(value))
    if len(matches) != 1:
        raise CatalogError(f"{field} requires exactly one Markdown link")
    match = matches[0]
    label = match.group(1).strip().strip("`")
    if not label:
        raise CatalogError(f"{field} link requires a named symbol")
    return label, _resolve_link(document, match.group(2))


def _has_named_definition(path: Path, name: str) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        raise CatalogError(f"cannot parse linked source: {path}") from error
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == name
        for node in ast.walk(tree)
    )


def _named_definition_count(path: Path, name: str) -> int:
    """Count definitions at a dotted owner path such as ``Class.method``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        raise CatalogError(f"cannot parse linked source: {path}") from error
    current: list[ast.AST] = [tree]
    for part in name.split("."):
        next_nodes: list[ast.AST] = []
        for container in current:
            body = getattr(container, "body", ())
            next_nodes.extend(
                node
                for node in body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == part
            )
        current = next_nodes
    return len(current)


def _owner_node(path: Path, name: str) -> ast.AST:
    count = _named_definition_count(path, name)
    if count != 1:
        raise CatalogError(f"owner path is not unique: {name} ({count} definitions)")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        raise CatalogError(f"cannot parse linked source: {path}") from error
    current: list[ast.AST] = [tree]
    for part in name.split("."):
        current = [
            node
            for container in current
            for node in getattr(container, "body", ())
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == part
        ]
    return current[0]


def _validate_owner_marker(path: Path, owner_symbol: str, marker: str) -> None:
    """Require one exact marker globally and within its qualified AST owner."""
    owner_node = _owner_node(path, owner_symbol)
    source_bytes = path.read_bytes()
    marker_bytes = marker.encode("utf-8")
    if source_bytes.count(marker_bytes) != 1:
        raise CatalogError(f"marker must occur exactly once at owner: {owner_symbol}")
    source_lines = source_bytes.splitlines(keepends=True)
    scope_start = owner_node.lineno - 1
    scope_end = owner_node.end_lineno or owner_node.lineno
    scope_bytes = b"".join(source_lines[scope_start:scope_end])
    if scope_bytes.count(marker_bytes) != 1:
        raise CatalogError(f"marker is outside canonical owner scope: {owner_symbol}")


def _validate_catalog(text: str) -> list[dict[str, object]]:
    cases = _parse_catalog(text)
    ids = tuple(case["id"] for case in cases)
    if ids != CASE_IDS:
        raise CatalogError(f"catalog IDs/order differ from settled inventory: {ids}")
    namespaces = tuple(registration.namespace for registration in default_registry().registrations)
    if len(set(namespaces)) != len(namespaces):
        raise CatalogError("default registry contains duplicate namespaces")
    for case in cases:
        case_id = str(case["id"])
        rows = case["rows"]
        assert isinstance(rows, list)
        row_namespaces = tuple(str(row["namespace"]) for row in rows)
        if set(row_namespaces) != set(namespaces) or len(row_namespaces) != len(namespaces):
            raise CatalogError(f"language matrix differs from registry: {case_id}")
        for row in rows:
            namespace = str(row["namespace"])
            fields = row["fields"]
            assert isinstance(fields, dict)
            status = str(fields["Status"])
            if status != STATUS_MATRIX[case_id][namespace]:
                raise CatalogError(f"settled status differs: {case_id}/{namespace}")
            if status == "NOT_APPLICABLE":
                continue
            owner_symbol, owner_path = _linked_field(str(fields["Owner"]), "Owner", CATALOG)
            proof_symbol, proof_path = _linked_field(str(fields["Proof"]), "Proof", CATALOG)
            if owner_path.suffix != ".py":
                raise CatalogError(f"owner source is not Python: {case_id}/{namespace}")
            _owner_node(owner_path, owner_symbol)
            if (
                proof_path.suffix != ".py"
                or not proof_symbol.startswith("test_")
                or not _has_named_definition(proof_path, proof_symbol)
            ):
                raise CatalogError(f"proof test function is not defined: {case_id}/{namespace}")
            marker = str(fields["Marker"])
            if (
                not marker.startswith(f"# {case_id}:")
                or not marker.endswith(".")
                or not MARKER_STATUS.search(marker)
            ):
                raise CatalogError(f"marker lacks exact ID or status clause: {case_id}/{namespace}")
            try:
                _validate_owner_marker(owner_path, owner_symbol, marker)
            except OSError as error:
                raise CatalogError(f"cannot read owner source: {owner_path}") from error
    return cases


def _validate_navigation(text: str, document: Path, target: str) -> None:
    matches = [match.group(2) for match in LINK.finditer(text) if match.group(2) == target]
    if len(matches) != 1:
        raise CatalogError(f"expected one catalog navigation link in {document}")
    _resolve_link(document, target)


def test_shipped_catalog_has_settled_matrix_and_traceability() -> None:
    cases = _validate_catalog(CATALOG.read_text(encoding="utf-8"))
    assert tuple(case["id"] for case in cases) == CASE_IDS
    for document, target in NAVIGATION.items():
        text = document.read_text(encoding="utf-8")
        _validate_navigation(text, document, target)
        assert not re.search(r"EDGE-(?:BIND|DECL)-\d{3}", text)


def test_catalog_owner_symbols_are_unambiguous() -> None:
    cases = _parse_catalog(CATALOG.read_text(encoding="utf-8"))
    for case in cases:
        for row in case["rows"]:
            fields = row["fields"]
            assert isinstance(fields, dict)
            if fields["Status"] == "NOT_APPLICABLE":
                continue
            owner_symbol, owner_path = _linked_field(str(fields["Owner"]), "Owner", CATALOG)
            assert _named_definition_count(owner_path, owner_symbol) == 1


def test_catalog_rejects_marker_outside_qualified_owner_scope(tmp_path: Path) -> None:
    marker = (
        "# EDGE-BIND-001: supports direct function-body named-import source-position resolution."
    )
    source = tmp_path / "owner.py"
    source.write_text(
        f"{marker}\n\nclass _ScopeCallVisitor:\n    def visit_ImportFrom(self):\n        pass\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="outside canonical owner scope"):
        _validate_owner_marker(source, "_ScopeCallVisitor.visit_ImportFrom", marker)


def test_create_guide_preserves_ordered_catalog_maintenance() -> None:
    text = re.sub(
        r"\s+",
        " ",
        (ROOT / "docs/guides/create-a-language-interpreter.md").read_text(encoding="utf-8"),
    )
    ordered_steps = (
        "After registering a new interpreter, maintain the [interpreter edge-case catalog]",
        "add one status row for every existing case",
        "for each applicable row add a minimal example, expected graph facts, "
        "one owner marker, and one natural behavioral proof",
        "add a new append-only case only when a source-proven invariant is genuinely new",
        "then run the catalog integrity test followed by all six CI lanes",
    )
    positions = tuple(text.find(step) for step in ordered_steps)
    assert all(position >= 0 for position in positions)
    assert positions == tuple(sorted(positions))


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        (
            "duplicate ID",
            lambda text: text.replace("### EDGE-BIND-002 —", "### EDGE-BIND-001 —", 1),
        ),
        (
            "category outside grammar",
            lambda text: text.replace("### EDGE-BIND-001 —", "### EDGE-bind-001 —", 1),
        ),
        (
            "duplicate language row",
            lambda text: text.replace(
                "#### JavaScript — minotaur-javascript", "#### Python — minotaur-python", 1
            ),
        ),
        (
            "missing question",
            lambda text: text.replace(
                "Question: When a direct named import appears in a function body, do calls and "
                "non-call loads after the import resolve at their own source positions?\n",
                "",
                1,
            ),
        ),
        (
            "malformed status",
            lambda text: text.replace("Status: SUPPORTED", "Status: MAYBE", 1),
        ),
        (
            "missing registered namespace",
            lambda text: text.replace(
                "#### JavaScript — minotaur-javascript\n\n"
                "Status: NOT_APPLICABLE\n"
                "Reason: JavaScript ESM imports are module-level syntax and have no "
                "equivalent conditional function-body import operation.",
                "",
                1,
            ),
        ),
        (
            "NA row marker",
            lambda text: text.replace(
                "Reason: JavaScript ESM imports are module-level syntax and have no "
                "equivalent conditional function-body import operation.",
                "Reason: JavaScript ESM imports are module-level syntax and have no "
                "equivalent conditional function-body import operation.\n"
                "Marker: # EDGE-BIND-001: supports invalid NA marker.",
                1,
            ),
        ),
        (
            "missing proof",
            lambda text: text.replace(
                "Proof: [`test_source_position_routes_prove_owner_location_and_syntactic_imports`]"
                "(../../tests/language_interpreter/python/test_interpreter.py); "
                "the natural fixture reaches the public Python analyzer and asserts "
                "each resolved call/load source position, exact target, and syntactic "
                "import evidence.\n",
                "",
                1,
            ),
        ),
        (
            "wrong owner path",
            lambda text: text.replace(
                "../../src/minotaur/language_interpreter/python/interpreter.py); "
                "the direct import visitor",
                "../../src/minotaur/language_interpreter/python/__init__.py); "
                "the direct import visitor",
                1,
            ),
        ),
        (
            "duplicate source marker",
            lambda text: text.replace(
                "# EDGE-BIND-003: supports conservative unresolved divergent or omitted "
                "conditional routes.",
                "# EDGE-BIND-002: supports agreeing conditional import routes after a flow join.",
                1,
            ),
        ),
        (
            "statusless marker",
            lambda text: text.replace(
                "# EDGE-BIND-002: supports agreeing conditional import routes after a flow join.",
                "# EDGE-BIND-002: helper",
                1,
            ),
        ),
        (
            "absent test function",
            lambda text: text.replace(
                "[`test_source_position_routes_prove_owner_location_and_syntactic_imports`]",
                "[`test_missing_catalog_proof_function`]",
                1,
            ),
        ),
    ],
)
def test_catalog_rejects_mutated_contract(label: str, mutate: Callable[[str], str]) -> None:
    original = CATALOG.read_text(encoding="utf-8")
    mutated = mutate(original)
    with pytest.raises(CatalogError):
        _validate_catalog(mutated)


def test_catalog_rejects_broken_relative_navigation_link() -> None:
    document = ROOT / "docs/concepts/structural-analysis-contract.md"
    text = document.read_text(encoding="utf-8").replace(
        "[interpreter edge-case catalog](interpreter-edge-cases.md)",
        "[interpreter edge-case catalog](missing-catalog.md)",
        1,
    )
    with pytest.raises(CatalogError, match="expected one catalog navigation link"):
        _validate_navigation(text, document, "interpreter-edge-cases.md")
