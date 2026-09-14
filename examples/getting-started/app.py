"""A small call chain for learning how source becomes a graph."""


def greeting(name: str) -> str:
    """Build a greeting without printing it."""
    return f"Hello, {name}!"


def welcome() -> str:
    """Call the helper with a name chosen by this application."""
    return greeting("Ada")
