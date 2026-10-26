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
                 work_seconds: float = 40.0, clock: Callable[[], datetime] = now_kst,
                 style: str = "codex", conversation: str = "MY AI OFFICE 구축하기",
                 project: str = "MY AI OFFICE 구축하기"):
        super().__init__(conversation, project)
        self.style = style
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
        t = fmt_clock(self._reset_at)
        if self.style == "claude":       # 닫을 수 있는 토스트
            self.limit_text = f"사용량 제한을 초과하여 종료됨\n{t}에 초기화\n더 알아보기\n사용량 추가 구매"
            self.banner_extra = []
        else:                            # 하단 카드 + 대화 기록에 남는 오류 줄
            self.limit_text = ("Codex 및 Work 사용량을 모두 사용했습니다\n"
                               f"크레딧을 추가하거나 플랜을 업그레이드하세요. 또는 {t}에 사용량이 재설정될 때까지 기다리세요.\n"
                               "크레딧 추가")
            self.banner_extra = [f"사용 한도에 도달했습니다. 계속하려면 요금제를 업그레이드하거나 크레딧을 추가하세요. "
                                 f"또는 {t}에 다시 시도하세요."]
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
