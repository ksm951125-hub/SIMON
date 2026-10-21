"""Windows 전용 저수준 유틸 (ctypes). 비Windows 에서는 안전한 기본값을 돌려준다."""
from __future__ import annotations

import sys
import time

IS_WINDOWS = sys.platform == "win32"

SW_SHOWNOACTIVATE = 4
SW_RESTORE = 9
GA_ROOT = 2


def _u32():
    import ctypes
    return ctypes.windll.user32


def input_desktop_available() -> bool:
    """잠금 화면/보안 데스크톱이면 False (이때는 UI 조작 불가)."""
    if not IS_WINDOWS:
        return True
    try:
        u = _u32()
        h = u.OpenInputDesktop(0, False, 0x0100)   # DESKTOP_SWITCHDESKTOP
        if not h:
            return False
        u.CloseDesktop(h)
        return True
    except Exception:
        return True


def idle_seconds() -> float:
    """마지막 키보드/마우스 입력 이후 경과 시간."""
    if not IS_WINDOWS:
        return 1e9
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _u32().GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0


def foreground_hwnd() -> int:
    return int(_u32().GetForegroundWindow() or 0) if IS_WINDOWS else 0


def focused_root_hwnd() -> int:
    """키 입력을 받을 창(포커스 hwnd)의 최상위 부모."""
    if not IS_WINDOWS:
        return 0
    import ctypes
    from ctypes import wintypes

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]

    u = _u32()
    gi = GUITHREADINFO()
    gi.cbSize = ctypes.sizeof(gi)
    fg = u.GetForegroundWindow()
    if not fg:
        return 0
    tid = u.GetWindowThreadProcessId(fg, None)
    if not u.GetGUIThreadInfo(tid, ctypes.byref(gi)):
        return int(fg)
    h = gi.hwndFocus or fg
    return int(u.GetAncestor(h, GA_ROOT) or h)


def is_minimized(hwnd: int) -> bool:
    return bool(IS_WINDOWS and _u32().IsIconic(hwnd))


def restore_no_activate(hwnd: int) -> bool:
    """최소화된 창을 포커스를 가져오지 않고 복원."""
    if not IS_WINDOWS:
        return False
    u = _u32()
    u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    time.sleep(0.4)
    return not u.IsIconic(hwnd)


def force_foreground(hwnd: int) -> bool:
    """대상 창을 앞으로. 성공 여부를 반환(실제 foreground 가 hwnd 인지 확인)."""
    if not IS_WINDOWS:
        return False
    import ctypes
    u = _u32()
    k = ctypes.windll.kernel32
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_RESTORE)
    cur_tid = k.GetCurrentThreadId()
    fg = u.GetForegroundWindow()
    fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached = bool(u.AttachThreadInput(cur_tid, fg_tid, True))
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        if attached:
            u.AttachThreadInput(cur_tid, fg_tid, False)
    for _ in range(10):
        if u.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.1)
    return False


def restore_foreground(prev_hwnd: int, was_codex_minimized: bool, codex_hwnd: int) -> None:
    """키보드 방식으로 앞으로 가져왔던 창을 원래 상태로 되돌린다(best-effort)."""
    if not IS_WINDOWS:
        return
    u = _u32()
    try:
        if prev_hwnd and prev_hwnd != codex_hwnd and u.IsWindow(prev_hwnd):
            force_foreground(prev_hwnd)
        if was_codex_minimized:
            u.ShowWindow(codex_hwnd, 6)     # SW_MINIMIZE
    except Exception:
        pass
