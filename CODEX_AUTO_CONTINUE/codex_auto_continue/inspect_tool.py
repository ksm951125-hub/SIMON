"""STEP 1 환경 분석 도구: 실행 중인 Codex 앱의 프로세스/창/UI 요소를 덤프하고,
감지기가 지금 화면을 어떻게 해석하는지 보여준다. (읽기 전용 - 아무것도 입력/클릭하지 않음)"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from .config import Config
from .detector import detect_limit
from .fmt import fmt_clock
from .identify import identify
from .timeparse import now_kst


def run_inspect(cfg: Config, home: Path, max_lines: int = 4000, tag: str = "app") -> int:
    if sys.platform != "win32":
        print("inspect 는 Windows 에서 Codex 앱이 실행 중일 때 사용합니다.")
        return 2
    import psutil
    from pywinauto import Desktop
    from .backend_uia import UiaBackend

    print("=== 1) Codex 관련 프로세스 ===")
    names = [n.lower() for n in cfg.process_names]
    found_any = False
    for p in psutil.process_iter(["pid", "name", "exe"]):
        n = (p.info["name"] or "").lower()
        if any(x in n for x in names):
            found_any = True
            print(f"  pid={p.info['pid']:<6} {p.info['name']}  {p.info['exe']}")
    if not found_any:
        print("  (없음) 대상 앱을 먼저 실행하세요. process_names 설정도 확인하세요:", cfg.process_names)

    print("\n=== 2) 최상위 창 목록 ===")
    desk = Desktop(backend="uia")
    for w in desk.windows():
        try:
            pid = w.process_id()
            exe = psutil.Process(pid).name()
            t = (w.window_text() or "")[:60]
            if not t and not any(x in exe.lower() for x in names):
                continue
            mark = "  <== Codex 후보" if any(x in exe.lower() for x in names) else ""
            print(f"  [{exe}] pid={pid} min={w.is_minimized()} '{t}'{mark}")
        except Exception:
            continue

    be = UiaBackend(cfg)
    win = be._find_window()
    if win is None:
        print("\nCodex 창을 찾지 못했습니다 (트레이로 숨김/미실행/process_names 불일치).")
        return 1

    out_dir = home / "inspect"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tree_file = out_dir / f"uia_tree_{tag}_{stamp}.txt"
    print(f"\n=== 3) UI Automation 트리 덤프 → {tree_file} ===")
    count = _dump_tree(win, tree_file, max_lines)
    print(f"  {count}개 요소 기록. 요소가 거의 없다면 Chromium 접근성이 꺼져 있는 것입니다 (README 참고).")

    print("\n=== 4) 감지기가 읽은 현재 화면 ===")
    snap = be.snapshot()
    now = now_kst()
    print("  창 제목        :", snap.title)
    print("  최소화         :", snap.minimized)
    print("  활성 대화 후보 :", snap.conversation_candidates[:8])
    print("  프로젝트 후보  :", snap.project_candidates[:8])
    print("  작업 중(busy)  :", snap.busy)
    print("  입력창         : found=%s text=%r" % (snap.input_found, snap.input_text))
    print("  하단 텍스트    :", (snap.banner_texts or [])[-8:])
    frags = snap.banner_texts if snap.banner_texts is not None else snap.texts[-cfg.tail_fragments:]
    det = detect_limit(frags, now, max_len=cfg.banner_max_len, extra_patterns=cfg.extra_limit_patterns)
    if det:
        print("  사용량 제한    : 감지됨 →", det.message)
        if det.reset:
            print("  초기화 시각    :", fmt_clock(det.reset.reset_at, now), "/ 자동 재개:",
                  fmt_clock(det.reset.reset_at + __import__('datetime').timedelta(minutes=cfg.resume_delay_minutes), now))
        else:
            print("  초기화 시각    : 읽지 못함")
    else:
        print("  사용량 제한    : 감지되지 않음")
    print("  하단 영역 live 여부:", None if det is None else det.live)
    ident = identify(snap, cfg.target, require_project=cfg.require_project_match, mode=cfg.match_mode)
    print(f"  대상 대화 식별 : {'OK' if ident.ok else 'FAIL'} - {ident.reason}  (대상: {cfg.target.label})")
    print("\n사이드바 대화 목록:", be.list_conversations()[:30])
    print(f"\n문제가 있으면 위 출력과 {tree_file} 를 기준으로 data/config.json 해당 프로필의 selectors 를 조정하세요.")
    return 0


def _dump_tree(win, path: Path, max_lines: int) -> int:
    from pywinauto.controls.uiawrapper import UIAWrapper
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        def walk(info, depth):
            nonlocal n
            if n >= max_lines or depth > 14:
                return
            try:
                r = info.rectangle
                sel = ""
                if info.control_type in ("ListItem", "TreeItem", "TabItem", "DataItem"):
                    try:
                        sel = " SELECTED" if UIAWrapper(info).iface_selection_item.CurrentIsSelected else ""
                    except Exception:
                        pass
                f.write("%s%s name=%r id=%r cls=%r rect=(%d,%d,%d,%d)%s%s\n" % (
                    "  " * depth, info.control_type, (info.name or "")[:100], info.automation_id,
                    info.class_name, r.left, r.top, r.right, r.bottom,
                    "" if info.enabled else " DISABLED", sel))
                n += 1
                for c in info.children():
                    walk(c, depth + 1)
            except Exception:
                return
        walk(win.element_info, 0)
    return n
