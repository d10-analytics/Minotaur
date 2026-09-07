#!/usr/bin/env bash
# Run the repository's GitHub Actions checks in fresh temporary environments.

if [[ -z "${MINOTAUR_CI_SIGNAL_RESET-}" ]]; then
    export MINOTAUR_CI_SIGNAL_RESET=1
    exec python3 - "$0" "$@" <<'PY'
import os
import signal
import sys

script = os.path.abspath(sys.argv[1])
signal.signal(signal.SIGINT, signal.SIG_DFL)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
shell = os.environ.get("BASH", "/bin/bash")
os.execv(shell, [shell, script, *sys.argv[2:]])
PY
fi

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
checkout_root="$(cd -- "$script_dir/.." && pwd -P)"
cd "$checkout_root"

python_bin="${PYTHON_BIN:-python3}"
lanes=(test lint typecheck package browser build)

declare -A default_timeouts=(
    [test]=600
    [lint]=300
    [typecheck]=300
    [package]=300
    [browser]=600
    [build]=300
)

usage() {
    cat <<'EOF'
Usage: scripts/run_ci.sh [all|test|lint|typecheck|package|browser|build]

Run one GitHub Actions parity lane locally. Each lane uses a fresh temporary
virtual environment and an invocation-owned source copy. "all" runs test,
lint, typecheck, package, browser, then build and continues after ordinary
lane failures.

Set PYTHON_BIN to choose the Python interpreter used to create each venv.
Set MINOTAUR_CI_TIMEOUT_SECONDS or MINOTAUR_CI_TERM_GRACE_SECONDS to positive
integer values for bounded diagnostic runs. Overrides are recorded in the
result and make the run diagnostic-only. Results and retained lane logs are
written below ${XDG_STATE_HOME:-$HOME/.local/state}/minotaur-ci/runs.
Dirty input is accepted for diagnosis; only an unchanged clean revision with
default limits is eligible as final acceptance evidence.
EOF
}

positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

timeout_override="${MINOTAUR_CI_TIMEOUT_SECONDS-}"
grace_override="${MINOTAUR_CI_TERM_GRACE_SECONDS-}"
timeout_override_set="${MINOTAUR_CI_TIMEOUT_SECONDS+x}"
grace_override_set="${MINOTAUR_CI_TERM_GRACE_SECONDS+x}"
if [[ -n "$timeout_override_set" ]] && ! positive_integer "$timeout_override"; then
    usage >&2
    printf 'MINOTAUR_CI_TIMEOUT_SECONDS must be a positive integer\n' >&2
    exit 2
fi
if [[ -n "$grace_override_set" ]] && ! positive_integer "$grace_override"; then
    usage >&2
    printf 'MINOTAUR_CI_TERM_GRACE_SECONDS must be a positive integer\n' >&2
    exit 2
fi

selector="${1:-all}"
case "$selector" in
    --help|-h)
        usage
        exit 0
        ;;
    all)
        selected_lanes=("${lanes[@]}")
        ;;
    test|lint|typecheck|package|browser|build)
        selected_lanes=("$selector")
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

timeout_for() {
    if [[ -n "$timeout_override" ]]; then
        printf '%s' "$timeout_override"
    else
        printf '%s' "${default_timeouts[$1]}"
    fi
}

state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
results_root="$state_home/minotaur-ci/runs"
mkdir -p "$results_root"
run_id="$(date +%Y%m%dT%H%M%S%N)-$RANDOM"
run_root="$results_root/$run_id"
while ! mkdir "$run_root" 2>/dev/null; do
    run_id="$(date +%Y%m%dT%H%M%S%N)-$RANDOM"
    run_root="$results_root/$run_id"
done
logs_root="$run_root/logs"
mkdir -p "$logs_root"
manifest="$run_root/result.json"

temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/minotaur-ci.XXXXXX")"
source_copy="$temporary_root/source"
build_output="$temporary_root/build"
browser_root="$temporary_root/browser"
mkdir -p "$build_output" "$browser_root"
cleanup_temporary_root() {
    rm -rf "$temporary_root"
}
trap cleanup_temporary_root EXIT

