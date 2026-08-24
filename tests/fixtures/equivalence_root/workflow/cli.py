"""Command-line entry point for the fixture workflow."""

from __future__ import annotations

from workflow.app import main as run_application
from workflow.util import format_report


def describe() -> str:
    """Return the banner printed before the application runs."""

    return format_report([1, 2, 3])


def main() -> int:
    """Print the banner and delegate to the application."""

    print(describe())
    return run_application()
