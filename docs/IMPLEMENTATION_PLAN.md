# Flexible scan targets, exclusions, and collection readiness

Status: Migrations 003 and 004 approved and implemented. Production scanning remains
unauthorized. Updated 2026-11-09.
Specifications: REQUIREMENTS.md version 0.2. No actual collection scan is authorized.

## Existing implementation and scope

Milestones 1/2 provide one SQLite database, writer locking, migrations 001/002,
folder-only provisional identities, persistent directory/file jobs, and recovery.
The approved extension adds selected-volume binding, drive roots/overlapping targets,
immutable exclusions, explicit policy successors and Migration 003. Metadata extraction
and media hashing are now integrated by approved Migration 004. Historical folder-only
records remain intact and are not relabeled as comprehensive collection.

The target collection is five static external drives, one explicit target at a
time. Initial collection must capture filesystem and embedded photo/video metadata
and complete SHA-256 hashes for photos only. Video size/metadata supports potential
duplicate candidates, never assertions of exact equality. Export, organization and
duplicate-group analysis remain later work. This plan authorizes none of that code
until approved; metadata/hashing stages and real collection scans need approval too.

## 1. Confirm persistent identity before enabling overlapping roots

Resolve only the selected target's containing volume and mount point using a
Windows adapter. Do not enumerate all drives or choose targets. Use an application
volume UUID with GUID, filesystem serial, capacity and device observations as
evidence; identifiers alone cannot resolve cloned/reformatted/ambiguous media.
Refuse ambiguous matches and require an explicit identity decision. No source
markers, writes or metadata updates are permitted.

Store exact volume-relative paths with independently versioned comparison keys.
For ordinary Windows paths, compare case-insensitively using a documented Windows
comparison implementation, component by component. SQLite NOCASE and Python
casefold alone do not reproduce every Windows filename equivalence. Reject
ambiguous case-sensitive directories until explicitly supported. Preserve display
spelling; do not collapse distinct Unicode names via unrequested normalization.
Reject drive-relative, ADS and unsupported device-namespace paths. Do not follow
links/junctions across roots or volume mount points.

