"""플랫폼 독립 데이터 모델과 백엔드 인터페이스."""
from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


def norm_text(s: Optional[str]) -> str:
    """대화 제목 비교용 정규화: 유니코드 정규화, 대소문자/공백/구두점 제거."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"[\s\-_·•|:,.、/\\()\[\]{}\"'“”‘’]+", "", s)


@dataclass
class Target:
    """자동 재개 대상 대화. (v1은 1개만 사용, 구조는 다중 대상으로 확장 가능)"""
    project: str = ""
    conversation: str = ""
    enabled: bool = True

    @property
    def key(self) -> str:
        return f"{norm_text(self.project)}/{norm_text(self.conversation)}"

    @property
    def label(self) -> str:
        if self.project and self.project != self.conversation:
            return f"{self.project} / {self.conversation}"
        return self.conversation or self.project or "(미지정)"


@dataclass
class Snapshot:
    """Codex 창을 한 번 읽은 결과."""
    ui_available: bool = True          # False: 잠금 화면/절전 등으로 UI 조작 불가
    found: bool = False                # Codex 창을 찾았는가
    minimized: bool = False
    title: str = ""
    texts: List[str] = field(default_factory=list)            # 문서 순서의 텍스트 조각
    banner_texts: Optional[List[str]] = None                  # 하단(입력창 주변) 텍스트. None=알 수 없음
    conversation_candidates: List[str] = field(default_factory=list)  # 현재 활성 대화 제목 후보
    project_candidates: List[str] = field(default_factory=list)       # 현재 프로젝트명 후보
    busy: bool = False                 # Codex가 작업 중(중지 버튼/작업 중 표시)
    input_found: bool = False
    input_text: str = ""
    error: str = ""


@dataclass
class ActionResult:
    """select_conversation / send_text 결과.

    ok=False 이면 '메시지가 제출되지 않았음이 확실'해야 한다.
    제출 여부가 불확실하면 ok=True 로 돌려주고 엔진의 사후 검증에 맡긴다.
    """
    ok: bool = False
    deferred: bool = False   # 사용자가 PC를 사용 중이라 지금은 조작하지 않음 (오류 아님)
    method: str = ""
    error: str = ""


class CodexBackend(ABC):
    @abstractmethod
    def snapshot(self) -> Snapshot: ...

    @abstractmethod
    def restore_window(self) -> bool:
        """최소화된 Codex 창을 포커스를 빼앗지 않고 복원."""

    @abstractmethod
    def select_conversation(self, target: Target) -> ActionResult:
        """기존 대화 목록에서 대상 대화를 선택(새 대화 생성 금지)."""

    @abstractmethod
    def send_text(self, text: str, target: Target) -> ActionResult: ...

    def list_conversations(self) -> List[str]:
        return []
