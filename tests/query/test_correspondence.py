from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace

import orjson
import pytest

import minotaur.query.correspondence as correspondence
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Evidence
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.loading import GraphLoadError, LoadedGraph, load_graph_bytes
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


def _full_load(document: GraphDocument) -> LoadedGraph:
    """Run the real full byte loader before a positive correspondence proof."""
    return load_graph_bytes(orjson.dumps(document.to_dict()))


def _prepare_after_full_load(document: GraphDocument, *, side: str = "local"):
    """Prepare a fixture only after the real full loader has accepted its bytes."""
    return correspondence.prepare_correspondence(_full_load(document).document, side=side)


def _assert_invalid_loader_parity(
    document: GraphDocument,
    expected_codes: tuple[IssueCode, ...],
) -> None:
    """Compare direct admission with full and trusted loader semantic rejection."""
    report = validate_document(document)
    assert tuple(issue.code for issue in report) == expected_codes
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as direct_error:
        correspondence.prepare_correspondence(document)
    assert direct_error.value.report.issues == report.issues

    payload = orjson.dumps(document.to_dict())
    expected_loader_message = "graph semantic validation failed: " + "; ".join(
        f"{issue.json_pointer}: {issue.message}" for issue in report
    )
    with pytest.raises(GraphLoadError) as full_error:
        load_graph_bytes(payload)
    with pytest.raises(GraphLoadError) as trusted_error:
        load_graph_bytes(payload, _skip_schema=True, _digest="trusted")
    assert str(full_error.value) == expected_loader_message
    assert str(trusted_error.value) == expected_loader_message


def _invalid_missing_origin() -> GraphDocument:
    fake_origin = "node:sha256:" + "a" * 64
    location = _location("src/a.py", 0)
    identity = NodeIdentity(
        IdentityBasis.UNRESOLVED_REFERENCE,
        "python",
        originating_node=fake_origin,
    )
    node = Node(
        id=compute_node_id(
            identity,
            node_class=NodeClass.UNRESOLVED_REFERENCE.value,
            reference_text="missing",
            location=location,
        ),
        identity=identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE,
        label="missing",
        reference_text="missing",
        location=location,
    )
    return _document(node)


def _invalid_missing_endpoint(endpoint: str, kind: str = "contains") -> GraphDocument:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    edge = replace(
        _relationship(source, target, kind),
        **{endpoint: "node:sha256:" + "f" * 64},
    )
    return _document(source, target, relationships=(edge,))


def _invalid_missing_source_endpoint() -> GraphDocument:
    return _invalid_missing_endpoint("source")


def _invalid_missing_target_endpoint() -> GraphDocument:
    return _invalid_missing_endpoint("target")


def _invalid_both_endpoints() -> GraphDocument:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    edge = replace(
        _relationship(source, target, "contains"),
        source="node:sha256:" + "f" * 64,
        target="node:sha256:" + "e" * 64,
    )
    return _document(source, target, relationships=(edge,))


def _invalid_duplicate_node_id() -> GraphDocument:
    source = _symbol("source", 0)
    duplicate = replace(source, label="duplicate")
    return _document(source, duplicate)


def _invalid_duplicate_relationship() -> GraphDocument:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    edge = _relationship(source, target, "references")
    return _document(source, target, relationships=(edge, edge))


def _invalid_reversed_node_range() -> GraphDocument:
    source = _symbol("source", 0)
    location = Location("src/a.py", Range(Position(2, 0), Position(1, 0)))
    node = replace(
        source,
        id=compute_node_id(
            source.identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind=source.symbol_kind,
            location=location,
        ),
        location=location,
    )
    return _document(node)


def _invalid_reversed_evidence_range() -> GraphDocument:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    location = Location("src/a.py", Range(Position(2, 0), Position(1, 0)))
    edge = Relationship(
        source.id,
        target.id,
        "references",
        (Evidence(provenance=Provenance.STATIC_ANALYSIS, locations=(location,)),),
    )
    return _document(source, target, relationships=(edge,))


def _invalid_unresolved_target(kind: str) -> GraphDocument:
    source = _symbol("source", 0)
    target = _unresolved(source, "missing", 1)
    return _document(source, target, relationships=(_relationship(source, target, kind),))


def _invalid_duplicate_attribution() -> GraphDocument:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    first = Evidence(provenance=Provenance.STATIC_ANALYSIS, locations=(_location("a.py", 1),))
    second = Evidence(provenance=Provenance.STATIC_ANALYSIS, locations=(_location("a.py", 2),))
    edge = Relationship(source.id, target.id, "references", (first, second))
    return _document(source, target, relationships=(edge,))


def _invalid_duplicate_evidence_location() -> GraphDocument:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    location = _location("a.py", 1)
    evidence = Evidence(provenance=Provenance.STATIC_ANALYSIS, locations=(location, location))
    edge = Relationship(source.id, target.id, "references", (evidence,))
    return _document(source, target, relationships=(edge,))


def test_preparation_uses_validator_and_builds_complete_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _symbol("caller", 0)
    target = _symbol("callee", 1)
    edge = _relationship(source, target, "calls")
    document = _document(source, target, relationships=(edge,))
    _full_load(document)
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
        correspondence.prepare_correspondence(document, side="new")

    assert [issue.code.value for issue in raised.value.report] == ["node-id-mismatch"]
    assert raised.value.report.issues[0].path == ("nodes", 0, "id")
    assert raised.value.side == "new"


def test_resource_kind_is_observation_and_does_not_change_key() -> None:
    absent = _resource("database", 0)
    present = _resource("database", 0, kind="db:table")
    present = replace(
        present,
        id=compute_node_id(
            present.identity,
            node_class=NodeClass.RESOURCE.value,
            symbol_kind=present.symbol_kind,
            location=present.location,
        ),
    )
    _full_load(_document(absent))
    _full_load(_document(present))
    assert correspondence.node_key(absent) == correspondence.node_key(present)
    assert absent.symbol_kind is None
    assert present.symbol_kind == "db:table"


