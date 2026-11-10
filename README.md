# InventoryBuilder

Local Python 3.11+ / SQLite media inventory for Windows 11.
Migration 004 inventories one explicitly selected drive root or directory using
persistent identity, exclusions and independent resumable discovery, ExifTool metadata
and photo SHA-256 jobs. Videos receive metadata and exact sizes but are never hashed.
Duplicate grouping, export and file organization are not implemented.

## Setup

Install the Windows ExifTool executable from [ExifTool](https://exiftool.org/install.html)
and put it on PATH or configure `exiftool_path`. There are no additional Python
runtime dependencies. The real integration tests require ExifTool and fail clearly
if it is unavailable; they create their own disposable media.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
.\.venv\Scripts\python -m inventory_builder init
.\.venv\Scripts\python -m inventory_builder status
.\.venv\Scripts\python -m inventory_builder volumes
.\.venv\Scripts\python -m pytest
```

Runtime defaults to `%LOCALAPPDATA%\InventoryBuilder`, with `inventory.sqlite3`
and `logs\inventory_builder.log`. Keep this on the internal SSD, outside source
media, Git, and cloud-synchronized directories. Explicit initialization creates
the runtime directory. Status and volumes do not initialize a missing database.

Copy `config/example.toml` to that directory as `config.toml` to customize.
Relative runtime paths resolve against the TOML file's directory; directory
exclusions resolve against the selected scan target.
Defaults are overridden by TOML, then `--database`. Global options precede the
command, for example `inventory-builder --config C:\LocalData\config.toml init`.
An explicit config file can be used if LOCALAPPDATA is unavailable.

The application does not yet verify SSD type or detect cloud-sync products;
the operator must choose a suitable runtime location. No external drives need
to be connected. The database will eventually contain sensitive file paths and
GPS information; keep database backups private and local.

Run `init` after upgrading to apply migrations. Newer schemas and modified
applied migrations are refused. Never copy only a live WAL-mode database file
for backup: use SQLite's backup API or close all connections before copying.

## Scanning and exclusions

After choosing and authorizing a specific folder, run:

```powershell
python -m inventory_builder scan "C:\YourExplicitlySelectedFolder" --exclude "Cache" --exclude "Private"
# Drive roots are supported too, after explicit authorization for that target:
python -m inventory_builder scan E:\ --exclude "E:\Private"
```

Drive-relative paths such as `E:` are rejected. The scanner does not select targets
or enumerate drives; it resolves only the chosen target's containing volume.
The database and logs must be outside included source media. Dedicated runtime
directories beneath a whole-drive target are pruned before enumeration; in-scope
databases must be in a dedicated `InventoryBuilder` or `runtime` directory.
The scanner reads filesystem attributes and embedded metadata and streams photo
contents for hashing. It never writes source files. Reparse points,
symlinks and junctions are skipped. Do not mutate the directory structure during
a scan: path revalidation is defensive, not an atomic filesystem snapshot or a
security boundary against malicious concurrent path replacement.

Ctrl+C exits with code 130. Repeat the same command to resume; committed results
are retained after unexpected process termination too. Once a session completes,
the next invocation starts a fresh pass, reusing completed work whose size, modification
time, filesystem file identity and processing/tool policy still match. Missing files
are retained. Files added to already completed
directories during a pause require a fresh pass after the resumed session completes.
Unchanged files still receive an observation and last-seen update; their inventory
attributes and existing embedded metadata are not reprocessed.

Inaccessible items produce errors and a blocked, resumable session (exit code 1).
Retry on the next invocation, after correcting access or reconnecting the source.
Database failures stop immediately and preserve prior transactions. Exit code 0
means included discovery, metadata and every required photo hash completed successfully.
Discovery can finish while processing remains incomplete. `status` shows recent sessions,
directory/file states, pruned-path counts, errors, separate metadata/hash counts and
checkpoints. `volumes` lists identities without device discovery.

```powershell
python -m inventory_builder status "C:\YourExplicitlySelectedFolder"
python -m inventory_builder verify "C:\YourExplicitlySelectedFolder"
```

`verify TARGET` checks the latest recorded session for that exact target and returns
0 only when discovery coverage and all current required stage results are present.
It returns 1 for missing/incomplete/failed/legacy filesystem-only collection. It is
a database checkpoint check, not a fresh read of a disconnected drive or proof that
files have not changed since collection. A subsequent scan refreshes that evidence.

Progress counters describe this process invocation. Discovered counts newly queued
candidate jobs; processed/unchanged counts successfully inventoried file jobs;
errors includes file and directory failures. Unsupported counts enumeration
encounters and may recount an unfinished directory after a retry. No total-file
percentage is displayed.

Ordinary `scan TARGET` automatically resumes its matching unfinished session.
If rules changed, it stops before source traversal and explains the difference.
Advanced explicit choices (mutually exclusive) are:

- `--resume-session ID`: use the saved policy, after displaying differences;
  current mandatory runtime/default/matcher safety must still be supported.
- `--new-scan`: supersede unfinished work and create a fresh linked session.
- `--reconcile-session ID`: create a linked successor with current rules, rediscovering
  included scope and reusing current media-level results.

Use `scan TARGET --retry-failed` to grant terminal failed jobs another bounded attempt
budget; attempt/error history is retained. Transient failures retry with persisted
backoff up to `processing_max_attempts`. If a retry is not yet due, the scan returns
incomplete rather than waiting indefinitely. Repairing/changing a failed file causes
its revision to be refreshed on resume; unchanged valid results remain reusable.

Completed Migration 003 sessions remain historical filesystem-only sessions. Their
next normal scan starts comprehensive collection without changing media IDs. Unfinished
filesystem-only sessions require an explicit new/reconciled successor. Metadata-tool
or unsupported processing-version changes also require an explicit successor; hashes
are reused when the file revision and hashing policy remain current.

## Metadata and photo hashing

ExifTool runs in fixed-size batches (32 by default, maximum 256) through UTF-8 argument
files in excluded application temporary storage. It receives validated file paths,
never recursive directory arguments or metadata-write commands. Default ExifTool
configuration loading is disabled. Per-file errors are isolated; transient batch
failures fall back to single-file attempts. `metadata_mode = "single"` is available
for debugging. Timeouts, output limits and Windows process-tree cleanup bound failed
invocations; Ctrl+C preserves committed results.

Original grouped tag values, timestamps, timezone offsets, camera/image/video/GPS
attributes, warnings, tool versions and historical results remain in SQLite. Capture
dates never fall back to filesystem dates or filename guesses. Filenames remain intact
for future analysis. Unaware dates remain unaware, including ambiguous QuickTime
creation dates; the computer's timezone is not silently applied. The selected date is
an embedded-source candidate with provenance/confidence, not proof of capture time.
Missing or invalid capture dates remain unknown. Missing optional metadata is not a
processing failure when the file is otherwise recognized and extraction succeeds.

Photo hashing uses bounded chunks (8 MiB default), file identity/size/mtime checks
before and after the full read, and an atomic complete-result transaction. An interrupted
attempt restarts that file from byte zero; no partial digest is accepted. Video jobs
cannot be inserted into the hash queue. Known video signatures and detected video MIME
types also block hashing of mislabeled photo filenames. Unknown/corrupt photo data
may still have a complete hash but failed metadata, so the session stays incomplete.

Progress separates discovery, metadata and photo hashing, showing completed, pending,
failed, retried attempts and reused results, plus current/completed hash byte counts.
No comprehensive completion is inferred from discovery alone or missing job links.

The integration matrix covers PNG/JPEG EXIF, HEIC/HEIF, Canon CR2/CR3, Nikon NEF, DNG,
MOV and generated MP4 metadata, including dates, dimensions and GPS where present.
See [the metadata review](docs/METADATA_REVIEW.md) for fixture provenance, ISO/lens/codec
corrections and exact coverage. The fixtures validate metadata parsing rather than
full pixel/video decoding. Additional camera/codec variants, stream-only telemetry
and unusual filenames still require compatibility validation before production.
ExifTool's static metadata output is retained; full frame-by-frame GPS telemetry
extraction is not implemented. These changes do not authorize or start a production scan.

Version 0.3.1 retains fractional derived ISO values with `sources.iso` and
`iso_is_derived` provenance instead of inventing nominal ISO settings. Lens identifiers
are not model names, and image compression is not a video codec. Raw source metadata
and existing historical results remain untouched; corrected metadata policies require
a new result while reusable photo hashes remain valid. No production-drive scan is
started by upgrading.

Persist `exclude_directories = ["Cache", "Private"]` in TOML, optionally with
`config_schema_version = 2`. Relative exclusions are relative to TARGET, not the
config file. Absolute ordinary exclusions must be inside TARGET. Repeated CLI,
configuration, system and runtime rules are combined. No exclusion globs or inclusion
overrides are supported. Excluded descendants are never enumerated; existing inventory
records remain, with no missing/deleted inference. Excluding the entire target is an error.

System defaults are anchored at the selected volume root: `$RECYCLE.BIN`,
`System Volume Information`, `Windows`, `Program Files`, `Program Files (x86)`,
`ProgramData`, `Recovery`, `$WinREAgent`. They do not match those names elsewhere.
Dedicated application runtime/log/temp/backup/report directories and exact database
companions are mandatory global exclusions. Unrelated outside-root rules are inactive.

Windows path keys use versioned native ordinal comparisons, preserving spelling.
Per-directory case-sensitive sources, UNC/device paths and reparse traversal are
unsupported. A changed Windows matcher version blocks affected scans until an explicit
path-key migration is provided; do not manually reindex with a different collation.
The application registers SQLite's WINPATH collation; external tools need the same
collation to write indexed path fields safely.

Overlapping roots reference the same physical-volume/media path records. Drive-letter
changes alone preserve identity and resume state. Ambiguous/cloned volume evidence
requires an explicit `--volume-id UUID` decision (or `--volume-id new` to register a
distinct identity). Exact clones may not be distinguishable from identical observations;
the application does not claim hardware-level identity proof or silently resolve ambiguity.
No marker is written to source storage.

Legacy provisional folders require `--confirm-legacy-volume` on their original root.
Unfinished legacy sessions also require `--new-scan` or `--reconcile-session ID`.
IDs, history and metadata are preserved in one conversion transaction; path/case
collisions abort it for explicit resolution. Resolve overlapping legacy scopes before
scanning their parent/root. Unresolved or changed directory identity is never auto-merged.

The repository's portable interpreter, if present, can run the suite with
`.\.venv\python.exe -m pytest -q`; a conventional virtual environment uses
`.\.venv\Scripts\python.exe -m pytest -q`.

See [architecture](docs/ARCHITECTURE.md) and [requirements](docs/REQUIREMENTS.md).
