"""Small helpers shared by the fixture workflow."""

from __future__ import annotations

from collections.abc import Sequence

from workflow.helper import fixture_helper


def fixture_outer() -> type[object]:
    """Build nested classes whose headers and bodies use different scopes."""

    class Outer:
        fixture_helper = staticmethod(lambda: None)

        class Assigned:
            @fixture_helper
            def run(self) -> int:
                return fixture_helper()

        class Imported:
            from externallib import fixture_helper

            @fixture_helper
            def run(self) -> int:
                return fixture_helper()

    return Outer


class FixtureScopes:
    """Exercise nested class scope isolation in a direct class body."""

    fixture_helper = staticmethod(lambda: None)

    class Assigned:
        @fixture_helper
        def run(self) -> int:
            return fixture_helper()

    class Imported:
        from externallib import fixture_helper

        @fixture_helper
        def run(self) -> int:
            return fixture_helper()


def format_report(values: Sequence[int]) -> str:
    """Render *values* as one stable line."""

    return "report: " + ", ".join(str(value) for value in values)


def summarize(values: Sequence[int]) -> int:
    """Return the total of *values*."""

    return sum(values)


def unused_helper(value: int) -> int:
    """Never called from anywhere: the fixture's unreferenced symbol."""

    return value * 2
