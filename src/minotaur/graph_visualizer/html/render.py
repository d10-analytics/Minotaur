"""Pure self-contained HTML rendering for canonical presentation payloads."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files


def render_html(presentation: Mapping[str, object]) -> bytes:
    """Return a complete UTF-8 HTML document without touching the filesystem.

    JSON is placed in a non-executing script element with ``</`` neutralized,
    and all user-originating display values are later assigned through DOM text
    APIs in ``viewer.js``. No graph string becomes HTML markup. Resources are
    embedded instead of linked so the output remains inspectable after a user
    downloads it or opens it directly with a ``file://`` URL.
    """
    directory = files("minotaur.graph_visualizer").joinpath("html")
    payload = json.dumps(presentation, ensure_ascii=True, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    template = directory.joinpath("template.html").read_text(encoding="utf-8")
    replacements = {
        "/*__VIEWER_CSS__*/": directory.joinpath("viewer.css").read_text(encoding="utf-8"),
        "/*__CYTOSCAPE__*/": directory.joinpath("vendor/cytoscape-3.34.0.min.js").read_text(
            encoding="utf-8"
        ),
        "/*__CYTOSCAPE_DAGRE__*/": directory.joinpath("vendor/cytoscape-dagre-4.0.0.js").read_text(
            encoding="utf-8"
        ),
        "/*__VIEWER_JS__*/": directory.joinpath("viewer.js").read_text(encoding="utf-8"),
    }
    # Expand code and style markers before the data marker. Excerpts may contain
    # these marker strings verbatim, and treating data as template syntax would
    # corrupt source evidence or accidentally duplicate executable code.
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    # Presentation data can contain source excerpts from this renderer, which
    # naturally include our template-marker strings. Insert it last so those
    # strings remain data rather than becoming an accidental second expansion.
    template = template.replace("/*__PRESENTATION__*/", payload)
    return template.encode("utf-8")
