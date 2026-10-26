"""Windows UI Automation(pywinauto, UIA) 기반 Codex 데스크톱 앱 어댑터.

원칙
- 화면 좌표 클릭을 쓰지 않는다. 요소 이름/역할/UIA 패턴(Value/Invoke/SelectionItem)만 사용.
- 1순위: 포커스를 옮기지 않는 UIA ValuePattern + Invoke(전송 버튼).
- 2순위(키보드): 사용자가 일정 시간 입력이 없을 때만, 대상 창이 실제 foreground/focus 임을
  확인한 뒤에만 입력. 어느 단계든 확인에 실패하면 입력하지 않고 중단한다.
- 비공개 API, 인증정보, 네트워크 가로채기는 일절 사용하지 않는다.

주의: Codex 앱의 실제 UI 트리는 이 코드를 작성한 환경에서 확인할 수 없었다. 첫 실행 전
`inspect` 로 요소 이름/구조를 확인하고, 필요하면 data/config.json 의 selectors 를 보정할 것.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, List, Tuple

from . import winutil
from .config import Config
from .models import ActionResult, CodexBackend, Snapshot, Target, norm_text

log = logging.getLogger("cac.uia")

TEXT_TYPES = ("Text", "Hyperlink")
ITEM_TYPES = ("ListItem", "TreeItem", "TabItem", "DataItem", "Hyperlink", "Button", "Text")
SELECTABLE_TYPES = ("ListItem", "TreeItem", "TabItem", "DataItem")
_TITLE_SPLIT = re.compile(r"\s+[-–—|·]\s+")
_SELF_EXE_PREFIX = "codex_auto"     # 이 프로그램의 exe 빌드본은 Codex 로 오인하지 않는다


def _rect(info) -> Tuple[int, int, int, int]:
    r = info.rectangle
    return r.left, r.top, r.right, r.bottom


class UiaBackend(CodexBackend):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._hwnd = 0
        self._pid = 0
        self._desktop = None

    # ------------------------------------------------------------ 창 찾기
    def _find_window(self):
        from pywinauto import Desktop
        import psutil

        if self._desktop is None:
            self._desktop = Desktop(backend="uia")
        names = [n.lower() for n in self.cfg.process_names if n]
        regex = re.compile(self.cfg.window_title_regex) if self.cfg.window_title_regex else None
        own = os.getpid()
        best, best_area = None, -1
        for w in self._desktop.windows():
            try:
                pid = w.process_id()
                if pid == own:
                    continue
                exe = psutil.Process(pid).name().lower()
                if exe.startswith(_SELF_EXE_PREFIX):
                    continue
                title = w.window_text() or ""
                by_name = bool(names) and any(n in exe for n in names)
                by_title = regex is not None and bool(regex.search(title))
                if not (by_name or by_title):
                    continue
                r = w.rectangle()
                area = max(0, r.width()) * max(0, r.height())
                if area > best_area:
                    best, best_area = w, area
            except Exception:
                continue
        if best is not None:
            self._hwnd = best.handle
            self._pid = best.process_id()
        return best

    # ------------------------------------------------------------ 읽기
    def snapshot(self) -> Snapshot:
        snap = Snapshot()
        if not winutil.input_desktop_available():
            snap.ui_available = False
            return snap
        try:
            win = self._find_window()
        except Exception as e:
            snap.error = f"창 검색 오류: {e}"
            log.debug("find_window 실패", exc_info=True)
            return snap
        if win is None:
            return snap
        snap.found = True
        try:
            snap.title = win.window_text() or ""
            snap.minimized = bool(win.is_minimized())
            self._scan(win, snap)
        except Exception as e:
            snap.error = f"UI 읽기 오류: {e}"
            log.debug("UI 스캔 실패", exc_info=True)
        return snap

    def _scan(self, win, snap: Snapshot) -> None:
        sel = self.cfg.selectors
        wl, wt, wr, wb = _rect(win.element_info)
        width = max(1, wr - wl)
        deadline = time.time() + float(sel.get("scan_seconds", 8.0))
        busy_re = re.compile(sel.get("busy_text_regex") or r"(?!x)x")
        stop_names = [s.lower() for s in sel.get("stop_button_names", [])]
        zone = int(sel.get("banner_zone_px", 340))
        hzone = int(sel.get("header_zone_px", 150))
        side_x = wl + width * float(sel.get("sidebar_ratio", 0.30))

        texts: List[str] = []
        banner: List[str] = []
        headers: List[str] = []
        side: List[Tuple[str, bool, List[str]]] = []     # (이름, 선택됨, 조상 이름들) - 사이드바 문서 순서
        edits: List[Any] = []
        busy = False

        infos = win.element_info.descendants()
        limit = int(sel.get("max_elements", 4000))
        for i, info in enumerate(infos):
            if i >= limit or time.time() > deadline:
                break
            try:
                ct = info.control_type
                name = (info.name or "").strip()
                if ct in TEXT_TYPES and name:
                    texts.append(name)
                    l, t, r, b = _rect(info)
                    if b >= wb - zone:
                        banner.append(name)
                    if t <= wt + hzone and (l + r) / 2 > side_x:
                        headers.append(name)
                    if busy_re.search(name) and b >= wb - zone:
                        busy = True
                elif ct == "Button" and name and name.lower() in stop_names and info.enabled and info.visible:
                    busy = True
                elif ct == "Edit" and info.visible:
                    edits.append(info)
                if ct in ITEM_TYPES and name and 1 < len(name) <= 120 and _rect(info)[2] <= side_x + 20 \
                        and (not side or side[-1][0] != name):
                    sel_flag = ct in SELECTABLE_TYPES and self._is_selected(info)
                    side.append((name, sel_flag, self._ancestor_names(info) if sel_flag else []))
            except Exception:
                continue

        snap.texts = texts
        snap.banner_texts = banner
        snap.busy = busy
        title_parts = [snap.title] + [p for p in _TITLE_SPLIT.split(snap.title) if p]
        header_norm = {norm_text(h) for h in headers}
        active = [i for i, e in enumerate(side) if e[1]]
        if not active:     # 선택 상태가 UIA 로 노출되지 않으면, 상단 제목과 같은 이름의 사이드바 항목을 활성으로 본다
            active = [i for i, e in enumerate(side) if norm_text(e[0]) in header_norm]
        selected = [side[i][0] for i in active]
        # 프로젝트: 활성 항목의 조상 + 사이드바에서 그 항목 바로 위쪽 항목들(프로젝트 머리글은 보통 형제 항목)
        context: List[str] = []
        for i in active:
            context += side[i][2] + [n for n, _, _ in side[max(0, i - 8):i]]
        snap.conversation_candidates = [c for c in title_parts + selected + headers if c]
        snap.project_candidates = [c for c in context + headers + title_parts if c]

        if edits:
            edit = max(edits, key=lambda e: _rect(e)[1])      # 가장 아래의 입력창
            snap.input_found = True
            snap.input_text = self._edit_value(edit)

    @staticmethod
    def _is_selected(info) -> bool:
        try:
            from pywinauto.controls.uiawrapper import UIAWrapper
            return bool(UIAWrapper(info).iface_selection_item.CurrentIsSelected)
        except Exception:
            return False

    @staticmethod
    def _ancestor_names(info, depth: int = 6) -> List[str]:
        out, cur = [], info
        for _ in range(depth):
            try:
                cur = cur.parent
            except Exception:
                break
            if cur is None:
                break
            try:
                if cur.name:
                    out.append(cur.name.strip())
            except Exception:
                pass
        return out

    def _edit_value(self, info) -> str:
        from pywinauto.controls.uiawrapper import UIAWrapper
        w = UIAWrapper(info)
        try:
            v = w.iface_value.CurrentValue or ""
        except Exception:
            try:
                v = w.window_text() or ""
            except Exception:
                v = ""
        v = v.strip()
        if v in [p.strip() for p in self.cfg.selectors.get("input_placeholders", [])]:
            return ""
        return v

    # ------------------------------------------------------------ 창 복원
    def restore_window(self) -> bool:
        return bool(self._hwnd) and winutil.restore_no_activate(self._hwnd)

    # ------------------------------------------------------------ 대화 선택
    def list_conversations(self) -> List[str]:
        win = self._find_window()
        if win is None:
            return []
        wl, _, wr, _ = _rect(win.element_info)
        side_x = wl + (wr - wl) * float(self.cfg.selectors.get("sidebar_ratio", 0.30))
        seen, out = set(), []
        for info in win.element_info.descendants():
            try:
                if info.control_type in ITEM_TYPES and info.name and _rect(info)[2] <= side_x + 20:
                    n = info.name.strip()
                    if 1 < len(n) <= 120 and n not in seen:
                        seen.add(n)
                        out.append(n)
            except Exception:
                continue
        return out[:300]

    def _user_busy_in_codex(self) -> bool:
        """사용자가 지금 이 PC를 쓰는 중이고 그 대상이 Codex 창이면 True."""
        return (winutil.idle_seconds() < self.cfg.idle_required_seconds
                and winutil.foreground_hwnd() == self._hwnd)

    def select_conversation(self, target: Target) -> ActionResult:
        win = self._find_window()
        if win is None:
            return ActionResult(error="Codex 창 없음")
        if self._user_busy_in_codex():
            return ActionResult(deferred=True, error="사용자가 Codex를 사용 중 - 대화 전환 보류")
        from pywinauto.controls.uiawrapper import UIAWrapper
        wl, _, wr, _ = _rect(win.element_info)
        side_x = wl + (wr - wl) * float(self.cfg.selectors.get("sidebar_ratio", 0.30))
        want = norm_text(target.conversation)
        proj = norm_text(target.project)
        matches, proj_seen = [], False
        for info in win.element_info.descendants():
            try:
                if info.control_type not in ITEM_TYPES or not info.name:
                    continue
                if _rect(info)[2] > side_x + 20:
                    continue
                n = norm_text(info.name)
                if proj and n == proj and n != want:
                    proj_seen = True
                if n == want:
                    matches.append((info, proj_seen))
            except Exception:
                continue
        if not matches:
            return ActionResult(error="사이드바에서 대상 대화를 찾지 못함")
        if len(matches) > 1:
            after = [m for m in matches if m[1]]
            if len(after) >= 1 and proj:
                matches = after[:1]
            else:
                return ActionResult(error="동일한 이름의 대화가 여러 개라 특정할 수 없음")
        info = matches[0][0]
        w = UIAWrapper(info)
        for how in ("select", "invoke"):
            try:
                getattr(w, how)()
                time.sleep(0.8)
                return ActionResult(ok=True, method=f"uia-{how}")
            except Exception:
                continue
        if winutil.idle_seconds() >= self.cfg.idle_required_seconds:
            try:
                w.click_input()          # 요소 중심 클릭 (고정 좌표 아님). 사용자 유휴 시에만.
                time.sleep(0.8)
                return ActionResult(ok=True, method="uia-click")
            except Exception as e:
                return ActionResult(error=f"대화 선택 실패: {e}")
        return ActionResult(deferred=True, error="사용자 활동 중 - 클릭 방식 대화 전환 보류")

    # ------------------------------------------------------------ 전송
    def send_text(self, text: str, target: Target) -> ActionResult:
        win = self._find_window()
        if win is None:
            return ActionResult(error="Codex 창 없음")
        method = self.cfg.send_method
        if method in ("auto", "uia"):
            r = self._send_via_uia(win, text)
            if r.ok or method == "uia":
                return r
            log.info("UIA 직접 전송 불가(%s) → 키보드 방식 시도", r.error)
            if r.deferred:
                return r
        return self._send_via_keyboard(win, text, target)

    def _find_input_and_button(self, win):
        from pywinauto.controls.uiawrapper import UIAWrapper
        names = [n.lower() for n in self.cfg.selectors.get("send_button_names", [])]
        edits, buttons = [], []
        for info in win.element_info.descendants():
            try:
                if not info.visible:
                    continue
                if info.control_type == "Edit":
                    edits.append(info)
                elif info.control_type == "Button" and info.name and \
                        any(n == info.name.strip().lower() for n in names):
                    buttons.append(info)
            except Exception:
                continue
        edit = max(edits, key=lambda e: _rect(e)[1]) if edits else None
        btn = max(buttons, key=lambda e: _rect(e)[1]) if buttons else None
        return (UIAWrapper(edit) if edit is not None else None,
                UIAWrapper(btn) if btn is not None else None)

    def _send_via_uia(self, win, text: str) -> ActionResult:
        if self._user_busy_in_codex():
            return ActionResult(deferred=True, error="사용자가 앱을 사용 중")
        edit, btn = self._find_input_and_button(win)
        if edit is None:
            return ActionResult(error="입력창을 찾지 못함")
        if btn is None:
            return ActionResult(error="전송 버튼을 이름으로 찾지 못함")
        try:
            was_enabled = bool(btn.is_enabled())
        except Exception:
            was_enabled = True
        if was_enabled:
            # 빈 입력창에서도 버튼이 활성이면, 앱이 입력 변화에 반응하는지 확인할 수 없다
            # (값만 바뀌고 실제로는 전송되지 않는 상황을 막기 위해 키보드 방식으로 넘긴다)
            return ActionResult(error="전송 버튼 활성 상태로 UIA 입력 반영 여부를 검증할 수 없음")

        def clear():
            try:
                edit.iface_value.SetValue("")
            except Exception:
                pass
        try:
            edit.iface_value.SetValue(text)
        except Exception as e:
            return ActionResult(error=f"ValuePattern 미지원: {e}")
        time.sleep(0.5)
        try:
            ok_text = (edit.iface_value.CurrentValue or "").strip() == text
            enabled_now = bool(btn.is_enabled())
        except Exception:
            ok_text, enabled_now = False, False
        if not (ok_text and enabled_now):
            clear()      # 앱 상태에 반영되지 않음(전송 버튼이 여전히 비활성)
            return ActionResult(error="UIA 입력이 앱에 반영되지 않음")
        try:
            btn.invoke()
        except Exception as e:
            clear()
            return ActionResult(error=f"전송 버튼 호출 실패: {e}")
        return ActionResult(ok=True, method="uia-invoke")

    def _send_via_keyboard(self, win, text: str, target: Target) -> ActionResult:
        from pywinauto.keyboard import send_keys
        need = self.cfg.idle_required_seconds
        if winutil.idle_seconds() < need:
            return ActionResult(deferred=True, error=f"사용자 입력이 {need:.0f}초 이내에 있었음 - 키보드 전송 보류")
        edit, _ = self._find_input_and_button(win)
        if edit is None:
            return ActionResult(error="입력창을 찾지 못함")
        prev = winutil.foreground_hwnd()
        was_min = winutil.is_minimized(self._hwnd)
        try:
            if not winutil.force_foreground(self._hwnd):
                return ActionResult(error="Codex 창을 앞으로 가져오지 못함 - 입력하지 않음")
            try:
                edit.set_focus()
            except Exception:
                pass
            time.sleep(0.3)
            if winutil.idle_seconds() < 1.0:      # 포커스 맞추는 사이 사용자가 입력함
                return ActionResult(deferred=True, error="사용자 입력 감지 - 중단")
            if winutil.foreground_hwnd() != self._hwnd or winutil.focused_root_hwnd() != self._hwnd:
                return ActionResult(error="포커스가 Codex 입력창이 아님 - 입력하지 않음")
            send_keys("^a", pause=0.05)
            send_keys(_escape_keys(text), pause=0.05, with_spaces=True)
            time.sleep(0.3)
            if winutil.foreground_hwnd() != self._hwnd:
                return ActionResult(ok=False, error="입력 직후 포커스가 바뀜 - Enter 전송하지 않음")
            if self._edit_value(edit.element_info) != text:
                return ActionResult(ok=False, error="입력창 내용이 예상과 다름 - Enter 전송하지 않음")
            send_keys("{ENTER}", pause=0.05)
            return ActionResult(ok=True, method="keyboard")
        except Exception as e:
            return ActionResult(error=f"키보드 전송 오류: {e}")
        finally:
            winutil.restore_foreground(prev, was_min, self._hwnd)


def _escape_keys(text: str) -> str:
    return "".join("{%s}" % c if c in "{}()+^%~" else c for c in text)
