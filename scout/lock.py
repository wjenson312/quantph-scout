"""A single-holder lock file, so two cron runs can never overlap.

Uses flock(2), which the kernel releases even if the process is killed -- a stale
lock file left behind by a reboot or SIGKILL does not wedge the next run.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from types import TracebackType


class LockBusy(RuntimeError):
    """Raised when another quantph-scout run already holds the lock."""


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                holder = self._read_holder()
                raise LockBusy(
                    f"another run holds {self.path}{holder}; exiting without doing anything"
                ) from exc
            raise
        os.truncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd
        return self

    def _read_holder(self) -> str:
        try:
            pid = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return f" (pid {pid})" if pid else ""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
