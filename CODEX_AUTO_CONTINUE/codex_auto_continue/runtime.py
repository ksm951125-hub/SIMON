"""설정/백엔드/엔진 조립. 앱 프로필(GPT Codex, Claude Code ...)마다 엔진 1개를 만든다."""
from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .backend_thread import ThreadedBackend, new_uia_executor
from .config import Profile, Settings, load_settings, default_base, default_profile_specs, build_profile
from .engine import Engine
from .logsetup import RingHandler, setup_logging
from .models import CodexBackend
from .state import StateStore

log = logging.getLogger("cac.runtime")


@dataclass
class Unit:
    profile: Profile
    backend: CodexBackend
    store: StateStore
    engine: Engine
    thread: Optional[threading.Thread] = None


@dataclass
class Runtime:
    home: Path
    settings: Settings
    units: List[Unit]
    ring: RingHandler
    test_mode: bool = False
    dry_run: bool = False

    @property
    def cfg(self):          # 전역 설정 화면용: 첫 프로필의 설정을 대표로 사용
        return self.units[0].profile.cfg

    def start(self) -> None:
        for u in self.units:
            if u.thread is None:
                u.thread = threading.Thread(target=u.engine.run_forever, name=f"engine-{u.profile.name}",
                                            daemon=True)
                u.thread.start()

    def stop(self) -> None:
        for u in self.units:
            u.engine.stop()

    def pause_all(self) -> None:
        for u in self.units:
            u.engine.pause()

    def resume_all(self) -> None:
        for u in self.units:
            u.engine.resume()

    def unit(self, name: str) -> Optional[Unit]:
        return next((u for u in self.units if u.profile.name == name), None)


def _tune_for_test(cfg, delay_seconds: float) -> None:
    cfg.resume_delay_minutes = delay_seconds / 60.0
    cfg.poll_interval_seconds = 2
    cfg.waiting_poll_seconds = 5
    cfg.persist_recheck_seconds = 10
    cfg.retry_seconds = 10
    cfg.wake_grace_seconds = 0
    cfg.verify_delay_seconds = 1
    cfg.min_send_interval_minutes = 0.1
    cfg.check_network = False


def build_runtime(*, home: Path, dry_run: bool = False, test_mode: bool = False,
                  scenario: str = "normal", test_delay_seconds: float = 20.0,
                  test_reset_minutes: float = 1.0, console_log: bool = False,
                  debug: bool = False, only: Optional[List[str]] = None) -> Runtime:
    if test_mode:
        home = Path(home) / "test_mode"          # 실제 상태/설정과 분리
        home.mkdir(parents=True, exist_ok=True)
    ring = setup_logging(home, console=console_log, debug=debug)

    if test_mode:
        base = default_base()
        settings = Settings(base, [build_profile(sp, base) for sp in default_profile_specs()])
    else:
        settings = load_settings(home)
    profiles = [p for p in settings.profiles if p.enabled and (not only or p.name in only)]
    if not profiles:
        raise RuntimeError("활성화된 앱 프로필이 없습니다. data\\config.json 의 profiles 를 확인하세요.")

    executor = None if test_mode else new_uia_executor()
    locks: Dict[str, threading.Lock] = {}
    units: List[Unit] = []
    for i, prof in enumerate(profiles):
        cfg = prof.cfg
        if dry_run:
            cfg.dry_run = True
        if test_mode:
            from .simulator import SimulatedCodex
            _tune_for_test(cfg, test_delay_seconds)
            backend: CodexBackend = SimulatedCodex(
                scenario=scenario, reset_in_minutes=test_reset_minutes + i, work_seconds=30,
                style=prof.app, conversation=cfg.target.conversation, project=cfg.target.project)
        else:
            if sys.platform != "win32":
                raise RuntimeError("실제 자동화는 Windows 에서만 동작합니다. 다른 OS 에서는 --test-mode 를 사용하세요.")
            from .backend_uia import UiaBackend
            backend = ThreadedBackend(UiaBackend(cfg), executor=executor)
        store = StateStore(home / f"state_{prof.name}.json")
        if store.load_error:
            log.error("[%s] %s", prof.name, store.load_error)
        lock = locks.setdefault(prof.lock_key, threading.Lock())
        engine = Engine(cfg, backend, store, name=prof.name, action_lock=lock)
        units.append(Unit(prof, backend, store, engine))
        log.info("프로필 [%s] %s → 대상: %s", prof.name, prof.label, cfg.target.label)
    if test_mode:
        log.info("TEST MODE: 가상 앱 사용 (실제 Codex/Claude 에는 아무것도 전송하지 않음), 시나리오=%s", scenario)
    return Runtime(home, settings, units, ring, test_mode, dry_run)
