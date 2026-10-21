"""자동 재개 엔진: 제한 감지 → 초기화 시각 파악 → +N분 대기 → 안전 확인 → '계속' 전송 → 반복."""
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable, Optional

from .config import Config
from .detector import LimitDetection, detect_limit
from .fmt import fmt_clock, fmt_remaining
from .identify import identify
from .models import CodexBackend, Snapshot, Target
from .state import Cycle, StateStore
from .timeparse import now_kst

log = logging.getLogger("cac.engine")

RESET_JITTER = timedelta(seconds=90)


@dataclass
class EngineStatus:
    program: str = "중지"            # 실행 중 | 일시정지 | 중지
    automation_on: bool = False
    codex: str = "확인 중"           # 연결 안 됨 | 최소화 | 정상 | 사용량 제한 | UI 사용 불가
    limit_detected: bool = False
    target: str = ""
    reset_at: Optional[datetime] = None
    resume_at: Optional[datetime] = None
    last_result: str = "-"
    last_send_at: Optional[datetime] = None
    message: str = ""
    last_error: str = ""
    dry_run: bool = False
    updated_at: Optional[datetime] = None


def check_network(hosts, timeout: float = 3.0) -> bool:
    for h in hosts:
        try:
            host, _, port = h.partition(":")
            with socket.create_connection((host, int(port or 443)), timeout=timeout):
                return True
        except (OSError, ValueError):
            continue
    return False


def count_exact(texts, message: str) -> int:
    return sum(1 for t in texts if t.strip() == message)


def verify_sent(before: Snapshot, after: Snapshot, message: str) -> Optional[bool]:
    """True=전송 확인, False=제출되지 않음(입력창에 남음), None=확인 불가."""
    typed = after.input_text.strip()
    if typed == message:
        return False
    if typed:
        return None
    if after.busy or count_exact(after.texts, message) > count_exact(before.texts, message):
        return True
    return None


