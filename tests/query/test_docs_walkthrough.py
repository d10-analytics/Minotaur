"""Keep the README's first-run transcript aligned with the public walkthrough."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_readme_walkthrough_prints_exact_output_without_changing_examples() -> None:
    """Run the documented entry point using its own temporary workspace."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    transcripts = re.findall(r"^```text\n(.*?)^```$", readme, re.DOTALL | re.MULTILINE)
    assert len(transcripts) == 1
    transcript = transcripts[0]
    assert "$ minotaur query definitions greeting " in transcript
    assert "$ minotaur query callers app.greeting " in transcript
    assert "python examples/run_walkthrough.py python\n" in readme

    def bundled_files() -> dict[str, bytes]:
        return {
            str(path.relative_to(ROOT)): path.read_bytes()
            for path in (ROOT / "examples").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }

    before = bundled_files()
    completed = subprocess.run(
        [sys.executable, "examples/run_walkthrough.py", "python", "--no-pause"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == transcript
    assert bundled_files() == before, "walkthrough changed bundled examples"
