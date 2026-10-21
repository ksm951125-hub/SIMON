"""설정/백엔드/엔진 조립."""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from .backend_thread import ThreadedBackend
from .config import Config, load_config
from .engine import Engine
from .logsetup import RingHandler, setup_logging
from .models import CodexBackend
from .state import StateStore

log = logging.getLogger("cac.runtime")


@dataclass
class Runtime:
    home: Path
    cfg: Config
    backend: CodexBackend
    store: StateStore
    engine: Engine
    ring: RingHandler
    test_mode: bool = False


def build_runtime(*, home: Path, dry_run: bool = False, test_mode: bool = False,
                  scenario: str = "normal", test_delay_seconds: float = 20.0, test_reset_minutes: float = 1.0,
                  console_log: bool = False, debug: bool = False) -> Runtime:
    if test_mode:
        home = Path(home) / "test_mode"          # 실제 상태/설정과 분리
        home.mkdir(parents=True, exist_ok=True)
    ring = setup_logging(home, console=console_log, debug=debug)
    cfg = Config() if test_mode else load_config(home)
    if dry_run:
        cfg.dry_run = True
    if test_mode:
        from .simulator import SimulatedCodex
        cfg.resume_delay_minutes = test_delay_seconds / 60.0
        cfg.poll_interval_seconds = 2
        cfg.waiting_poll_seconds = 5
        cfg.persist_recheck_seconds = 10
        cfg.retry_seconds = 10
        cfg.wake_grace_seconds = 0
        cfg.verify_delay_seconds = 1
        cfg.min_send_interval_minutes = 0.1
        cfg.check_network = False
        backend: CodexBackend = SimulatedCodex(scenario=scenario, reset_in_minutes=test_reset_minutes, work_seconds=30)
        log.info("TEST MODE: 가상 Codex 사용 (실제 Codex 에는 아무것도 전송하지 않음), 시나리오=%s", scenario)
    else:
        if sys.platform != "win32":
            raise RuntimeError("실제 Codex 자동화는 Windows 에서만 동작합니다. 다른 OS 에서는 --test-mode 를 사용하세요.")
        from .backend_uia import UiaBackend
        backend = ThreadedBackend(UiaBackend(cfg))
    store = StateStore(home / "state.json")
    if store.load_error:
        log.error(store.load_error)
    engine = Engine(cfg, backend, store)
    return Runtime(home, cfg, backend, store, engine, ring, test_mode)
