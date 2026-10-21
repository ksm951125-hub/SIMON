"""Windows 로그인 시 자동 실행 (HKCU\\...\\Run). 관리자 권한 불필요."""
from __future__ import annotations

import sys
from pathlib import Path

from .config import project_dir

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "CODEX_AUTO_CONTINUE"


def _command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" gui'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    py = pyw if pyw.exists() else exe
    return f'"{py}" "{project_dir() / "run_gui.pyw"}"'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE)
            return True
    except OSError:
        return False


def set_enabled(on: bool) -> None:
    if sys.platform != "win32":
        raise RuntimeError("자동 실행 등록은 Windows 에서만 지원됩니다.")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(k, VALUE)
            except FileNotFoundError:
                pass
