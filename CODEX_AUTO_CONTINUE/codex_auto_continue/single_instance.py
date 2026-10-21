"""중복 실행 방지: 두 개가 동시에 돌면 '계속'이 두 번 전송될 수 있다."""
from __future__ import annotations

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        try:
            fh.seek(0); fh.truncate(); fh.write(str(os.getpid())); fh.flush()
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