initial_commit="$(git rev-parse HEAD)"
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    initial_clean=false
else
    initial_clean=true
fi

declare -A lane_status lane_exit lane_duration lane_log lane_timeout lane_environment
for lane in "${selected_lanes[@]}"; do
    lane_status[$lane]=pending
    lane_exit[$lane]=null
    lane_duration[$lane]=0
    lane_log[$lane]="logs/$lane.log"
    lane_timeout[$lane]="$(timeout_for "$lane")"
    lane_environment[$lane]="$temporary_root/env-$lane"
    : > "$run_root/${lane_log[$lane]}"
done

diagnostic_only=false
if [[ "$initial_clean" != true || -n "$timeout_override$grace_override" ]]; then
    diagnostic_only=true
fi
overall_status=running
final_commit="$initial_commit"
final_clean="$initial_clean"
final_changed=false
active_pid=""
active_grace=30
interrupted_signal=""

signal_active_group() {
    interrupted_signal="$1"
    if [[ -n "$active_pid" ]]; then
        kill -TERM -- "-$active_pid" 2>/dev/null || true
        if declare -F terminate_group >/dev/null 2>&1; then
            terminate_group "$active_pid" "$active_grace"
        fi
    fi
}
trap 'signal_active_group INT' INT
trap 'signal_active_group TERM' TERM

