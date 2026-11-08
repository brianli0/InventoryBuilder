"""Process-held lock; a stale lock file does not imply a held lock."""

from contextlib import contextmanager
import os
from pathlib import Path


class ScannerBusyError(RuntimeError):
    pass


@contextmanager
def scanner_lock(database: Path):
    target = database.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Never unlink the file: another process may already have it open.
    with Path(str(target) + ".lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ScannerBusyError("Another writer holds the database lock") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
