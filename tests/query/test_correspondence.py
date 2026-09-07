from __future__ import annotations

from dataclasses import replace

import orjson
import pytest

import minotaur.query.correspondence as correspondence
from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.evidence import Evidence
from minotaur.graph_model.identity import NodeIdentity, compute_node_id
from minotaur.graph_model.loading import load_graph_bytes
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
        correspondence.prepare_correspondence(document, side="new")

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
    prepared = correspondence.prepare_correspondence(
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
    prepared = correspondence.prepare_correspondence(
        _document(first, second, target, relationships=(edge,))
    )
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
    prepared = correspondence.prepare_correspondence(_document(first, second, target))
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
        correspondence.prepare_correspondence(document)

    assert raised.value.endpoint == "target"
    assert raised.value.side == "local"
    assert raised.value.node is chained
    assert raised.value.origin is unresolved_origin


def test_exposed_lookup_state_is_deeply_immutable() -> None:
    source = _symbol("caller", 0)
    target = _symbol("callee", 1)
    prepared = correspondence.prepare_correspondence(
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
    prepared = correspondence.prepare_correspondence(_document(first, second))
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
    prepared = correspondence.prepare_correspondence(_document(source, orphan))
    assert prepared.nodes_by_id[orphan.id] is orphan
    orphan_key = correspondence.node_key(orphan, origin=correspondence.node_key(source))
    assert orphan_key not in prepared.nodes_by_key


def test_requested_duplicate_target_reports_target_candidates() -> None:
    source = _symbol("source", 0)
    first = _symbol("target", 1)
    second = _symbol("target", 2)
    edge = _relationship(source, first)
    prepared = correspondence.prepare_correspondence(
        _document(source, first, second, relationships=(edge,))
    )
    with pytest.raises(correspondence.CorrespondenceAmbiguityError) as raised:
        prepared.validate_required_keys({next(iter(prepared.relationship_groups))}, side="old")
    assert raised.value.endpoint == "target"
    assert raised.value.candidate_ids == tuple(node.id for node in (first, second))


def test_prepare_is_fresh_when_a_reused_id_gets_a_new_label() -> None:
    first = _symbol("first", 0)
    changed = replace(first, label="second")
    before = correspondence.prepare_correspondence(_document(first))
    after = correspondence.prepare_correspondence(_document(changed))
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


def test_trusted_loader_does_not_bypass_comparison_id_verification() -> None:
    source = _symbol("source", 0)
    altered = replace(source, symbol_kind="method")
    document = _document(altered)
    payload = orjson.dumps(document.to_dict())
    loaded = load_graph_bytes(payload, _skip_schema=True, _digest="trusted")
    with pytest.raises(correspondence.CorrespondenceAdmissionError):
        correspondence.prepare_correspondence(loaded.document)


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
    with pytest.raises(correspondence.CorrespondenceAdmissionError):
        correspondence.prepare_correspondence(_document(source, target, relationships=(duplicate,)))


def test_duplicate_evidence_location_is_rejected() -> None:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    location = _location("src/a.py", 2)
    repeated = Evidence(provenance=Provenance.STATIC_ANALYSIS, locations=(location, location))
    edge = Relationship(source.id, target.id, "references", (repeated,))
    report = validate_document(_document(source, target, relationships=(edge,)))
    assert report.issues[0].code == IssueCode.EVIDENCE_LOCATION_DUPLICATE


@pytest.mark.parametrize("kind", ["references", "calls", "imports"])
def test_unresolved_source_participation_requires_direct_ordinary_origin(kind: str) -> None:
    ordinary = _symbol("ordinary", 0)
    origin = _unresolved(ordinary, "outer", 1)
    unresolved = _unresolved(origin, "missing", 2)
    relationship = _relationship(unresolved, ordinary, kind)
    document = _document(ordinary, origin, unresolved, relationships=(relationship,))
    with pytest.raises(correspondence.CorrespondenceEligibilityError):
        correspondence.prepare_correspondence(document)


def test_same_unresolved_endpoint_in_multiple_supported_relationships_is_one_candidate() -> None:
    source = _symbol("source", 0)
    first_target = _symbol("first", 1)
    second_target = _symbol("second", 2)
    unresolved = _unresolved(source, "missing", 3)
    first = _relationship(unresolved, first_target, "references")
    second = _relationship(unresolved, second_target, "calls")
    prepared = correspondence.prepare_correspondence(
        _document(source, first_target, second_target, unresolved, relationships=(first, second))
    )
    unresolved_key = correspondence.node_key(unresolved, origin=correspondence.node_key(source))
    assert prepared.nodes_by_key[unresolved_key] == (unresolved,)
    for relationship_key in prepared.relationship_groups:
        assert prepared.validate_required_keys({relationship_key}) is prepared


def test_unsupported_relationships_never_enter_groups_when_endpoints_are_indexed() -> None:
    source = _symbol("source", 0)
    target = _symbol("target", 1)
    supported = _relationship(source, target, "calls")
    unsupported = _relationship(source, target, "contains")
    prepared = correspondence.prepare_correspondence(
        _document(source, target, relationships=(supported, unsupported))
    )
    assert len(prepared.relationship_groups) == 1
    assert next(iter(prepared.relationship_groups.values()))[0].relationship is supported


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
        correspondence.prepare_correspondence(first_document, side="new")
    with pytest.raises(correspondence.CorrespondenceEligibilityError) as second_error:
        correspondence.prepare_correspondence(second_document, side="new")
    assert (first_error.value.endpoint, first_error.value.node.id, first_error.value.side) == (
        second_error.value.endpoint,
        second_error.value.node.id,
        "new",
    )


def test_unsupported_only_unresolved_source_is_retained() -> None:
    ordinary = _symbol("ordinary", 0)
    unresolved = _unresolved(ordinary, "missing", 1)
    relationship = _relationship(unresolved, ordinary, "contains")
    prepared = correspondence.prepare_correspondence(
        _document(ordinary, unresolved, relationships=(relationship,))
    )
    assert prepared.nodes_by_id[unresolved.id] is unresolved
    assert not prepared.relationship_groups


def test_unresolved_target_chain_is_rejected_only_for_supported_references() -> None:
    ordinary = _symbol("ordinary", 0)
    origin = _unresolved(ordinary, "outer", 1)
    chained = _unresolved(origin, "inner", 2)
    edge = _relationship(ordinary, chained, "references")
    document = _document(ordinary, origin, chained, relationships=(edge,))
    with pytest.raises(correspondence.CorrespondenceEligibilityError) as raised:
        correspondence.prepare_correspondence(document, side="old")
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
    absent = correspondence.prepare_correspondence(_document(first, second, unresolved))
    assert (
        absent.validate_required_keys(
            {(correspondence.node_key(first), correspondence.node_key(second), "references")}
        )
        is absent
    )

    source = _symbol("source", 3)
    edge = _relationship(source, unresolved, "references")
    present = correspondence.prepare_correspondence(
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
    prepared = correspondence.prepare_correspondence(document)
    assert prepared.nodes_by_key[correspondence.node_key(source)] == (source,)


def test_empty_requested_set_still_checks_graph_admission_and_chain_eligibility() -> None:
    ordinary = _symbol("ordinary", 0)
    origin = _unresolved(ordinary, "outer", 1)
    chained = _unresolved(origin, "inner", 2)
    edge = _relationship(ordinary, chained, "references")
    document = _document(ordinary, origin, chained, relationships=(edge,))
    with pytest.raises(correspondence.CorrespondenceEligibilityError):
        correspondence.prepare_correspondence(document).validate_required_keys(set())


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
    prepared = correspondence.prepare_correspondence(document)
    permuted = correspondence.prepare_correspondence(reordered)
    groups = list(prepared.relationship_groups.values())
    assert len(groups) == 1
    pairs = {(item.relationship.source, item.relationship.target) for item in groups[0]}
    assert pairs == {(source_one.id, target_one.id), (source_two.id, target_two.id)}
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
    with pytest.raises(correspondence.CorrespondenceEligibilityError):
        correspondence.prepare_correspondence(document)
    assert document.to_dict() == before


def test_resource_kind_is_excluded_for_every_resource_basis() -> None:
    source = _resource("resource", 0)
    source_kind = replace(source, symbol_kind="db:table")
    upstream = _upstream_resource("upstream")
    upstream_kind = replace(upstream, symbol_kind="db:table")
    keyed = _resource_key("keyed")
    keyed_kind = replace(keyed, symbol_kind="db:table")
    assert correspondence.node_key(source) == correspondence.node_key(source_kind)
    assert correspondence.node_key(upstream) == correspondence.node_key(upstream_kind)
    assert correspondence.node_key(keyed) == correspondence.node_key(keyed_kind)
