# InventoryBuilder — Requirements Specification

**Version:** 0.2

**Status:** Comprehensive collection implemented through Migration 004; production scanning requires explicit authorization
**Platform:** Windows 11  
**Language:** Python 3.11+  
**Database:** SQLite  
**Development environment:** VS Code with OpenAI Codex  
**Source control:** Git / GitHub

---

## 1. Project Overview

InventoryBuilder is a local Python application that inventories large collections of photographs and videos stored across multiple internal or external storage devices.

The initial target is a collection containing hundreds of thousands of media files distributed across five external drives.

The application must create a searchable, persistent inventory containing file locations, filesystem attributes, embedded metadata, and information useful for future organization and duplicate detection.

The application must be reliable, resumable, and non-destructive.

### 1.0 Collection policy

Maintain one combined SQLite inventory across five static external drives. Scan
one explicitly selected drive root or directory at a time; never choose targets
automatically. Initial collection must include filesystem attributes, embedded
photo/video metadata, and complete SHA-256 hashes for every supported photo.
Do not hash videos. Video size and metadata may identify potential duplicates,
not establish exact content equality. Discovery, extraction and photo hashing
must all be resumable. Reporting and organization remain separate later phases.
No actual external drive or personal collection may be scanned without explicit
user authorization. Requirements and implementation plans do not grant that authorization.

### 1.1 Primary goals

1. Inventory photographs and videos across multiple drives.
2. Collect comprehensive metadata without modifying original media.
3. Maintain one combined SQLite database.
4. Scan one drive at a time.
5. Support interruption and resumption without restarting completed work.
6. Support repeated scans to identify new and changed files.
7. Maintain a persistent SQLite inventory that supports future analysis, querying, and reporting. Exporting results to CSV or other formats is optional and belongs to a later reporting phase.
8. Provide a foundation for duplicate detection and date-based organization.
9. Support future graphical and AI-powered search interfaces.

### 1.2 Non-goals for initial release

The initial version will not:

- Rename, move, or delete original media.
- Automatically delete duplicates.
- Upload photos or videos to cloud services.
- Generate image embeddings.
- Implement RAG or natural-language searching.
- Require a web server or MongoDB.
- Require all external drives to remain connected simultaneously.

## 2. Core Design Principles

### 2.1 Data safety

All initial scanning operations must be strictly read-only with respect to source media.

The scanner must never modify original files, file metadata, folder names, or directory structures.

The SQLite database and generated output files must be stored outside included
media directories. Application runtime paths are mandatory exclusions; if a
selected drive root contains a dedicated runtime directory, prune that subtree
before enumeration. Reject a target/configuration that cannot isolate runtime
writes from included source media. Never exclude an arbitrary shared media parent
silently just because it contains a database file.

### 2.2 Recoverability

The scanner must support graceful interruption through `Ctrl+C`.

Completed results must persist in SQLite.

Restarting a scan must resume unfinished work instead of repeating all completed metadata extraction.

An unexpected program termination or USB disconnection must not corrupt previously committed inventory data.

### 2.3 Incremental operation

The application must recognize previously inventoried files and avoid unnecessary work.

An unchanged file may be skipped based on its drive identity, relative path, size, modification timestamp, and applicable processing/version status.

A changed file must be reprocessed.

Missing or inaccessible files must not automatically be deleted from the inventory.

### 2.4 Separation of responsibilities

Directory discovery, metadata extraction, database storage, hashing, reporting, and future file organization should be implemented as separate modules.

### 2.5 Portability

The same source code should run on multiple Windows 11 computers.

Machine-specific paths and drive assignments must not be hardcoded into application logic.

## 3. Storage Device Management

### 3.1 Drive identification

Each storage volume must have a persistent inventory identity independent of its current drive letter.

Record:

| Attribute | Description |
|---|---|
| Volume ID | Stable application identifier |
| Volume GUID | Windows volume GUID path, when available |
| Volume serial | Filesystem volume serial, when available |
| Drive letter | Current Windows assignment |
| Volume label | Human-readable volume name |
| Filesystem | NTFS, exFAT, etc. |
| Capacity | Total storage capacity |
| First seen | First inventory observation |
| Last seen | Most recent observation |

