# -*- coding: latin-1 -*- (café)
"""Declared-encoding sample whose bytes are latin-1, such as the é in café."""


def accented(word: str) -> str:
    """Return the word followed by an accented suffix."""

    return word + " café"


banner = "Déjà vu: " + accented("menu")
