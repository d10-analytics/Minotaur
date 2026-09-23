"""Shared relationship accumulation and evidence assembly for interpreters."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from minotaur.graph_model.evidence import Evidence, Producer
from minotaur.graph_model.location import Location
from minotaur.graph_model.provenance import Provenance
from minotaur.graph_model.relationship import Relationship


@dataclass(slots=True)
class _Observation:
    evidence: Evidence
    locations: list[Location]


class RelationshipAccumulator:
    """Collect relationship observations and build graph relationships."""

    def __init__(self) -> None:
        self._relationships: dict[tuple[str, str, str], list[_Observation]] = defaultdict(list)

    def add(
        self,
        source: str,
        target: str,
        kind: str,
        location: Location | None,
        extensions: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        key = (source, target, kind)
        attribution = Evidence(Provenance.STATIC_ANALYSIS, extensions=extensions)
        for observation in self._relationships[key]:
            if observation.evidence.attribution_key == attribution.attribution_key:
                if location is not None:
                    observation.locations.append(location)
                return

        observation = _Observation(attribution, [])
        if location is not None:
            observation.locations.append(location)
        self._relationships[key].append(observation)

    def documents(self, producer: Producer) -> tuple[Relationship, ...]:
        return tuple(
            Relationship(
                source=key[0],
                target=key[1],
                kind=key[2],
                evidence=tuple(
                    Evidence(
                        observation.evidence.provenance,
                        producer=producer,
                        rule=observation.evidence.rule,
                        locations=tuple(dict.fromkeys(observation.locations)),
                        extensions=observation.evidence.extensions,
                    )
                    for observation in observations
                ),
            )
            for key, observations in self._relationships.items()
        )
