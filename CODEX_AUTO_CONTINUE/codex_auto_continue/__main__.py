"""CLI 진입점:  python -m codex_auto_continue [gui|run|once|inspect|set-target|autostart|history|reset] ..."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import APP_NAME, __version__, autostart
from .config import app_home, load_config, save_config
from .fmt import fmt_clock, fmt_remaining
from .single_instance import InstanceLock
from .timeparse import now_kst


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codex_auto_continue", description=APP_NAME)
    p.add_argument("command", nargs="?", default="gui",
                   choices=["gui", "run", "once", "inspect", "set-target", "autostart", "history", "reset"])
    p.add_argument("--dry-run", action="store_true", help="조건을 모두 확인하되 실제로는 전송하지 않음")
    p.add_argument("--test-mode", action="store_true", help="가상 Codex 로 전체 주기를 시뮬레이션")
    p.add_argument("--scenario", default="normal", choices=["normal", "wrong-conversation", "user-typing"])
    p.add_argument("--test-delay-seconds", type=float, default=20.0, help="테스트 모드의 재개 지연(초)")
    p.add_argument("--test-reset-minutes", type=float, default=1.0, help="테스트 모드: 가상 제한이 풀릴 때까지(분)")
    p.add_argument("--home", help="데이터 폴더 (기본: 프로젝트의 data)")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--project")
    p.add_argument("--conversation")
    p.add_argument("--on", action="store_true")
    p.add_argument("--off", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p


def _print_status(rt) -> None:
    st = rt.engine.get_status()
    now = now_kst()
    print(f"[{fmt_clock(now, seconds=True)}] 프로그램={st.program} Codex={st.codex} 대상={st.target} "
          f"초기화={fmt_clock(st.reset_at, now) if st.reset_at else '-'} "
          f"재개={fmt_clock(st.resume_at, now) if st.resume_at else '-'} "
          f"남은={fmt_remaining(st.resume_at - now) if st.resume_at else '-'} "
          f"마지막={st.last_result} | {st.message}")


def main(argv=None) -> int:
    a = _parser().parse_args(argv)
    home = Path(a.home) if a.home else app_home()
    home.mkdir(parents=True, exist_ok=True)

    if a.command == "set-target":
        if not a.conversation:
            print("--conversation 이 필요합니다 (--project 선택)")
            return 2
        cfg = load_config(home)
        cfg.target.project = a.project if a.project is not None else cfg.target.project
        cfg.target.conversation = a.conversation
        save_config(cfg, home)
        print("감시 대상:", cfg.target.label)
        return 0
    if a.command == "autostart":
        if a.on == a.off:
            print("자동 실행:", "ON" if autostart.is_enabled() else "OFF", " (--on / --off 로 변경)")
            return 0
        autostart.set_enabled(a.on)
        print("자동 실행:", "ON" if autostart.is_enabled() else "OFF")
        return 0
    if a.command == "history":
        from .state import StateStore
        for h in StateStore(home / "state.json").history[-30:]:
            print(json.dumps(h, ensure_ascii=False))
        return 0
    if a.command == "reset":
        if not a.yes:
            print("전송 이력/진행 상태를 지웁니다. 계속하려면 --yes 를 붙이세요.")
            return 2
        (home / "state.json").unlink(missing_ok=True)
        print("상태 초기화 완료")
        return 0
    if a.command == "inspect":
        from .inspect_tool import run_inspect
        return run_inspect(load_config(home), home)

    from .runtime import build_runtime
    if not a.test_mode:
        lock = InstanceLock(home / "instance.lock")
        if not lock.acquire():
            msg = f"{APP_NAME} 이(가) 이미 실행 중입니다. (중복 실행 시 '계속'이 중복 전송될 수 있어 종료합니다)"
            print(msg)
            if a.command == "gui" and sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(0, msg, APP_NAME, 0x40)
                except Exception:
                    pass
            return 3
    try:
        rt = build_runtime(home=home, dry_run=a.dry_run, test_mode=a.test_mode, scenario=a.scenario,
                           test_delay_seconds=a.test_delay_seconds, test_reset_minutes=a.test_reset_minutes,
                           console_log=a.command in ("run", "once"), debug=a.debug)
    except RuntimeError as e:
        print(e)
        return 2
    logging.getLogger("cac").info("%s %s 시작 (대상=%s, dry_run=%s, test_mode=%s)", APP_NAME, __version__,
                                  rt.cfg.target.label, rt.cfg.dry_run, rt.test_mode)

    if a.command == "once":
        rt.engine.tick()
        _print_status(rt)
        return 0
    if a.command == "run":
        import threading
        t = threading.Thread(target=rt.engine.run_forever, daemon=True)
        t.start()
        try:
            while t.is_alive():
                time.sleep(10)
                _print_status(rt)
        except KeyboardInterrupt:
            rt.engine.stop()
            print("종료합니다.")
        return 0

    from .ui import App
    App(rt).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
