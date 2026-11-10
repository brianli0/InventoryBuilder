# InventoryBuilder architecture — Foundation and flexible targets

Migration 003 implements the flexible-target/exclusion stage in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md), aligned with requirements 0.2.
Migration 004 adds approved embedded metadata and photo hashing, described below.
Production collection remains unauthorized. The Milestone 2 restrictions later are historical;
the current behavior is described here and in README.md.

## Current flexible-target stage (003)

The Windows adapter queries only the selected path's containing volume/mount,
GUID, filesystem serial and capacity; it never enumerates drives. Evidence maps
to an application UUID; ambiguous evidence requires an explicit decision. An exact
clone with identical observable identifiers cannot be proved distinct automatically.
Overlapping scan roots reference the same volume-relative file keys and media IDs.
Legacy scopes must be explicitly confirmed before conversion or overlapping scans.

Path keys preserve spelling, normalize separators/components and use SQLite's WINPATH
collation, backed by Windows CompareStringOrdinal(ignoreCase=True), with an OS/matcher
version stored on volume/policy records. This avoids SQLite's ASCII-only NOCASE and
Unicode casefold expansion. Unsupported matcher changes block scans; a future explicit
key-reconciliation migration is required before changing comparison semantics.
Case-sensitive directories, UNC/device targets and reparse traversal are refused.

The immutable scan policy stores compiled active rules and input provenance, including
config/default/matcher versions. Default scan resumes a matching interrupted session
without extra flags. Changed rules require explicit saved-policy resume or a linked
new/reconciled session. Successors rediscover included scope and reuse current media
records; policies and predecessor history are never rewritten. Newly mandatory runtime
rules block unsafe saved-policy resume. No missing/deleted classification is implemented.

The compiler checks component containment, accepts root-relative/in-root absolute
directory exclusions, and combines repeated CLI/config rules with root-anchored Windows
defaults and exact application runtime paths. Pruning happens before queue insertion,
directory enumeration, and recovered directory/file processing. Per-session coverage
records distinguish enumerated, excluded and unavailable paths; excluded descendant
counts are unknown. Runtime databases within a target require dedicated InventoryBuilder/
runtime directories. Selecting an application runtime directory itself is rejected
before lock/database/log creation. Source media is never opened or written.

Migration 003 rebuilds session/directory/file state constraints with foreign-key
enforcement temporarily disabled outside the transaction. All DDL, row copies and
ledger updates share one transaction; foreign_key_check runs before commit, and
enforcement is restored on success/failure. Existing IDs, observations, errors, metadata
and original snapshots survive. Legacy path keys remain unconfirmed/null until explicit
identity conversion. Conversion checks collisions and updates keys/identity/audit together.

