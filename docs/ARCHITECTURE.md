# Milestone 1 architecture

The runtime uses only Python's standard library. pytest is a development
dependency and setuptools is the packaging backend. TOML avoids a YAML runtime
dependency. No scanning or volume discovery exists in this milestone.

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
to source media. This milestone only creates the tables.

Scan roots are volume-relative. Media uniqueness is volume plus exact
volume-relative path, preventing overlapping roots from duplicating files.
Per-session observations preserve root-relative paths. Exact spelling is retained;
future Windows path handling must account for case-sensitive directories.

A scan session is one durable inventory pass; scan runs are individual process
executions. A partial unique index prevents multiple unfinished sessions per root.
Directory jobs and file records will be committed together in bounded batches.
A directory may only become complete after all children and candidate files are
durable. Interrupted jobs will be re-enumerated and upserted idempotently. Recovery
must acquire the writer lock before resetting abandoned processing claims.

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
when future scanners read files; the application will never request source writes
or attempt to restore source timestamps.