def test_upstream_symbol_kind_is_required_in_correspondence_key() -> None:
    function = _upstream_symbol("function")
    method = _upstream_symbol("method")
    _full_load(_document(function))
    _full_load(_document(method))
    assert function.id == method.id
    assert correspondence.node_key(function) != correspondence.node_key(method)


def test_unresolved_occurrences_use_nested_origin_and_remain_paired() -> None:
    source = _symbol("caller", 0)
    target = _symbol("target", 1)
    unresolved_source = _unresolved(source, "Missing", 2)
    unresolved_target = _unresolved(target, "Other", 3)
    first = _relationship(unresolved_source, target)
    second = _relationship(source, unresolved_target)
    prepared = _prepare_after_full_load(
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
    prepared = _prepare_after_full_load(_document(first, second, target, relationships=(edge,)))
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
    prepared = _prepare_after_full_load(_document(first, second, target))
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
        _prepare_after_full_load(document)

    assert raised.value.endpoint == "target"
    assert raised.value.side == "local"
    assert raised.value.node.id == chained.id
    assert raised.value.origin.id == unresolved_origin.id


def test_exposed_lookup_state_is_deeply_immutable() -> None:
    source = replace(
        _symbol("caller", 0),
        extensions={"node": {"nested": {"value": "kept"}}},
    )
    target = _symbol("callee", 1)
    unresolved = _unresolved(source, "missing", 2)
    relationship = replace(
        _relationship(unresolved, target),
        extensions={"edge": {"nested": {"value": "kept"}}},
    )
    document = _document(source, target, unresolved, relationships=(relationship,))
    before = document.to_dict()
    prepared = _prepare_after_full_load(document)
    source_key = correspondence.node_key(source)
    unresolved_key = correspondence.node_key(unresolved, origin=source_key)
    relation_key = next(iter(prepared.relationship_groups))
    nodes_by_id_before = dict(prepared.nodes_by_id)
    nodes_by_key_before = dict(prepared.nodes_by_key)
    relationships_before = dict(prepared.relationships_by_key)
    origin_before = dict(prepared.origin_dependencies)

    def assert_unchanged() -> None:
        assert dict(prepared.nodes_by_id) == nodes_by_id_before
        assert dict(prepared.nodes_by_key) == nodes_by_key_before
        assert dict(prepared.relationships_by_key) == relationships_before
        assert dict(prepared.origin_dependencies) == origin_before
        assert prepared.validate_required_keys({relation_key}) is prepared
        assert document.to_dict() == before

    mapping_attempts = (
        (prepared.nodes_by_id, source.id, prepared.nodes_by_id[target.id]),
        (prepared.nodes_by_key, source_key, ()),
        (prepared.candidate_groups, source_key, ()),
        (prepared.relationships_by_key, relation_key, ()),
        (prepared.relationship_groups, relation_key, ()),
        (prepared.origin_dependencies, unresolved_key, source_key),
    )
    for mapping, key, value in mapping_attempts:
        with pytest.raises(TypeError):
            mapping[key] = value  # type: ignore[index]
        assert_unchanged()
        with pytest.raises(TypeError):
            del mapping[key]  # type: ignore[misc]
        assert_unchanged()

    with pytest.raises(TypeError):
        prepared.nodes_by_key[source_key][0] = prepared.nodes_by_id[source.id]  # type: ignore[index]
    assert_unchanged()
    with pytest.raises(TypeError):
        prepared.relationships_by_key[relation_key][0] = prepared.relationships_by_key[
            relation_key
        ][0]  # type: ignore[index]
    assert_unchanged()
    with pytest.raises(FrozenInstanceError):
        prepared.nodes_by_id[source.id].label = "changed"  # type: ignore[misc]
    assert_unchanged()
    with pytest.raises(FrozenInstanceError):
        prepared.relationships_by_key[relation_key][0].relationship.kind = "calls"  # type: ignore[misc]
    assert_unchanged()
    with pytest.raises(TypeError):
        prepared.nodes_by_id[source.id].extensions["node"]["nested"]["value"] = "changed"  # type: ignore[index]
    assert_unchanged()
    with pytest.raises(TypeError):
        prepared.relationships_by_key[relation_key][0].relationship.extensions["edge"]["nested"][
            "value"
        ] = "changed"  # type: ignore[index]
    assert_unchanged()


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
    _full_load(_document(node))
    key = correspondence.node_key(node)
    assert key[0] == expected_basis
    assert key[1] == node.node_class.value
    assert key[2] == node.identity.namespace


def test_key_uses_location_path_over_shadowed_node_path() -> None:
    node = _symbol("s", 0)
    shadowed = replace(node, path="other.py")
    _full_load(_document(node))
    _full_load(_document(shadowed))
    assert correspondence.node_key(node) == correspondence.node_key(shadowed)
    moved = replace(node, location=_location("moved.py", 0))
    moved = replace(
        moved,
        id=compute_node_id(
            moved.identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind=moved.symbol_kind,
            location=moved.location,
        ),
    )
    _full_load(_document(moved))
    assert correspondence.node_key(node) != correspondence.node_key(moved)


def test_labels_and_separators_remain_structured_and_orderable() -> None:
    first = _symbol("a|b", 0, path="pkg/a|b.py")
    second = _symbol("a", 0, path="pkg/a.py")
    _full_load(_document(first))
    _full_load(_document(second))
    assert correspondence.node_key(first) != correspondence.node_key(second)
    prepared = _prepare_after_full_load(_document(first, second))
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
    _full_load(_document(first))
    _full_load(_document(moved))
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
        correspondence.prepare_correspondence(document)
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
        correspondence.prepare_correspondence(document)
    assert [issue.code for issue in raised.value.report] == [
        IssueCode.RELATIONSHIP_UNRESOLVED_TARGET_KIND
    ]


def test_orphan_unresolved_node_is_retained_without_eligibility_error() -> None:
    source = _symbol("s", 0)
    orphan = _unresolved(source, "unused", 1)
    prepared = _prepare_after_full_load(_document(source, orphan))
    assert prepared.nodes_by_id[orphan.id].to_dict() == orphan.to_dict()
    orphan_key = correspondence.node_key(orphan, origin=correspondence.node_key(source))
    assert orphan_key not in prepared.nodes_by_key


def test_requested_duplicate_target_reports_target_candidates() -> None:
    source = _symbol("source", 0)
    first = _symbol("target", 1)
    second = _symbol("target", 2)
    edge = _relationship(source, first)
    prepared = _prepare_after_full_load(_document(source, first, second, relationships=(edge,)))
    with pytest.raises(correspondence.CorrespondenceAmbiguityError) as raised:
        prepared.validate_required_keys({next(iter(prepared.relationship_groups))}, side="old")
    assert raised.value.endpoint == "target"
    assert raised.value.candidate_ids == tuple(node.id for node in (first, second))


def test_prepare_is_fresh_when_a_reused_id_gets_a_new_label() -> None:
    first = _symbol("first", 0)
    changed = replace(first, label="second")
    before = _prepare_after_full_load(_document(first))
    after = _prepare_after_full_load(_document(changed))
    assert correspondence.node_key(first) in before.nodes_by_key
    assert correspondence.node_key(changed) in after.nodes_by_key
    assert before.nodes_by_key != after.nodes_by_key


def test_direct_and_full_trusted_blob_loaders_feed_the_same_preparation() -> None:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    document = _document(source, target, relationships=(_relationship(source, target),))
    payload = orjson.dumps(document.to_dict())
    full = load_graph_bytes(payload)
    trusted = load_graph_bytes(payload, _skip_schema=True, _digest="trusted")
    assert (
        correspondence.prepare_correspondence(full.document).relationships_by_key
        == correspondence.prepare_correspondence(trusted.document).relationships_by_key
    )


def test_full_loader_valid_supported_unresolved_fixture_prepares_and_groups() -> None:
    source = _symbol("source", 0)
    unresolved = _unresolved(source, "missing", 1)
    relationship = _relationship(source, unresolved, "references")
    document = _document(source, unresolved, relationships=(relationship,))
    loaded = load_graph_bytes(orjson.dumps(document.to_dict()))
    prepared = correspondence.prepare_correspondence(loaded.document)
    assert len(prepared.relationship_groups) == 1
    assert (
        next(iter(prepared.relationship_groups.values()))[0].relationship
        is loaded.document.relationships[0]
    )


def test_distinct_same_key_unresolved_sources_validate_as_occurrences() -> None:
    origin = _symbol("origin", 0)
    target = _symbol("target", 1)
    first = _unresolved(origin, "missing", 2)
    second = _unresolved(origin, "missing", 3)
    edges = (_relationship(first, target), _relationship(second, target))
    document = _document(origin, target, first, second, relationships=edges)
    loaded = load_graph_bytes(orjson.dumps(document.to_dict()))
    prepared = correspondence.prepare_correspondence(loaded.document)
    relationship_key = next(iter(prepared.relationship_groups))
    assert len(prepared.nodes_by_key[relationship_key[0]]) == 2
    assert len(prepared.relationship_groups[relationship_key]) == 2
    assert prepared.validate_required_keys({relationship_key}) is prepared


def test_distinct_same_key_unresolved_targets_validate_as_occurrences() -> None:
    source = _symbol("source", 0)
    origin = _symbol("origin", 1)
    first = _unresolved(origin, "missing", 2)
    second = _unresolved(origin, "missing", 3)
    edges = (_relationship(source, first), _relationship(source, second))
    document = _document(source, origin, first, second, relationships=edges)
    loaded = load_graph_bytes(orjson.dumps(document.to_dict()))
    prepared = correspondence.prepare_correspondence(loaded.document)
    relationship_key = next(iter(prepared.relationship_groups))
    assert len(prepared.nodes_by_key[relationship_key[1]]) == 2
    assert len(prepared.relationship_groups[relationship_key]) == 2
    assert prepared.validate_required_keys({relationship_key}) is prepared


def test_distinct_unresolved_occurrences_still_fail_for_duplicate_origins() -> None:
    first_origin = _symbol("origin", 0)
    second_origin = _symbol("origin", 1)
    target = _symbol("target", 2)
    occurrence = _unresolved(first_origin, "missing", 3)
    edge = _relationship(occurrence, target)
    document = _document(first_origin, second_origin, target, occurrence, relationships=(edge,))
    loaded = load_graph_bytes(orjson.dumps(document.to_dict()))
    prepared = correspondence.prepare_correspondence(loaded.document)
    relationship_key = next(iter(prepared.relationship_groups))
    with pytest.raises(correspondence.CorrespondenceAmbiguityError) as raised:
        prepared.validate_required_keys({relationship_key})
    assert raised.value.origin is True


def test_trusted_loader_does_not_bypass_comparison_id_verification() -> None:
    source = _symbol("source", 0)
    altered = replace(source, symbol_kind="method")
    document = _document(altered)
    payload = orjson.dumps(document.to_dict())
    with pytest.raises(GraphLoadError) as full_error:
        load_graph_bytes(payload)
    loaded = load_graph_bytes(payload, _skip_schema=True, _digest="trusted")
    report = validate_document(loaded.document)
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as direct_error:
        correspondence.prepare_correspondence(document, side="new")
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare_correspondence(loaded.document, side="new")
    assert raised.value.report.issues == report.issues
    assert direct_error.value.report.issues == report.issues
    assert raised.value.side == "new"
    assert direct_error.value.side == raised.value.side
    assert tuple(issue.code for issue in raised.value.report) == (IssueCode.NODE_ID_MISMATCH,)
    assert raised.value.report.issues[0].path == ("nodes", 0, "id")
    assert raised.value.report.issues[0].message in str(full_error.value)
    assert str(full_error.value).startswith("graph semantic validation failed: /nodes/0/id:")

    valid_payload = orjson.dumps(_document(source).to_dict())
    valid_full = load_graph_bytes(valid_payload)
    valid_trusted = load_graph_bytes(valid_payload, _skip_schema=True, _digest="trusted")
    valid_full_prepared = correspondence.prepare_correspondence(valid_full.document, side="new")
    valid_trusted_prepared = correspondence.prepare_correspondence(
        valid_trusted.document, side="new"
    )
    assert valid_full_prepared.nodes_by_id == {source.id: valid_full.document.nodes[0]}
    assert valid_trusted_prepared.nodes_by_id == {source.id: valid_trusted.document.nodes[0]}
    assert valid_full_prepared.nodes_by_key == valid_trusted_prepared.nodes_by_key
    assert (
        valid_full_prepared.relationship_groups == valid_trusted_prepared.relationship_groups == {}
    )


@pytest.mark.parametrize(
    ("case_factory", "expected_codes"),
    [
        pytest.param(
            _invalid_missing_origin,
            (IssueCode.IDENTITY_ORIGIN_MISSING,),
            id="missing-origin",
        ),
        pytest.param(
            _invalid_missing_source_endpoint,
            (IssueCode.RELATIONSHIP_ENDPOINT_MISSING,),
            id="missing-source-endpoint-unsupported-edge",
        ),
        pytest.param(
            _invalid_missing_target_endpoint,
            (IssueCode.RELATIONSHIP_ENDPOINT_MISSING,),
            id="missing-target-endpoint-unsupported-edge",
        ),
        pytest.param(
            _invalid_both_endpoints,
            (IssueCode.RELATIONSHIP_ENDPOINT_MISSING, IssueCode.RELATIONSHIP_ENDPOINT_MISSING),
            id="missing-both-endpoints-unsupported-edge",
        ),
        pytest.param(
            _invalid_duplicate_node_id,
            (IssueCode.NODE_ID_DUPLICATE,),
            id="duplicate-valid-node-id",
        ),
        pytest.param(
            _invalid_duplicate_relationship,
            (IssueCode.RELATIONSHIP_DUPLICATE,),
            id="duplicate-relationship-tuple",
        ),
        pytest.param(
            _invalid_reversed_node_range,
            (IssueCode.RANGE_END_BEFORE_START,),
            id="reversed-node-range",
        ),
        pytest.param(
            _invalid_reversed_evidence_range,
            (IssueCode.RANGE_END_BEFORE_START,),
            id="reversed-evidence-range",
        ),
        pytest.param(
            lambda: _invalid_unresolved_target("calls"),
            (IssueCode.RELATIONSHIP_UNRESOLVED_TARGET_KIND,),
            id="unresolved-target-calls",
        ),
        pytest.param(
            lambda: _invalid_unresolved_target("imports"),
            (IssueCode.RELATIONSHIP_UNRESOLVED_TARGET_KIND,),
            id="unresolved-target-imports",
        ),
        pytest.param(
            lambda: _invalid_unresolved_target("contains"),
            (IssueCode.RELATIONSHIP_UNRESOLVED_TARGET_KIND,),
            id="unresolved-target-core-extension",
        ),
        pytest.param(
            lambda: _invalid_unresolved_target("python:decorates"),
            (IssueCode.RELATIONSHIP_UNRESOLVED_TARGET_KIND,),
            id="unresolved-target-extension",
        ),
        pytest.param(
            _invalid_duplicate_attribution,
            (IssueCode.EVIDENCE_DUPLICATE,),
            id="duplicate-evidence-attribution",
        ),
        pytest.param(
            _invalid_duplicate_evidence_location,
            (IssueCode.EVIDENCE_LOCATION_DUPLICATE,),
            id="duplicate-evidence-location",
        ),
    ],
)
def test_invalid_full_and_trusted_loaders_match_direct_preparation(
    case_factory: Callable[[], GraphDocument],
    expected_codes: tuple[IssueCode, ...],
) -> None:
    document = case_factory()
    _assert_invalid_loader_parity(document, expected_codes)


def test_unverifiable_digest_issue_from_real_validator_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _symbol("source", 0)
    document = _document(source)
    import minotaur.graph_model.validation as validation

    def fail(*args: object, **kwargs: object) -> bool:
        raise ValueError("test digest dependency failure")

    monkeypatch.setattr(validation, "verify_node_id", fail)
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare_correspondence(document)
    assert [issue.code for issue in raised.value.report] == [IssueCode.NODE_ID_UNVERIFIABLE]


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_missing_source_and_target_endpoints_keep_exact_validator_pointer(endpoint: str) -> None:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    edge = _relationship(source, target)
    edge = replace(edge, **{endpoint: "node:sha256:" + "f" * 64})
    report = validate_document(_document(source, target, relationships=(edge,)))
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare_correspondence(_document(source, target, relationships=(edge,)))
    assert raised.value.report.issues == report.issues
    assert report.issues[0].path == ("relationships", 0, endpoint)


def test_reversed_evidence_range_and_duplicate_evidence_reports_are_admission_errors() -> None:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    reversed_location = Location("src/a.py", Range(Position(2, 0), Position(1, 0)))
    evidence = Evidence(provenance=Provenance.STATIC_ANALYSIS, locations=(reversed_location,))
    duplicate = Relationship(
        source=source.id,
        target=target.id,
        kind="references",
        evidence=(evidence, evidence),
    )
    report = validate_document(_document(source, target, relationships=(duplicate,)))
    assert [issue.code for issue in report] == [
        IssueCode.RANGE_END_BEFORE_START,
        IssueCode.EVIDENCE_DUPLICATE,
        IssueCode.RANGE_END_BEFORE_START,
    ]
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare_correspondence(_document(source, target, relationships=(duplicate,)))
    assert raised.value.report.issues == report.issues


def test_duplicate_evidence_location_is_rejected() -> None:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    location = _location("src/a.py", 2)
    repeated = Evidence(provenance=Provenance.STATIC_ANALYSIS, locations=(location, location))
    edge = Relationship(source.id, target.id, "references", (repeated,))
    document = _document(source, target, relationships=(edge,))
    report = validate_document(document)
    assert report.issues[0].code == IssueCode.EVIDENCE_LOCATION_DUPLICATE
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare_correspondence(document, side="new")
    assert raised.value.report.issues == report.issues
    assert raised.value.side == "new"
    assert raised.value.report.issues[0].message == (
        "location duplicates an earlier location on the same evidence record"
    )
    assert raised.value.report.issues[0].path == ("relationships", 0, "evidence", 0, "locations", 1)


@pytest.mark.parametrize("kind", ["references", "calls", "imports"])
def test_unresolved_source_participation_requires_direct_ordinary_origin(kind: str) -> None:
    ordinary = _symbol("ordinary", 0)
    origin = _unresolved(ordinary, "outer", 1)
    unresolved = _unresolved(origin, "missing", 2)
    relationship = _relationship(unresolved, ordinary, kind)
    document = _document(ordinary, origin, unresolved, relationships=(relationship,))
    with pytest.raises(correspondence.CorrespondenceEligibilityError) as raised:
        _prepare_after_full_load(document)
    assert raised.value.endpoint == "source"
    assert raised.value.node.id == unresolved.id
    assert raised.value.origin.id == origin.id


def test_same_unresolved_endpoint_in_multiple_supported_relationships_is_one_candidate() -> None:
    source = _symbol("source", 0)
    first_target = _symbol("first", 1)
    second_target = _symbol("second", 2)
    unresolved = _unresolved(source, "missing", 3)
    first = _relationship(unresolved, first_target, "references")
    second = _relationship(unresolved, second_target, "calls")
    prepared = _prepare_after_full_load(
        _document(source, first_target, second_target, unresolved, relationships=(first, second))
    )
    unresolved_key = correspondence.node_key(unresolved, origin=correspondence.node_key(source))
    assert tuple(node.id for node in prepared.nodes_by_key[unresolved_key]) == (unresolved.id,)
    for relationship_key in prepared.relationship_groups:
        assert prepared.validate_required_keys({relationship_key}) is prepared


def test_unsupported_relationships_never_enter_groups_when_endpoints_are_indexed() -> None:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    supported = _relationship(source, target, "calls")
    unsupported = _relationship(source, target, "contains")
    prepared = _prepare_after_full_load(
        _document(source, target, relationships=(supported, unsupported))
    )
    assert len(prepared.relationship_groups) == 1
    assert (
        next(iter(prepared.relationship_groups.values()))[0].relationship.to_dict()
        == supported.to_dict()
    )


def test_multiple_eligibility_errors_are_canonical_across_relationship_permutations() -> None:
    ordinary = _symbol("ordinary", 0)
    first_origin = _unresolved(ordinary, "first-origin", 1)
    second_origin = _unresolved(ordinary, "second-origin", 2)
    first_chain = _unresolved(first_origin, "first-chain", 3)
    second_chain = _unresolved(second_origin, "second-chain", 4)
    first_edge = _relationship(first_chain, ordinary, "references")
    second_edge = _relationship(second_chain, ordinary, "calls")
    nodes = (ordinary, first_origin, second_origin, first_chain, second_chain)
    first_document = _document(*nodes, relationships=(first_edge, second_edge))
    second_document = _document(*reversed(nodes), relationships=(second_edge, first_edge))
    with pytest.raises(correspondence.CorrespondenceEligibilityError) as first_error:
        _prepare_after_full_load(first_document, side="new")
    with pytest.raises(correspondence.CorrespondenceEligibilityError) as second_error:
        _prepare_after_full_load(second_document, side="new")
    assert (first_error.value.endpoint, first_error.value.node.id, first_error.value.side) == (
        second_error.value.endpoint,
        second_error.value.node.id,
        "new",
    )


def test_unsupported_only_unresolved_source_is_retained() -> None:
    ordinary = _symbol("ordinary", 0)
    unresolved = _unresolved(ordinary, "missing", 1)
    relationship = _relationship(unresolved, ordinary, "contains")
    prepared = _prepare_after_full_load(
        _document(ordinary, unresolved, relationships=(relationship,))
    )
    assert prepared.nodes_by_id[unresolved.id].to_dict() == unresolved.to_dict()
    assert not prepared.relationship_groups


def test_unresolved_target_chain_is_rejected_only_for_supported_references() -> None:
    ordinary = _symbol("ordinary", 0)
    origin = _unresolved(ordinary, "outer", 1)
    chained = _unresolved(origin, "inner", 2)
    edge = _relationship(ordinary, chained, "references")
    document = _document(ordinary, origin, chained, relationships=(edge,))
    with pytest.raises(correspondence.CorrespondenceEligibilityError) as raised:
        _prepare_after_full_load(document, side="old")
    assert raised.value.endpoint == "target"
    assert raised.value.side == "old"


@pytest.mark.parametrize("kind", ["contains", "inherits", "implements", "python:decorates"])
def test_unsupported_unresolved_target_keeps_validator_rule(kind: str) -> None:
    ordinary = _symbol("ordinary", 0)
    unresolved = _unresolved(ordinary, "missing", 1)
    edge = _relationship(ordinary, unresolved, kind)
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare_correspondence(
            _document(ordinary, unresolved, relationships=(edge,))
        )
    assert raised.value.report.issues[0].code == IssueCode.RELATIONSHIP_UNRESOLVED_TARGET_KIND


def test_duplicate_origin_candidates_activate_only_when_local_relationship_is_present() -> None:
    first = _symbol("origin", 0)
    second = _symbol("origin", 1)
    unresolved = _unresolved(first, "missing", 2)
    absent = _prepare_after_full_load(_document(first, second, unresolved))
    assert (
        absent.validate_required_keys(
            {(correspondence.node_key(first), correspondence.node_key(second), "references")}
        )
        is absent
    )

    source = _symbol("source", 3)
    edge = _relationship(source, unresolved, "references")
    present = _prepare_after_full_load(
        _document(first, second, unresolved, source, relationships=(edge,))
    )
    with pytest.raises(correspondence.CorrespondenceAmbiguityError) as raised:
        present.validate_required_keys({next(iter(present.relationship_groups))})
    assert raised.value.origin is True


def test_preparation_does_not_open_files_or_mutate_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _symbol("source", 0)
    document = _document(source)
    _full_load(document)
    before = document.to_dict()

    def fail_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("correspondence preparation must not read files")

    monkeypatch.setattr("builtins.open", fail_open)
    correspondence.prepare_correspondence(document)
    assert document.to_dict() == before


def test_missing_unresolved_origin_is_admission_error_with_exact_pointer() -> None:
    fake_origin = "node:sha256:" + "a" * 64
    location = _location("src/a.py", 0)
    identity = NodeIdentity(
        IdentityBasis.UNRESOLVED_REFERENCE,
        "python",
        originating_node=fake_origin,
    )
    node_id = compute_node_id(
        identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE.value,
        reference_text="missing",
        location=location,
    )
    unresolved = Node(
        id=node_id,
        identity=identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE,
        label="missing",
        reference_text="missing",
        location=location,
    )
    document = _document(unresolved)
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare_correspondence(document)
    assert raised.value.report.issues[0].code == IssueCode.IDENTITY_ORIGIN_MISSING
    assert raised.value.report.issues[0].path == ("nodes", 0, "identity", "originating_node")


def test_duplicate_relationship_tuple_is_rejected_after_valid_evidence() -> None:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    first = _relationship(source, target, "references")
    second = _relationship(source, target, "references")
    document = _document(source, target, relationships=(first, second))
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare_correspondence(document)
    assert raised.value.report.issues[0].code == IssueCode.RELATIONSHIP_DUPLICATE
    assert raised.value.report.issues[0].path == ("relationships", 1)


def test_extreme_location_without_source_text_is_still_admitted() -> None:
    source = _symbol("source", 900)
    document = _document(source)
    prepared = _prepare_after_full_load(document)
    assert tuple(node.id for node in prepared.nodes_by_key[correspondence.node_key(source)]) == (
        source.id,
    )


def test_empty_requested_set_still_checks_graph_admission_and_chain_eligibility() -> None:
    ordinary = _symbol("ordinary", 0)
    origin = _unresolved(ordinary, "outer", 1)
    chained = _unresolved(origin, "inner", 2)
    edge = _relationship(ordinary, chained, "references")
    document = _document(ordinary, origin, chained, relationships=(edge,))
    with pytest.raises(correspondence.CorrespondenceEligibilityError) as raised:
        _prepare_after_full_load(document).validate_required_keys(set())
    assert raised.value.endpoint == "target"
    assert raised.value.node.id == chained.id
    assert raised.value.origin.id == origin.id


def test_same_key_unresolved_edges_keep_two_exact_pairs_without_cartesian_product() -> None:
    source_origin = _symbol("source-origin", 0)
    target_origin = _symbol("target-origin", 1)
    source_one = _unresolved(source_origin, "missing-source", 2)
    source_two = _unresolved(source_origin, "missing-source", 3)
    target_one = _unresolved(target_origin, "missing-target", 4)
    target_two = _unresolved(target_origin, "missing-target", 5)
    first = _relationship(source_one, target_one, "references")
    second = _relationship(source_two, target_two, "references")
    nodes = (source_origin, target_origin, source_one, source_two, target_one, target_two)
    document = _document(*nodes, relationships=(first, second))
    reordered = _document(*reversed(nodes), relationships=(second, first))
    prepared = _prepare_after_full_load(document)
    permuted = _prepare_after_full_load(reordered)
    groups = list(prepared.relationship_groups.values())
    assert len(groups) == 1
    pairs = {(item.relationship.source, item.relationship.target) for item in groups[0]}
    assert pairs == {(source_one.id, target_one.id), (source_two.id, target_two.id)}
    assert prepared.validate_required_keys({next(iter(prepared.relationship_groups))}) is prepared
    assert tuple(
        (item.relationship.source, item.relationship.target)
        for item in next(iter(permuted.relationship_groups.values()))
    ) == tuple((item.relationship.source, item.relationship.target) for item in groups[0])


def test_input_serialization_survives_admission_and_eligibility_errors() -> None:
    ordinary = _symbol("ordinary", 0)
    unresolved_origin = _unresolved(ordinary, "outer", 1)
    chained = _unresolved(unresolved_origin, "inner", 2)
    edge = _relationship(ordinary, chained)
    document = _document(ordinary, unresolved_origin, chained, relationships=(edge,))
    before = document.to_dict()
    _full_load(document)
    with pytest.raises(correspondence.CorrespondenceEligibilityError) as raised:
        _prepare_after_full_load(document)
    assert document.to_dict() == before
    assert raised.value.endpoint == "target"
    assert raised.value.node.id == chained.id
    assert raised.value.origin.id == unresolved_origin.id


def test_resource_kind_is_excluded_for_every_resource_basis() -> None:
    source = _resource("resource", 0)
    source_kind = replace(source, symbol_kind="db:table")
    upstream = _upstream_resource("upstream")
    upstream_kind = replace(upstream, symbol_kind="db:table")
    keyed = _resource_key("keyed")
    keyed_kind = replace(keyed, symbol_kind="db:table")
    source_kind = replace(
        source_kind,
        id=compute_node_id(
            source_kind.identity,
            node_class=NodeClass.RESOURCE.value,
            symbol_kind=source_kind.symbol_kind,
            location=source_kind.location,
        ),
    )
    for node in (source, source_kind, upstream, upstream_kind, keyed, keyed_kind):
        _full_load(_document(node))
    assert correspondence.node_key(source) == correspondence.node_key(source_kind)
    assert correspondence.node_key(upstream) == correspondence.node_key(upstream_kind)
    assert correspondence.node_key(keyed) == correspondence.node_key(keyed_kind)


def test_source_symbol_kind_and_supported_relationship_kind_are_key_bearing() -> None:
    function = _symbol("same", 0, kind="function")
    method = _symbol("same", 0, kind="method")
    extension = _symbol("same", 0, kind="python:callable")
    for node in (function, method, extension):
        _full_load(_document(node))
    assert correspondence.node_key(function) != correspondence.node_key(method)
    assert correspondence.node_key(method) != correspondence.node_key(extension)

    target = _symbol("target", 1)
    relationships = tuple(
        _relationship(function, target, kind) for kind in ("calls", "references", "imports")
    )
    prepared = _prepare_after_full_load(_document(function, target, relationships=relationships))
    assert {key[2] for key in prepared.relationship_groups} == {
        "calls",
        "references",
        "imports",
    }


def test_source_location_resource_candidates_remain_ambiguous_when_kind_changes() -> None:
    first = _resource("database", 0, kind="db:table")
    second = _resource("database", 1, kind="db:view")
    first = replace(
        first,
        id=compute_node_id(
            first.identity,
            node_class=NodeClass.RESOURCE.value,
            symbol_kind=first.symbol_kind,
            location=first.location,
        ),
    )
    second = replace(
        second,
        id=compute_node_id(
            second.identity,
            node_class=NodeClass.RESOURCE.value,
            symbol_kind=second.symbol_kind,
            location=second.location,
        ),
    )
    target = _symbol("target", 2)
    document = _document(first, second, target, relationships=(_relationship(first, target),))
    loaded = load_graph_bytes(orjson.dumps(document.to_dict()))
    prepared = correspondence.prepare_correspondence(loaded.document)
    key = correspondence.node_key(first)
    assert first.id != second.id
    assert tuple(node.id for node in prepared.nodes_by_key[key]) == (first.id, second.id)
    relationship_key = next(iter(prepared.relationship_groups))
    with pytest.raises(correspondence.CorrespondenceAmbiguityError) as raised:
        prepared.validate_required_keys({relationship_key})
    assert raised.value.endpoint == "source"
    assert raised.value.candidate_ids == (first.id, second.id)


def test_unresolved_derived_file_uses_location_path_then_node_path_then_absence() -> None:
    origin = _symbol("origin", 0)
    located = _unresolved(origin, "missing", 1)
    shadowed = replace(located, path="shadowed.py")
    fallback_identity = NodeIdentity(
        IdentityBasis.UNRESOLVED_REFERENCE,
        "python",
        originating_node=origin.id,
    )
    fallback_id = compute_node_id(
        fallback_identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE.value,
        reference_text="missing",
        location=None,
    )
    fallback = replace(located, id=fallback_id, location=None, path="fallback.py")
    absent = replace(fallback, id=fallback_id, path=None)
    _full_load(_document(origin))
    for node in (located, fallback, absent):
        _full_load(_document(origin, node, relationships=(_relationship(origin, node),)))
    origin_key = correspondence.node_key(origin)
    assert correspondence.node_key(located, origin=origin_key)[4:] == ("missing", "src/a.py")
    assert correspondence.node_key(shadowed, origin=origin_key)[4:] == ("missing", "src/a.py")
    assert correspondence.node_key(fallback, origin=origin_key)[4:] == ("missing", "fallback.py")
    assert correspondence.node_key(absent, origin=origin_key)[4:] == ("missing", None)
    for node in (located, fallback, absent):
        edge = _relationship(origin, node, "references")
        loaded = load_graph_bytes(
            orjson.dumps(_document(origin, node, relationships=(edge,)).to_dict())
        )
        assert correspondence.prepare_correspondence(loaded.document).relationship_groups


def test_range_only_movement_updates_ids_and_unresolved_descendants_transitively() -> None:
    origin = _symbol("origin", 0)
    unresolved = _unresolved(origin, "missing", 1)
    first_edge = _relationship(origin, unresolved, "references")
    first_document = _document(origin, unresolved, relationships=(first_edge,))

    moved_origin = _symbol("origin", 2)
    moved_unresolved = _unresolved(moved_origin, "missing", 3)
    moved_edge = _relationship(moved_origin, moved_unresolved, "references")
    moved_document = _document(moved_origin, moved_unresolved, relationships=(moved_edge,))
    first_loaded = load_graph_bytes(orjson.dumps(first_document.to_dict()))
    moved_loaded = load_graph_bytes(orjson.dumps(moved_document.to_dict()))
    first = correspondence.prepare_correspondence(first_loaded.document)
    moved = correspondence.prepare_correspondence(moved_loaded.document)
    assert origin.id != moved_origin.id
    assert unresolved.id != moved_unresolved.id
    assert tuple(first.relationship_groups) == tuple(moved.relationship_groups)
    assert tuple(first.nodes_by_key) == tuple(moved.nodes_by_key)


def test_neutral_observations_and_nested_extensions_are_preserved_without_key_changes() -> None:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    evidence = Evidence(
        provenance=Provenance.STATIC_ANALYSIS,
        locations=(_location("src/a.py", 4),),
        extensions={"tool": {"nested": {"value": "kept"}}},
    )
    relationship = Relationship(
        source.id,
        target.id,
        "references",
        (evidence,),
        extensions={"edge": {"nested": {"value": "kept"}}},
    )
    observed = replace(
        source,
        language="python",
        expected_symbol_kind="function",
        extensions={"node": {"nested": {"value": "kept"}}},
    )
    observed_relationship = replace(relationship, source=observed.id)
    document = _document(observed, target, relationships=(observed_relationship,))
    prepared = _prepare_after_full_load(document)
    assert correspondence.node_key(source) == correspondence.node_key(observed)
    assert prepared.document.to_dict() == document.to_dict()
    occurrence = next(iter(next(iter(prepared.relationship_groups.values()))))
    assert occurrence.relationship.to_dict() == observed_relationship.to_dict()
    with pytest.raises(TypeError):
        occurrence.relationship.extensions["edge"]["nested"]["value"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        occurrence.source.extensions["node"]["nested"]["value"] = "changed"  # type: ignore[index]


def test_ambiguity_failure_preserves_input_and_candidate_sites() -> None:
    first = _symbol("same", 0)
    second = _symbol("same", 1)
    target = _symbol("target", 2)
    edge = _relationship(first, target)
    document = _document(first, second, target, relationships=(edge,))
    before = document.to_dict()
    prepared = _prepare_after_full_load(document)
    key = next(iter(prepared.relationship_groups))
    with pytest.raises(correspondence.CorrespondenceAmbiguityError) as raised:
        prepared.validate_required_keys({key}, side="old")
    assert raised.value.side == "old"
    assert raised.value.candidate_ids == (first.id, second.id)
    assert document.to_dict() == before


def test_distinct_evidence_and_paired_occurrences_are_retained() -> None:
    source_origin = _symbol("source", 0)
    target_origin = _symbol("target", 1)
    source_occurrence = _unresolved(source_origin, "missing-source", 2)
    target_occurrence = _unresolved(target_origin, "missing-target", 3)
    first_evidence = Evidence(
        provenance=Provenance.STATIC_ANALYSIS,
        locations=(_location("src/a.py", 5), _location("src/a.py", 6)),
    )
    second_evidence = Evidence(
        provenance=Provenance.IMPORTED_GRAPH,
        locations=(_location("src/a.py", 5),),
    )
    relationship = Relationship(
        source_occurrence.id,
        target_occurrence.id,
        "references",
        (first_evidence, second_evidence),
        extensions={"edge": {"source": "paired"}},
    )
    document = _document(
        source_origin,
        target_origin,
        source_occurrence,
        target_occurrence,
        relationships=(relationship,),
    )
    loaded = load_graph_bytes(orjson.dumps(document.to_dict()))
    prepared = correspondence.prepare_correspondence(loaded.document)
    occurrence = next(iter(next(iter(prepared.relationship_groups.values()))))
    assert occurrence.relationship.source == source_occurrence.id
    assert occurrence.relationship.target == target_occurrence.id
    assert occurrence.relationship.evidence == relationship.evidence
    assert occurrence.relationship.extensions == relationship.extensions


def test_valid_duplicate_id_and_reversed_node_range_are_admission_findings() -> None:
    source = _symbol("source", 0)
    duplicate = _document(source, source)
    duplicate_report = validate_document(duplicate)
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as duplicate_error:
        correspondence.prepare_correspondence(duplicate)
    assert duplicate_error.value.report.issues == duplicate_report.issues
    assert duplicate_report.issues[0].code == IssueCode.NODE_ID_DUPLICATE
    assert duplicate_report.issues[0].path == ("nodes", 1, "id")

    reversed_location = Location("src/a.py", Range(Position(2, 0), Position(1, 0)))
    reversed_node = replace(
        source,
        id=compute_node_id(
            source.identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind=source.symbol_kind,
            location=reversed_location,
        ),
        location=reversed_location,
    )
    reversed_document = _document(reversed_node)
    reversed_report = validate_document(reversed_document)
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as reversed_error:
        correspondence.prepare_correspondence(reversed_document)
    assert reversed_error.value.report.issues == reversed_report.issues
    assert reversed_report.issues[0].code == IssueCode.RANGE_END_BEFORE_START
    assert reversed_report.issues[0].path == ("nodes", 0, "location", "range")


def test_absent_unresolved_request_does_not_activate_duplicate_origin_candidates() -> None:
    first = _symbol("origin", 0)
    second = _symbol("origin", 1)
    target = _symbol("target", 2)
    unresolved = _unresolved(first, "missing", 3)
    prepared = _prepare_after_full_load(_document(first, second, target, unresolved))
    unresolved_key = correspondence.node_key(unresolved, origin=correspondence.node_key(first))
    absent = (unresolved_key, correspondence.node_key(target), "references")
    assert prepared.validate_required_keys({absent}, side="right") is prepared


def test_invalid_input_report_is_exact_and_permutation_specific() -> None:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    first = replace(_relationship(source, target), source="node:sha256:" + "a" * 64)
    second = replace(_relationship(source, target), target="node:sha256:" + "b" * 64)
    document = _document(source, target, relationships=(first, second))
    report = validate_document(document)
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as raised:
        correspondence.prepare_correspondence(document)
    assert raised.value.report.issues == report.issues
    assert raised.value.report.issues == validate_document(document).issues

    permuted = _document(source, target, relationships=(second, first))
    permuted_report = validate_document(permuted)
    with pytest.raises(correspondence.CorrespondenceAdmissionError) as permuted_error:
        correspondence.prepare_correspondence(permuted)
    assert permuted_error.value.report.issues == permuted_report.issues
    assert permuted_error.value.report.issues != report.issues


def test_same_evidence_site_is_retained_across_distinct_valid_relationships() -> None:
    source = _symbol("source", 0)
    first_target = _symbol("first", 1)
    second_target = _symbol("second", 2)
    site = _location("src/a.py", 4)
    evidence = Evidence(provenance=Provenance.STATIC_ANALYSIS, locations=(site,))
    first = Relationship(source.id, first_target.id, "references", (evidence,))
    second = Relationship(source.id, second_target.id, "references", (evidence,))
    prepared = _prepare_after_full_load(
        _document(source, first_target, second_target, relationships=(first, second))
    )
    occurrences = [item for group in prepared.relationship_groups.values() for item in group]
    assert len(occurrences) == 2
    assert all(item.relationship.evidence[0].locations == (site,) for item in occurrences)
