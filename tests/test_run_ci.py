"""Behavioral coverage for the public local CI command."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "run_ci.sh"
LANES = ["test", "lint", "typecheck", "package", "browser", "build"]


FAKE_PYTHON = r"""#!/usr/bin/env bash
set -euo pipefail
name="$(basename "$0")"
log_call() { printf '%s\t%s\t%s\n' "$name" "$PWD" "$*" >> "$FAKE_LOG"; }
if [[ "$name" == python && -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]] \
    && [[ "${1-}" == -m ]] \
    && [[ "${2-}" == playwright || "${2-}" == pytest ]]; then
    printf '%s\t%s\n' "$name" "$PLAYWRIGHT_BROWSERS_PATH" >> "$FAKE_BROWSER_LOG"
fi
if [[ "${1-}" == -m && "${2-}" == venv ]]; then
    log_call "$@"
    if [[ -n "${FAKE_OBSERVED:-}" ]]; then
        printf 'cwd=%s\n' "$PWD" >> "$FAKE_OBSERVED"
        for path in tracked.txt untracked.sh link.txt ignored.txt; do
            if [[ -L "$path" ]]; then
                target="$(readlink "$path")"
                printf '%s=symlink:%s\n' "$path" "$target" >> "$FAKE_OBSERVED"
            elif [[ -e "$path" ]]; then
                content="$(cat "$path")"
                printf '%s=file:%s\n' "$path" "$content" >> "$FAKE_OBSERVED"
            else
                printf '%s=absent\n' "$path" >> "$FAKE_OBSERVED"
            fi
        done
    fi
    mkdir -p "$3/bin"
    for tool in python pip ruff mypy; do cp "$0" "$3/bin/$tool"; chmod +x "$3/bin/$tool"; done
    exit 0
fi
log_call "$@"
if [[ "$name" == pip ]]; then exit "${FAKE_PIP_STATUS:-0}"; fi
if [[ "$name" == ruff ]]; then exit "${FAKE_RUFF_STATUS:-0}"; fi
if [[ "$name" == mypy ]]; then exit "${FAKE_MYPY_STATUS:-0}"; fi
if [[ "${1-}" == -m ]]; then
    case "$2" in
        pytest)
            [[ -n "${FAKE_PYTEST_SLEEP:-}" ]] && sleep "$FAKE_PYTEST_SLEEP"
            exit "${FAKE_PYTEST_STATUS:-0}"
            ;;
        playwright) exit "${FAKE_PLAYWRIGHT_STATUS:-0}";;
        build) exit "${FAKE_BUILD_STATUS:-0}";;
    esac
