from __future__ import annotations

from dataclasses import replace

import pytest

import minotaur.query.correspondence as correspondence
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Evidence
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node
from minotaur.graph_model.provenance import (
    CoordinateEncoding,
    IdentityBasis,
    NodeClass,
    Provenance,
)
from minotaur.graph_model.relationship import Relationship
from minotaur.graph_model.validation import IssueCode, validate_document


def _location(path: str, line: int) -> Location:
    return Location(path, Range(Position(line, 0), Position(line, 1)))


def _symbol(label: str, line: int, *, path: str = "src/a.py", kind: str = "function") -> Node:
    location = _location(path, line)
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, "python")
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.SYMBOL.value,
        symbol_kind=kind,
        location=location,
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind=kind,
        location=location,
    )


def _resource(label: str, line: int, *, kind: str | None = None) -> Node:
    location = _location("src/resource.py", line)
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, "resource")
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.RESOURCE.value,
        location=location,
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.RESOURCE,
        label=label,
        symbol_kind=kind,
        location=location,
    )


def _upstream_symbol(kind: str) -> Node:
    identity = NodeIdentity(
        IdentityBasis.UPSTREAM_IDENTIFIER,
        "python",
        upstream_identifier="upstream-1",
    )
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.SYMBOL.value,
        symbol_kind=kind,
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label="external",
        symbol_kind=kind,
    )


def _unresolved(origin: Node, text: str, line: int) -> Node:
    location = _location("src/a.py", line)
    identity = NodeIdentity(
        IdentityBasis.UNRESOLVED_REFERENCE,
        "python",
        originating_node=origin.id,
    )
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE.value,
        reference_text=text,
        location=location,
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE,
        label=text,
        reference_text=text,
        location=location,
    )


def _file(path: str) -> Node:
    identity = NodeIdentity(IdentityBasis.FILE_PATH, "files")
    node_id = compute_node_id(identity, node_class=NodeClass.FILE.value, path=path)
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.FILE,
        label=path,
        path=path,
    )


def _upstream_resource(identifier: str) -> Node:
    identity = NodeIdentity(
        IdentityBasis.UPSTREAM_IDENTIFIER,
        "resources",
        upstream_identifier=identifier,
    )
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.RESOURCE.value,
    )
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.RESOURCE,
        label=identifier,
    )


def _resource_key(key: str) -> Node:
    identity = NodeIdentity(IdentityBasis.RESOURCE_KEY, "resources", resource_key=key)
    node_id = compute_node_id(identity, node_class=NodeClass.RESOURCE.value)
    return Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.RESOURCE,
        label=key,
    )


def _relationship(source: Node, target: Node, kind: str = "references") -> Relationship:
    return Relationship(
        source=source.id,
        target=target.id,
        kind=kind,
        evidence=(Evidence(provenance=Provenance.STATIC_ANALYSIS),),
    )


def _document(*nodes: Node, relationships: tuple[Relationship, ...] = ()) -> GraphDocument:
    return GraphDocument(
        coordinate_encoding=CoordinateEncoding.UTF_8,
        nodes=tuple(nodes),
        relationships=relationships,
    )


def test_preparation_uses_validator_and_builds_complete_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _symbol("caller", 0)
    target = _symbol("callee", 1)
    edge = _relationship(source, target, "calls")
    document = _document(source, target, relationships=(edge,))
    calls: list[tuple[GraphDocument, bool, object]] = []
    real_validator = correspondence.validate_document

    def observe(document: GraphDocument, **kwargs: object) -> object:
        calls.append((document, bool(kwargs["verify_node_ids"]), kwargs.get("source_text_by_path")))
        return real_validator(document, **kwargs)

    monkeypatch.setattr(correspondence, "validate_document", observe)
    prepared = correspondence.prepare_correspondence(document)

    assert calls == [(document, True, None)]
    source_keys = [
        key for key, candidates in prepared.nodes_by_key.items() if candidates == (source,)
    ]
    assert source_keys == [correspondence.node_key(source)]
    assert len(prepared.relationships_by_key) == 1
    occurrence = next(iter(prepared.relationship_groups.values()))[0]
    assert occurrence.source is source
    assert occurrence.target is target
    assert occurrence.relationship is edge


def test_admission_rejects_digest_mismatch_before_index_publication() -> None:
    source = _symbol("caller", 0)
    invalid = replace(source, symbol_kind="method")
    document = _document(invalid)

    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare(document, side="new")

    assert [issue.code.value for issue in raised.value.report] == ["node-id-mismatch"]
    assert raised.value.report.issues[0].path == ("nodes", 0, "id")
    assert raised.value.side == "new"


