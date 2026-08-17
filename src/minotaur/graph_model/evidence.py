"""Evidence model for Minotaur graph documents.

Evidence records are the core epistemic unit in Minotaur. Every relationship
must have at least one evidence record explaining HOW the relationship was
established. This design exists because a graph edge without evidence is an
unsupported assertion — Minotaur's value proposition is that every structural
claim is traceable to a producer, a process, and (when available) a source
location.

Evidence records live on relationships, not on nodes, because the claim being
supported is "node A relates to node B in this way," not "node A exists."
A node's existence is established by its identity and location; the evidence
explains the connections between nodes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from minotaur.graph_model._parsing import (
    freeze_extensions,
    reject_unknown_fields,
    serialize_extensions,
)
from minotaur.graph_model.location import Location
from minotaur.graph_model.provenance import Provenance


@dataclass(frozen=True, slots=True)
class Producer:
    """The tool or system that produced an evidence record.

    Producer identity is separate from provenance category: provenance says
    HOW the evidence was established (static analysis, import, curated rule),
    while producer says WHO did it (minotaur-python 0.1.0, example-index 2.4).

    This separation matters because multiple producers can use the same
    provenance method, and one producer might contribute evidence through
    different methods (e.g. a tool that both analyzes source and imports
    external graphs).
    """

    name: str
    version: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("producer name must be non-empty")
        # The schema defines version via nonEmptyString (minLength: 1).
        # An empty version string is distinct from an absent one — absent
        # means unknown, but empty means "I claim to know the version and
        # it's nothing," which is not a valid claim.
        if self.version is not None and not self.version:
            raise ValueError("producer version must be non-empty when present")

    def to_dict(self) -> dict[str, str]:
        result: dict[str, str] = {"name": self.name}
        if self.version is not None:
            result["version"] = self.version
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Producer:
        reject_unknown_fields(data, frozenset({"name", "version"}), "producer")
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("producer requires a non-empty 'name' string")
        version = data.get("version")
        if version is not None:
            if not isinstance(version, str):
                raise ValueError("producer 'version' must be a string when present")
            if not version:
                raise ValueError("producer 'version' must be non-empty when present")
        return cls(name=name, version=version)


@dataclass(frozen=True, slots=True)
class Rule:
    """A curated-rule identifier for evidence established by a declared rule.

    When provenance is "curated-rule", the rule object is REQUIRED — it
    identifies the inspectable reasoning contract that established the
    relationship. Without it, a curated-rule claim is unauditable: the
    consumer cannot look up what logic decided the relationship exists.

    The rule ID is an opaque identifier resolved through the producer's
    local rule catalog. Minotaur does not embed rule source, private paths,
    or explanatory prose — the ID is enough for a permitted consumer to
    find the rule definition.
    """

    id: str
    version: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("rule id must be non-empty")
        if self.version is not None and not self.version:
            raise ValueError("rule version must be non-empty when present")

    def to_dict(self) -> dict[str, str]:
        result: dict[str, str] = {"id": self.id}
        if self.version is not None:
            result["version"] = self.version
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Rule:
        reject_unknown_fields(data, frozenset({"id", "version"}), "rule")
        rule_id = data.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError("rule requires a non-empty 'id' string")
        version = data.get("version")
        if version is not None:
            if not isinstance(version, str):
                raise ValueError("rule 'version' must be a string when present")
            if not version:
                raise ValueError("rule 'version' must be non-empty when present")
        return cls(id=rule_id, version=version)


@dataclass(frozen=True, slots=True)
class Evidence:
    """One independently attributable evidence record supporting a relationship.

    A single relationship can have multiple evidence records when independent
    producers or methods support the same structural claim. For example, a
    "calls" relationship might be supported by both a static analyzer (which
    found it in source) and an imported graph (which carried it from another
    tool). Minotaur preserves both records on one relationship rather than
    creating duplicate visual edges or selecting one as the sole truth.

    Within one relationship, evidence records are unique by their complete
    canonical content EXCLUDING locations. Two records with the same provenance,
    producer, rule, and extensions but different locations are merged into one
    record with a combined location list. Two records identical in all non-
    location content are invalid duplicates — the semantic validator rejects
    them.

    This design prevents an importer from multiplying identical support while
    preserving every distinct call site for inspection.
    """

    provenance: Provenance
    producer: Producer | None = None
    rule: Rule | None = None
    # Locations are stored as a tuple (not list) because Evidence is frozen.
    # An empty tuple means no locations; the schema allows the field to be
    # absent entirely, which we represent as an empty tuple rather than None
    # to avoid a three-way distinction (None vs [] vs [...]) that would
    # complicate every consumer.
    locations: tuple[Location, ...] = field(default_factory=tuple)
    extensions: Mapping[str, Mapping[str, object]] | None = None

    def __post_init__(self) -> None:
        # Enforce the curated-rule invariant at construction: if you claim
        # curated-rule provenance, you must identify the rule. This is a
        # schema-level requirement, not just a semantic-validation check,
        # because a curated-rule evidence record without a rule ID is
        # structurally incomplete.
        if self.provenance == Provenance.CURATED_RULE and self.rule is None:
            raise ValueError(
                "evidence with 'curated-rule' provenance requires a 'rule' object"
            )
        # The schema also enforces the converse: non-curated-rule evidence
        # must NOT have a rule object. This prevents ambiguity about whether
        # a rule applies when the provenance says it shouldn't.
        if self.provenance != Provenance.CURATED_RULE and self.rule is not None:
            raise ValueError(
                f"evidence with '{self.provenance.value}' provenance must not "
                f"have a 'rule' object (rule is only for 'curated-rule')"
            )
        object.__setattr__(self, "extensions", freeze_extensions(self.extensions))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"provenance": self.provenance.value}
        if self.producer is not None:
            result["producer"] = self.producer.to_dict()
        if self.rule is not None:
            result["rule"] = self.rule.to_dict()
        if self.locations:
            result["locations"] = [loc.to_dict() for loc in self.locations]
        if self.extensions is not None:
            result["extensions"] = serialize_extensions(self.extensions)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Evidence:
        reject_unknown_fields(
            data,
            frozenset({"provenance", "producer", "rule", "locations", "extensions"}),
            "evidence",
        )
        # Provenance is required and must be a known core value.
        # Extension provenance values are not supported in v1 — the
        # provenance enum is closed, unlike symbol_kind and relationship_kind.
        prov_str = data.get("provenance")
        if not isinstance(prov_str, str):
            raise ValueError("evidence requires a 'provenance' string")
        try:
            provenance = Provenance(prov_str)
        except ValueError:
            raise ValueError(
                f"unknown provenance '{prov_str}'; v1 core values are: "
                f"{', '.join(p.value for p in Provenance)}"
            ) from None

        producer = None
        producer_data = data.get("producer")
        if producer_data is not None:
            if not isinstance(producer_data, dict):
                raise ValueError("'producer' must be an object when present")
            producer = Producer.from_dict(producer_data)

        rule = None
        rule_data = data.get("rule")
        if rule_data is not None:
            if not isinstance(rule_data, dict):
                raise ValueError("'rule' must be an object when present")
            rule = Rule.from_dict(rule_data)

        locations: tuple[Location, ...] = ()
        locations_data = data.get("locations")
        if locations_data is not None:
            if not isinstance(locations_data, list):
                raise ValueError("'locations' must be an array when present")
            if not locations_data:
                raise ValueError("'locations' array must be non-empty when present")
            locations = tuple(Location.from_dict(loc) for loc in locations_data)

        extensions = data.get("extensions")
        if extensions is not None and not isinstance(extensions, dict):
            raise ValueError("'extensions' must be an object when present")

        return cls(
            provenance=provenance,
            producer=producer,
            rule=rule,
            locations=locations,
            extensions=extensions,
        )
