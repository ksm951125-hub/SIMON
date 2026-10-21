"""대상 대화 식별. 확실히 일치할 때만 ok=True (잘못된 창/대화 입력 방지)."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Snapshot, Target, norm_text


@dataclass
class Identity:
    ok: bool
    reason: str = ""


def _match(candidates, wanted: str, mode: str) -> bool:
    for c in candidates:
        n = norm_text(c)
        if not n:
            continue
        if n == wanted or (mode == "contains" and wanted in n):
            return True
    return False


def identify(snap: Snapshot, target: Target, *, require_project: bool = True,
             mode: str = "exact") -> Identity:
    conv = norm_text(target.conversation)
    if not conv:
        return Identity(False, "대상 대화가 지정되지 않음")
    if not snap.found:
        return Identity(False, "Codex 창 없음")
    if not _match(snap.conversation_candidates, conv, mode):
        shown = ", ".join(c for c in snap.conversation_candidates[:4]) or "없음"
        return Identity(False, f"현재 활성 대화가 대상과 다름/확인 불가 (읽은 제목: {shown})")
    proj = norm_text(target.project)
    if require_project and proj:
        if not _match(list(snap.project_candidates) + list(snap.conversation_candidates), proj, mode):
            return Identity(False, "프로젝트명을 화면에서 확인하지 못함")
    return Identity(True, "대상 대화 확인됨")