def test_resource_kind_is_observation_and_does_not_change_key() -> None:
    absent = _resource("database", 0)
    present = _resource("database", 0, kind="db:table")
    assert correspondence.node_key(absent) == correspondence.node_key(present)
    assert absent.symbol_kind is None
    assert present.symbol_kind == "db:table"


def test_upstream_symbol_kind_is_required_in_correspondence_key() -> None:
    function = _upstream_symbol("function")
    method = _upstream_symbol("method")
    assert function.id == method.id
    assert correspondence.node_key(function) != correspondence.node_key(method)


def test_unresolved_occurrences_use_nested_origin_and_remain_paired() -> None:
    source = _symbol("caller", 0)
    target = _symbol("target", 1)
    unresolved_source = _unresolved(source, "Missing", 2)
    unresolved_target = _unresolved(target, "Other", 3)
    first = _relationship(unresolved_source, target)
    second = _relationship(source, unresolved_target)
    prepared = correspondence.prepare(
        _document(
            source,
            target,
            unresolved_source,
            unresolved_target,
            relationships=(first, second),
        )
    )

    assert len(prepared.relationship_groups) == 2
    unresolved_key = correspondence.node_key(
        unresolved_source,
        origin=correspondence.node_key(source),
    )
    assert prepared.origin_dependencies[unresolved_key] == correspondence.node_key(source)
    pairs = {
        (item.relationship.source, item.relationship.target)
        for group in prepared.relationship_groups.values()
        for item in group
    }
    assert pairs == {(unresolved_source.id, target.id), (source.id, unresolved_target.id)}


def test_requested_present_duplicate_reports_all_candidates_including_edgeless() -> None:
    first = _symbol("same", 0)
    second = _symbol("same", 1)
    target = _symbol("target", 2)
    edge = _relationship(first, target)
    prepared = correspondence.prepare(_document(first, second, target, relationships=(edge,)))
    requested = next(iter(prepared.relationship_groups))

    with pytest.raises(correspondence.CorrespondenceAmbiguityError) as raised:
        prepared.validate_required_keys({requested}, side="new")

    assert raised.value.side == "new"
    assert raised.value.endpoint == "source"
    assert raised.value.candidate_ids == (first.id, second.id)


def test_absent_requested_relationship_does_not_activate_ambiguity() -> None:
    first = _symbol("same", 0)
    second = _symbol("same", 1)
    target = _symbol("target", 2)
    prepared = correspondence.prepare(_document(first, second, target))
    absent = (
        correspondence.node_key(first),
        correspondence.node_key(target),
        "references",
    )
    assert prepared.validate_required_keys({absent}) is prepared


def test_participating_unresolved_origin_chain_is_eligibility_error() -> None:
    ordinary = _symbol("caller", 0)
    unresolved_origin = _unresolved(ordinary, "Outer", 1)
    chained = _unresolved(unresolved_origin, "Inner", 2)
    edge = _relationship(ordinary, chained)
    # The chain is structurally valid and the relationship kind is references;
    # comparison admission adds the approved direct-origin requirement.
    document = _document(ordinary, unresolved_origin, chained, relationships=(edge,))

    with pytest.raises(correspondence.CorrespondenceEligibilityError) as raised:
        correspondence.prepare(document)

    assert raised.value.endpoint == "target"
    assert raised.value.side == "local"
    assert raised.value.node is chained
    assert raised.value.origin is unresolved_origin


def test_exposed_lookup_state_is_deeply_immutable() -> None:
    source = _symbol("caller", 0)
    target = _symbol("callee", 1)
    prepared = correspondence.prepare(
        _document(source, target, relationships=(_relationship(source, target),))
    )
    key = correspondence.node_key(source)
    relation_key = next(iter(prepared.relationship_groups))

    with pytest.raises(TypeError):
        prepared.nodes_by_key[key] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        prepared.nodes_by_id[source.id] = target  # type: ignore[index]
    with pytest.raises(TypeError):
        prepared.origin_dependencies[key] = key  # type: ignore[index]
    assert prepared.nodes_by_key[key] == (source,)
    assert prepared.relationship_groups[relation_key][0].relationship.source == source.id


