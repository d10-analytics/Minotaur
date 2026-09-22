"""Public renderer contracts for ordinary graphs and comparisons."""

from minotaur.graph_visualizer.presentation import (
    build_comparison,
    build_comparison_presentation,
    build_presentation,
)
from minotaur.graph_visualizer.source import (
    capture_source_bytes,
    prepare_comparison_excerpts,
    prepare_excerpts,
    read_source_bytes,
)

__all__ = [
    "build_comparison",
    "build_comparison_presentation",
    "build_presentation",
    "capture_source_bytes",
    "prepare_comparison_excerpts",
    "prepare_excerpts",
    "read_source_bytes",
]
