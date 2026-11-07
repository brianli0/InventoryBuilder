# InventoryBuilder — Codex Instructions

This project is a local Python application for inventorying large photo and video collections across multiple external drives.

## Project requirements

- Use Python and SQLite.
- Maintain one combined inventory database.
- Scan one external drive at a time.
- Identify drives independently of their changing Windows drive letters.
- Extract filesystem and embedded media metadata.
- Support interruption, checkpoints, and reliable resumption.
- Preserve existing records when drives are disconnected.
- Never modify, rename, move, or delete source media files.
- Keep the database, logs, and generated reports outside Git.
- Implement and test features incrementally.

## Development workflow

1. Read `docs/REQUIREMENTS.md` before implementing features.
2. Explain significant architectural decisions.
3. Add automated tests for scanning and resume behavior.
4. Prioritize data safety, recoverability, and correctness over speed.
5. Do not perform destructive operations without explicit authorization.