@pytest.mark.parametrize(
    ("node_factory", "expected_basis"),
    [
        (lambda: _symbol("s", 0), "source-location"),
        (lambda: _file("pkg/mod.py"), "file-path"),
        (lambda: _upstream_symbol("function"), "upstream-identifier"),
        (lambda: _upstream_resource("resource-1"), "upstream-identifier"),
        (lambda: _resource_key("resource-1"), "resource-key"),
    ],
)
def test_each_ordinary_identity_basis_has_a_complete_structured_key(
    node_factory: object,
    expected_basis: str,
) -> None:
    node = node_factory()  # type: ignore[operator]
    key = correspondence.node_key(node)
    assert key[0] == expected_basis
    assert key[1] == node.node_class.value
    assert key[2] == node.identity.namespace


def test_key_uses_location_path_over_shadowed_node_path() -> None:
    node = _symbol("s", 0)
    shadowed = replace(node, path="other.py")
    assert correspondence.node_key(node) == correspondence.node_key(shadowed)
    moved = replace(node, location=_location("moved.py", 0))
    assert correspondence.node_key(node) != correspondence.node_key(moved)


def test_labels_and_separators_remain_structured_and_orderable() -> None:
    first = _symbol("a|b", 0, path="pkg/a|b.py")
    second = _symbol("a", 0, path="pkg/a.py")
    assert correspondence.node_key(first) != correspondence.node_key(second)
    prepared = correspondence.prepare(_document(first, second))
    assert tuple(prepared.nodes_by_key) == tuple(
        sorted(prepared.nodes_by_key, key=correspondence._key_sort)
    )


def test_range_only_movement_changes_verified_id_but_preserves_source_key() -> None:
    first = _symbol("s", 0)
    moved_location = _location("src/a.py", 1)
    identity = first.identity
    moved = replace(
        first,
        id=compute_node_id(
            identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind=first.symbol_kind,
            location=moved_location,
        ),
        location=moved_location,
    )
    assert validate_document(_document(moved)).is_valid
    assert correspondence.node_key(first) == correspondence.node_key(moved)


def test_complete_validator_report_is_preserved_before_indexing() -> None:
    first = _symbol("first", 0)
    second = _symbol("second", 1)
    duplicate = replace(second, id=first.id)
    missing = _relationship(first, second)
    missing = replace(missing, source="node:sha256:" + "0" * 64, target="node:sha256:" + "1" * 64)
    document = _document(first, duplicate, relationships=(missing,))
    real = validate_document(document)
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare(document)
    assert raised.value.report.issues == real.issues
    assert [issue.code for issue in real] == [
        IssueCode.NODE_ID_MISMATCH,
        IssueCode.NODE_ID_DUPLICATE,
        IssueCode.RELATIONSHIP_ENDPOINT_MISSING,
        IssueCode.RELATIONSHIP_ENDPOINT_MISSING,
    ]


@pytest.mark.parametrize("kind", ["calls", "imports"])
def test_calls_and_imports_to_unresolved_target_are_admission_errors(kind: str) -> None:
    source = _symbol("s", 0)
    unresolved = _unresolved(source, "missing", 1)
    document = _document(
        source,
        unresolved,
        relationships=(_relationship(source, unresolved, kind),),
    )
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare(document)
    assert [issue.code for issue in raised.value.report] == [
        IssueCode.RELATIONSHIP_UNRESOLVED_TARGET_KIND
    ]


def test_orphan_unresolved_node_is_retained_without_eligibility_error() -> None:
    source = _symbol("s", 0)
    orphan = _unresolved(source, "unused", 1)
    prepared = correspondence.prepare(_document(source, orphan))
    assert prepared.nodes_by_id[orphan.id] is orphan
    orphan_key = correspondence.node_key(orphan, origin=correspondence.node_key(source))
    assert orphan_key not in prepared.nodes_by_key


def test_requested_duplicate_target_reports_target_candidates() -> None:
    source = _symbol("source", 0)
    first = _symbol("target", 1)
    second = _symbol("target", 2)
    edge = _relationship(source, first)
    prepared = correspondence.prepare(_document(source, first, second, relationships=(edge,)))
    with pytest.raises(correspondence.CorrespondenceAmbiguityError) as raised:
        prepared.require({next(iter(prepared.relationship_groups))}, side="old")
    assert raised.value.endpoint == "target"
    assert raised.value.candidate_ids == tuple(node.id for node in (first, second))


def test_prepare_is_fresh_when_a_reused_id_gets_a_new_label() -> None:
    first = _symbol("first", 0)
    changed = replace(first, label="second")
    before = correspondence.prepare(_document(first))
    after = correspondence.prepare(_document(changed))
    assert correspondence.node_key(first) in before.nodes_by_key
    assert correspondence.node_key(changed) in after.nodes_by_key
    assert before.nodes_by_key != after.nodes_by_key
