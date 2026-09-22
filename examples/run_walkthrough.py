"""Run a documented scenario in a new temporary workspace.

Run from the checkout with its installed Python environment. Each CLI call is
printed beside its output and exit status. Temporary files are removed when
the scenario finishes; the first-run HTML stays openable until you press Enter.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Fixed author, committer, and timestamps for the comparison fixture. Pinning
#: every commit input keeps the two revision IDs stable, so the checked-in
#: comparison report and the walkthrough transcript stay reproducible.
SYSTEMS_BASELINE_TAG = "v1.0"
SYSTEMS_REVISION_TAG = "v2.0"
SYSTEMS_BASELINE_DATE = "2001-02-03T04:05:06+00:00"
SYSTEMS_REVISION_DATE = "2001-02-04T04:05:06+00:00"
SYSTEMS_BASELINE_MESSAGE = "Example baseline"
SYSTEMS_REVISION_MESSAGE = "Example change"
SYSTEMS_AUTHOR_NAME = "Minotaur Walkthrough"
SYSTEMS_AUTHOR_EMAIL = "walkthrough@example.invalid"
SYSTEMS_CONFIG = (
    '[minotaur]\nschema_version = 1\nroot = "."\ntargets = ["shop"]\ngraph = "graph.json"\n'
)

#: The second revision: a new cross-system call, an internal call-expression
#: change, a symbol moved to a new file, and a removed consumer file. The
#: baseline is the checked-in shop, so the story always starts from the same
#: example sources.
SYSTEMS_REVISION_FILES = {
    "shop/billing.py": (
        '"""The billing subsystem: charging orders once they are complete."""\n'
        "\n"
        "from shop.ledger import record\n"
        "\n"
        "\n"
        "def charge(order):\n"
        '    record({"kind": "charged", "order": order})\n'
        '    return {"order": order, "status": "charged"}\n'
        "\n"
        "\n"
        "def refund(order):\n"
        '    record({"kind": "refunded", "order": order})\n'
        '    return {"order": order, "status": "refunded"}\n'
    ),
    "shop/orders.py": (
        '"""The orders subsystem: creating and cancelling orders."""\n'
        "\n"
        "from shop.billing import refund\n"
        "from shop.ledger import record\n"
        "\n"
        "\n"
        "def create_order(cart):\n"
        '    order = {"cart": cart, "status": "new"}\n'
        '    record(("created", order))\n'
        "    return order\n"
        "\n"
        "\n"
        "def cancel_order(order):\n"
        "    return refund(order)\n"
    ),
    "shop/order_ops.py": (
        '"""Order completion, split out of the orders module."""\n'
        "\n"
        "from shop.billing import charge\n"
        "\n"
        "\n"
        "def complete_order(order):\n"
        "    charge(order)\n"
        '    order["status"] = "paid"\n'
        "    return order\n"
    ),
    "docs/systems/orders/system.toml": (
        'schema_version = 1\nname = "orders"\nfiles = ["shop/orders.py", "shop/order_ops.py"]\n'
    ),
}

#: Files deleted in the second revision, relative to the fixture root.
SYSTEMS_REVISION_REMOVALS = ("shop/checkout.py",)


def command(root: Path, *arguments: str, expected: int = 0) -> None:
    """Run the public CLI, including commands where a change returns status 1."""
    print("$ minotaur " + " ".join(arguments), flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "minotaur", *arguments],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    print(result.stdout, end="")
    if result.stderr:
        print("stderr:", flush=True)
        print(result.stderr.replace(str(root), "<workspace>"), end="")
    print(f"exit: {result.returncode}", flush=True)
    if result.returncode != expected:
        raise RuntimeError(f"expected exit {expected}, got {result.returncode}")


def analyze(root: Path, target: str = ".") -> None:
    command(root, "analyze", "--root", ".", "--output", "graph.json", "--force", target)


def query(root: Path, kind: str, *arguments: str) -> None:
    command(root, "query", kind, *arguments, "--graph", "graph.json", "--root", ".", "--no-refresh")


def first_run(root: Path, *, pause: bool) -> None:
    shutil.copy2(ROOT / "examples/getting-started/app.py", root / "app.py")
    analyze(root, "app.py")
    query(root, "definitions", "greeting")
    query(root, "callers", "app.greeting")
    command(
        root, "visualize", "--input", "graph.json", "--output", "graph.html", "--source-root", "."
    )
    if pause:
        print(f"Open this local file in your browser: {(root / 'graph.html').as_uri()}")
        input("Press Enter after viewing it to remove the temporary workspace: ")


def javascript(root: Path) -> None:
    for name in ("app.js", "lib.js"):
        shutil.copy2(ROOT / "examples/javascript-workflow" / name, root / name)
    analyze(root)
    query(root, "definitions", "greet")
    query(root, "callers", "lib.greet")
    query(root, "callers", "app.welcome")


def bindings(root: Path) -> None:
    (root / "lib.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (root / "other.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    (root / "app.py").write_text(
        "from lib import helper\n\n"
        "def same(flag):\n"
        "    if flag:\n        from lib import helper\n"
        "    else:\n        from lib import helper\n"
        "    helper()\n\n"
        "def different(flag):\n"
        "    if flag:\n        from lib import helper\n"
        "    else:\n        from other import helper\n"
        "    helper()\n\n"
        "def callback():\n    register(helper)\n\n"
        "class Service:\n"
        "    def run(self):\n        return 1\n\n"
        "    def known(self):\n        self.run()\n\n"
        "def unknown():\n    service.run()\n",
        encoding="utf-8",
    )
    (root / "callback_only.py").write_text(
        "def handler():\n    return 1\n\nregister(handler)\n",
        encoding="utf-8",
    )
    print((root / "app.py").read_text(), end="")
    analyze(root)
    query(root, "callers", "lib.helper")
    query(root, "impact", "lib.helper")
    query(root, "unreferenced", "lib.py")
    query(root, "callers", "app.Service.run")
    query(root, "callers", "callback_only.handler")
    query(root, "impact", "callback_only.handler")
    query(root, "unreferenced", "callback_only.py")


def malformed(root: Path) -> None:
    (root / "good.py").write_text("def healthy():\n    return 1\n", encoding="utf-8")
    broken = root / "broken.py"
    broken.write_text("def repaired(:\n    return 2\n", encoding="utf-8")
    command(root, "analyze", "--root", ".", "--output", "graph.json", ".", expected=1)
    query(root, "definitions", "healthy")
    broken.write_text("def repaired():\n    return 2\n", encoding="utf-8")
    analyze(root)
    query(root, "definitions", "repaired")


def git(root: Path, *arguments: str, timestamp: str | None = None) -> None:
    """Change Git state only in the temporary repository owned by this example."""
    environment = None
    if timestamp is not None:
        environment = {
            "GIT_AUTHOR_NAME": SYSTEMS_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": SYSTEMS_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": SYSTEMS_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": SYSTEMS_AUTHOR_EMAIL,
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        env={**os.environ, **environment} if environment is not None else None,
    )


def write_systems_workspace(root: Path) -> None:
    """Copy the checked-in shop and definitions and write the fixture config."""
    shutil.copytree(ROOT / "examples/system-walkthrough/shop", root / "shop")
    shutil.copytree(ROOT / "examples/system-walkthrough/docs", root / "docs")
    (root / ".minotaur.toml").write_text(SYSTEMS_CONFIG, encoding="utf-8")


def apply_systems_revision(root: Path) -> None:
    """Apply the documented second-revision source, definition, and removal."""
    for relative, text in SYSTEMS_REVISION_FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for relative in SYSTEMS_REVISION_REMOVALS:
        (root / relative).unlink()


def commit_systems_workspace(root: Path, message: str, timestamp: str) -> None:
    """Stage the fixture sources and commit them under a fixed identity and date."""
    git(root, "add", "shop", "docs", ".minotaur.toml")
    git(
        root,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        message,
        timestamp=timestamp,
    )


def create_systems_baseline(root: Path) -> None:
    """Write and commit the tagged baseline revision consumed by comparisons.

    No graph or sidecar is committed: the comparison analyzes each revision's
    configured source directly, so the fixture stays independent of any
    analyzer output and reproduces identically across environments.
    """
    write_systems_workspace(root)
    git(root, "init", "-q")
    git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    commit_systems_workspace(root, SYSTEMS_BASELINE_MESSAGE, SYSTEMS_BASELINE_DATE)
    git(root, "tag", SYSTEMS_BASELINE_TAG)


def stage_systems_revision(root: Path) -> None:
    """Apply the second revision in the working tree without committing it."""
    apply_systems_revision(root)


def commit_systems_revision(root: Path) -> None:
    """Commit and tag the staged second revision under its fixed date."""
    commit_systems_workspace(root, SYSTEMS_REVISION_MESSAGE, SYSTEMS_REVISION_DATE)
    git(root, "tag", SYSTEMS_REVISION_TAG)


def systems(root: Path) -> None:
    create_systems_baseline(root)
    command(root, "query", "diff", "--systems")
    stage_systems_revision(root)
    # HEAD still names the baseline, so this is the working-tree workflow.
    command(root, "query", "diff", "--systems", expected=1)
    command(
        root,
        "query",
        "diff",
        "--systems",
        "--html",
        "working-tree-comparison.html",
        expected=1,
    )
    command(root, "query", "diff", "--systems", "--json", expected=1)
    (root / "working-tree-comparison.html").unlink()
    commit_systems_revision(root)
    # Both tags now resolve, so this is the explicit historical-pair workflow.
    command(
        root,
        "query",
        "diff",
        "--systems",
        SYSTEMS_BASELINE_TAG,
        SYSTEMS_REVISION_TAG,
        expected=1,
    )
    command(
        root,
        "query",
        "diff",
        "--systems",
        SYSTEMS_BASELINE_TAG,
        SYSTEMS_REVISION_TAG,
        "--system",
        "billing",
        "--details",
        expected=1,
    )
    command(
        root,
        "query",
        "diff",
        "--systems",
        SYSTEMS_BASELINE_TAG,
        SYSTEMS_REVISION_TAG,
        "--html",
        "comparison.html",
        expected=1,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenario", choices=("python", "javascript", "bindings", "malformed", "systems")
    )
    parser.add_argument(
        "--no-pause", action="store_true", help="remove HTML without waiting for Enter"
    )
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="minotaur-walkthrough-") as directory:
        root = Path(directory)
        if arguments.scenario == "python":
            first_run(root, pause=not arguments.no_pause)
        else:
            scenarios = {
                "javascript": javascript,
                "bindings": bindings,
                "malformed": malformed,
                "systems": systems,
            }
            scenarios[arguments.scenario](root)


if __name__ == "__main__":
    main()
