"""간단한 관리 화면(tkinter) + 시스템 트레이(pystray). 앱(프로필)마다 한 줄씩 표시한다."""
from __future__ import annotations

import logging
import os
import threading

from . import APP_NAME, autostart
from .config import save_settings
from .fmt import fmt_clock, fmt_remaining
from .runtime import Runtime, Unit
from .timeparse import now_kst

log = logging.getLogger("cac.ui")

COLUMNS = [("app", "앱", 110), ("conv", "감시 대화", 190), ("codex", "앱 상태", 80), ("reset", "초기화", 90),
           ("resume", "자동 재개", 90), ("remain", "남은 시간", 80), ("last", "마지막 실행", 110),
           ("auto", "자동화", 55)]


def _make_icon(color: str):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.polygon([(24, 18), (24, 46), (48, 32)], fill="white")
    return img


class App:
    def __init__(self, rt: Runtime):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.rt = rt
        self.root = tk.Tk()
        suffix = (" [TEST MODE]" if rt.test_mode else "") + (" [DRY RUN]" if rt.dry_run else "")
        self.root.title(APP_NAME + suffix)
        self.root.geometry("920x430")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tray = None
        self._tray_color = None
        self._build()
        self._start_tray()
        rt.start()
        self.refresh()

    # ------------------------------------------------------------ 구성
    def _build(self):
        tk, ttk = self.tk, self.ttk
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)
        top = ttk.Frame(frm)
        top.pack(fill="x")
        ttk.Label(top, text=APP_NAME, font=("Malgun Gothic", 14, "bold")).pack(side="left")
        self.v_prog = tk.StringVar(value="-")
        ttk.Label(top, textvariable=self.v_prog).pack(side="right")

        self.tree = ttk.Treeview(frm, columns=[c[0] for c in COLUMNS], show="headings", height=4,
                                 selectmode="browse")
        for key, title, w in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, anchor="w")
        self.tree.pack(fill="x", pady=8)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())

        self.detail = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.detail, wraplength=880, justify="left").pack(anchor="w", pady=4, fill="x")

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=6)
        items = [("전체 시작", self.on_start_all), ("전체 일시정지", self.on_pause_all),
                 ("선택 앱 켜기/끄기", self.on_toggle_selected), ("감시 대상 대화 선택", self.on_target),
                 ("실행 로그", self.on_log), ("설정", self.on_settings), ("종료", self.quit)]
        for i, (t, cmd) in enumerate(items):
            ttk.Button(bar, text=t, command=cmd).grid(row=0, column=i, padx=2)
        self._rows = {}
        for u in self.rt.units:
            self._rows[u.profile.name] = self.tree.insert("", "end", iid=u.profile.name, values=[""] * len(COLUMNS))
        if self._rows:
            self.tree.selection_set(next(iter(self._rows)))

    def _selected_unit(self) -> Unit:
        sel = self.tree.selection()
        return self.rt.unit(sel[0]) if sel else self.rt.units[0]

    def _show_detail(self):
        u = self._selected_unit()
        st = u.engine.get_status()
        c = u.profile.cfg
        self.detail.set(f"[{u.profile.label}] 프로젝트: {c.target.project or '-'} | 현재 동작: {st.message or '-'}\n"
                        f"마지막 전송: {fmt_clock(st.last_send_at, now_kst(), seconds=True) if st.last_send_at else '-'}"
                        f" | 마지막 오류: {st.last_error or '-'}")

    # ------------------------------------------------------------ 갱신
    def refresh(self):
        try:
            now = now_kst()
            worst = "ok"
            for u in self.rt.units:
                st = u.engine.get_status()
                c = u.profile.cfg
                paused = u.store.paused
                self.tree.item(u.profile.name, values=[
                    u.profile.label, c.target.conversation or "-", st.codex,
                    fmt_clock(st.reset_at, now) if st.reset_at else "-",
                    fmt_clock(st.resume_at, now) if st.resume_at else "-",
                    fmt_remaining(st.resume_at - now) if st.resume_at else "-",
                    st.last_result, "OFF" if paused else "ON"])
                if paused:
                    worst = worst if worst != "ok" else "paused"
                elif st.codex == "연결 안 됨":
                    worst = "warn"
                elif st.limit_detected and worst == "ok":
                    worst = "limit"
            n_on = sum(1 for u in self.rt.units if not u.store.paused)
            self.v_prog.set(f"프로그램: 실행 중 | 자동화 ON {n_on}/{len(self.rt.units)}")
            self._show_detail()
            self._update_tray(worst)
        except Exception:
            log.exception("화면 갱신 오류")
        self.root.after(1000, self.refresh)

    # ------------------------------------------------------------ 동작
    def on_start_all(self):
        self.rt.resume_all()

    def on_pause_all(self):
        self.rt.pause_all()

    def on_toggle_selected(self):
        u = self._selected_unit()
        (u.engine.resume if u.store.paused else u.engine.pause)()

    def on_log(self):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("실행 로그")
        win.geometry("860x440")
        txt = tk.Text(win, wrap="none", font=("Consolas", 9))
        sb = self.ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        bar = self.ttk.Frame(win)
        bar.pack(fill="x")
        self.ttk.Button(bar, text="로그 파일 열기", command=lambda: self._open(self.rt.home / "logs" / "cac.log")
                        ).pack(side="left", padx=4, pady=3)
        last = {"n": -1}

        def tick():
            if not win.winfo_exists():
                return
            lines = self.rt.ring.get_lines()
            if len(lines) != last["n"]:
                last["n"] = len(lines)
                txt.delete("1.0", "end")
                txt.insert("end", "\n".join(lines))
                txt.see("end")
            win.after(1500, tick)
        tick()

    @staticmethod
    def _open(path):
        try:
            os.startfile(str(path))      # type: ignore[attr-defined]
        except Exception as e:
            log.error("열기 실패: %s", e)

    def on_target(self):
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title("감시 대상 대화 선택")
        win.geometry("480x470")
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        cur = self._selected_unit()
        names = [u.profile.label for u in self.rt.units]
        ttk.Label(frm, text="앱").pack(anchor="w")
        av = tk.StringVar(value=cur.profile.label)
        combo = ttk.Combobox(frm, textvariable=av, values=names, state="readonly")
        combo.pack(fill="x")
        pv, cv = tk.StringVar(), tk.StringVar()
        ttk.Label(frm, text="프로젝트 이름").pack(anchor="w", pady=(8, 0))
        ttk.Entry(frm, textvariable=pv).pack(fill="x")
        ttk.Label(frm, text="대화 이름 (사이드바/상단에 표시되는 제목과 동일하게)").pack(anchor="w", pady=(8, 0))
        ttk.Entry(frm, textvariable=cv).pack(fill="x")
        ttk.Label(frm, text="앱에서 읽은 항목 (선택하면 대화 이름에 채워짐)").pack(anchor="w", pady=(8, 0))
        lb = tk.Listbox(frm, height=9)
        lb.pack(fill="both", expand=True)
        status = tk.StringVar(value="")
        ttk.Label(frm, textvariable=status).pack(anchor="w")

        def unit():
            return next(u for u in self.rt.units if u.profile.label == av.get())

        def fill():
            c = unit().profile.cfg.target
            pv.set(c.project)
            cv.set(c.conversation)
        fill()

        def pick(_e=None):
            if lb.curselection():
                cv.set(lb.get(lb.curselection()[0]))
        lb.bind("<<ListboxSelect>>", pick)

        def load():
            u = unit()
            try:
                found, err = u.backend.list_conversations(), ""
            except Exception as e:
                found, err = [], str(e)

            def done():
                if not win.winfo_exists():
                    return
                lb.delete(0, "end")
                for n in found:
                    lb.insert("end", n)
                status.set(f"{len(found)}개 항목" if found else (err or "목록을 읽지 못함 - 직접 입력하세요"))
            self.root.after(0, done)

        def reload_all(_e=None):
            fill()
            threading.Thread(target=load, daemon=True).start()
        combo.bind("<<ComboboxSelected>>", reload_all)

        def save():
            if not cv.get().strip():
                status.set("대화 이름은 필수입니다")
                return
            u = unit()
            self.rt.settings.set_target(u.profile, pv.get().strip(), cv.get().strip())
            save_settings(self.rt.settings, self.rt.home)
            log.info("[%s] 감시 대상 변경: %s", u.profile.name, u.profile.cfg.target.label)
            win.destroy()
        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="목록 새로고침", command=reload_all).pack(side="left")
        ttk.Button(bar, text="저장", command=save).pack(side="right")
        threading.Thread(target=load, daemon=True).start()

    def on_settings(self):
        tk, ttk = self.tk, self.ttk
        cfg = self.rt.cfg
        win = tk.Toplevel(self.root)
        win.title("설정 (모든 앱 공통)")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        fields = [("재개 지연(분, 초기화 후)", "resume_delay_minutes", float),
                  ("감시 주기(초)", "poll_interval_seconds", float),
                  ("제한 유지 시 재확인 간격(초)", "persist_recheck_seconds", float),
                  ("사용자 유휴 시간 기준(초)", "idle_required_seconds", float),
                  ("전송 메시지", "send_message", str)]
        vars_ = {}
        for i, (lab, key, typ) in enumerate(fields):
            ttk.Label(frm, text=lab).grid(row=i, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=str(getattr(cfg, key)))
            vars_[key] = (v, typ)
            ttk.Entry(frm, textvariable=v, width=18).grid(row=i, column=1, sticky="w")
        r = len(fields)
        ttk.Label(frm, text="전송 방식").grid(row=r, column=0, sticky="w")
        mv = tk.StringVar(value=cfg.send_method)
        ttk.Combobox(frm, textvariable=mv, values=["auto", "uia", "keyboard"], width=16, state="readonly"
                     ).grid(row=r, column=1, sticky="w")
        checks = [("Dry Run (실제 전송 안 함)", "dry_run"), ("프로젝트명까지 일치해야 전송", "require_project_match"),
                  ("창 닫기 시 트레이로 최소화", "hide_to_tray_on_close")]
        bvars = {}
        for j, (lab, key) in enumerate(checks):
            b = tk.BooleanVar(value=getattr(cfg, key))
            bvars[key] = b
            ttk.Checkbutton(frm, text=lab, variable=b).grid(row=r + 1 + j, column=0, columnspan=2, sticky="w")
        av = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(frm, text="Windows 로그인 시 자동 실행", variable=av).grid(
            row=r + 1 + len(checks), column=0, columnspan=2, sticky="w")
        msg = tk.StringVar()
        ttk.Label(frm, textvariable=msg, foreground="red").grid(row=r + 3 + len(checks), column=0, columnspan=2)

        def save():
            try:
                new = {k: typ(v.get().strip()) for k, (v, typ) in vars_.items()}
                new["send_method"] = mv.get()
                new.update({k: b.get() for k, b in bvars.items()})
                for k, val in new.items():
                    self.rt.settings.apply_global(k, val)
                if not self.rt.test_mode:
                    save_settings(self.rt.settings, self.rt.home)
                    if av.get() != autostart.is_enabled():
                        autostart.set_enabled(av.get())
            except Exception as e:
                msg.set(f"저장 실패: {e}")
                return
            log.info("설정 저장됨")
            win.destroy()
        ttk.Button(frm, text="저장", command=save).grid(row=r + 2 + len(checks), column=1, sticky="e", pady=8)

    # ------------------------------------------------------------ 트레이
    def _start_tray(self):
        try:
            import pystray
        except Exception:
            log.warning("pystray/Pillow 없음 - 트레이 아이콘 없이 창으로만 실행")
            return
        menu = pystray.Menu(
            pystray.MenuItem("열기", lambda: self.root.after(0, self.show), default=True),
            pystray.MenuItem("전체 시작", lambda: self.root.after(0, self.on_start_all)),
            pystray.MenuItem("전체 일시정지", lambda: self.root.after(0, self.on_pause_all)),
            pystray.MenuItem("실행 로그", lambda: self.root.after(0, self.on_log)),
            pystray.MenuItem("종료", lambda: self.root.after(0, self.quit)),
        )
        self.tray = pystray.Icon("cac", _make_icon("#6b7280"), APP_NAME, menu)
        self.tray.run_detached()

    def _update_tray(self, worst: str):
        if not self.tray:
            return
        color = {"ok": "#22c55e", "limit": "#f59e0b", "warn": "#ef4444", "paused": "#6b7280"}[worst]
        if color != self._tray_color:
            self._tray_color = color
            self.tray.icon = _make_icon(color)
        now = now_kst()
        lines = [APP_NAME]
        for u in self.rt.units:
            st = u.engine.get_status()
            tail = f"재개 {fmt_clock(st.resume_at, now)}" if st.resume_at else st.codex
            lines.append(f"{u.profile.label}: {tail}")
        self.tray.title = "\n".join(lines)[:120]

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def on_close(self):
        if self.tray and self.rt.cfg.hide_to_tray_on_close:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        log.info("프로그램 종료")
        self.rt.stop()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()
