# Graph freshness and snapshot order

Minotaur answers graph queries from a recorded snapshot. The analyzer records
the selected root-relative targets and the exact SHA-256 bytes of every file
node. A later command may compare those records with the workspace, but the
comparison is deliberately bounded: it is not a general file-watcher and it
does not promise to notice every change a filesystem can represent.

The table below is the contract. The mechanism column names the implementation
owner so a reader can verify the claim, and the observable column is deliberately
literal because agents commonly parse these messages and JSON fields.

| Sequence | Detected? | Mechanism (function and file) | Observable (stderr / JSON field / exit code) | Guard or escape hatch |
| --- | --- | --- | --- | --- |
| `analyze`, edit a tracked `.py`, then query | yes | `drift.changed` in `minotaur/query/freshness.py` (AC-03 scenario (a)) | `minotaur: refreshed graph (N drifted paths)`; `minotaur: stale: <path>`; JSON `refreshed: true`, `stale: [<path>]`; exit `0` when no diagnostics | Re-run `analyze --force` when the intended selection also changed |
| `analyze`, delete a tracked file, then query | yes | `drift.missing` in `minotaur/query/freshness.py` (AC-03 scenario (d)) | The same refresh and stale lines; JSON `stale`; exit `0` when the replacement analysis is clean | Recreate the file or analyze the desired targets again |
| `analyze` a directory, add a `.py` below that recorded directory, then query | yes | `_added_files` and `drift.added` in `minotaur/query/freshness.py` (AC-03 scenario (c)) | `minotaur: refreshed graph (N drifted paths)`; `minotaur: stale: <path>`; JSON `refreshed: true`; exit `0` when clean | Analyze a different target set if the new file is intentionally out of scope |
| `analyze`, rename a tracked file, then query | yes | `drift.missing` plus `drift.added` in `minotaur/query/freshness.py` (AC-03 scenario (e)) | Both root-relative paths are reported as `minotaur: stale: <path>`; JSON `stale` contains both; exit `0` when clean | Analyze the renamed target explicitly if it is now outside the recorded directory |
| `analyze`, switch branches so selected bytes differ, then query | yes | `drift.changed` in `minotaur/query/freshness.py` (same changed-byte observable as AC-03 scenario (a)) | Refresh and stale diagnostics name the changed paths; JSON has `refreshed: true`; exit `0` when clean | Treat the resulting graph as the new branch snapshot |
| `analyze`, edit a tracked `.py`, then query with `--no-refresh` | yes, without refreshing | `drift.changed` and `_load_and_refresh_graph` in `minotaur/query/freshness.py` (AC-03 scenario (b)) | `minotaur: stale: <path>` with no refreshed line; JSON `refreshed: false`, `stale: [<path>]`; exit `0` for a clean answer or `1` if the saved graph has diagnostics | Omit `--no-refresh` when current facts are required |
| `analyze`, then `touch` a tracked file without changing bytes | no drift | `hashlib.sha256` comparison in `drift` (`minotaur/query/freshness.py`; AC-03 scenario (f)) | No stderr freshness line; JSON `refreshed: false`, `stale: []`; exit `0` | Change the bytes or use `analyze --force` if a new snapshot is required for another reason |
| `analyze`, edit a tracked file, then restore identical bytes | no drift | Byte comparison in `drift` (`minotaur/query/freshness.py`; AC-03 scenario (g)) | No refresh or stale diagnostic; JSON `stale: []`; exit `0` | None is needed: the snapshot is byte-current again |
| `analyze` a target, add a new `.py` outside every recorded directory | no | `_added_files` only walks `recorded_selection` in `minotaur/query/freshness.py` (AC-18: `test_new_python_outside_recorded_target_is_not_detected`) | No refresh; JSON `refreshed: false`, `stale: []`; exit `0` | Analyze the containing directory or file explicitly |
| `analyze`, edit a non-`.py` file | no | The registry selects only supported extensions; no file node reaches `drift` (`minotaur/language_interpreter/registry.py`; AC-18: `test_non_python_edit_is_not_detected`) | No refresh; JSON `stale: []`; exit `0` | Analyze the supported source that consumes the data, if appropriate |
| `analyze`, edit a file under an excluded or hidden directory | no | `_is_excluded` in `minotaur/language_interpreter/selection.py` prevents discovery (AC-18: `test_excluded_and_hidden_directory_edits_are_not_detected`) | No refresh; JSON `refreshed: false`, `stale: []`; exit `0` | Explicitly select the excluded target when it is intentionally in scope |
| `analyze`, edit a file reached only through an out-of-root symlink | no | `select_sources` resolves and rejects the escape in `minotaur/language_interpreter/selection.py` (AC-18: `test_out_of_root_symlink_edit_is_not_detected`) | No refresh; JSON `stale: []`; exit `0` | Analyze a root that contains the resolved file, or copy it inside the root |
| `analyze` a file with a parse failure, then edit that file | no `changed`/`missing` finding | `interpreter.py` omits the failed file node after `SyntaxError`; `drift` can compare no recorded hash (AC-18: `test_parse_failed_file_has_no_changed_or_missing_finding_but_new_file_is_added`) | `changed` and `missing` remain empty; exit `0` for direct drift inspection | Fix the parse error, then analyze; a new supported file below a recorded directory is reported as `added` |
| Load a graph with no recorded selection and query after source drift | no automatic refresh | `recorded_selection` and the guard in `_load_and_refresh_graph` (`minotaur/query/freshness.py`, `minotaur/cli.py`; AC-18: `test_graph_without_recorded_selection_refuses_automatic_refresh`) | `graph has no recorded source selection; cannot refresh`; exit `2` | Re-run `analyze` so the graph records its targets |
| Hand-edit graph bytes and regenerate its sidecar, then read it | graph-file integrity is not detected on the trusted path | `load_graph_file` and `validate_document(..., verify_node_ids=False)` in `minotaur/graph_model/loading.py` (AC-18: `test_regenerated_sidecar_trusts_hand_edited_graph_until_validate`) | The trusted read loads without a finding; `--validate` runs the full check and reports a node-ID mismatch; exit `2` for the invalid validated read | Pass `--validate` after external graph edits |
| Run `analyze` with an existing graph and a clean source selection | clean-skip only when all three conditions hold | The probe in `minotaur/cli.py` compares `drift(...).is_clean`, `recorded_selection`, and Git `source_control` (AC-03 scenario (h), after (f)) | `minotaur: graph is up to date, skipping analysis`; exit `0` | `--force` bypasses the probe |
| Query a graph whose bytes are clean but its selection metadata differs from the requested analyze targets | queries compare drift only | `_load_and_refresh_graph` calls `drift`; the analyze probe additionally compares selection and source control (`minotaur/cli.py`; AC-18: `test_query_ignores_selection_mismatch_but_analyze_reconciles_it`) | Query may answer without refresh; `analyze` re-runs instead of printing the clean-skip line | Use `analyze` to reconcile the recorded target set |
| An edit lands after `drift()` and before the answer is printed | no concurrency detection | No lock or `flock`/`fcntl` guard exists around freshness and answer production (`minotaur/query/freshness.py`, `minotaur/cli.py`; AC-18: `test_edit_after_drift_is_not_detected_between_drift_and_answer`) | The answer can describe the pre-edit snapshot; no special diagnostic is promised | Coordinate writers externally when a consistent multi-process snapshot matters |
| Run `query diff` | no source freshness check | `_run_diff` in `minotaur/cli.py` loads and compares two graph files directly (AC-18: `test_diff_does_not_call_source_drift`) | The normal diff text or JSON; exit `0` for a valid comparison; no `stale` field is added | Use a freshness-checked graph query first when comparing current source |
| Run `query context` | no graph refresh; per-file hash comparison only | `_run_context` and `context` in `minotaur/cli.py` and `minotaur/query/context.py` (AC-18: `test_context_does_not_call_source_drift_and_no_refresh_is_a_noop`) | Text begins `[file changed since analysis]` when bytes differ, or JSON has `stale: true`; exit `0` | Read the marker as a current-source warning, not as a graph refresh |
| Run `query context --no-refresh` | latent no-op | The shared parser accepts the option, but `_run_context` never reads it (`minotaur/cli.py`; AC-18: `test_context_does_not_call_source_drift_and_no_refresh_is_a_noop`) | Stdout, stderr, and exit code are byte-identical to `context` without the flag | Do not rely on the flag to suppress context's per-file hash marker |