Volume identifiers may not always be unique or stable after reformatting or cloning. The application must detect ambiguous identities and request confirmation rather than merging two different volumes automatically.

### 3.2 One-drive-at-a-time scanning

Only one explicitly supplied source drive root or directory is actively scanned
during a given command. Both `E:\` and `E:\Photos` are valid target forms.
Reject ambiguous drive-relative forms such as `E:`. Resolving the selected
target's containing volume is allowed; enumerating unrelated drives is not.

All drives share the same SQLite database.

Progress and checkpoints must be maintained independently for each scan root.

The application must support switching between drives without losing previously recorded progress.

### 3.4 Overlapping scan roots

Allow a volume root and any of its subdirectories to be scanned in separate
commands. File identity is the confirmed persistent volume identity plus a
normalized volume-relative path, not the selected root or drive letter. Different
root/session observations reference the same media record. Preserve exact path
spelling separately from comparison keys. Do not merge ambiguous volume identities
or path collisions automatically. Transactional conversion of existing provisional
folder scopes must preserve media IDs, metadata, fingerprints, observations and
history; collisions require explicit resolution and cannot silently discard records.

### 3.3 Disconnected volumes

When a previously inventoried drive is disconnected:

- Its records remain in the database.
- Its scan progress remains available.
- It must not be marked empty.
- No existing media records may be deleted merely because the drive is unavailable.

When reconnected, the application should recognize its identity and resume the corresponding scan.

## 4. Supported Media Formats

### 4.1 Initial photo formats

- JPEG / JPG
- HEIC / HEIF
- PNG
- TIFF / TIF
- BMP
- GIF
- WebP
- DNG
- Common camera RAW formats, including CR2, CR3, NEF, ARW, RW2, and ORF

### 4.2 Initial video formats

- MP4
- MOV
- M4V
- AVI
- MKV
- WMV
- MPG / MPEG
- MTS / M2TS
- 3GP

Additional formats may be added through configuration.

GoPro and 360-degree camera formats should be considered in later compatibility testing.

### 4.3 Format recognition

Extension-based discovery is acceptable for the initial inventory.

The application should record the detected MIME or container format when available and report extension/content mismatches.

Unsupported files should be counted separately, not treated as scanner failures.

## 5. Required Metadata

### 5.1 File identity and location

| Field | Requirement |
|---|---|
| Media ID | Unique internal identifier |
| Volume ID | Reference to storage volume |
| Relative path | Path relative to scan root |
| Full path | Last observed absolute file location |
| Parent directory | Original parent folder |
| Filename | Original filename |
| Extension | Original file extension |
| File type | Photo or video |
| Size bytes | Exact file size |
| Filesystem created | Creation timestamp from Windows |
| Filesystem modified | Last modification timestamp |
| Filesystem accessed | Optional; avoid altering access times where possible |
| First inventoried | First observation timestamp |
| Last inventoried | Most recent observation timestamp |

Do not rely exclusively on a drive letter and absolute path as the file's permanent identity.

### 5.2 Capture dates

Store the following independently when available:

- EXIF DateTimeOriginal
- EXIF CreateDate
- MediaCreateDate
- TrackCreateDate
- Filesystem creation timestamp
- Filesystem modification timestamp
- Original timezone or UTC offset
- Selected capture datetime
- Selected capture date
- Selected date source
- Confidence or reliability classification

Preserve original metadata date values when practical.

Do not silently replace an unknown capture date with a filesystem timestamp.

### 5.3 Preferred capture-date selection

Use a documented, configurable precedence policy.

The initial policy should:

1. Prefer valid original camera capture metadata.
2. Use suitable embedded video creation metadata when photo capture metadata is unavailable.
3. Prefer timezone-aware timestamps where trustworthy.
4. Preserve timezone ambiguity rather than assuming that every timestamp represents UTC.
5. Mark files without reliable capture metadata as having an unknown capture date.
6. Offer filesystem dates as separately labeled fallback candidates, subject to later user review.

Do not convert an unknown date into an authoritative date merely to create an organization folder.

### 5.4 Image metadata

Collect when available:

- Width and height in pixels
- Orientation
- Display-oriented dimensions
- Megapixels (derived)
- Camera manufacturer
- Camera model
- Lens model
- ISO
- Aperture
- Shutter speed
- Focal length
- Software/application
- Color space
- Embedded GPS latitude
- Embedded GPS longitude
- GPS altitude
- GPS timestamp

GPS information may be sensitive and must remain local unless explicitly exported or shared by the user.

### 5.5 Video metadata

Collect when available:

- Width and height
- Duration in seconds
- Frame rate
- Video codec
- Audio codec
- Container format
- Rotation/orientation
- Video creation timestamp
- Camera manufacturer/model
- GPS coordinates
- Bit rate
- Number of video/audio streams

### 5.6 Metadata provenance

Record:

- Extraction tool and version
- Extraction timestamp
- Raw metadata source fields relevant to date selection
- Extraction status
- Warning/error details

Unknown metadata must be stored as null or an explicit unknown status, not fabricated.

## 6. Metadata Extraction

ExifTool is the preferred metadata extraction utility.

The Python application may invoke ExifTool through a managed subprocess.

Requirements:

- Use bounded processing batches.
- Avoid loading full photo or video contents into memory.
- Handle Unicode filenames.
- Handle filenames containing spaces and special characters.
- Apply sensible subprocess timeouts.
- Record metadata extraction failures without terminating the entire scan.
- Support retries for transient errors.
- Record tool versions for reproducibility.
- Do not execute arbitrary commands constructed from unsanitized filenames.
- Keep source files unchanged.

Additional utilities such as ffprobe may be introduced if ExifTool does not provide adequate video metadata.

## 7. SQLite Database

### 7.1 Database location

The active SQLite database must reside on the notebook's internal SSD, outside
included scan scope and cloud-synchronized directories. For a whole-drive target,
an intersecting dedicated runtime subtree must be mandatorily excluded as defined
in section 8.2.1; source-media directories must never receive runtime writes.

The database file must not be committed to Git.

### 7.2 Logical schema

The initial schema should include:

**volumes**

Persistent information identifying storage volumes.

**scan_roots**

Configured starting paths associated with volumes.

**media_files**

File identity, paths, attributes, capture timestamps, media metadata, and processing status.

**scan_directories**

Persistent directory discovery queue and completion checkpoints.

**scan_runs**

Start and end times, status, statistics, failures, and configuration versions for each scan execution.

**scan_errors**

Individual scanning or metadata extraction failures, including retry status.

**file_hashes** (initial collection)

Complete photo SHA-256 results, observed file fingerprints and verification
information. Unfinished/failed hash work belongs in durable processing jobs.
Never store a partial digest as a completed file hash. No video hash jobs/results.

**scan_policies / scan_exclusions**

Immutable effective exclusion/processing policy snapshots and structured rules,
including origins, matching/configuration versions and persistent volume-relative
paths. Each session references its exact policy.

**processing_jobs / session_processing_jobs / file_revisions**

Independently resumable metadata and photo-hash work, linked to media fingerprints,
sessions and claiming runs. Keep discovery completion separate from collection
processing completion.

**metadata_results**

Immutable extraction history and original metadata/date values. Preserve legacy
metadata before replacing any current projection.

**duplicate_groups** (later phase)

Groups of files identified as duplicates.

The implementation may normalize additional metadata into related tables or store extensible metadata as JSON where appropriate.

### 7.3 Database integrity

Requirements:

- Parameterized SQL statements.
- Explicit schema versioning and migrations.
- Transactions covering file records and related checkpoint state.
- Uniqueness constraints that prevent duplicate records on resume.
- Appropriate indexes for volume, paths, dates, file sizes, and statuses.
- Foreign-key enforcement.
- A single active writer.
- Protection against concurrent scanner instances modifying the same database.

Use SQLite WAL mode when appropriate for concurrent reporting and database access, while preserving safe backup procedures.

## 8. Scanner Architecture and Recovery

### 8.1 Directory discovery

Use memory-efficient, recursive directory enumeration, preferably through `os.scandir()`.

Avoid materializing the complete filesystem tree into memory.

Directory paths and discovery progress must be persisted in SQLite.

Do not recursively follow symbolic links, junctions, or other directory reparse points by default.

Avoid scanning outside the selected root.

### 8.2 Transactional checkpoints

A directory must not be marked fully discovered until its relevant child directories and candidate media files have been durably recorded.

Use a persistent directory work queue.

On interruption, an unfinished directory must be eligible for re-enumeration.

Database uniqueness constraints must make repeated directory enumeration safe.

Use small, configurable transaction batches.

### 8.2.1 Directory exclusions and path safety

- Accept repeated `--exclude` directory arguments and persistent
  `exclude_directories` configuration entries. Combine them with documented
  system/application defaults by union; no implicit inclusion overrides.
- Relative user/configuration exclusions are relative to the selected scan root,
  never the shell's current directory or configuration-file directory. Absolute
  exclusions must resolve inside that root. Directory rules exclude the directory
  itself and every descendant, using path-component containment, not string-prefix
  matching. User exclusion globs are not part of this increment.
- Normalize separators and dot segments, reject escapes, drive-relative paths,
  alternate-data-stream syntax and unsupported device namespaces. Do not follow
  junctions/symlinks/reparse points to resolve exclusions outside the selected root.
  Nonexistent exclusion directories may be recorded lexically without enumerating
  parents or descendants. Existing file paths supplied as directory exclusions
  are validation errors. Reject a user exclusion equal to the entire target.
- Windows comparisons must use Windows-compatible case-insensitive path semantics,
  preserving display spelling. Do not rely on SQLite NOCASE or generic Unicode
  casefold as an exact substitute. If a case-sensitive Windows directory creates
  ambiguous matches, stop for explicit resolution rather than merge or overexclude.
  Record the matching implementation/version with the session.
- Ordinary exclusions outside the target are errors. Only documented, typed global
  rules may lie outside it: selected-volume-root system rules and exact configured
  application runtime directories/files. Such rules intersect the selected scope;
  rules with no intersection are recorded as inactive and never traversed.
- Default system directory rules are anchored at the selected volume root:
  `$RECYCLE.BIN`, `System Volume Information`, `Windows`, `Program Files`,
  `Program Files (x86)`, `ProgramData`, `Recovery`, and `$WinREAgent`.
  Do not apply these names at arbitrary depths; do not default-exclude `Users`
  or personal media directories. Their contents must not be enumerated.
- Mandatory application rules protect dedicated runtime/log/temp/backup/report
  directories and exact database, WAL, SHM and lock files where needed. These are
  path-specific rules, not a blanket exclusion of every .db or .log file.
- Check exclusions before queueing/opening directories and before claiming any
  previously queued directory or file job. Selecting a target wholly covered by
  a mandatory exclusion is an error. Runtime protection cannot be disabled.
- Store canonical effective rules, supplied values, origins, normalization and
  default-rule versions, and configuration version with every session before work
  starts. Store paths relative to persistent volume identities so changed drive
  letters do not change the semantic policy. Retain the original invocation too.
- A session's policy is immutable. Resume may explicitly use its saved policy;
  when current CLI/configuration/defaults differ, refuse silent resume and explain
  the difference. Require an explicit new or reconciled scan. Reconciliation creates
  a linked successor session; it never rewrites the predecessor's policy/history.
- Excluded files/directories have no observation requirement in that session.
  They and unavailable paths must not be classified as deleted or missing merely
  because they were not observed. Reconciliation is scoped to covered, included
  paths with successful enumeration, never the whole volume unconditionally.

### 8.3 File processing states

Suggested states:

- `pending`
- `processing`
- `completed`
- `failed`
- `retry_pending`
- `unsupported`

Transient `processing` states must be recovered after an unexpected shutdown.

A completed record must include the relevant extraction version and file fingerprint so that future scans can determine whether reprocessing is necessary.

### 8.4 Graceful interruption

On `Ctrl+C`:

1. Stop scheduling new work.
2. Complete or safely abandon the active operation.
3. Commit completed records.
4. Preserve directory and file processing checkpoints.
5. Close the database cleanly.
6. Display scan statistics.

A second forced interruption or power failure may lose the current uncommitted transaction, but previously committed results must remain intact.

### 8.5 Resume behavior

The command:

`python -m inventory_builder scan E:\`

must resume outstanding work for the recognized volume and scan root.

A changed drive letter must not automatically create a new volume identity.

Completed, unchanged files must not undergo redundant metadata extraction.

Any partially processed directory or file must be safely revisited.

Resume must also preserve the saved exclusion policy and recover unfinished
metadata/photo-hash jobs. Changed rules require an explicit successor scan; reuse
completed work only when file fingerprints and the relevant processing policy/tool
versions still match. Unavailable volumes never trigger inventory removal.

### 8.6 Subsequent inventory scans

After an initial full scan, a new scan should detect:

- New files
- Changed files
- Previously failed files eligible for retry
- Previously inventoried files not observed during a successfully completed reconciliation scan

Do not classify unobserved files as deleted if scanning was interrupted, incomplete, or the drive was unavailable.

Record a status such as `not_seen_in_latest_complete_scan` until a later policy determines how to handle absent files.

### 8.7 Progress reporting

Display:

- Selected volume and scan root
- Current directory
- Files discovered
- Files processed
- Files skipped
- Files failed
- Bytes inventoried
- Elapsed time
- Average processing throughput
- Checkpoint timestamp

The total number of files may be unknown during directory discovery. Do not present an inaccurate percentage as an authoritative measure of completion.

## 9. Command-Line Interface

```powershell
python -m inventory_builder scan E:\ --exclude "E:\Private" --exclude "Cache"
python -m inventory_builder scan "E:\Photos" --exclude "Unsorted" --exclude "Old Copies"
python -m inventory_builder status
python -m inventory_builder volumes
python -m inventory_builder verify "E:\Photos"
python -m inventory_builder status "E:\Photos"
```

The target/exclusion syntax above is implemented through Migration 003. Explicit
saved-policy resume and new/reconciled-session controls are specified in
`docs/IMPLEMENTATION_PLAN.md`. Examples do not authorize a real collection scan.

Migration 004 integrates metadata and photo hashing into `scan TARGET`. `verify TARGET`
returns success only for comprehensive recorded completion of the latest session at
that target, including all required current-revision results. It does not access the
source to prove that files remain unchanged after the checkpoint. Status separates
discovery/metadata/hash counts and retry history. Failed stages keep the scan incomplete.
`--retry-failed` explicitly grants failed jobs another bounded attempt budget.

## 10. Reporting and Data Export — Future Phase.

Support a single combined CSV covering all inventoried drives.

The export must include:

- Drive identity, label, and last known letter
- Full path
- Filename
- File type
- Size
- Capture date and date source
- Filesystem timestamps
- Resolution
- Camera information
- GPS coordinates
- Duration and video attributes
- Metadata extraction status
- Scan timestamp

Export timestamps in clearly documented formats, preferably ISO 8601.

Store file sizes in exact bytes.

CSV export must use streaming or bounded-memory processing.

Excel compatibility should be considered, including UTF-8 BOM as an optional export format and protection against spreadsheet formula injection in filename/path fields.

Support filtering by volume or media type in a future release.

Reporting must be independent of the inventory scanner and support exporting selected query or analysis results, including duplicate groups, missing metadata, suspicious dates, and organization plans.

## 10. Reporting and Data Export — Future Phase

Reporting and data export are not required during the initial inventory collection phase.

All inventory data and analytical findings must be stored in SQLite as the primary data store.

Reporting must be independent of the inventory scanner and support exporting selected query or analysis results, including:

- Duplicate files and duplicate groups across drives
- Files with missing or unreliable capture dates
- Files with inconsistent or suspicious metadata
- Media grouped by capture date, camera, or location
- Proposed file organization plans
- Complete inventory listings when needed

Supported output formats may include CSV, Excel, and future UI-based reports.

Exports should include the relevant file attributes and analysis findings for the selected report. There is no requirement for every report to contain every inventory field.

Exported timestamps should use documented formats, preferably ISO 8601. File sizes should use exact byte counts.

Large exports must use streaming or bounded-memory processing.

Excel compatibility and protection against spreadsheet formula injection must be considered.

The reporting system must not modify original media files or inventory records.

## 11. Duplicate Detection — Later Phase

Exact duplicate detection should operate across all inventoried drives.

Initial collection hashes every supported photo, even when it has no current
size-matched duplicate candidate. Read bounded chunks, use SHA-256, and verify
the file fingerprint before and after reading. A changed file invalidates that
attempt. Persist the complete digest only after successful full-file processing.
Interrupted photo hashing restarts that file from byte zero; completed photo jobs
are retained and skipped when current. Mid-file hash-state serialization is not
required. Do not hash videos.

Subsequent photo duplicate analysis uses a staged approach:

1. Group candidate files by size.
2. Reuse complete current photo SHA-256 results; queue missing/stale photo results.
3. Group files by matching hash and file size.
4. Record duplicate groups.
5. Present candidate duplicates for review.

Hash computation must be resumable at the file-job level.

Do not claim that a partially calculated hash is a completed file hash.

A SHA-256 match provides a strong indication of exact content equality.

Perceptual image hashing and visually similar media detection belong to a later phase.

Video duplicate candidates use file size plus available duration, resolution,
container/codec, capture date and other suitable metadata. Label them potential
duplicates requiring review; matching size/metadata is not proof of byte equality.

The initial duplicate detector must never delete source files.

## 12. Date-Based Organization — Later Phase

The future organizer should propose a consistent date hierarchy.

Example:

`OrganizedMedia/2024/2024-07/2024-07-14/`

Files with uncertain or missing capture dates should be assigned to a separate review queue rather than silently placed under an arbitrary date.

The organizer must support:

- Dry-run previews
- Naming collision detection
- Capture-date confidence review
- Copy-first behavior
- Post-copy verification
- Persistent operation journals
- Independent approval before source removal

No source file deletion is authorized by this requirements specification.

## 13. Logging and Error Handling

Use Python's standard logging system.

Provide:

- Console progress summaries
- Rotating log files
- Structured error records in SQLite
- Useful error messages for inaccessible files, disconnected drives, unsupported metadata, and database failures

Errors on individual files must not terminate the entire scan unless continued operation would compromise correctness or safety.

Disk-full and database-integrity errors must stop affected writes safely.

## 14. Testing Requirements

Use `pytest`.

Tests should include:

- Recursive directory enumeration
- Paths containing spaces and Unicode
- Duplicate filenames in different folders
- Empty directories
- Unsupported extensions
- Missing metadata
- Malformed metadata
- Interrupted scanning
- Recovery after unexpected termination
- File changes between scans
- New files between scans
- Missing or disconnected volumes
- Drive-letter changes
- Transaction rollback behavior
- CSV export accuracy
- Database uniqueness and referential integrity
- Drive-root and subdirectory targets without unrelated-drive enumeration
- Repeated CLI exclusions combined with persistent configuration exclusions
- Excluded subtrees never opened/enumerated, including requeued work on resume
- Windows case/separator variants, path-component boundaries and exclusion escapes
- Immutable saved policies, changed defaults, drive-letter changes and explicit successor scans
- Overlapping roots reuse media IDs and valid completed processing work
- Excluded/unavailable files never incorrectly classified as missing/deleted
- Photo-only full-file SHA-256, stale-fingerprint invalidation and interrupted job recovery

### 14.1 Mandatory recovery tests

Verify that:

1. Interrupting a scan preserves committed file records.
2. Running the same command resumes unfinished work.
3. Completed unchanged files are not unnecessarily reprocessed.
4. Files discovered in an interrupted directory are not duplicated.
5. A disconnected drive's records remain intact.
6. Reconnecting a volume under a different drive letter preserves its identity.
7. An incomplete scan never incorrectly classifies existing records as deleted.

### 14.2 Performance validation

Test first with:

1. A small test folder.
2. A directory containing several thousand files.
3. A full external drive.

Measure processing throughput and SQLite growth.

Do not optimize prematurely at the expense of correctness.

## 15. Configuration

Support configuration for:

- SQLite database path
- Log directory
- Export directory
- Supported extensions
- Batch size
- Metadata extraction timeout
- Retry policy
- Directory exclusions (`exclude_directories`, a list of root-relative or in-root absolute paths)
- Exclusion default/matching/configuration versions and documented global runtime rules
- Capture-date precedence policy

Keep machine-specific configuration out of Git.

Provide a committed configuration example.

Use `pathlib` for path handling where appropriate.

## 16. Security and Privacy

The application must run locally.

Do not automatically upload:

- Source media
- GPS coordinates
- File paths
- Metadata
- Database contents

No external AI or cloud services are required for initial operation.

The database and reports may contain sensitive location and personal information. Document this and exclude them from version control by default.

## 17. Initial Project Structure

Proposed layout:

```text
InventoryBuilder/
├── src/
│   └── inventory_builder/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── scanner.py
│       ├── metadata.py
│       ├── database.py
│       ├── volumes.py
│       └── exporter.py
├── tests/
├── docs/
│   └── REQUIREMENTS.md
├── config/
│   └── example.toml
├── AGENTS.md
├── .gitignore
├── pyproject.toml
└── README.md
```

Runtime database files, logs, reports, and temporary output should reside in a separate configured data directory.

## 18. Development Milestones

### Milestone 1 — Project foundation

- Python package setup
- CLI entry point
- SQLite initialization and migrations
- Configuration handling
- Logging
- Git exclusions
- Basic tests

### Milestone 2 — Resumable file discovery

- Storage volume identification
- Persistent directory queue
- Recursive file enumeration
- Filesystem metadata collection
- Transactional checkpoints
- Interrupt/resume support
- Recovery tests

**Acceptance criterion:** Successfully interrupt and resume a scan of a test folder without duplicate or lost records.

### Milestone 3 — Embedded metadata

- ExifTool integration
- Photo capture-date extraction
- Video metadata extraction
- Camera and resolution attributes
- GPS extraction
- Date selection rules
- Metadata error handling

**Acceptance criterion:** Successfully inventory representative JPEG, HEIC, RAW, MP4, and MOV samples.

### Approved prerequisite extension — Flexible targets and exclusions

Before authorizing real collection, implement persistent physical-volume binding,
drive-root/directory targets, overlapping-root identity reuse, default/config/CLI
exclusions, immutable versioned policies, and explicit resume/new/reconciled sessions.
Test solely with disposable folders and mocked drive/volume interfaces. Implemented
through Migration 003; real external-drive scanning still requires authorization.

### Initial collection readiness — Resumable photo hashes

Implement durable photo-only SHA-256 jobs and integrate metadata/photo hashing
with session processing completion. Filesystem discovery, embedded metadata and
complete photo hashing must all be available for initial collection; do not defer
required hashes until duplicate analysis. Videos have metadata but no hash jobs.
Each implementation stage requires its own approval; this document update does
not begin Milestone 3, hashing implementation, or actual external-drive scanning.

### Milestone 4 — Reporting and incremental scans

- Combined CSV export
- Per-volume status reports
- Incremental re-scanning
- New/modified/missing file classification
- Scan performance statistics

**Acceptance criterion:** Inventory a full external drive, repeat the scan, and skip unchanged metadata extraction.

### Milestone 5 — Exact duplicate detection

- Candidate grouping by file size
- Reuse initial-collection photo SHA-256 results; refresh stale/missing photo jobs
- Size/metadata-based potential video duplicate candidates; no video hashing
- Cross-drive duplicate grouping
- Duplicate report

### Milestone 6 — Organization planning

- Date-based destination proposals
- Capture-date confidence review
- Collision detection
- Non-destructive dry-run report

### Future milestones

- Desktop UI
- Visual duplicate detection
- Thumbnail caching
- Camera and location browsing
- AI semantic search
- Optional RAG capabilities

## 19. Initial Definition of Done

The first production-ready inventory release must:

1. Scan one selected external drive without modifying source files.
2. Store results in a single SQLite database.
3. Capture filesystem and embedded media metadata.
4. Preserve distinct date fields and their sources.
5. Identify drives independently of assigned letters where supported.
6. Resume cleanly after interruption.
7. Preserve records when a drive is disconnected.
8. Skip unchanged completed files during subsequent scans.
9. Report individual file extraction failures.
10. Preserve queryable SQLite results; CSV/reporting remains optional and later.
11. Pass automated tests covering interruption and recovery.
12. Successfully process a representative full external drive after explicit authorization.
13. Complete SHA-256 for every included supported photo, without hashing videos.
14. Preserve immutable exclusion policies and correct coverage across resume and overlapping roots.

---

## 20. Guidance for Codex

Implement the project milestone by milestone.

Do not attempt to build the entire system in one change.

Begin with Milestone 1 and Milestone 2.

Before implementing a significant architectural change, explain the proposed approach and any tradeoffs.

Prioritize correctness, recoverability, database integrity, and source-media safety over optimization.

Do not implement destructive operations without explicit user authorization.
