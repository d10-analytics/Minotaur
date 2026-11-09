"""Binding-lifecycle sample: names whose binding changes after a definition."""

from __future__ import annotations

from typing import Annotated, Literal

from workflow.declared_encoding import banner  # noqa: F401
from workflow.helper import fixture_helper


def replaced() -> int:
    """Declare a name that a later assignment rebinds."""

    return 1


def deleted() -> int:
    """Declare a name that a later ``del`` removes."""

    return 2


def conditional() -> int:
    """Declare a name that a conditional assignment may replace."""

    return 3


def guarded() -> int:
    """Declare a name that a ``try`` import may replace."""

    return 4


def helper() -> int:
    """Share a name with the module reached through ``workflow.helper``."""

    return 5


def starred() -> int:
    """Declare a name a star import may rebind."""

    return 6


replaced = fixture_helper  # noqa: F811
replaced()

del deleted

if fixture_helper:
    conditional = fixture_helper  # noqa: F811
conditional()

try:  # noqa: SIM105
    from workflow.helper import fixture_helper as guarded  # noqa: F811
except ImportError:
    pass
guarded()

helper()


from workflow.util import *  # noqa: E402, F401, F403

starred()


class Holder:
    """Annotate a name in a class body without binding it."""

    label: str

    def label_text(self, label: str = "") -> str:
        """Return the argument."""

        return label

    class Inner:
        """Use a module name from a nested-class header and body."""

        value = helper()
        loaded = helper

        def run(self, first: int = helper(), second: object = helper) -> int:
            """Call and load the module name from a nested method."""

            return helper() + int(bool(helper))


def local_annotation() -> int:
    """Annotate a function-local name without a value."""

    total: int
    total = helper()
    return total


def string_annotations(
    first: Literal["helper"], second: Annotated[int, "starred"]
) -> "list[Holder]":  # noqa: UP037
    """Resolve string annotations that name a class."""

    item: "Holder" = Holder()  # noqa: UP037
    return [item]


from workflow.util import format_report as Forward  # noqa: E402, N812


def forward_reference(value: "Forward") -> "Forward":  # noqa: UP037
    """Annotate with a quoted name the module rebinds afterwards."""

    return value


from workflow.util import summarize as Forward  # noqa: E402, F811, N812


def local_import_class() -> type[object]:
    """Call and load a function-local import from a nested class body."""

    from workflow.util import format_report as Forward  # noqa: F811, N812, PLC0415

    class Local:
        """Resolve the function's import, not the module's."""

        value = Forward([1])
        loaded = Forward

    return Local


def loop_global() -> None:
    """Write a ``global`` inside a loop, leaving the later use uncertain."""

    global Holder
    for _ in range(0):
        Holder = ConditionalImport
    Holder()


class ConditionalImport:
    """Import a name in an ``if`` suite and use it in the class body."""

    if fixture_helper:
        from workflow.helper import fixture_helper as conditional_helper

    value = conditional_helper()


def nested_class_import() -> type[object]:
    """Resolve a nested class body's import over the module's same-name import."""

    class Shadowed:
        from workflow.util import format_report as Forward

        value = Forward([1])

    return Shadowed


def directive_class_import() -> int:
    """Write a name through ``global`` in a nested class and use it there."""

    class Directive:
        global Holder

        from workflow.helper import fixture_helper as Holder

        value = Holder()

    return Directive.value


def header_comprehension() -> type[object]:
    """Keep a comprehension in a nested class's method default off the class import."""

    class Defaults:
        from workflow.util import format_report as Forward

        def run(self, reports: object = [Forward([1]) for _ in range(1)]) -> object:
            return reports

    return Defaults


class NestedFunctionScope:
    """Resolve a function defined in a class-body ``if`` without the class names."""

    from workflow.helper import fixture_helper as class_only_helper
    from workflow.util import format_report as Forward

    if fixture_helper:

        def run() -> object:
            return class_only_helper, Forward([1])  # noqa: F405


from workflow import util as workflow  # noqa: E402, F401


class PlainImport:
    """Resolve a class-body plain import over the module's import of the same name."""

    import workflow

    value = workflow.helper.fixture_helper()