## Row notes

### Tracked edit

The file node stores bytes, so changing a tracked Python file is the ordinary
refresh case. The refresh announcement comes before the per-path diagnostics,
which lets a caller distinguish a rewritten graph from a stale answer.

### Tracked deletion

Deletion is compared against the file node rather than the directory walk.
The refresh can produce an empty graph while preserving selection metadata, so
the same path can be detected if it returns later.

### Added file under a directory target

`added` is intentionally narrower than “anything new below the root.” It asks
the shared selector to rescan only each recorded directory target, preserving
the same extension, exclusion, and containment policy used by `analyze`.

### Rename

A rename has two independent facts: the old path is missing and the new path
is added. Reporting both prevents an agent from mistaking a moved file for a
simple deletion or a new unrelated source file.

### Branch switch with changed bytes

Queries use content hashes, so a checkout that changes selected bytes is
detected even if filesystem timestamps are unchanged. The refreshed graph then
describes the checked-out branch.

### Edit with `--no-refresh`

This option changes the action after detection, not the detection itself. The
saved graph remains available for historical questions, and the stale lines and
JSON fields make that choice visible to both humans and agents.

### Touch without a byte change

Timestamps are not evidence in this contract. A touch-only operation leaves the
SHA-256 comparison equal and therefore cannot trigger a refresh.

### Edit and restore

The same byte rule makes restoration meaningful: once the original bytes are
back, the graph is current again even if the file's mtime is newer.

### New file outside the recorded target

The recorded target list is an explicit scope boundary. A repository-wide
query cannot accidentally start analyzing unrelated new source merely because
it happens to share the same root directory.

### Non-Python edit

