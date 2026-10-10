"""Same-named definitions told apart only by their declaration decorators."""

from __future__ import annotations

import typing
from typing import overload


class Gauge:
    """Expose one stored level through a property with every accessor."""

    _level: int

    @property
    def level(self) -> int:
        """Return the current level."""

        return self._level

    @level.getter
    def level(self) -> int:
        """Return the current level through an explicit getter."""

        return self._level

    @level.setter
    def level(self, value: int) -> None:
        """Replace the current level."""

        self._level = value

    @level.deleter
    def level(self) -> None:
        """Reset the current level."""

        self._level = 0


@overload
def scale(value: int) -> int: ...


@overload
def scale(value: float) -> float: ...


@typing.overload
def scale(value: str) -> str: ...


def scale(value: int | float | str) -> int | float | str:
    """Return *value* doubled."""

    return value * 2