Native API references: [CompareStringOrdinal](https://learn.microsoft.com/en-us/windows/win32/api/stringapiset/nf-stringapiset-comparestringordinal),
[GetVolumePathNameW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getvolumepathnamew),
[GetVolumeNameForVolumeMountPointW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getvolumenameforvolumemountpointw).

## Comprehensive collection stage (004)

File revisions snapshot size, mtime and filesystem identity. Processing jobs are unique
by revision/stage/tool/policy; metadata/date-policy changes do not invalidate current
photo hashes. Session links record required/reused work across overlapping roots.
Discovery observations and required-job scheduling commit together. Subprocesses and
photo reads occur outside SQLite transactions; a complete result and job completion
commit atomically. Database constraints reject partial/photo-invalid/video hash work.
Immutable results preserve old metadata and hashes, while a current projection selects
the applicable cached metadata result without re-extraction. Retry/error history is
retained; explicit retry increases a bounded budget without resetting attempt counts.

ExifTool's bounded-batch read adapter uses grouped JSON, original date values, disabled
user config loading, UTF-8 argument files and output/timeout limits. Windows job objects
with KILL_ON_JOB_CLOSE prevent owned subprocess trees from surviving normal interruption
or process termination. Batch responses are matched per file and failures isolated;
retried files use single-file calls. The parser preserves raw data, normalizes useful
photo/video fields, and selects only embedded date candidates. Missing/invalid dates
stay unknown; no filesystem/filename date inference or implicit host-timezone conversion.

Photo hashing streams bytes with pre/post fingerprint checks and a complete-byte count.
Interrupted attempts have no stored partial digest. Videos cannot have hash jobs/results;
known video signatures and video MIME detections also guard misnamed photo files.

Session completion requires discovery coverage plus every applicable current-revision
metadata/photo-hash result. Legacy policies do not satisfy comprehensive verification.
`verify TARGET` checks the latest recorded target scope without accessing source media;
`status TARGET` presents stages, failures, retries and reuse. Source mutation since the
last checkpoint requires a new scan to detect. No reporting/export or organization is
implemented. ExifTool/Windows references: [ExifTool JSON and filename options](https://exiftool.org/exiftool_pod.html),
[Windows job object lifetime](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

The runtime uses only Python's standard library. pytest is a development
dependency and setuptools is the packaging backend. TOML avoids a YAML runtime
dependency. Folder scanning uses no additional dependencies. Physical-volume
discovery and embedded metadata extraction remain deferred.

The CLI orchestrates configuration, rotating logging and storage. SQL resources
ship with the package. A migration ledger records SHA-256 checksums of migration
text (never media contents). All pending migrations and ledger updates execute
in one explicit transaction; SQL scripts must not contain transaction control.
Every connection enables foreign keys. Writers use WAL, FULL synchronization,
short transactions and a bounded busy timeout.

All application mutations must hold the process lock for the canonical database
path, then acquire SQLite transactions. The OS releases locks on process exit;
the persistent lock file is deliberately never deleted. Read-only CLI commands
use SQLite read-only connections. Do not access one database through hard-link
aliases or multiple network shares: these are not supported runtime locations.

## Schema and future recovery

Volumes have application UUIDs. Observations retain Windows GUID/serial and
mount-path evidence without asserting these identifiers are globally unique.
Future discovery must refuse ambiguous matches, without writing identity markers
to source media. Current folder scanning creates explicitly provisional identities,
not claims of persistent physical-volume identification.

Scan roots are volume-relative. Media uniqueness is volume plus exact
volume-relative path, preventing overlapping roots from duplicating files.
Per-session observations preserve root-relative paths. Exact spelling is retained;
future Windows path handling must account for case-sensitive directories.

A scan session is one durable inventory pass; scan runs are individual process
executions. A partial unique index prevents multiple unfinished sessions per root.
Directory jobs and candidate file jobs are committed together in bounded discovery
batches. A directory may only become complete after all children and candidate jobs
are durable. Filesystem attribute processing has its own durable queue. File inventory,
observations, processing completion and checkpoint updates share one transaction.
Interrupted jobs are re-enumerated and upserted idempotently. Recovery acquires the
writer lock before resetting abandoned directory/file claims and transient metadata
claims, retaining committed observations and existing metadata payloads. This does
not perform metadata extraction. Failures are attempted once per invocation, leaving
failed jobs for the next resume rather than entering an unbounded retry loop.

Filesystem timestamps use integer nanoseconds, with nullable creation time when
unavailable. Application timestamps use UTC ISO 8601. Metadata dates remain
separate, with raw date values and timezone ambiguity preserved in JSON. Typed
capture-date columns support later queries; extensible photo/video fields use
attributes JSON until extraction requirements justify further normalization.

Discovery fingerprints and extraction fingerprints are independent: a changed
file cannot make old metadata current. Metadata completion requires provenance
and the processed fingerprint. No metadata extraction is performed now.

No source records are automatically deleted. Later missing-file classification
requires a complete reconciliation pass with no unresolved enumeration failures.
Resuming unfinished work cannot discover new files added to already completed
directories; a fresh subsequent pass is required for that guarantee.

The duplicate reporting section in REQUIREMENTS.md and CSV acceptance checks
apply to future milestones. Reporting, hashing, organization and extraction are
outside this foundation. Windows may update filesystem-managed access times
when files are read; the application will never request source writes
or attempt to restore source timestamps.

## Historical Milestone 2 baseline (migration 002)

Migration 001 remains unchanged. Migration 002 adds identity_kind, folder_bindings,
scan_files, identity_transitions and error relative-path/retry lookup support.
Bindings retain the exact original selected path, machine namespace, directory
identity evidence and original scope UUID. Media paths in folder scopes are relative
to that folder; session observations independently retain root-relative paths.

`storage.identities.promote_folder` is a database-only preparation API, not an
automatic discovery service or a CLI command. Its caller must hold the writer lock
and independently confirm the target physical-volume identity and volume-relative
folder prefix. The source must have exactly one root and no unfinished session.
The conversion updates media volume/path fields and the root binding in one
transaction. Existing media IDs, timestamps, extraction fingerprints, metadata,
observations, jobs, and errors remain intact. An audit row retains old/new volume
IDs and root prefixes; the original binding and provisional volume are retained.
Future scans use the new volume-relative prefix without repeating metadata work.

Root or file identity/path collisions abort the entire transaction. No records are
merged, replaced, or deleted, even when fingerprints appear equal. Case aliases
are conservatively treated as collisions until filesystem-aware reconciliation
is explicitly implemented. A future conflict-resolution workflow must obtain an
explicit decision and preserve references/history; it must not silently use REPLACE.

Validation rejects drive roots, reparse roots/components, overlapping bindings,
and runtime paths inside source folders before logging/locking creates files.
Each queued path is revalidated, and root directory identity evidence must match.
This is not a snapshot filesystem; concurrent malicious path swaps are outside the
guarantee. Normal changing files are safely revisited on later passes, and missing
sources never trigger inventory deletion.
