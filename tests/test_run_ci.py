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
log_call() {
    printf '%s\t%s\t%s\n' "$name" "$PWD" "$*" >> "$FAKE_LOG"
    if [[ -n "${FAKE_ARGS:-}" ]]; then
        printf '%s\0%s\0' "$name" "$PWD" >> "$FAKE_ARGS"
        if (( $# )); then
            printf '%s\0' "$@" >> "$FAKE_ARGS"
        fi
        printf '\036' >> "$FAKE_ARGS"
    fi
}
browser_command=false
if [[ "${2-}" == playwright ]] \
    || [[ "${2-}" == pytest && "$*" == *test_visualizer_browser.py* ]]; then
    browser_command=true
fi
if [[ "$name" == python && -n "${PLAYWRIGHT_BROWSERS_PATH:-}" && "$browser_command" == true ]]; then
    printf '%s\t%s\n' "$name" "$PLAYWRIGHT_BROWSERS_PATH" >> "$FAKE_BROWSER_LOG"
    mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
    printf '%s\n' "$name" > "$PLAYWRIGHT_BROWSERS_PATH/$name.marker"
fi
if [[ "${1-}" == -m && "${2-}" == venv ]]; then
    log_call "$@"
    if [[ -n "${FAKE_OBSERVED:-}" ]]; then
        printf 'cwd=%s\n' "$PWD" >> "$FAKE_OBSERVED"
        for path in tracked.txt staged.txt deleted.txt untracked.sh link.txt ignored.txt; do
            if [[ -L "$path" ]]; then
                target="$(readlink "$path")"
                printf '%s=symlink:%s\n' "$path" "$target" >> "$FAKE_OBSERVED"
            elif [[ -e "$path" ]]; then
                content="$(cat "$path")"
                mode="$(stat -c '%a' "$path")"
                printf '%s=file:%s:mode=%s\n' "$path" "$content" "$mode" >> "$FAKE_OBSERVED"
            else
                printf '%s=absent\n' "$path" >> "$FAKE_OBSERVED"
            fi
        done
    fi
    mkdir -p "$3/bin"
    for tool in python pip ruff mypy; do cp "$0" "$3/bin/$tool"; chmod +x "$3/bin/$tool"; done
    exit 0
fi
if [[ "$name" == ruff && -n "${FAKE_NEXT_LANE_CHECK:-}" ]]; then
    descendant_pid="$(cat "$FAKE_DESCENDANT_PID")"
    if [[ -n "${FAKE_DESCENDANT_PID:-}" ]] && kill -0 "$descendant_pid" 2>/dev/null; then
        printf 'alive\n' > "$FAKE_NEXT_LANE_CHECK"
    else
        printf 'gone\n' > "$FAKE_NEXT_LANE_CHECK"
    fi
fi
if [[ "$name" == ruff && -n "${FAKE_LINT_SENTINEL:-}" ]]; then
    if [[ -e "$FAKE_LINT_SENTINEL" ]]; then
        printf 'seen\n' > "${FAKE_LINT_SENTINEL_OBSERVED:?}"
        exit 17
    fi
    printf 'clean\n' > "${FAKE_LINT_SENTINEL_OBSERVED:?}"
fi
if [[ "$name" == ruff && "${1-}" == check && -n "${FAKE_HOLD_FILE:-}" ]]; then
    : > "${FAKE_HOLD_READY:?}"
    while [[ -e "$FAKE_HOLD_FILE" ]]; do sleep 0.05; done
fi
log_call "$@"
if [[ "$name" == pip ]]; then exit "${FAKE_PIP_STATUS:-0}"; fi
if [[ "$name" == ruff ]]; then exit "${FAKE_RUFF_STATUS:-0}"; fi
if [[ "$name" == mypy ]]; then exit "${FAKE_MYPY_STATUS:-0}"; fi
if [[ "${1-}" == -m ]]; then
    case "$2" in
        pytest)
            if [[ -n "${FAKE_DESCENDANT_PID:-}" ]]; then
                if [[ -n "${FAKE_DESCENDANT_OBSERVED:-}" ]]; then
                    (
                        trap 'if [[ -d "$environment_dir" ]]; then
                            printf "present\\n" > "$FAKE_DESCENDANT_OBSERVED"
                        else
                            printf "missing\\n" > "$FAKE_DESCENDANT_OBSERVED"
                        fi
                        trap "" TERM' TERM
                        while :; do sleep 1; done
                    ) &
                else
                    (trap '' TERM; while :; do sleep 1; done) &
                fi
                printf '%s\n' "$!" > "$FAKE_DESCENDANT_PID"
            fi
            if [[ -n "${FAKE_MUTATE_CHECKOUT:-}" ]]; then
                printf 'mutation during lane\n' > "$FAKE_MUTATE_CHECKOUT/provenance.txt"
                git -C "$FAKE_MUTATE_CHECKOUT" add provenance.txt
                git -C "$FAKE_MUTATE_CHECKOUT" -c user.email=ci@example.test \
                    -c user.name=CI commit -qm 'mutate during lane'
                : > "${FAKE_MUTATION_DONE:-/dev/null}"
            fi
            if [[ -n "${FAKE_CREATE_SENTINEL:-}" ]]; then
                : > "$FAKE_CREATE_SENTINEL"
            fi
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
    (checkout / "staged.txt").write_text("baseline staged\n", encoding="utf-8")
    (checkout / "deleted.txt").write_text("baseline deleted\n", encoding="utf-8")
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


def argument_records(path: Path) -> list[tuple[str, str, list[str]]]:
    records = []
    for raw in path.read_bytes().split(b"\036"):
        fields = raw.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if fields:
            records.append(
                (fields[0].decode(), fields[1].decode(), [item.decode() for item in fields[2:]])
            )
    return records


def test_help_and_unknown_selector_are_nonexecuting(fixture: tuple[Path, Path, Path]) -> None:
    checkout, fake_python, state = fixture
    help_result = run_ci(checkout, fake_python, state, "--help")
    assert help_result.returncode == 0
    assert "MINOTAUR_CI_TIMEOUT_SECONDS" in help_result.stdout
    assert not (state / "calls.log").exists()
    unknown = run_ci(checkout, fake_python, state, "unknown")
    assert unknown.returncode == 2
    assert not (state / "calls.log").exists()


def test_omitted_selector_retains_all_default(fixture: tuple[Path, Path, Path]) -> None:
    checkout, fake_python, state = fixture
    result = run_ci(checkout, fake_python, state)
    assert result.returncode == 0
    assert [row["name"] for row in manifest(state)["lanes"]] == LANES


def test_all_manifest_records_default_limits_relative_logs_and_safe_metadata(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    result = run_ci(checkout, fake_python, state)
    assert result.returncode == 0
    evidence = manifest(state)
    expected_limits = {
        "test": {"timeout_seconds": 600, "term_grace_seconds": 30},
        "lint": {"timeout_seconds": 300, "term_grace_seconds": 30},
        "typecheck": {"timeout_seconds": 300, "term_grace_seconds": 30},
        "package": {"timeout_seconds": 300, "term_grace_seconds": 30},
        "browser": {"timeout_seconds": 600, "term_grace_seconds": 30},
        "build": {"timeout_seconds": 300, "term_grace_seconds": 30},
    }
    assert {row["name"]: row["limits"] for row in evidence["lanes"]} == expected_limits
    assert all(row["duration_seconds"] >= 0 for row in evidence["lanes"])
    assert all(row["log"].startswith("logs/") for row in evidence["lanes"])
    assert not {"environment", "command", "pid", "host"}.intersection(evidence)


def test_all_runs_six_lanes_and_keeps_later_lanes_after_failure(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    result = run_ci(checkout, fake_python, state, FAKE_RUFF_STATUS="7")
    assert result.returncode != 0
    evidence = manifest(state)
    assert [row["name"] for row in evidence["lanes"]] == LANES
    assert evidence["lanes"][1]["status"] == "failed"
    assert evidence["lanes"][1]["exit_code"] == 7
    assert all(row["status"] != "pending" for row in evidence["lanes"])
    assert evidence["lanes"][2]["status"] == "passed"
    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert "-m build" in calls


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("test", ["install --upgrade pip", "install -e .[dev]", "-m pytest tests/ -v"]),
        ("lint", ["install ruff==0.16.3", "check .", "format --check ."]),
        ("typecheck", ["install --upgrade pip", "install -e .[dev]", "mypy"]),
        ("package", ["install --upgrade pip", "install .", "schema"]),
        (
            "browser",
            [
                "install --upgrade pip",
                "install -e .[dev,visualizer]",
                "-m playwright install chromium",
                "-m pytest tests/test_visualizer_browser.py -v",
            ],
        ),
        ("build", ["install build", "-m build --outdir"]),
    ],
)
def test_each_selector_records_the_parity_payload_in_an_isolated_venv(
    fixture: tuple[Path, Path, Path], selector: str, expected: list[str]
) -> None:
    checkout, fake_python, state = fixture
    args_file = state / "args.bin"
    result = run_ci(checkout, fake_python, state, selector, FAKE_ARGS=str(args_file))
    assert result.returncode == 0
    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert all(fragment in calls for fragment in expected)
    venv_calls = [line for line in calls.splitlines() if "-m venv" in line]
    assert len(venv_calls) == 1
    assert str(checkout) not in calls
    records = argument_records(args_file)
    assert records
    assert all(cwd != str(checkout) for _, cwd, _ in records)
    venv = next(args for name, _, args in records if args[:2] == ["-m", "venv"])
    assert len(venv) == 3
    assert all(args[0] == "install" for _, _, args in records if args and args[0] == "install")
    if selector == "test":
        assert [args for _, _, args in records if args[:2] == ["-m", "pytest"]] == [
            ["-m", "pytest", "tests/", "-v"]
        ]
    elif selector == "lint":
        assert [args for _, _, args in records if args[:1] == ["check"]] == [["check", "."]]
        assert [args for _, _, args in records if args[:1] == ["format"]] == [
            ["format", "--check", "."]
        ]
    elif selector == "typecheck":
        assert [(name, args) for name, _, args in records if name == "mypy"] == [("mypy", [])]
    elif selector == "package":
        assert [args for _, _, args in records if args[:2] == ["install", "."]] == [
            ["install", "."]
        ]
        assert [args for _, _, args in records if args[:1] == ["-c"]] == [
            [
                "-c",
                (
                    "from minotaur.graph_model.loading import schema; "
                    'assert schema()["$id"] == "urn:minotaur:schemas:minotaur-graph:0.1.0"'
                ),
            ]
        ]
    elif selector == "browser":
        assert [args for _, _, args in records if args[:2] == ["-m", "playwright"]] == [
            ["-m", "playwright", "install", "chromium"]
        ]
        assert [args for _, _, args in records if args[:2] == ["-m", "pytest"]] == [
            ["-m", "pytest", "tests/test_visualizer_browser.py", "-v"]
        ]
    else:
        build = [args for _, _, args in records if args[:2] == ["-m", "build"]]
        assert len(build) == 1
        assert build[0][:3] == ["-m", "build", "--outdir"]
        assert build[0][3].endswith("/build")


def test_exit_five_is_accepted_and_manifest_is_reader_safe(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    result = run_ci(checkout, fake_python, state, "test", FAKE_PYTEST_STATUS="5")
    assert result.returncode == 0
    evidence = manifest(state)
    row = evidence["lanes"][0]
    assert row["status"] == "passed"
    assert row["exit_code"] == 5
    assert row["log"] == "logs/test.log"
    assert evidence["complete"] is True
    assert (state / "minotaur-ci/runs" / evidence["run_id"] / row["log"]).is_file()


def test_all_materializes_an_independent_source_copy_for_each_lane(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    sentinel = ".test-lane-sentinel"
    observed = state / "lint-sentinel"
    result = run_ci(
        checkout,
        fake_python,
        state,
        "all",
        FAKE_CREATE_SENTINEL=sentinel,
        FAKE_LINT_SENTINEL=sentinel,
        FAKE_LINT_SENTINEL_OBSERVED=str(observed),
    )
    assert result.returncode == 0
    assert observed.read_text(encoding="utf-8") == "clean\n"


def test_source_copy_keeps_dirty_visible_entries_and_excludes_ignored(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    (checkout / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (checkout / "tracked.txt").chmod(0o755)
    (checkout / "staged.txt").write_text("staged dirty\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=checkout, check=True)
    (checkout / "deleted.txt").unlink()
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
    assert "tracked.txt=file:dirty:mode=755" in lines
    assert "staged.txt=file:staged dirty:mode=664" in lines
    assert "deleted.txt=absent" in lines
    assert "untracked.sh=file:untracked:mode=755" in lines
    assert "link.txt=symlink:tracked.txt" in lines
    assert "ignored.txt=absent" in lines


def test_dirty_input_is_recorded_diagnostic_only(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    (checkout / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    result = run_ci(checkout, fake_python, state, "test")
    assert result.returncode == 0
    evidence = manifest(state)
    assert evidence["initial"]["clean"] is False
    assert evidence["final"]["clean"] is False
    assert evidence["final"]["changed"] is False
    assert evidence["diagnostic_only"] is True


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


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        ({"FAKE_PLAYWRIGHT_STATUS": "7"}, "failed"),
        ({"FAKE_PYTEST_STATUS": "5"}, "failed"),
        (
            {
                "FAKE_PYTEST_SLEEP": "5",
                "MINOTAUR_CI_TIMEOUT_SECONDS": "1",
                "MINOTAUR_CI_TERM_GRACE_SECONDS": "1",
            },
            "timeout",
        ),
    ],
)
def test_browser_failure_and_timeout_are_nonpass_and_remove_owned_root(
    fixture: tuple[Path, Path, Path], extra: dict[str, str], status: str
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
        **extra,
        PLAYWRIGHT_BROWSERS_PATH=str(ambient),
    )
    assert result.returncode != 0
    evidence = manifest(state)
    assert evidence["lanes"][0]["status"] == status
    if extra.get("FAKE_PYTEST_STATUS") == "5":
        assert evidence["lanes"][0]["exit_code"] == 5
    roots = [line.split("\t", 1)[1] for line in (state / "browser.log").read_text().splitlines()]
    assert roots and all(root != str(ambient) for root in roots)
    assert not Path(roots[0]).exists()
    assert (ambient / "sentinel").read_text(encoding="utf-8") == "keep\n"


def test_invalid_limits_fail_before_any_payload(fixture: tuple[Path, Path, Path]) -> None:
    checkout, fake_python, state = fixture
    for variable in ("MINOTAUR_CI_TIMEOUT_SECONDS", "MINOTAUR_CI_TERM_GRACE_SECONDS"):
        for value in ("", "0", "-1", "1.5", "nope"):
            result = run_ci(checkout, fake_python, state, "test", **{variable: value})
            assert result.returncode == 2
            assert not (state / "calls.log").exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"MINOTAUR_CI_TIMEOUT_SECONDS": "2"},
        {"MINOTAUR_CI_TERM_GRACE_SECONDS": "2"},
        {"MINOTAUR_CI_TIMEOUT_SECONDS": "2", "MINOTAUR_CI_TERM_GRACE_SECONDS": "2"},
    ],
)
def test_valid_limit_overrides_are_recorded_diagnostic_only(
    fixture: tuple[Path, Path, Path], overrides: dict[str, str]
) -> None:
    checkout, fake_python, state = fixture
    result = run_ci(checkout, fake_python, state, "test", **overrides)
    assert result.returncode == 0
    evidence = manifest(state)
    assert evidence["diagnostic_only"] is True
    limits = evidence["lanes"][0]["limits"]
    assert limits["timeout_seconds"] == int(overrides.get("MINOTAUR_CI_TIMEOUT_SECONDS", 600))
    assert limits["term_grace_seconds"] == int(overrides.get("MINOTAUR_CI_TERM_GRACE_SECONDS", 30))


def test_concurrent_invocations_keep_distinct_complete_result_roots(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    processes: list[subprocess.Popen[bytes]] = []
    state.mkdir(parents=True)
    for index in range(2):
        env = os.environ.copy()
        env.update(
            {
                "PYTHON_BIN": str(fake_python),
                "XDG_STATE_HOME": str(state),
                "FAKE_LOG": str(state / f"calls-{index}.log"),
                "FAKE_BROWSER_LOG": str(state / f"browser-{index}.log"),
                "FAKE_PYTEST_SLEEP": "1",
            }
        )
        processes.append(
            subprocess.Popen([str(checkout / "scripts/run_ci.sh"), "test"], cwd=checkout, env=env)
        )
    assert all(process.wait(timeout=30) == 0 for process in processes)
    roots = list((state / "minotaur-ci/runs").glob("*/result.json"))
    assert len({path.parent for path in roots}) == 2
    assert all(json.loads(path.read_text())["complete"] for path in roots)


def test_manifest_is_reader_safe_while_a_later_lane_is_held(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    hold = state / "hold"
    ready = state / "ready"
    state.mkdir(parents=True)
    hold.touch()
    env = os.environ.copy()
    env.update(
        {
            "PYTHON_BIN": str(fake_python),
            "XDG_STATE_HOME": str(state),
            "FAKE_LOG": str(state / "calls.log"),
            "FAKE_BROWSER_LOG": str(state / "browser.log"),
            "FAKE_HOLD_FILE": str(hold),
            "FAKE_HOLD_READY": str(ready),
        }
    )
    process = subprocess.Popen([str(checkout / "scripts/run_ci.sh"), "all"], cwd=checkout, env=env)
    for _ in range(300):
        if ready.exists():
            break
        time.sleep(0.02)
    result_path = next((state / "minotaur-ci/runs").glob("*/result.json"))
    evidence = json.loads(result_path.read_text(encoding="utf-8"))
    assert evidence["lanes"][0]["status"] == "passed"
    assert all(row["status"] == "pending" for row in evidence["lanes"][1:])
    hold.unlink()
    assert process.wait(timeout=30) == 0


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
    assert evidence["lanes"][0]["exit_code"] == 124
    assert evidence["lanes"][0]["limits"] == {"timeout_seconds": 1, "term_grace_seconds": 1}
    assert not list((state / "tmp").glob("minotaur-ci.*/source"))


def test_all_timeout_removes_every_lane_source_copy(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    tmp_root = state / "tmp"
    tmp_root.mkdir(parents=True)
    result = run_ci(
        checkout,
        fake_python,
        state,
        "all",
        MINOTAUR_CI_TIMEOUT_SECONDS="1",
        MINOTAUR_CI_TERM_GRACE_SECONDS="1",
        FAKE_PYTEST_SLEEP="5",
        TMPDIR=str(tmp_root),
    )
    assert result.returncode != 0
    assert not list(tmp_root.glob("minotaur-ci.*"))


def test_timeout_preserves_unrelated_process(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    sentinel = subprocess.Popen(["sleep", "20"])
    try:
        result = run_ci(
            checkout,
            fake_python,
            state,
            "test",
            MINOTAUR_CI_TIMEOUT_SECONDS="1",
            MINOTAUR_CI_TERM_GRACE_SECONDS="1",
            FAKE_PYTEST_SLEEP="5",
        )
        assert result.returncode != 0
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)


def _assert_dead(pid_file: Path) -> None:
    pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    pytest.fail(f"descendant {pid} is still alive")


def test_timeout_drains_term_ignoring_descendant_before_result_cleanup(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    descendant = state / "descendant.pid"
    observed = state / "descendant.observed"
    result = run_ci(
        checkout,
        fake_python,
        state,
        "test",
        MINOTAUR_CI_TIMEOUT_SECONDS="1",
        MINOTAUR_CI_TERM_GRACE_SECONDS="1",
        FAKE_PYTEST_SLEEP="20",
        FAKE_DESCENDANT_PID=str(descendant),
        FAKE_DESCENDANT_OBSERVED=str(observed),
    )
    assert result.returncode != 0
    evidence = manifest(state)
    assert evidence["lanes"][0]["status"] == "timeout"
    assert evidence["lanes"][0]["exit_code"] == 124
    _assert_dead(descendant)
    assert observed.read_text(encoding="utf-8") == "present\n"


def test_ordinary_failure_drains_descendant_before_next_lane(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    descendant = state / "descendant.pid"
    next_lane = state / "next-lane.observed"
    result = run_ci(
        checkout,
        fake_python,
        state,
        "all",
        FAKE_PYTEST_STATUS="7",
        FAKE_DESCENDANT_PID=str(descendant),
        FAKE_NEXT_LANE_CHECK=str(next_lane),
        MINOTAUR_CI_TERM_GRACE_SECONDS="1",
    )
    assert result.returncode != 0
    evidence = manifest(state)
    assert evidence["lanes"][0]["status"] == "failed"
    assert evidence["lanes"][0]["exit_code"] == 7
    assert evidence["lanes"][1]["status"] == "passed"
    assert next_lane.read_text(encoding="utf-8") == "gone\n"


def test_success_drains_descendant_before_next_lane(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    descendant = state / "descendant.pid"
    next_lane = state / "next-lane.observed"
    result = run_ci(
        checkout,
        fake_python,
        state,
        "all",
        FAKE_DESCENDANT_PID=str(descendant),
        FAKE_NEXT_LANE_CHECK=str(next_lane),
        MINOTAUR_CI_TERM_GRACE_SECONDS="1",
    )
    assert result.returncode == 0
    assert manifest(state)["outcome"] == "passed"
    assert next_lane.read_text(encoding="utf-8") == "gone\n"


def test_missing_setsid_marks_all_lanes_setup_failed_without_payload(
    fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    checkout, fake_python, state = fixture
    tool_path = tmp_path / "tools"
    tool_path.mkdir()
    for directory in (Path("/usr/bin"), Path("/bin")):
        for candidate in directory.iterdir():
            if (
                candidate.name == "setsid"
                or not candidate.is_file()
                or not os.access(candidate, os.X_OK)
            ):
                continue
            target = tool_path / candidate.name
            if not target.exists():
                target.symlink_to(candidate)
    result = run_ci(checkout, fake_python, state, "all", PATH=str(tool_path))
    assert result.returncode != 0
    evidence = manifest(state)
    assert [row["name"] for row in evidence["lanes"]] == LANES
    assert all(row["status"] == "setup_failed" for row in evidence["lanes"])
    assert not (state / "calls.log").exists()


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
    assert process.wait(timeout=30) != 0
    evidence = manifest(state)
    assert evidence["lanes"][0]["status"] == "interrupted"
    assert all(row["status"] == "not_run" for row in evidence["lanes"][1:])


def test_sigint_handles_parent_inherited_ignored_disposition(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    env = os.environ.copy()
    env.update(
        {
            "PYTHON_BIN": str(fake_python),
            "XDG_STATE_HOME": str(state),
            "FAKE_LOG": str(state / "calls.log"),
            "FAKE_PYTEST_SLEEP": "5",
            "MINOTAUR_CI_TIMEOUT_SECONDS": "1",
            "MINOTAUR_CI_TERM_GRACE_SECONDS": "1",
        }
    )
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        process = subprocess.Popen(
            [str(checkout / "scripts/run_ci.sh"), "all"], cwd=checkout, env=env
        )
    finally:
        signal.signal(signal.SIGINT, previous)
    for _ in range(200):
        calls = state / "calls.log"
        if calls.is_file() and "-m venv" in calls.read_text(encoding="utf-8"):
            break
        time.sleep(0.02)
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=30) != 0
    evidence = manifest(state)
    assert evidence["lanes"][0]["status"] == "interrupted"
    assert all(row["status"] == "not_run" for row in evidence["lanes"][1:])


def test_sigterm_records_final_provenance_after_lane_changes_checkout(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    env = os.environ.copy()
    env.update(
        {
            "PYTHON_BIN": str(fake_python),
            "XDG_STATE_HOME": str(state),
            "FAKE_LOG": str(state / "calls.log"),
            "FAKE_MUTATE_CHECKOUT": str(checkout),
            "FAKE_MUTATION_DONE": str(state / "mutation.done"),
            "FAKE_PYTEST_SLEEP": "20",
            "MINOTAUR_CI_TIMEOUT_SECONDS": "10",
            "MINOTAUR_CI_TERM_GRACE_SECONDS": "1",
        }
    )
    process = subprocess.Popen([str(checkout / "scripts/run_ci.sh"), "all"], cwd=checkout, env=env)
    for _ in range(200):
        if (state / "mutation.done").exists():
            break
        time.sleep(0.02)
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=30) != 0
    evidence = manifest(state)
    assert evidence["lanes"][0]["status"] == "interrupted"
    assert all(row["status"] == "not_run" for row in evidence["lanes"][1:])
    assert evidence["final"]["commit"] != evidence["initial"]["commit"]
    assert evidence["final"]["clean"] is True
    assert evidence["final"]["changed"] is True
    assert evidence["diagnostic_only"] is True


def test_normal_completion_records_commit_mutation_as_diagnostic(
    fixture: tuple[Path, Path, Path],
) -> None:
    checkout, fake_python, state = fixture
    mutation_done = state / "mutation.done"
    result = run_ci(
        checkout,
        fake_python,
        state,
        "test",
        FAKE_MUTATE_CHECKOUT=str(checkout),
        FAKE_MUTATION_DONE=str(mutation_done),
    )
    assert result.returncode == 0
    assert mutation_done.exists()
    evidence = manifest(state)
    assert evidence["initial"]["clean"] is True
    assert evidence["final"]["clean"] is True
    assert evidence["final"]["commit"] != evidence["initial"]["commit"]
    assert evidence["final"]["changed"] is True
    assert evidence["diagnostic_only"] is True


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_browser_interrupt_removes_owned_root_and_stops_pending_lanes(
    fixture: tuple[Path, Path, Path], interrupt: signal.Signals
) -> None:
    checkout, fake_python, state = fixture
    ambient = state / "ambient"
    ambient.mkdir(parents=True)
    (ambient / "sentinel").write_text("keep\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PYTHON_BIN": str(fake_python),
            "XDG_STATE_HOME": str(state),
            "FAKE_LOG": str(state / "calls.log"),
            "FAKE_BROWSER_LOG": str(state / "browser.log"),
            "PLAYWRIGHT_BROWSERS_PATH": str(ambient),
            "FAKE_PYTEST_SLEEP": "20",
            "MINOTAUR_CI_TIMEOUT_SECONDS": "10",
            "MINOTAUR_CI_TERM_GRACE_SECONDS": "1",
        }
    )
    process = subprocess.Popen(
        [str(checkout / "scripts/run_ci.sh"), "browser"], cwd=checkout, env=env
    )
    for _ in range(500):
        browser_log = state / "browser.log"
        if browser_log.is_file() and len(browser_log.read_text().splitlines()) >= 2:
            time.sleep(0.2)
            break
        time.sleep(0.02)
    process.send_signal(interrupt)
    assert process.wait(timeout=30) != 0
    evidence = manifest(state)
    browser_row = next(row for row in evidence["lanes"] if row["name"] == "browser")
    assert browser_row["status"] == "interrupted"
    assert len(evidence["lanes"]) == 1
    roots = [line.split("\t", 1)[1] for line in (state / "browser.log").read_text().splitlines()]
    assert roots and not Path(roots[0]).exists()
    assert (ambient / "sentinel").read_text(encoding="utf-8") == "keep\n"
