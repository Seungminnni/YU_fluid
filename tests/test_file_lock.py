from __future__ import annotations

import errno
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from cfd_twin.file_lock import lock_file, unlock_file


class FileLockTests(unittest.TestCase):
    def test_other_process_cannot_enter_until_unlock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            code = "from cfd_twin.file_lock import lock_file; import sys; f=open(sys.argv[1], 'a+b'); lock_file(f)"
            with path.open("a+b") as stream:
                lock_file(stream)
                blocked = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, timeout=20)
                self.assertNotEqual(blocked.returncode, 0)
                unlock_file(stream)
                released = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, timeout=20)
                self.assertEqual(released.returncode, 0, released.stderr)

    def test_windows_contention_retries_and_unlocks_the_same_byte(self):
        api = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock(side_effect=[OSError(errno.EACCES, "busy"), None, None]))
        with tempfile.TemporaryFile("a+b") as stream:
            with patch("cfd_twin.file_lock.os.name", "nt"), patch.dict(sys.modules, msvcrt=api), patch("cfd_twin.file_lock.time.sleep") as sleep:
                lock_file(stream, blocking=True)
                unlock_file(stream)
                sleep.assert_called_once_with(0.1)
                self.assertEqual(api.locking.call_args.args, (stream.fileno(), api.LK_UNLCK, 1))
                stream.seek(0)
                self.assertEqual(stream.read(), b"1")

    def test_windows_nonblocking_busy_is_not_silently_ignored(self):
        api = types.SimpleNamespace(LK_NBLCK=2, locking=Mock(side_effect=OSError(errno.EACCES, "busy")))
        with tempfile.TemporaryFile("a+b") as stream:
            with patch("cfd_twin.file_lock.os.name", "nt"), patch.dict(sys.modules, msvcrt=api):
                with self.assertRaises(BlockingIOError):
                    lock_file(stream)
