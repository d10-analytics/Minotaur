"""Application entry point for the fixture workflow."""

from __future__ import annotations

from workflow.service import Service
from workflow.util import format_report


def build() -> Service:
    """Return the service this application drives."""

    return Service(name="app")


def main() -> int:
    """Print one report and return a fixed status."""

    service = build()
    print(format_report(service.run(2)))
    print(service.total(2))
    return 0
