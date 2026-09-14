"""Run a documented scenario in a new temporary workspace.

Run from the checkout with its installed Python environment. Each CLI call is
printed beside its output and exit status. Temporary files are removed when
the scenario finishes; the first-run HTML stays openable until you press Enter.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def git(root: Path, *arguments: str) -> None:
    """Change Git state only in the temporary repository owned by this example."""
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def systems(root: Path) -> None:
    shutil.copytree(ROOT / "examples/system-walkthrough/shop", root / "shop")
    shutil.copytree(ROOT / "examples/system-walkthrough/docs", root / "docs")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ntargets = ["shop"]\ngraph = "graph.json"\n',
        encoding="utf-8",
    )
    git(root, "init", "-q")
    command(root, "analyze")
    git(root, "add", "shop", "docs", ".minotaur.toml", "graph.json", "graph.json.sha256")
    git(
        root,
        "-c",
        "user.name=Walkthrough",
        "-c",
        "user.email=walkthrough@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "Example baseline",
    )
    command(root, "query", "diff", "--systems")
    source = root / "shop/billing.py"
    original = source.read_bytes()
    source.write_bytes(original + b"\n\ndef refund(order):\n    return charge(order)\n")
    orders = root / "shop/orders.py"
    original_orders = orders.read_bytes()
    orders.write_bytes(
        original_orders.replace(b"import charge", b"import charge, refund")
        + b"\n\ndef cancel_order(order):\n    return refund(order)\n"
    )
    command(root, "query", "diff", "--systems", expected=1)
    command(root, "query", "diff", "--systems", "--system", "billing", expected=1)
    command(root, "query", "diff", "--systems", "--system", "billing", "--details", expected=1)
    command(root, "query", "diff", "--systems", "--system", "billing", "--json", expected=1)
    # Restore source before changing membership: the final comparison then
    # demonstrates a definition change with exactly the original source bytes.
    source.write_bytes(original)
    orders.write_bytes(original_orders)
    definition = root / "docs/systems/billing/system.toml"
    definition.write_text(
        'schema_version = 1\nname = "billing"\nfiles = ["shop/billing.py", "shop/ledger.py"]\n',
        encoding="utf-8",
    )
    command(root, "query", "diff", "--systems", expected=1)


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
