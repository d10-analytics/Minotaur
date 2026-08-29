"""Fixture package analyzed by the baseline/branch equivalence harness."""

from .helper import fixture_helper as fixture_helper


def package_scope_probe() -> type[object]:
    """Exercise relative imports while walking a function-nested class."""

    class Nested:
        from .helper import fixture_helper

        fixture_helper()

    return Nested