class Engine:
    def __init__(self, cfg: Config, backend: CodexBackend, store: StateStore, *,
                 clock: Callable[[], datetime] = now_kst,
                 sleep: Callable[[float], None] = time.sleep,
                 net_check: Optional[Callable[[], bool]] = None):
        self.cfg = cfg
        self.backend = backend
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self.net_check = net_check or (lambda: check_network(self.cfg.network_hosts))
        self._stop = threading.Event()
        self._slock = threading.Lock()
        self._prev_tick: Optional[datetime] = None
        self._grace_until: Optional[datetime] = None
        self.status = EngineStatus(target=cfg.target.label, dry_run=cfg.dry_run)
        self._sync_last_send()

        c = store.cycle
        if c is not None and c.status == "sending":
            # 전송 도중 비정상 종료: 제출됐는지 알 수 없으므로 중복 방지를 위해 재전송하지 않는다.
            log.warning("이전 실행이 전송 중 종료됨 → 중복 방지를 위해 재전송하지 않고 종결 처리")
            store.finish_cycle("sent_unconfirmed", self.clock(), dry_run=cfg.dry_run,
                               note="전송 중 프로그램 종료 - 재전송 안 함")
            self._sync_last_send()
        elif c is not None:
            log.info("이전 실행의 진행 중 주기를 복원: 재개 예정 %s", fmt_clock(c.resume_at))

    # ------------------------------------------------------------------ 제어
    def pause(self) -> None:
        self.store.set_paused(True)
        self._set(program="일시정지", automation_on=False, message="자동화 일시정지됨")
        log.info("자동화 일시정지")

    def resume(self) -> None:
        self.store.set_paused(False)
        self._set(program="실행 중", automation_on=True, message="자동화 재개")
        log.info("자동화 재개")

    def stop(self) -> None:
        self._stop.set()

    def get_status(self) -> EngineStatus:
        with self._slock:
            return replace(self.status)

    def _set(self, **kw) -> None:
        with self._slock:
            for k, v in kw.items():
                setattr(self.status, k, v)
            self.status.updated_at = self.clock()
            self.status.target = self.cfg.target.label
            self.status.dry_run = self.cfg.dry_run

    def _sync_last_send(self) -> None:
        ls = self.store.last_send
        if not ls:
            return
        label = {"sent": "정상 전송", "sent_unconfirmed": "전송됨(확인 불가)",
                 "dry_run": "Dry Run(전송 안 함)"}.get(ls["status"], ls["status"])
        self._set(last_result=label, last_send_at=datetime.fromisoformat(ls["finished_at"]))

    # ------------------------------------------------------------------ 루프
    def run_forever(self) -> None:
        self._set(program="일시정지" if self.store.paused else "실행 중",
                  automation_on=not self.store.paused)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:      # 감시 루프는 어떤 오류에도 죽지 않는다
                log.exception("틱 처리 중 예외")
                self._set(last_error=f"내부 오류: {e}")
            self._stop.wait(self.next_delay())
        self._set(program="중지", automation_on=False, message="중지됨")

    def next_delay(self) -> float:
        c = self.store.cycle
        if c is None or c.resume_at is None:
            return self.cfg.poll_interval_seconds
        wait = (c.resume_at - self.clock()).total_seconds()
        if wait <= 0:
            nxt = c.next_check_at
            gap = (nxt - self.clock()).total_seconds() if nxt else 0
            return max(1.0, min(gap if gap > 0 else self.cfg.poll_interval_seconds,
                                self.cfg.persist_recheck_seconds))
        if wait <= self.cfg.waiting_poll_seconds + 5:
            return max(1.0, wait)         # 재개 시각에 맞춰 정확히 깨어난다
        return self.cfg.waiting_poll_seconds

    # ------------------------------------------------------------------ 1회 처리
    def tick(self) -> None:
        cfg = self.cfg
        now = self.clock()
        self._detect_wake(now)

        if self.store.paused:
            self._set(program="일시정지", automation_on=False, message="자동화 일시정지됨")
            return
        self._set(program="실행 중", automation_on=True)

        c = self.store.cycle
        if c and c.resume_at and now - c.resume_at > timedelta(hours=cfg.stale_after_hours):
            self._finish("cancelled", now, note="오래된 주기 폐기")
            log.warning("재개 예정 시각이 %.0f시간 이상 지난 주기를 폐기", cfg.stale_after_hours)

        snap = self.backend.snapshot()
        if not snap.ui_available:
            self._set(codex="UI 사용 불가", message="잠금 화면/절전 등으로 UI 조작 불가 - 복귀 후 재확인")
            return
        if not snap.found:
            self._set(codex="연결 안 됨", limit_detected=self.store.cycle is not None,
                      message="Codex 창을 찾을 수 없음 - 실행되면 다시 확인" +
                              (f" ({snap.error})" if snap.error else ""))
            return
        self._set(codex="최소화" if snap.minimized else "정상")

        det = self._detect(snap, now)
        if det:
            self._on_limit_seen(det, now)
        else:
            self._on_no_limit(snap, now)

        c = self.store.cycle
        self._publish_cycle(det, snap)
        if c is None or c.resume_at is None:
            return
        due = max(c.resume_at, c.next_check_at or c.resume_at)
        if now < due:
            return
        if self._grace_until and now < self._grace_until:
            self._set(message="절전 복귀/시작 직후 안정화 대기")
            return
        self._attempt(c, snap, det, now)
        self._publish_cycle(None, None)

    # ------------------------------------------------------------------ 감지
    def _detect(self, snap: Snapshot, now: datetime) -> Optional[LimitDetection]:
        frags = snap.banner_texts if snap.banner_texts is not None \
            else snap.texts[-self.cfg.tail_fragments:]
        return detect_limit(frags, now, past_grace=timedelta(minutes=self.cfg.past_grace_minutes),
                            max_len=self.cfg.banner_max_len,
                            extra_patterns=self.cfg.extra_limit_patterns)

    def _detect_wake(self, now: datetime) -> None:
        prev, self._prev_tick = self._prev_tick, now
        grace = timedelta(seconds=self.cfg.wake_grace_seconds)
        if prev is None:
            self._grace_until = now + grace
            return
        gap = (now - prev).total_seconds()
        if gap < 0:
            log.warning("시스템 시계가 %.0f초 뒤로 이동", -gap)
        elif gap > max(self.cfg.wake_gap_seconds, 4 * self.cfg.poll_interval_seconds):
            self._grace_until = now + grace
            log.info("절전/중단 복귀 감지 (틱 간격 %.0f초) → %s초 안정화 후 재확인", gap,
                     int(self.cfg.wake_grace_seconds))

    def _make_key(self, reset_at: Optional[datetime], now: datetime) -> str:
        t = self.cfg.target.key
        if reset_at is None:
            return f"{t}|unknown|{now:%Y-%m-%dT%H}"
        return f"{t}|{reset_at:%Y-%m-%dT%H:%M}"

    def _on_limit_seen(self, det: LimitDetection, now: datetime) -> None:
        cfg = self.cfg
        delay = timedelta(minutes=cfg.resume_delay_minutes)
        c = self.store.cycle
        info = det.reset
        if c is None:
            key = self._make_key(info.reset_at if info else None, now)
            if self.store.is_done(key, cfg.dry_run):
                return          # 이미 처리한 초기화 주기 (중복 전송 방지)
            if info:
                c = Cycle(key, cfg.target.key, info.kind, info.raw, info.reset_at,
                          info.reset_at + delay, now)
                log.info("사용량 제한 감지: 초기화 %s → 자동 재개 %s", fmt_clock(c.reset_at, now),
                         fmt_clock(c.resume_at, now))
            else:
                c = Cycle(key, cfg.target.key, "unknown", "", None, None, now)
                log.warning("사용량 제한 감지, 그러나 초기화 시각을 읽지 못함 → 제한 해제 확인 후 재개")
            self.store.set_cycle(c)
            return
        if info is None:
            return
        if c.kind == "unknown":
            c.kind, c.raw, c.reset_at, c.resume_at = info.kind, info.raw, info.reset_at, info.reset_at + delay
            c.key = self._make_key(c.reset_at, now)
            log.info("초기화 시각 확인: %s", fmt_clock(c.reset_at, now))
            self.store.touch()
            return
        # 같은 안내문이면 유지(초기화 시각이 지난 뒤 날짜 재계산으로 24시간 밀리는 것 방지)
        if info.kind == "absolute" and c.kind == "absolute" and info.raw == c.raw:
            return
        if abs(info.reset_at - c.reset_at) < RESET_JITTER:
            return
        log.info("초기화 시각 변경: %s → %s", fmt_clock(c.reset_at, now), fmt_clock(info.reset_at, now))
        c.kind, c.raw, c.reset_at = info.kind, info.raw, info.reset_at
        c.resume_at = info.reset_at + delay
        c.key = self._make_key(c.reset_at, now)
        c.next_check_at = None
        c.attempts = 0
        c.note = ""
        self.store.touch()

    def _on_no_limit(self, snap: Snapshot, now: datetime) -> None:
        c = self.store.cycle
        if c is None:
            return
        if c.kind == "unknown" and c.resume_at is None:
            c.reset_at = now
            c.resume_at = now + timedelta(minutes=self.cfg.resume_delay_minutes)
            log.info("제한 안내가 사라짐 → %s 에 재개 시도", fmt_clock(c.resume_at, now))
            self.store.touch()
        if snap.busy and now < (c.resume_at or now):
            self._finish("cancelled", now, note="사용자가 먼저 작업을 재개함")
            log.info("재개 시각 전에 Codex가 이미 작업 중 → 자동 전송 취소")

    # ------------------------------------------------------------------ 전송 시도
    def _attempt(self, c: Cycle, snap: Snapshot, det: Optional[LimitDetection], now: datetime) -> None:
        cfg = self.cfg
        target: Target = cfg.target

        def defer(reason: str, seconds: float, level: int = logging.INFO) -> None:
            if c.note != reason:
                log.log(level, "보류: %s", reason)
            c.note = reason
            c.next_check_at = now + timedelta(seconds=seconds)
            self.store.touch()
            self._set(message=f"보류 - {reason}")

        def fail_attempt(reason: str) -> None:
            c.attempts += 1
            log.error("재개 시도 실패(%d/%d): %s", c.attempts, cfg.max_attempts, reason)
            self._set(last_error=reason)
            if c.attempts >= cfg.max_attempts:
                self._finish("failed", now, note=reason)
                self._set(last_result=f"실패: {reason}", message="재시도 횟수 초과 - 이 주기는 포기")
                return
            c.status = "waiting"
            c.note = reason
            c.next_check_at = now + timedelta(seconds=cfg.retry_seconds)
            self.store.touch()

        if cfg.check_network and not self.net_check():
            return defer("인터넷 연결 없음", cfg.retry_seconds)

        if snap.minimized:
            log.info("Codex 창이 최소화됨 → 포커스를 가져오지 않고 복원")
            if not self.backend.restore_window():
                return defer("최소화된 창을 복원하지 못함", cfg.retry_seconds)
            self.sleep(1.0)
            snap = self.backend.snapshot()
            det = self._detect(snap, now)
        if not snap.ui_available:
            return defer("UI 조작 불가(잠금/절전)", cfg.retry_seconds)
        if not snap.found:
            return defer("Codex 창 없음", cfg.retry_seconds)

        if det is not None:
            if det.reset:
                self._on_limit_seen(det, now)
                if c.resume_at and c.resume_at > now:
                    return          # 초기화 시각이 뒤로 밀림 → 새 시각까지 대기
            return defer("초기화 시각이 지났지만 사용량 제한이 유지됨 - 재확인", cfg.persist_recheck_seconds)

        if not snap.texts and not snap.input_found:
            return fail_attempt("Codex UI 요소를 읽을 수 없음 (접근성 트리 비어 있음)")

        ident = identify(snap, target, require_project=cfg.require_project_match, mode=cfg.match_mode)
        if not ident.ok:
            sel = self.backend.select_conversation(target)
            if sel.deferred:
                return defer(sel.error or "사용자가 PC 사용 중 - 대화 전환 보류", cfg.retry_seconds)
            if sel.ok:
                self.sleep(1.5)
                snap = self.backend.snapshot()
                ident = identify(snap, target, require_project=cfg.require_project_match,
                                 mode=cfg.match_mode)
            if not ident.ok:
                return fail_attempt(f"대상 대화 식별 실패 - 전송하지 않음: {ident.reason}"
                                    + (f" / {sel.error}" if sel.error else ""))

        if snap.busy:
            self._finish("cancelled", now, note="대상 대화가 이미 작업 진행 중")
            log.info("대상 대화가 이미 작업 중 → 자동 전송 취소")
            return
        for mk in cfg.completion_markers:
            if mk and any(mk in t for t in snap.texts[-5:]):
                self._finish("cancelled", now, note=f"작업 완료 표시 감지: {mk}")
                log.info("작업 완료 표시('%s') 감지 → 전송 안 함", mk)
                return
        if not snap.input_found:
            return fail_attempt("메시지 입력창을 찾지 못함")
        draft = snap.input_text.strip()
        if draft and draft != cfg.send_message:
            return defer("입력창에 사용자 작성 내용이 있음 - 덮어쓰지 않음", cfg.retry_seconds)

        last = self.store.last_real_send_at()
        if not cfg.dry_run and last and now - last < timedelta(minutes=cfg.min_send_interval_minutes):
            return defer("직전 전송 후 최소 간격 미경과", cfg.retry_seconds)

        if cfg.dry_run:
            log.info("[DRY RUN] 대상 '%s' 에 '%s' 전송 조건 충족 - 실제 전송은 하지 않음",
                     target.label, cfg.send_message)
            self._finish("dry_run", now, dry_run=True, note="전송 안 함")
            self._set(last_result="Dry Run(전송 안 함)", last_send_at=now, message="Dry Run 완료")
            return

        c.status = "sending"
        self.store.touch()        # 전송 도중 종료돼도 재전송하지 않도록 먼저 기록
        res = self.backend.send_text(cfg.send_message, target)
        if res.deferred:
            c.status = "waiting"
            return defer(res.error or "사용자가 PC 사용 중 - 전송 보류", 30)
        if not res.ok:
            return fail_attempt(f"전송 실패: {res.error}")

        self.sleep(cfg.verify_delay_seconds)
        after = self.backend.snapshot()
        v = verify_sent(snap, after, cfg.send_message)
        if v is True:
            self._finish("sent", now, method=res.method)
            self._set(last_result="정상 전송", last_send_at=now, last_error="", message="'계속' 전송 완료")
            log.info("'%s' 전송 확인됨 (방식: %s)", cfg.send_message, res.method)
        elif v is False:
            fail_attempt("전송 후에도 입력창에 메시지가 남아 있음(제출 안 됨)")
        else:
            self._finish("sent_unconfirmed", now, method=res.method, note="전송 확인 불가")
            self._set(last_result="전송됨(확인 불가)", last_send_at=now,
                      message="전송했으나 결과를 확인하지 못함 - 중복 방지를 위해 재전송 안 함")
            log.warning("전송 후 결과를 확인하지 못함 - 중복 방지를 위해 재전송하지 않음")

    def _finish(self, status: str, now: datetime, **kw) -> None:
        kw.setdefault("dry_run", self.cfg.dry_run)
        self.store.finish_cycle(status, now, **kw)

    def _publish_cycle(self, det, snap) -> None:
        c = self.store.cycle
        if c is None:
            self._set(limit_detected=False, reset_at=None, resume_at=None)
            if not self.get_status().message.startswith(("'계속'", "Dry", "전송했으나", "재시도")):
                self._set(message="감시 중")
            return
        now = self.clock()
        self._set(limit_detected=True, reset_at=c.reset_at, resume_at=c.resume_at)
        if det is not None:
            self._set(codex="사용량 제한")
        if c.resume_at and now < c.resume_at:
            self._set(message=f"재개 대기 - {fmt_clock(c.resume_at, now)} "
                              f"(남은 시간 {fmt_remaining(c.resume_at - now)})")
        elif c.resume_at is None:
            self._set(message="제한 해제 확인 대기 (초기화 시각 미확인)")
