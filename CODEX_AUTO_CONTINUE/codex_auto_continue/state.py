"""재시작 후에도 유지되는 상태(진행 중 주기, 전송 이력, 일시정지 여부)."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

HISTORY_LIMIT = 300
OK_STATUSES = ("sent", "sent_unconfirmed")


def _dt(v: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(v) if v else None


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() if d else None


@dataclass
class Cycle:
    """사용량 제한 1회(= 초기화 시각 1개)에 대응하는 재개 작업."""
    key: str
    target_key: str
    kind: str                      # absolute | relative | unknown
    raw: str
    reset_at: Optional[datetime]
    resume_at: Optional[datetime]
    detected_at: datetime
    status: str = "waiting"        # waiting | sending
    attempts: int = 0
    next_check_at: Optional[datetime] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("reset_at", "resume_at", "detected_at", "next_check_at"):
            d[k] = _iso(d[k])
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Cycle":
        d = dict(d)
        for k in ("reset_at", "resume_at", "detected_at", "next_check_at"):
            d[k] = _dt(d.get(k))
        return cls(**d)


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._paused = False
        self._cycle: Optional[Cycle] = None
        self._history: List[Dict[str, Any]] = []
        self._last_send: Optional[Dict[str, Any]] = None
        self.load_error = ""
        self._load()

    # ---- 읽기 ----
    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def cycle(self) -> Optional[Cycle]:
        return self._cycle

    @property
    def last_send(self) -> Optional[Dict[str, Any]]:
        return self._last_send

    @property
    def history(self) -> List[Dict[str, Any]]:
        return list(self._history)

    def is_done(self, key: str, dry_run: bool) -> bool:
        """이 초기화 주기에 대해 이미 처리했는가. 실전송 모드에서는 dry-run 이력을 무시."""
        return any(h["cycle_key"] == key and (dry_run or not h.get("dry_run"))
                   for h in self._history)

    def last_real_send_at(self) -> Optional[datetime]:
        for h in reversed(self._history):
            if h["status"] in OK_STATUSES and not h.get("dry_run"):
                return _dt(h["finished_at"])
        return None

    # ---- 쓰기 ----
    def set_paused(self, v: bool) -> None:
        with self._lock:
            self._paused = bool(v)
            self.save()

    def set_cycle(self, c: Optional[Cycle]) -> None:
        with self._lock:
            self._cycle = c
            self.save()

    def touch(self) -> None:
        with self._lock:
            self.save()

    def finish_cycle(self, status: str, now: datetime, *, dry_run: bool = False,
                     method: str = "", note: str = "") -> None:
        """status: sent | sent_unconfirmed | failed | cancelled | dry_run"""
        with self._lock:
            c = self._cycle
            if c is None:
                return
            entry = {
                "cycle_key": c.key, "target_key": c.target_key, "status": status,
                "reset_at": _iso(c.reset_at), "resume_at": _iso(c.resume_at),
                "finished_at": now.isoformat(), "dry_run": bool(dry_run),
                "method": method, "note": note, "attempts": c.attempts,
            }
            self._history.append(entry)
            self._history = self._history[-HISTORY_LIMIT:]
            if status in OK_STATUSES or status == "dry_run":
                self._last_send = entry
            self._cycle = None
            self.save()

    # ---- 영속화 ----
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self._paused = bool(d.get("paused", False))
            self._history = list(d.get("history", []))[-HISTORY_LIMIT:]
            self._last_send = d.get("last_send")
            c = d.get("cycle")
            self._cycle = Cycle.from_dict(c) if c else None
        except (OSError, ValueError, TypeError, KeyError) as e:
            self.load_error = f"상태 파일 손상: {e}"
            try:
                self.path.replace(self.path.with_suffix(".bad"))
            except OSError:
                pass
            self._paused, self._cycle, self._history, self._last_send = False, None, [], None

    def save(self) -> None:
        with self._lock:
            data = {
                "version": 1, "paused": self._paused,
                "cycle": self._cycle.to_dict() if self._cycle else None,
                "history": self._history, "last_send": self._last_send,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