The registry currently has one `.py` registration. An unsupported file has no
file node and cannot become a source-freshness finding until a supported
interpreter owns its extension.

### Excluded or hidden directory

Ordinary recursive selection prunes hidden directories, caches, and virtual
environments. This is a discovery policy, not an error condition; explicit
selection remains the way to opt into such a path.

### Out-of-root symlink

Selection resolves a candidate before accepting it. A link whose target escapes
the analysis root is omitted, so edits to the external file cannot silently
expand the graph's authority.

### Parse-failed file

The interpreter emits a diagnostic and contributes no file node for a syntax
failure. There is consequently no recorded hash for `changed` or `missing` to
compare; once fixed, the file can reappear through the recorded directory walk
as an added path.

### Graph with no recorded selection

Older or foreign graphs may contain file nodes but no target metadata. Minotaur
refuses to guess which targets should be refreshed and returns exit `2`, making
the missing provenance actionable rather than silently broadening scope.

### Hand-edited graph with regenerated sidecar

The sidecar proves only that the current graph bytes match the sidecar digest.
It does not prove that those bytes were produced by Minotaur or that their node
IDs still agree with their identities; `--validate` is the explicit integrity
check for this accepted trusted-sidecar risk.

### Analyze clean-skip

The analyze command has to preserve more than query freshness: it also keeps
selection metadata and Git snapshot identity coherent. That is why its probe
checks three conditions even when a query would consider the source bytes clean.

### Query drift comparison

The query path intentionally asks only whether the recorded source bytes and
directory additions have drifted. Target-set and source-control reconciliation
belongs to `analyze`, which can be invoked with the desired targets.

### Concurrent edit

Freshness is a point-in-time comparison, not a lock. Atomic replacement keeps
individual graph files from being torn, but an edit or a second refresh can win
after the comparison; callers needing serialization must provide coordination.

### `diff`

`diff` compares two supplied snapshots and has no source root argument. Its
result is therefore about graph-to-graph structure, not whether either graph
still matches files on disk.

### `context`

`context` serves current source excerpts and independently marks a changed file
using its recorded hash. It does not rewrite the graph, so the marker must not
be interpreted as proof that graph-backed queries have refreshed.

### `context --no-refresh`

The option is accepted by the shared query parser for consistency, but context
does not consult it. The pinning test compares the complete terminal result so
future code cannot accidentally turn this latent no-op into an undocumented
behavior change.

## Tracked edits and refresh

For a tracked supported file, `drift` resolves the query root, hashes current
bytes, and compares them with the file node's recorded
`extensions["minotaur-python"]["content_sha256"]`. Missing files and new files
under recorded directory targets use separate `missing` and `added` sets. The
query refresh path reports the sorted union before re-analyzing, so an agent can
see exactly why the answer changed. A refresh that emits source diagnostics
returns exit code `1`; a clean replacement returns `0`.

The `--no-refresh` escape hatch leaves the graph on disk and answers from its
old facts. It still prints one `minotaur: stale: <path>` line per drifted path,
and JSON still exposes `refreshed` and `stale`. It is therefore suitable for
deliberately examining a prior snapshot, not for silently treating stale facts
as current.

## Changes outside the recorded selection

The analyzer records targets, not an open-ended promise to watch the whole
workspace. New files outside those targets, unsupported files, excluded or
hidden directories, and symlink paths that resolve outside the root are outside
the freshness boundary. A parse failure is also absent from the graph, so later
edits to that failed file have no recorded hash to compare. The escape hatch is
to fix the source or select the intended target again, using `--force` when a
clean-skip probe would otherwise reuse an existing graph.

## Graph-file integrity and trusted reads

The sidecar is a digest of the graph file, not of the source tree. A matching
sidecar authorizes the trusted load path to skip schema and node-ID checks while
retaining the other semantic checks. This is an accepted risk for a graph and
sidecar edited together: the normal read can load it, while `--validate` forces
the complete check and reports a node-ID mismatch. The sidecar is not a claim
that the graph describes the current source; source freshness remains the
separate `drift` contract above.

## Analyze's clean-skip probe

`analyze` has a stricter probe than a query. It skips only when the source drift
is clean, the requested target selection equals the graph's recorded selection,
and the current Git branch/commit metadata equals the graph's metadata. Queries
compare only source drift. Thus a branch change whose selected bytes are
identical can leave a query graph fresh while still causing `analyze` to write a
new snapshot with current source-control identity.

## Snapshot queries and concurrency

`diff` compares two graph documents and does not inspect a source root.
`context` reads current source and compares the requested file's recorded hash,
but it does not refresh the graph. Its accepted `--no-refresh` spelling is a
latent no-op and is pinned as current behavior. There is no concurrency lock
between freshness detection and answer production or between two refreshers;
atomic writes prevent torn individual files, but concurrent writers remain
last-writer-wins.

## For agents

Read every `minotaur: stale:` line and the JSON `stale`/`refreshed` fields before
using a query result. Do not pass `--no-refresh` when answering a question about
current code unless stale facts are acceptable. For graph integrity after a
graph or sidecar transfer, use `--validate`; for an intentionally different
source selection, run `analyze --force` with the desired targets.
