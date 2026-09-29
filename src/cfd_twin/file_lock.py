"""Process locks shared by Windows and POSIX workflow entry points."""

from __future__ import annotations

import errno
import os
import time
from typing import BinaryIO


def lock_file(stream: BinaryIO, *, blocking: bool = False) -> None:
    """Lock an open binary file until unlock or close; never truncate it."""
    if os.name != "nt":
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return

    import msvcrt

    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"1")
        stream.flush()
    while True:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if not blocking:
                raise BlockingIOError(exc.errno, "Another process holds this lock") from exc
            time.sleep(0.1)


def unlock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_UN)
