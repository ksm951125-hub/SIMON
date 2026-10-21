"""설정 로드/저장. 모든 데이터는 프로젝트의 data 폴더(또는 CAC_HOME)에 저장한다."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any, Dict, List

from .models import Target


def project_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_home() -> Path:
    p = Path(os.environ.get("CAC_HOME") or (project_dir() / "data"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_selectors() -> Dict[str, Any]:
    # Codex 앱 UI 문구/구조는 inspect 명령으로 확인 후 config.json 의 selectors 로 보정할 수 있다.
    return {
        "send_button_names": ["전송", "보내기", "제출", "Send", "Submit"],
        "stop_button_names": ["중지", "중단", "작업 중지", "Stop", "Interrupt"],
        "busy_text_regex": r"(작업 중|생각 중|실행 중|Working|Thinking|Running)",
        "input_placeholders": [],
        "banner_zone_px": 340,        # 창 하단 이 높이 안의 텍스트를 '안내문 영역'으로 본다
        "header_zone_px": 150,        # 창 상단 이 높이 안의 텍스트를 '대화 제목 영역'으로 본다
        "sidebar_ratio": 0.30,        # 창 왼쪽 이 비율까지를 사이드바로 본다
        "max_elements": 4000,
        "scan_seconds": 8.0,
    }


@dataclass
class Config:
    target: Target = field(default_factory=lambda: Target("MY AI OFFICE 구축하기", "MY AI OFFICE 구축하기"))
    send_message: str = "계속"
    resume_delay_minutes: float = 2.0         # 초기화 시각 + N분 뒤 재개
    poll_interval_seconds: float = 15.0       # 평상시 감시 주기
    waiting_poll_seconds: float = 30.0        # 재개 시각 대기 중 점검 주기
    persist_recheck_seconds: float = 120.0    # 초기화 후에도 제한이 유지될 때 재확인 간격
    retry_seconds: float = 60.0
    max_attempts: int = 5
    verify_delay_seconds: float = 5.0
    wake_grace_seconds: float = 20.0          # 절전 복귀/프로그램 시작 직후 대기
    wake_gap_seconds: float = 120.0           # 이 이상 틱 간격이 벌어지면 절전 복귀로 판단
    stale_after_hours: float = 12.0           # 재개 시각 후 이 시간이 지나면 이전 주기 폐기
    min_send_interval_minutes: float = 10.0   # 실전송 사이 최소 간격(폭주 방지)
    past_grace_minutes: float = 180.0
    dry_run: bool = False
    send_method: str = "auto"                 # auto | uia | keyboard
    idle_required_seconds: float = 20.0       # 키보드/포커스 조작 허용: 사용자 입력 없는 시간
    require_project_match: bool = True
    match_mode: str = "exact"                 # exact | contains
    process_names: List[str] = field(default_factory=lambda: ["codex"])
    window_title_regex: str = ""
    tail_fragments: int = 120
    banner_max_len: int = 400
    extra_limit_patterns: List[str] = field(default_factory=list)
    completion_markers: List[str] = field(default_factory=list)
    check_network: bool = True
    network_hosts: List[str] = field(default_factory=lambda: ["chatgpt.com:443", "1.1.1.1:443"])
    hide_to_tray_on_close: bool = True
    selectors: Dict[str, Any] = field(default_factory=default_selectors)

    # ---- 직렬화 ----
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["target"] = asdict(self.target)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Config":
        cfg = cls()
        for f in fields(cls):
            if f.name not in d:
                continue
            v = d[f.name]
            cur = getattr(cfg, f.name)
            if f.name == "target" and isinstance(v, dict):
                cfg.target = Target(str(v.get("project", "")), str(v.get("conversation", "")),
                                    bool(v.get("enabled", True)))
            elif f.name == "selectors" and isinstance(v, dict):
                merged = default_selectors()
                merged.update(v)
                cfg.selectors = merged
            elif isinstance(cur, bool) and isinstance(v, bool):
                setattr(cfg, f.name, v)
            elif isinstance(cur, (int, float)) and not isinstance(cur, bool) \
                    and isinstance(v, (int, float)) and not isinstance(v, bool):
                setattr(cfg, f.name, type(cur)(v))
            elif isinstance(cur, str) and isinstance(v, str):
                setattr(cfg, f.name, v)
            elif isinstance(cur, list) and isinstance(v, list):
                setattr(cfg, f.name, v)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        self.resume_delay_minutes = max(0.0, self.resume_delay_minutes)
        self.poll_interval_seconds = max(1.0, self.poll_interval_seconds)
        self.waiting_poll_seconds = max(1.0, self.waiting_poll_seconds)
        self.persist_recheck_seconds = max(5.0, self.persist_recheck_seconds)
        self.max_attempts = max(1, self.max_attempts)
        if self.send_method not in ("auto", "uia", "keyboard"):
            self.send_method = "auto"
        if self.match_mode not in ("exact", "contains"):
            self.match_mode = "exact"
        if not self.send_message.strip():
            self.send_message = "계속"


def config_path(home: Path = None) -> Path:
    return (home or app_home()) / "config.json"


def load_config(home: Path = None) -> Config:
    p = config_path(home)
    if p.exists():
        try:
            return Config.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            try:
                p.replace(p.with_suffix(".bad"))
            except OSError:
                pass
    cfg = Config()
    save_config(cfg, home)
    return cfg


def save_config(cfg: Config, home: Path = None) -> None:
    p = config_path(home)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