Allow drive roots (`E:\`) and directories (`E:\Photos`). A media file has one
volume/path identity even if several roots observe it. Per-session observations
retain root-relative paths, while media records retain volume-relative paths.
Unchanged current filesystem/metadata/photo-hash results are reusable across roots;
observing a file in a new root does not require extracting/hash-processing it again.
Distinct hard-link paths remain distinct inventory locations in this increment.

Convert existing provisional scopes under the writer lock and one transaction.
Preserve all media IDs, foreign-key references, observations and valid processing
results. Keep original path/identity evidence and an audit mapping. Preflight
root/file/case-key collisions. Abort the entire conversion on ambiguity. A later
explicit reconciliation may map aliases or consolidate proven duplicate inventory
identities, but must remap every reference, retain conflicting metadata/observation
history, and never infer equality merely from matching size/mtime. No automatic
destructive merge or INSERT OR REPLACE. Conversion itself does not rescan metadata.

## 2. Compile an explicit exclusion policy before scheduling

Implemented CLI:

```powershell
python -m inventory_builder scan E:\ --exclude "E:\Private" --exclude "Cache"
python -m inventory_builder scan "E:\Photos" --exclude "Unsorted" --exclude "Old Copies"
```

Implemented TOML additions:

```toml
config_schema_version = 2
exclude_directories = ["Cache", "Private"]
```

Relative configuration and CLI exclusions resolve against the selected target.
Absolute ordinary exclusions must be within it. Union config, repeated CLI, system
defaults and mandatory runtime rules. De-duplicate identical/nested rules for
matching while preserving all supplied inputs and provenance for explanations.
No user glob expansion, arbitrary file exclusions or inclusion overrides initially.

Validate component containment, not string prefixes: excluding `Photos/Old` must
not exclude `Photos/Older`. Normalize separators/dot segments and apply the same
Windows comparison model used for identity. Reject escapes, external absolute paths,
reparse traversal, file-as-directory exclusions and an exclusion equal to the target.
Nonexistent paths are accepted lexically so a later-created directory stays excluded.
Do not enumerate excluded trees to canonicalize their names. If a safe comparison
cannot be established without entering an excluded tree, reject the ambiguous rule.

Global rules are structured application rules, not a way for ordinary `--exclude`
to escape validation. The documented outside-root exceptions are:

- Selected-volume-root system subtrees: `$RECYCLE.BIN`, `System Volume Information`,
  `Windows`, `Program Files`, `Program Files (x86)`, `ProgramData`, `Recovery`,
  `$WinREAgent`. Do not match these basenames at arbitrary depths. Never default
  exclude `Users` or typical photo folders.
- Exact configured application runtime paths: dedicated runtime/log/temp/backup/
  generated-report directories, database and its WAL/SHM/lock companions. Outside-root
  rules have no matching effect. Do not exclude all files named *.db or *.log.

Intersect global rules with the selected scope without visiting their contents.
If a rule contains the entire selected target, reject the target with an explanation.
Mandatory runtime protection cannot be disabled. Keep runtime on the internal SSD
and outside included source media. If it lies beneath a whole-drive target, its
dedicated subtree is pruned; refuse layouts that require writing inside an included
media directory or silently excluding an arbitrary shared media parent.

Apply rules before queue insertion and again before claiming/opening queued work,
including file/metadata/hash jobs. Do not call scandir on an excluded directory.
Reparse points stay skipped regardless of exclusions. Reading the target's own
directory entries to discover an excluded child's name is allowed; entering that
child's subtree is not. Count pruned roots, not guessed numbers of unseen files.

## 3. Immutable policy snapshots and explicit resume choices

Persist a canonical effective snapshot before the first job, containing volume/root
identity, compiled rule paths, original supplied forms, origins, active/inactive
global rules, config schema version, default-rules version, path-matching version,
extension policy, and required processing stages/tool-policy versions. Hashing
this policy text for identity is distinct from hashing source media.

Comparison keys use persistent volume-relative paths, not drive letters. Retain
the original invocation/mount observation for auditing. A changed drive letter alone
must not change semantic policy. Raw input order/duplicate rules do not create a
different effective policy, but origins/inputs remain available for diagnostics.

Planned explicit controls:

- `scan TARGET` resumes only when the saved/current effective policy matches.
  Otherwise show a rule/configuration difference and stop before traversal.
- `scan TARGET --resume-session ID` explicitly selects that session's saved policy,
  after reporting differences from today's config/defaults. Require the selected
  target to map to the saved volume/root. Never let an ID redirect a scan elsewhere.
- `scan TARGET --new-scan` explicitly supersedes an unfinished session and creates
  a linked successor with a fresh discovery queue and the requested policy.
  Retain predecessor jobs, observations, errors and policy. Reuse valid media-level
  results; do not transplant directory-complete markers from a different scope.
- `scan TARGET --reconcile-session ID` creates a linked successor with the new
  rules, revalidates coverage, and selectively schedules processing. It may initially
  use a full included-tree discovery pass for correctness. This is not an in-place
  mutation of the saved session. The above session-choice flags are mutually exclusive.

Safety takes precedence over historical policy: newly mandatory runtime protection
or unsupported old matching behavior must block saved-policy resume and require an
explicit successor. Never silently remove mandatory protection or modify old rules.

## 4. Coverage and processing semantics

Keep discovery coverage separate from metadata/hash completion. A completed directory
means candidate/child jobs are durable; it does not mean extraction/hashing finished.
Persist policy-linked pruned-directory coverage, completed-directory coverage and
unavailable/error coverage. Label exclusions as exclusions, never failures or missing
files. Do not enumerate them to produce a file count. Files observed previously stay
in inventory even when a new policy excludes them or a drive is unavailable.

Only a successful included-scope reconciliation can label a previously observed
in-scope path `not_seen_in_latest_complete_scan`. It never deletes records. Do not
replace this with a volume-wide missing flag: a file can have different coverage
under different roots/policies. Excluded/unavailable/error prefixes are outside any
missing-file inference. A changed exclusion policy cannot retroactively reinterpret
old observations or completion markers.

Persist independent metadata and photo-SHA-256 jobs with file fingerprints,
processing versions, attempts, claiming runs and retry state. Reuse current completed
results when overlapping roots observe the same media file. Recover abandoned jobs
only after obtaining the single-writer lock; preserve committed observations/results.
Revalidate identity, policy inclusion and fingerprint before using queued work.

Hash every included supported photo, not just current duplicate candidates, using
bounded reads and full SHA-256. Compare size/mtime and available file identity before
and after reading. On interruption/change, discard that attempt; restart the file
from byte zero. Do not serialize Python hash internals or label partial digests complete.
Commit complete digest, input fingerprint, provenance and job state atomically.
Videos have metadata jobs but no hash jobs. Discovery and every processing stage
must be resumable before real initial collection is declared ready.

## 5. Database migrations

Never edit applied migrations 001/002. Proposed stages may be split further after
review; existing data must survive every migration and integrity check.

### 003 — Volume paths, policies and session coverage (implemented)

| Table | Proposed change |
|---|---|
| `volumes`, `volume_observations` | Retain UUID/evidence model; add path-comparison version and any needed confirmed-binding provenance. No globally unique constraint on serial/GUID alone. |
| `scan_roots`, `media_files` | Add normalized volume-relative comparison keys alongside exact paths; unique `(volume_id, path_key)`. Preflight collisions before backfill/index creation. Never invent a physical binding from a folder path. |
| `folder_bindings`, `identity_transitions` | Retain original bindings/audit; extend transition provenance with confirmation and comparison policy. Existing conversion API must be extended to populate new keys atomically. |
| `scan_policies` | New immutable snapshot: ID, semantic key/digest, canonical JSON, config/default/matcher/processing versions, created timestamp. Digest is a lookup aid; compare canonical policy too. |
| `scan_exclusions` | New immutable rules linked to policy: supplied/normalized path, volume ID, comparison key, directory/exact-runtime-file match kind, origin, global rule ID, active-in-scope status. |
| `scan_sessions` | Add policy ID, predecessor ID, successor reason, separate discovery/processing states and completion times. Add a `superseded` lifecycle state and rebuild the active-session uniqueness index to support explicit successors. This requires a constraint/table migration, not just adding columns. |
| `scan_runs` | Preserve invocation/config provenance and reference the effective session policy; retain process-level statistics. |
| `scan_directories`, `scan_files` | Add normalized relative keys and policy-linked exclusion handling; allow an explicit excluded terminal state where queued legacy work needs pruning. Never mark such work observed/completed. |
| `scan_coverage` | New per-session volume-relative prefix, comparison key, coverage status (`enumerated`, `excluded`, `unavailable`), matched rule ID/error and timestamp. Existing completed-directory checkpoints remain authoritative for enumeration. |

Coverage exclusions are per session, not a global deleted/present flag on media_files.
Keys are only backfilled where identity and comparison semantics are known; unresolved
legacy scopes remain gated pending explicit confirmation. Existing session snapshots
are marked legacy/discovery-only, never fabricated as metadata/hash-complete collection.
Incomplete legacy sessions require an explicit successor if their old semantics cannot
be represented exactly and safely. Do not invalidate existing metadata or hashes simply
because path keys were added.

Migrate state/foreign-key constraints in a single controlled transaction with the
writer lock, preserving IDs and referencing rows; verify foreign_key_check and
uniqueness before commit. Any collision/ambiguity rolls back the whole migration or
conversion, leaving original records available for explicit resolution.

### 004 — Collection processing jobs and photo hashes (implemented)

| Table | Proposed change |
|---|---|
| `processing_jobs` | New media ID, stage (`metadata`, `photo_sha256`), fingerprint/version key, state, attempts, retry time, claiming run, timestamps; uniqueness prevents duplicate work across roots. Enforce photo-only hash jobs. |
| `session_processing_jobs` | New session-to-job links so reused work has coverage/provenance under each applicable session. |
| `file_hashes` | New completed photo result: media ID, algorithm (`sha256`), full digest, exact size/mtime and file-identity evidence, processing version and completion time. Stale results remain historical; never use them as current. Enforce photo-only results. |
| `media_metadata` | Preserve existing metadata and provenance; bind its current result to the matching processing fingerprint/version. No destructive replacement during migration. |
| `scan_errors` | Link errors to processing job/rule/coverage where applicable, retaining existing errors and retry history. |

The implementation also adds file_revisions and immutable metadata_results history,
including a snapshot of existing metadata. Exact stage/tool/date-policy keys control
reuse; metadata and photo-hash jobs are independent and shared across applicable roots.
ExifTool uses bounded 32-file batches with single-file fallback/debugging, output limits,
timeouts and owned Windows job-object process-tree cleanup. Photo hashes stream in
8 MiB chunks and restart only the interrupted file. No videos are hashed. Verification
checks actual required result coverage, not just stored session flags. See README.md.

Completed hashes/results and job completion must share a transaction. Add indexes
for claimable/retry jobs, volume/path, policy, coverage, size and complete digest.
Do not implement duplicate groups, video hashing, export, or organization here.

## 6. Implementation sequence after approval

1. Implement/test selected-volume identity, comparison keys and transactional legacy
   conversion. Exercise collisions and overlapping roots without scanning real drives.
2. Implement exclusion compiler/config/CLI, immutable policy schema and coverage.
3. Integrate policy checks before scheduling and claiming; implement explicit saved
   resume and successor sessions. Update status to distinguish discovery, exclusion,
   error and processing completion; no reporting/export feature.
4. Run the complete test suite plus wheel/migration checks. Release flexible-target
   support only after mocked drive-root and real disposable-folder tests pass.
5. Implement embedded metadata and full photo hashing stages (approved and implemented
   by Migration 004); prove independent recovery, fingerprint reuse and completion.
6. Obtain explicit authorization for the actual selected external drive/directory
   before any real collection scan. Never advance automatically to another drive.

## 7. Required validation

- Root/subdirectory targets, changed drive letters, ambiguity and overlapping roots;
  no unrelated-drive enumeration and no source writes.
- Repeated CLI/config exclusions, default/global intersection, nonexistent rules,
  invalid external paths, case/separator/Unicode variants, sibling prefix traps,
  reparse points and shared runtime-directory safety.
- Instrument scandir to fail if an excluded subtree is opened; test exclusions
  against both newly discovered and previously queued directory/file jobs.
- Interrupted/crashed directory, filesystem, metadata and photo-hash jobs retain
  committed observations and have no duplicate media/results after recovery.
- Same-policy resume; raw input reordering; changed config/defaults/matcher; explicit
  saved policy and linked successor scans; mandatory safety changes block old policies.
- Previously observed excluded/unavailable files never become missing/deleted;
  reconciliation is scoped to included successfully covered prefixes.
- Migrate real 001/002 fixtures; prove IDs, metadata, provenance and history survive,
  including transaction rollback on late path/case/identity collisions.
- Photo hashes equal a known complete SHA-256, partial results never accepted, changed
  fingerprints invalidate work, and video hash functions are never invoked.

Only temporary files and mocked volume interfaces may be used without additional
authorization. No exports, organization or source deletion are authorized.

## Implemented-stage validation

Version 0.2.0: 69 automated tests pass, including populated schema-2 migration,
late-DDL and foreign-key rollback, overlapping roots, changed mount paths,
exclusion pruning before enumeration/recovered jobs, immutable policies and explicit
successors, case-only renames and interrupted/crashed processing-state recovery.
The packaged wheel also initializes schema 3 and resumes a disposable excluded-tree
scan without Migration 004. No actual external drive or personal collection was scanned.

Remaining limits: case-sensitive directories, UNC/device namespaces and reparse
traversal are refused. Cross-version Windows comparison-key migration and automatic
resolution of ambiguous/cloned identities are not implemented. Reconciliation uses
a fresh included-tree pass and preserves all inventory; missing-file classification
remains deferred. Metadata extraction and photo hashing are added by Migration 004.
Dedicated in-scope database
runtime directories must be named InventoryBuilder or runtime. These limits are
documented in README.md; they do not authorize the next stage.

## Migration 004 implementation and production gate

Version 0.3.0 adds file_revisions, independent processing_jobs and per-session job
requirements, immutable metadata_results and complete file_hashes. Existing metadata
is archived and linked before any current projection is replaced. Migrations 001–003
remain unchanged; late Migration 004 failures roll back its entire schema/ledger/data
change and restore foreign-key enforcement. Legacy filesystem-only sessions stay
historical and never satisfy comprehensive verification automatically.

ExifTool reads bounded 32-file batches by default; single-file debugging and isolated
single-file transient retries are tested. A 32-generated-JPEG benchmark measured
0.328 seconds for one batch versus 6.484 seconds for 32 invocations on this machine.
These tiny local fixtures measure startup overhead, not production drive throughput.
Python output buffers cap at 16 MiB by default; photo read chunks default to 8 MiB.
Windows job objects close owned child trees on timeout, output overflow, interruption
or process exit. Unicode input uses UTF-8 argument files, with default tool configs
disabled and no write/recursive options. Original timestamps, offsets, grouped raw
tags, filenames and provenance are retained. Filesystem dates never replace missing
capture dates. Capture-date precedence is configurable and versioned with the jobs.

`scan TARGET` interleaves discovery and bounded processing with transactional job/result
checkpoints. Exclusions and fingerprints are revalidated before processing. Changed
files are refreshed on resume; independent completed work is retained. Photo hashes
restart at byte zero after interruption, and no video hash jobs/results are allowed.
Static GPS and other photo/video metadata are extracted when ExifTool exposes them;
full time-series telemetry extraction is not part of this stage.

`verify TARGET` gives a database-only completion decision and exit code for the latest
session at that target; status/scan show separate discovery, metadata and hash counts,
including pending/failed/retried/reused work. Verification checks required result
coverage against current recorded revisions, not just session flags. It does not
re-read an offline source or prove source bytes stayed unchanged after collection.

Real integration tests generate PNG/JPEG EXIF and QuickTime video fixtures with dates,
offsets, camera data, orientation, resolution and GPS. Full-format HEIC/RAW and further
codec/telemetry/Unicode compatibility still require representative validation. Only
disposable fixtures and mocked drive interfaces have been scanned; production scanning
remains gated by explicit user authorization. No duplicate grouping, export or file
organization implementation is authorized or included.

Final validation: 98 tests passed, including real ExifTool batch/single processing,
transient/permanent failures, metadata/hash interruption and process crashes, source
immutability, migrated history and late-Migration-004 rollback. The 0.3.0 wheel built
and passed a packaged scan/resume/verification smoke test with a generated PNG and
exclusions. ExifTool child TEMP/TMP/TMPDIR paths are explicitly kept in excluded
application runtime storage. No required test is skipped in this environment.
