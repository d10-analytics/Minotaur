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
ANY_CASE_HEADING = re.compile(r"^###\s+(\S+)")
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
            if owner_path.suffix != ".py" or not _has_named_definition(owner_path, owner_symbol):
                raise CatalogError(f"owner symbol is not defined: {case_id}/{namespace}")
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
                source_bytes = owner_path.read_bytes()
            except OSError as error:
                raise CatalogError(f"cannot read owner source: {owner_path}") from error
            if source_bytes.count(marker.encode("utf-8")) != 1:
                raise CatalogError(
                    f"marker must occur exactly once at owner: {case_id}/{namespace}"
                )
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


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        (
            "duplicate ID",
            lambda text: text.replace("### EDGE-BIND-002 —", "### EDGE-BIND-001 —", 1),
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
                "Proof: [`test_function_local_import_routes_bind_calls_and_loads_"
                "at_source_positions`]"
                "(../../tests/language_interpreter/python/test_interpreter.py); "
                "the natural fixture reaches the public Python analyzer and asserts "
                "both edge kinds and no unresolved use.\n",
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
                "[`test_function_local_import_routes_bind_calls_and_loads_at_source_positions`]",
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