write_manifest() {
    local finished="${1:-false}"
    local lane_data=''
    local lane
    for lane in "${selected_lanes[@]}"; do
        lane_data+="$lane|${lane_status[$lane]}|${lane_exit[$lane]}|${lane_duration[$lane]}|${lane_timeout[$lane]}|${grace_override:-30}|${lane_log[$lane]}"$'\n'
    done
    MANIFEST_PATH="$manifest" \
    MANIFEST_RUN_ID="$run_id" \
    MANIFEST_SELECTOR="$selector" \
    MANIFEST_INITIAL_COMMIT="$initial_commit" \
    MANIFEST_INITIAL_CLEAN="$initial_clean" \
    MANIFEST_FINAL_COMMIT="$final_commit" \
    MANIFEST_FINAL_CLEAN="$final_clean" \
    MANIFEST_FINAL_CHANGED="$final_changed" \
    MANIFEST_DIAGNOSTIC_ONLY="$diagnostic_only" \
    MANIFEST_TIMEOUT="$(timeout_for "${selected_lanes[0]}")" \
    MANIFEST_GRACE="${grace_override:-30}" \
    MANIFEST_STATUS="$overall_status" \
    MANIFEST_FINISHED="$finished" \
    MANIFEST_LANES="$lane_data" \
    python3 - "$manifest" <<'PY'
import json
import os
import pathlib
import tempfile

def boolean(name: str) -> bool:
    return os.environ[name] == "true"

rows = []
for line in os.environ["MANIFEST_LANES"].splitlines():
    name, status, exit_code, duration, timeout, grace, log = line.split("|", 6)
    rows.append({
        "name": name,
        "status": status,
        "exit_code": None if exit_code == "null" else int(exit_code),
        "duration_seconds": float(duration),
        "limits": {
            "timeout_seconds": int(timeout),
            "term_grace_seconds": int(grace),
        },
        "log": log,
    })
payload = {
    "schema_version": 1,
    "run_id": os.environ["MANIFEST_RUN_ID"],
    "selector": os.environ["MANIFEST_SELECTOR"],
    "initial": {
        "commit": os.environ["MANIFEST_INITIAL_COMMIT"],
        "clean": boolean("MANIFEST_INITIAL_CLEAN"),
    },
    "final": {
        "commit": os.environ["MANIFEST_FINAL_COMMIT"],
        "clean": boolean("MANIFEST_FINAL_CLEAN"),
        "changed": boolean("MANIFEST_FINAL_CHANGED"),
    },
    "diagnostic_only": boolean("MANIFEST_DIAGNOSTIC_ONLY"),
    "limits": {
        "timeout_seconds": int(os.environ["MANIFEST_TIMEOUT"]),
        "term_grace_seconds": int(os.environ["MANIFEST_GRACE"]),
    },
    "lanes": rows,
    "outcome": os.environ["MANIFEST_STATUS"],
    "complete": boolean("MANIFEST_FINISHED"),
}
path = pathlib.Path(os.environ["MANIFEST_PATH"])
fd, temporary = tempfile.mkstemp(prefix=".result.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

write_manifest false

# Materialize the present Git-visible checkout, including dirty tracked files,
# non-ignored untracked files, modes, and symlinks. Git metadata is retained in
# an independent clone so repository-aware tests can create disposable trees.
copy_source() {
    local relative destination
    git clone --no-local "$checkout_root" "$source_copy" >/dev/null 2>&1
    git -C "$source_copy" remote remove origin >/dev/null 2>&1 || true
    find "$source_copy" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf -- {} +
    while IFS= read -r -d '' relative; do
        [[ -e "$relative" || -L "$relative" ]] || continue
        destination="$source_copy/$relative"
        mkdir -p "$(dirname -- "$destination")"
        cp -a -- "$relative" "$destination"
    done < <(git ls-files --cached --others --exclude-standard -z)
}

if ! copy_source; then
    for lane in "${selected_lanes[@]}"; do
        lane_status[$lane]=setup_failed
        lane_exit[$lane]=127
        printf 'source copy setup failed\n' > "$run_root/${lane_log[$lane]}"
    done
    overall_status=failed
    cd "$checkout_root"
    final_commit="$(git rev-parse HEAD)"
    if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
        final_clean=false
    else
        final_clean=true
    fi
    write_manifest true
    exit 1
fi
cd "$source_copy"

run_in_environment() (
    set -e
    mkdir -p "$environment_dir"
    ci_python="$environment_dir/bin/python"
    ci_pip="$environment_dir/bin/pip"
    export environment_dir ci_python ci_pip
    "$python_bin" -m venv "$environment_dir"
    "$@"
)

install_dev() {
    "$ci_pip" install --upgrade pip
    "$ci_pip" install -e ".[dev]"
}

test_lane() {
    install_dev
    "$ci_python" -m pytest tests/ -v
}

lint_lane() {
    "$ci_pip" install ruff==0.16.3
    "$environment_dir/bin/ruff" check .
    "$environment_dir/bin/ruff" format --check .
}

typecheck_lane() {
    install_dev
    "$environment_dir/bin/mypy"
}

package_lane() {
    "$ci_pip" install --upgrade pip
    "$ci_pip" install .
    "$ci_python" -c 'from minotaur.graph_model.loading import schema; assert schema()["$id"] == "urn:minotaur:schemas:minotaur-graph:0.1.0"'
}

browser_lane() {
    export PLAYWRIGHT_BROWSERS_PATH="$browser_root"
    "$ci_pip" install --upgrade pip
    "$ci_pip" install -e ".[dev,visualizer]"
    "$ci_python" -m playwright install chromium
    "$ci_python" -m pytest tests/test_visualizer_browser.py -v
}

build_lane() {
    "$ci_pip" install build
    "$ci_python" -m build --outdir "$build_output"
}

lane_body() (
    set -e
    run_in_environment "${1}_lane"
)
export -f run_in_environment install_dev test_lane lint_lane typecheck_lane
export -f package_lane browser_lane build_lane lane_body
export python_bin temporary_root browser_root build_output

terminate_group() {
    local pid="$1"
    local grace="$2"
    kill -TERM -- "-$pid" 2>/dev/null || true
    local deadline=$((SECONDS + grace))
    while (( SECONDS < deadline )); do
        kill -0 -- "-$pid" 2>/dev/null || return 0
        sleep 0.05
    done
    kill -KILL -- "-$pid" 2>/dev/null || true
    sleep 0.05
}

setsid_path="$(command -v setsid || true)"
if [[ -z "$setsid_path" ]]; then
    for lane in "${selected_lanes[@]}"; do
        lane_status[$lane]=setup_failed
        lane_exit[$lane]=127
        printf 'setsid is required but unavailable\n' > "$run_root/${lane_log[$lane]}"
    done
    overall_status=failed
    write_manifest true
    exit 1
fi

if [[ -n "$interrupted_signal" ]]; then
    for lane in "${selected_lanes[@]}"; do
        lane_status[$lane]=not_run
        lane_exit[$lane]=null
    done
    overall_status=interrupted
    cd "$checkout_root"
    final_commit="$(git rev-parse HEAD)"
    final_clean="$initial_clean"
    write_manifest true
    exit 130
fi

run_lane() {
    local lane="$1"
    local started ended status grace timeout marker watchdog
    started="$(date +%s.%N)"
    grace="${grace_override:-30}"
    active_grace="$grace"
    timeout="${lane_timeout[$lane]}"
    marker="$temporary_root/$lane.timeout"
    environment_dir="${lane_environment[$lane]}"
    export environment_dir
    rm -f "$marker"
    : > "$run_root/${lane_log[$lane]}"
    set +e
    "$setsid_path" bash -c 'lane_body "$1"' _ "$lane" > "$run_root/${lane_log[$lane]}" 2>&1 &
    active_pid=$!
    "$setsid_path" bash -c '
        sleep "$1"
        if kill -0 "$2" 2>/dev/null; then
            : > "$3"
            kill -TERM -- "-$2" 2>/dev/null || true
            sleep "$4"
            kill -KILL -- "-$2" 2>/dev/null || true
        fi
    ' _ "$timeout" "$active_pid" "$marker" "$grace" &
    watchdog=$!
    wait "$active_pid"
    status=$?
    kill -TERM -- "-$watchdog" 2>/dev/null || true
    wait "$watchdog" 2>/dev/null || true
    set -e
    ended="$(date +%s.%N)"
    lane_duration[$lane]="$(awk -v start="$started" -v end="$ended" 'BEGIN { d=end-start; if (d<0) d=0; printf "%.6f", d }')"
    terminate_group "$active_pid" "$grace"
    active_pid=""
    rm -rf "${lane_environment[$lane]}"

    if [[ -n "$interrupted_signal" ]]; then
        lane_status[$lane]=interrupted
        lane_exit[$lane]="$status"
        return 130
    fi
    if [[ -e "$marker" ]]; then
        lane_status[$lane]=timeout
        lane_exit[$lane]=124
        return 0
    fi
    lane_exit[$lane]="$status"
    if [[ "$status" -eq 0 ]]; then
        lane_status[$lane]=passed
    elif [[ "$lane" == test && "$status" -eq 5 ]]; then
        lane_status[$lane]=no_tests
    else
        lane_status[$lane]=failed
    fi
    return 0
}

for lane in "${selected_lanes[@]}"; do
    if [[ -n "$interrupted_signal" ]]; then
        lane_status[$lane]=not_run
        lane_exit[$lane]=null
        continue
    fi
    if run_lane "$lane"; then
        run_result=0
    else
        run_result=$?
    fi
    if [[ "$run_result" -ne 0 ]]; then
        overall_status=interrupted
        for pending_lane in "${selected_lanes[@]}"; do
            if [[ "${lane_status[$pending_lane]}" == pending ]]; then
                lane_status[$pending_lane]=not_run
                lane_exit[$pending_lane]=null
            fi
        done
        break
    fi
    write_manifest false
done

if [[ "$overall_status" == running ]]; then
    overall_status=passed
    for lane in "${selected_lanes[@]}"; do
        if [[ "${lane_status[$lane]}" != passed ]]; then
            overall_status=failed
            break
        fi
    done
fi

cd "$checkout_root"
final_commit="$(git rev-parse HEAD)"
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    final_clean=false
else
    final_clean=true
fi
if [[ "$final_commit" != "$initial_commit" || "$final_clean" != "$initial_clean" ]]; then
    final_changed=true
    diagnostic_only=true
fi
write_manifest true

if [[ "$overall_status" == passed ]]; then
    exit 0
fi
exit 1
