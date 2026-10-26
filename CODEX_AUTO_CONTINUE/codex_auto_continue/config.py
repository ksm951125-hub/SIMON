"""설정 로드/저장. 모든 데이터는 프로젝트의 data 폴더(또는 CAC_HOME)에 저장한다."""
from __future__ import annotations

import json
import os
import sys
import copy
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        "send_button_names": ["전송", "보내기", "메시지 보내기", "제출", "Send", "Send message", "Submit"],
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
    target: Target = field(default_factory=Target)
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


# ---------------------------------------------------------------------------
# 앱별 프로필 (GPT Codex, Claude Code ...). 각 프로필은 자기 대상 대화·상태·주기를 따로 가진다.
# 기본값은 스크린샷(Codex 사이드바 폭, 입력창 placeholder 등)을 반영한 것이며 inspect 로 보정 가능.
# ---------------------------------------------------------------------------
APP_PRESETS: Dict[str, Dict[str, Any]] = {
    "codex": {
        "process_names": ["codex"],
        "selectors": {"sidebar_ratio": 0.36, "input_placeholders": ["Codex와 함께 작업하세요"]},
    },
    "claude": {
        "process_names": ["claude"],
        "selectors": {"sidebar_ratio": 0.32, "input_placeholders": ["/ 입력 시 명령어"]},
    },
}

GLOBAL_EXCLUDE = ("target", "process_names", "window_title_regex", "selectors")


def default_profile_specs() -> List[Dict[str, Any]]:
    return [
        {"name": "codex", "label": "GPT Codex", "app": "codex", "enabled": True,
         "target": {"project": "AI STUDIO", "conversation": "점검 OCI 이전 준비"}},
        {"name": "claude", "label": "Claude Code", "app": "claude", "enabled": True,
         "target": {"project": "1_블로그포스팅", "conversation": "네이버 블로그 AI 포스팅 생성기 (fork)"}},
        # 같은 Codex 의 다른 대화도 쓰려면 enabled 를 true 로 (같은 앱은 순차 처리됨)
        {"name": "codex-myai", "label": "GPT Codex (MY AI OFFICE)", "app": "codex", "enabled": False,
         "target": {"project": "AI STUDIO", "conversation": "MY AI OFFICE 구축하기"}},
    ]


@dataclass
class Profile:
    name: str
    label: str
    app: str
    enabled: bool
    cfg: Config
    spec: Dict[str, Any]

    @property
    def lock_key(self) -> str:
        return ",".join(sorted(n.lower() for n in self.cfg.process_names)) or self.name


@dataclass
class Settings:
    raw_base: Dict[str, Any]
    profiles: List[Profile]

    def apply_global(self, key: str, value: Any) -> None:
        self.raw_base[key] = value
        for p in self.profiles:
            if key not in p.spec:
                setattr(p.cfg, key, value)
                p.cfg.validate()

    def set_target(self, profile: Profile, project: str, conversation: str) -> None:
        profile.cfg.target = Target(project, conversation, True)
        profile.spec["target"] = {"project": project, "conversation": conversation}


def build_profile(spec: Dict[str, Any], base: Dict[str, Any]) -> Profile:
    app = str(spec.get("app", "codex"))
    preset = copy.deepcopy(APP_PRESETS.get(app, {}))
    merged: Dict[str, Any] = dict(base)
    merged.update({k: v for k, v in preset.items() if k != "selectors"})
    sel = dict(preset.get("selectors", {}))
    sel.update(spec.get("selectors", {}))
    merged["selectors"] = sel
    for k, v in spec.items():
        if k not in ("name", "label", "app", "enabled", "selectors"):
            merged[k] = v
    cfg = Config.from_dict(merged)
    name = str(spec.get("name") or app)
    return Profile(name, str(spec.get("label") or name), app, bool(spec.get("enabled", True)), cfg, spec)


def default_base() -> Dict[str, Any]:
    return {k: v for k, v in Config().to_dict().items() if k not in GLOBAL_EXCLUDE}


def settings_path(home: Optional[Path] = None) -> Path:
    return (home or app_home()) / "config.json"


config_path = settings_path


def load_settings(home: Optional[Path] = None) -> Settings:
    p = settings_path(home)
    data: Dict[str, Any] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config 최상위가 객체가 아님")
        except (OSError, ValueError):
            try:
                p.replace(p.with_suffix(".bad"))
            except OSError:
                pass
            data = {}
    specs = data.get("profiles") if isinstance(data.get("profiles"), list) else None
    legacy_target = data.get("target") if isinstance(data.get("target"), dict) else None
    base = default_base()
    base.update({k: v for k, v in data.items() if k not in GLOBAL_EXCLUDE and k != "profiles"})
    if specs is None:
        specs = default_profile_specs()
        if legacy_target:          # 1.0 설정 파일 이전
            specs[0]["target"] = legacy_target
    profiles, seen = [], set()
    for sp in specs:
        if not isinstance(sp, dict):
            continue
        prof = build_profile(sp, base)
        if prof.name in seen:
            continue
        seen.add(prof.name)
        profiles.append(prof)
    st = Settings(base, profiles)
    if not p.exists() or "profiles" not in data:
        save_settings(st, home)
    return st


def save_settings(st: Settings, home: Optional[Path] = None) -> None:
    p = settings_path(home)
    out = dict(st.raw_base)
    out["profiles"] = [pr.spec | {"name": pr.name, "label": pr.label, "app": pr.app, "enabled": pr.enabled}
                       for pr in st.profiles]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
