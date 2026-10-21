"""--test-mode: 실제 Codex 없이 '제한 → 초기화 → 재개 → 다음 제한' 반복을 실시간으로 재현."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .backend_fake import FakeBackend
from .fmt import fmt_clock
from .models import ActionResult, Target
from .timeparse import now_kst


class SimulatedCodex(FakeBackend):
    """실제 사용량 제한이 없어도 전체 주기를 확인할 수 있는 가상 Codex.

    scenario:
      normal             - 정상 반복
      wrong-conversation - 다른 대화가 열려 있고 전환 불가 → 전송하지 않아야 함
      user-typing        - 사용자가 입력창에 글을 쓰고 있음 → 덮어쓰지 않아야 함
    """

    def __init__(self, scenario: str = "normal", reset_in_minutes: float = 2.0,
                 work_seconds: float = 40.0, clock: Callable[[], datetime] = now_kst):
        super().__init__()
        self.clock = clock
        self.scenario = scenario
        self.reset_in = reset_in_minutes
        self.work_seconds = work_seconds
        self._reset_at = None
        self._busy_until = None
        self._start_limit()
        if scenario == "wrong-conversation":
            self.active_conversation = "다른 대화 A"
            self.active_project = "다른 프로젝트"
            self.select_behavior = "fail"
        elif scenario == "user-typing":
            self.input_text = "사용자가 작성 중인 문장입니다..."

    def _start_limit(self) -> None:
        t = self.clock() + timedelta(minutes=self.reset_in)
        self._reset_at = (t + timedelta(minutes=1)).replace(second=0, microsecond=0) \
            if t.second else t.replace(microsecond=0)
        self.limit_text = (f"Codex 및 Work 사용량을 모두 사용했습니다. "
                           f"요청 한도는 {fmt_clock(self._reset_at)}에 재설정됩니다.")
        self.busy = False

    def _advance(self) -> None:
        now = self.clock()
        if self.limit_text and self._reset_at and now >= self._reset_at:
            self.limit_text = ""        # 초기화 시각이 지나면 제한 안내가 사라짐
        if self.busy and self._busy_until and now >= self._busy_until:
            self._busy_until = None
            self._start_limit()

    def snapshot(self):
        self._advance()
        return super().snapshot()

    def send_text(self, text: str, target: Target) -> ActionResult:
        now = self.clock()
        if self._reset_at and now < self._reset_at:
            return ActionResult(ok=False, error="아직 사용량 제한 중(시뮬레이터)")
        res = super().send_text(text, target)
        if res.ok:
            self._busy_until = self.clock() + timedelta(seconds=self.work_seconds)
        return res
