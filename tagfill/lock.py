"""One writer per workdir.

Two concurrent runs will not corrupt the append-only journal structurally,
but they race each other's census.csv rewrite and interleave decisions
about the same files. A GUI is exactly where that happens: someone
double-clicks the run button, or leaves a scheduled sweep going and starts
another by hand. A lockfile turns that from a subtle mess into a clean
error.

O_EXCL create is the portable primitive here -- it is atomic on POSIX and
on Windows, needs no fcntl (which Windows lacks) and no msvcrt (which
POSIX lacks). Checking whether the holder is still alive is the part that
does need a platform split; see _alive_win32 for why.

The cost of O_EXCL is that a killed process leaves the file behind, so
the holder's pid goes inside it and a lock whose pid is gone is taken over
rather than obeyed. Reading is unaffected: this guards writes to the
workdir, not the collection.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .util import private_mkdir


class WorkdirBusy(RuntimeError):
    pass


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _alive_win32(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True             # someone else's process, so it exists
    return True


def _alive_win32(pid: int) -> bool:
    """os.kill(pid, 0) is not a liveness probe on Windows.

    Signal 0 is CTRL_C_EVENT there, so CPython turns the "check" into a
    GenerateConsoleCtrlEvent and sends Ctrl+C to the target's console
    group. The Windows CI job found this the only way it could: pytest took
    the interrupt and died at 83% with a KeyboardInterrupt raised inside
    this function.

    OpenProcess is the actual question. A null handle with
    ERROR_ACCESS_DENIED means the process is there and not ours to look at,
    which still counts as alive -- treating that as dead would steal a live
    lock.
    """
    import ctypes
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x102
    ERROR_ACCESS_DENIED = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


class WorkdirLock:
    """Context manager. Raises WorkdirBusy if another live run holds it."""

    def __init__(self, workdir: Path):
        private_mkdir(workdir)
        self.path = workdir / "lock"
        self._held = False

    def __enter__(self) -> WorkdirLock:
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    def acquire(self) -> None:
        try:
            self._create()
        except FileExistsError:
            pid = self._holder()
            if pid is not None and pid != os.getpid() and _alive(pid):
                raise WorkdirBusy(
                    f"another tagfill run (pid {pid}) is using "
                    f"{self.path.parent}") from None
            # A crash left it behind, or it is ours. Take it over.
            self.path.unlink(missing_ok=True)
            self._create()
        self._held = True

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def _create(self) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)

    def _holder(self) -> int | None:
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
