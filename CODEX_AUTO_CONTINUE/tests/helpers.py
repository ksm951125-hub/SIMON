import atexit, shutil, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codex_auto_continue.backend_fake import FakeBackend
from codex_auto_continue.config import Config
from codex_auto_continue.engine import Engine
from codex_auto_continue.models import Target
from codex_auto_continue.state import StateStore
from codex_auto_continue.timeparse import KST

BANNER = "Codex 및 Work 사용량을 모두 사용했습니다. 요청 한도는 {t}에 재설정됩니다."


def dt(h, m=0, s=0, day=21, month=10):
    return datetime(2026, month, day, h, m, s, tzinfo=KST)


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)

    def sleep(self, seconds):
        self.advance(seconds)

    def set(self, t):
        self.t = t


def make_cfg(**kw):
    cfg = Config()
    cfg.target = Target("MY AI OFFICE 구축하기", "MY AI OFFICE 구축하기")
    cfg.wake_grace_seconds = 0
    cfg.check_network = True
    cfg.verify_delay_seconds = 1
    cfg.min_send_interval_minutes = 10
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class Rig:
    """엔진 + 가짜 백엔드 + 가상 시계 + 임시 상태파일."""

    def __init__(self, start=None, state_path=None, cfg=None, backend=None, net=True):
        self.tmp = tempfile.mkdtemp(prefix="cac-test-")
        atexit.register(shutil.rmtree, self.tmp, True)
        self.path = Path(state_path or Path(self.tmp) / "state.json")
        self.clock = Clock(start or dt(11, 0))
        self.cfg = cfg or make_cfg()
        self.be = backend or FakeBackend()
        self.net = net
        self.store = StateStore(self.path)
        self.engine = self.new_engine()

    def new_engine(self):
        self.engine = Engine(self.cfg, self.be, self.store, clock=self.clock,
                             sleep=self.clock.sleep, net_check=lambda: self.net)
        return self.engine

    def restart(self):
        """프로그램 재시작: 같은 상태파일을 새 StateStore/Engine 이 읽는다."""
        self.store = StateStore(self.path)
        return self.new_engine()

    def tick(self, advance=0):
        if advance:
            self.clock.advance(advance)
        self.engine.tick()
        return self.engine.get_status()

    def run_until(self, t, step=15):
        while self.clock.t < t:
            self.clock.advance(min(step, (t - self.clock.t).total_seconds()))
            self.engine.tick()

    def limit(self, clock_text="오전 11:35"):
        self.be.limit_text = BANNER.format(t=clock_text)
