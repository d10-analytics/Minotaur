"""Shared relationship accumulation and evidence assembly for interpreters."""

from __future__ import annotations

from collections import defaultdict

from minotaur.graph_model.evidence import Evidence, Producer
from minotaur.graph_model.location import Location
from minotaur.graph_model.provenance import Provenance
from minotaur.graph_model.relationship import Relationship


class RelationshipAccumulator:
    """Collect relationship observations and build graph relationships."""

    def __init__(self) -> None:
        self._relationships: dict[tuple[str, str, str], list[Location]] = defaultdict(list)

    def add(
        self,
        source: str,
        target: str,
        kind: str,
        location: Location | None,
    ) -> None:
        key = (source, target, kind)
        if location is not None:
            self._relationships[key].append(location)
        else:
            self._relationships.setdefault(key, [])

    def documents(self, producer: Producer) -> tuple[Relationship, ...]:
        return tuple(
            Relationship(
                source=key[0],
                target=key[1],
                kind=key[2],
                evidence=(
                    Evidence(
                        Provenance.STATIC_ANALYSIS,
                        producer=producer,
                        locations=tuple(dict.fromkeys(locations)),
                    ),
                ),
            )
            for key, locations in self._relationships.items()
        )
