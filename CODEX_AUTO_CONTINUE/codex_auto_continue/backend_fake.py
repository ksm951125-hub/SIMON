"""테스트/시뮬레이션용 가짜 Codex 백엔드 (실제 Codex 와 무관, 어떤 외부 상태도 건드리지 않음)."""
from __future__ import annotations

from typing import List

from .models import ActionResult, CodexBackend, Snapshot, Target, norm_text


class FakeBackend(CodexBackend):
    def __init__(self, conversation: str = "MY AI OFFICE 구축하기", project: str = "MY AI OFFICE 구축하기"):
        self.window_found = True
        self.minimized = False
        self.ui_available = True
        self.active_conversation = conversation
        self.active_project = project
        self.other_conversations: List[str] = ["다른 대화 A", "다른 대화 B"]
        self.select_behavior = "ok"       # ok | deferred | fail
        self.busy = False
        self.input_found = True
        self.input_text = ""
        self.limit_text = ""              # 비어 있지 않으면 제한 안내가 보임
        self.body_texts: List[str] = ["앞선 작업 내용...", "테스트 코드를 작성했습니다."]
        self.send_behavior = "ok"         # ok | fail | silent | not_submitted | deferred
        self.banner_zone = True           # False면 banner_texts=None (꼬리 텍스트로 감지)
        # 기록
        self.sent: List[str] = []
        self.restore_calls = 0
        self.select_calls = 0
        self.snapshots = 0

    # ---- Snapshot ----
    def snapshot(self) -> Snapshot:
        self.snapshots += 1
        if not self.ui_available:
            return Snapshot(ui_available=False)
        if not self.window_found:
            return Snapshot(found=False)
        banner = [self.limit_text] if self.limit_text else []
        texts = list(self.body_texts) + banner
        return Snapshot(
            ui_available=True, found=True, minimized=self.minimized,
            title=f"{self.active_conversation} - Codex", texts=texts,
            banner_texts=banner if self.banner_zone else None,
            conversation_candidates=[self.active_conversation] if self.active_conversation else [],
            project_candidates=[self.active_project] if self.active_project else [],
            busy=self.busy, input_found=self.input_found, input_text=self.input_text)

    def restore_window(self) -> bool:
        self.restore_calls += 1
        self.minimized = False
        return True

    def select_conversation(self, target: Target) -> ActionResult:
        self.select_calls += 1
        if self.select_behavior == "deferred":
            return ActionResult(deferred=True, error="사용자가 PC 사용 중")
        if self.select_behavior == "fail":
            return ActionResult(ok=False, error="사이드바에서 대화를 찾지 못함")
        self.active_conversation = target.conversation
        self.active_project = target.project
        return ActionResult(ok=True, method="fake-select")

    def send_text(self, text: str, target: Target) -> ActionResult:
        # 실제 백엔드와 같은 최종 안전장치: 활성 대화가 대상이 아니면 절대 입력하지 않는다.
        if norm_text(self.active_conversation) != norm_text(target.conversation):
            return ActionResult(ok=False, error="활성 대화가 대상이 아님")
        b = self.send_behavior
        if b == "deferred":
            return ActionResult(deferred=True, error="사용자 활동 중")
        if b == "fail":
            return ActionResult(ok=False, error="입력 실패")
        self.sent.append(text)
        if b == "not_submitted":
            self.input_text = text
        elif b == "silent":
            pass
        else:
            self.input_text = ""
            self.busy = True
            self.limit_text = ""
            self.body_texts.append(text)
        return ActionResult(ok=True, method="fake")

    def list_conversations(self) -> List[str]:
        return [self.active_conversation] + self.other_conversations
