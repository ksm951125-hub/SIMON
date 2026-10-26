"""모든 백엔드 호출을 전용 스레드 1개에서 실행 (UI Automation/COM 은 스레드 간 공유 금지)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from .models import ActionResult, CodexBackend, Snapshot, Target


def new_uia_executor() -> ThreadPoolExecutor:
    """프로세스 전체에서 UIA/COM 호출을 처리할 단일 스레드 (모든 앱 프로필이 공유)."""
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia")


class ThreadedBackend(CodexBackend):
    def __init__(self, inner: CodexBackend, timeout: float = 120.0,
                 executor: Optional[ThreadPoolExecutor] = None):
        self._inner = inner
        self._timeout = timeout
        self._own = executor is None
        self._ex = executor or new_uia_executor()

    def _call(self, fn, *a):
        return self._ex.submit(fn, *a).result(timeout=self._timeout)

    def snapshot(self) -> Snapshot:
        return self._call(self._inner.snapshot)

    def restore_window(self) -> bool:
        return self._call(self._inner.restore_window)

    def select_conversation(self, target: Target) -> ActionResult:
        return self._call(self._inner.select_conversation, target)

    def send_text(self, text: str, target: Target) -> ActionResult:
        return self._call(self._inner.send_text, text, target)

    def list_conversations(self) -> List[str]:
        return self._call(self._inner.list_conversations)

    def close(self) -> None:
        if self._own:
            self._ex.shutdown(wait=False)
