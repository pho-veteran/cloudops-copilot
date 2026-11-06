"""Bounded host file locks for trusted authority files outside PostgreSQL.

POSIX hosts use ``flock`` shared/exclusive modes. Windows development and test hosts use an
exclusive byte-range lock for both modes: stricter, never weaker. Deployments run on Linux.
"""

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path


class LockUnavailable(RuntimeError):
    """The lock was not acquired within its bound; callers must fail closed."""


@contextmanager
def file_lock(path: Path, *, exclusive: bool, timeout: float = 15.0, create: bool = False):
    """Hold ``path`` locked. Readers open it read-only, so a read-only mount is enough."""
    mode = "ab" if create else "rb"
    with open(path, mode) as handle:
        deadline = time.monotonic() + timeout
        if sys.platform == "win32":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockUnavailable("Authority lock unavailable.") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            while True:
                try:
                    fcntl.flock(handle.fileno(), flag | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockUnavailable("Authority lock unavailable.") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def replace_durably(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Atomically replace ``path`` after its new content reaches storage."""
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    deadline = time.monotonic() + 2
    while True:
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            # Windows refuses replacement while a lock-free reader holds the old file open.
            if sys.platform != "win32" or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    if sys.platform != "win32":
        os.chmod(path, mode)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def append_durably(path: Path, line: bytes, mode: int = 0o640) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        _write_all(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]
