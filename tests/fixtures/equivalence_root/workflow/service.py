"""The fixture workflow's only stateful component."""

from __future__ import annotations

from dataclasses import dataclass

from workflow.util import summarize


@dataclass(frozen=True)
class Service:
    """Run a fixed number of steps and report their values."""

    name: str

    def run(self, steps: int) -> list[int]:
        """Return one value per step."""

        return [self.step(index) for index in range(steps)]

    def step(self, index: int) -> int:
        """Return the value of a single step."""

        return index + len(self.name)

    def total(self, steps: int) -> int:
        """Return the total of every step's value."""

        return summarize(self.run(steps))


def main() -> int:
    """Run the service once with a fixed configuration."""

    return Service(name="service").total(3)