fi
exit 0
"""


@pytest.fixture
def fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return a disposable Git checkout, fake Python, and state directory."""

    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "tests").mkdir()
    shutil.copy2(SCRIPT, checkout / "scripts/run_ci.sh")
    (checkout / "pyproject.toml").write_text("[build-system]\nrequires=[]\n", encoding="utf-8")
    (checkout / "tests/sample.py").write_text("def test_sample(): pass\n", encoding="utf-8")
    (checkout / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    (checkout / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.test"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=checkout, check=True)
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=checkout, check=True)

    fake_python = tmp_path / "fake-python"
    fake_python.write_text(FAKE_PYTHON, encoding="utf-8")
    fake_python.chmod(0o755)
    state = tmp_path / "state"
    return checkout, fake_python, state


def run_ci(
    checkout: Path,
    fake_python: Path,
    state: Path,
    selector: str = "all",
    **extra: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHON_BIN": str(fake_python),
            "XDG_STATE_HOME": str(state),
            "FAKE_LOG": str(state / "calls.log"),
            "FAKE_BROWSER_LOG": str(state / "browser.log"),
            **extra,
        }
    )
    return subprocess.run(
        [str(checkout / "scripts/run_ci.sh"), selector],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def manifest(state: Path) -> dict[str, object]:
    paths = list((state / "minotaur-ci/runs").glob("*/result.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_help_and_unknown_selector_are_nonexecuting(fixture: tuple[Path, Path, Path]) -> None:
    checkout, fake_python, state = fixture
    help_result = run_ci(checkout, fake_python, state, "--help")
    assert help_result.returncode == 0
    assert "MINOTAUR_CI_TIMEOUT_SECONDS" in help_result.stdout
    assert not (state / "calls.log").exists()

    unknown = run_ci(checkout, fake_python, state, "unknown")
    assert unknown.returncode == 2
    assert not (state / "calls.log").exists()


def test_all_runs_six_lanes_and_keeps_later_lanes_after_failure(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    result = run_ci(checkout, fake_python, state, FAKE_RUFF_STATUS="7")
    assert result.returncode != 0
    evidence = manifest(state)
    assert [row["name"] for row in evidence["lanes"]] == LANES
    assert evidence["lanes"][1]["status"] == "failed"
    assert all(row["status"] != "pending" for row in evidence["lanes"])
    assert evidence["lanes"][2]["status"] == "passed"
    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert "-m build" in calls


def test_exit_five_is_diagnostic_no_tests_and_manifest_is_reader_safe(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    result = run_ci(checkout, fake_python, state, "test", FAKE_PYTEST_STATUS="5")
    assert result.returncode != 0
    evidence = manifest(state)
    row = evidence["lanes"][0]
    assert row["status"] == "no_tests"
    assert row["exit_code"] == 5
    assert row["log"] == "logs/test.log"
    assert evidence["complete"] is True
    assert (state / "minotaur-ci/runs" / evidence["run_id"] / row["log"]).is_file()


def test_source_copy_keeps_dirty_visible_entries_and_excludes_ignored(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    (checkout / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    untracked = checkout / "untracked.sh"
    untracked.write_text("untracked\n", encoding="utf-8")
    untracked.chmod(0o755)
    (checkout / "link.txt").symlink_to("tracked.txt")
    (checkout / "ignored.txt").write_text("must not copy\n", encoding="utf-8")
    observed = state / "observed.log"
    result = run_ci(checkout, fake_python, state, "test", FAKE_OBSERVED=str(observed))
    assert result.returncode == 0
    lines = observed.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("cwd=") and str(checkout) not in line for line in lines)
    assert "tracked.txt=file:dirty" in lines
    assert "untracked.sh=file:untracked" in lines
    assert "link.txt=symlink:tracked.txt" in lines
    assert "ignored.txt=absent" in lines


def test_browser_install_and_test_share_owned_browser_root(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    ambient = state / "ambient"
    ambient.mkdir(parents=True)
    (ambient / "sentinel").write_text("keep\n", encoding="utf-8")
    result = run_ci(
        checkout,
        fake_python,
        state,
        "browser",
        PLAYWRIGHT_BROWSERS_PATH=str(ambient),
    )
    assert result.returncode == 0
    roots = [line.split("\t", 1)[1] for line in (state / "browser.log").read_text().splitlines()]
    assert len(roots) == 2
    assert roots[0] == roots[1]
    assert roots[0] != str(ambient)
    assert not Path(roots[0]).exists()
    assert (ambient / "sentinel").read_text(encoding="utf-8") == "keep\n"


def test_invalid_limits_fail_before_any_payload(fixture: tuple[Path, Path, Path]) -> None:
    checkout, fake_python, state = fixture
    for variable, value in (
        ("MINOTAUR_CI_TIMEOUT_SECONDS", ""),
        ("MINOTAUR_CI_TIMEOUT_SECONDS", "0"),
        ("MINOTAUR_CI_TIMEOUT_SECONDS", "-1"),
        ("MINOTAUR_CI_TIMEOUT_SECONDS", "1.5"),
        ("MINOTAUR_CI_TERM_GRACE_SECONDS", "nope"),
    ):
        result = run_ci(checkout, fake_python, state, "test", **{variable: value})
        assert result.returncode == 2
        assert not (state / "calls.log").exists()


def test_timeout_kills_owned_group_and_removes_disposable_root(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    (state / "tmp").mkdir(parents=True)
    result = run_ci(
        checkout,
        fake_python,
        state,
        "test",
        MINOTAUR_CI_TIMEOUT_SECONDS="1",
        MINOTAUR_CI_TERM_GRACE_SECONDS="1",
        FAKE_PYTEST_SLEEP="5",
        TMPDIR=str(state / "tmp"),
    )
    assert result.returncode != 0
    evidence = manifest(state)
    assert evidence["lanes"][0]["status"] == "timeout"
    assert evidence["lanes"][0]["limits"] == {"timeout_seconds": 1, "term_grace_seconds": 1}
    assert not list((state / "tmp").glob("minotaur-ci.*/source"))


def test_sigint_marks_active_and_pending_lanes(fixture: tuple[Path, Path, Path]) -> None:
    checkout, fake_python, state = fixture
    env = os.environ.copy()
    env.update(
        {
            "PYTHON_BIN": str(fake_python),
            "XDG_STATE_HOME": str(state),
            "FAKE_LOG": str(state / "calls.log"),
            "FAKE_PYTEST_SLEEP": "20",
            "MINOTAUR_CI_TIMEOUT_SECONDS": "10",
            "MINOTAUR_CI_TERM_GRACE_SECONDS": "1",
        }
    )
    process = subprocess.Popen([str(checkout / "scripts/run_ci.sh"), "all"], cwd=checkout, env=env)
    for _ in range(200):
        calls = state / "calls.log"
        if calls.is_file() and "-m venv" in calls.read_text(encoding="utf-8"):
            break
        time.sleep(0.02)
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=10) != 0
    evidence = manifest(state)
    assert evidence["lanes"][0]["status"] == "interrupted"
    assert all(row["status"] == "not_run" for row in evidence["lanes"][1:])
