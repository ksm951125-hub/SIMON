"""간단한 관리 화면(tkinter) + 시스템 트레이(pystray)."""
from __future__ import annotations

import logging
import os
import threading

from . import APP_NAME, autostart
from .config import save_config
from .fmt import fmt_clock, fmt_remaining
from .runtime import Runtime
from .timeparse import now_kst

log = logging.getLogger("cac.ui")


def _make_icon(color: str):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.polygon([(24, 18), (24, 46), (48, 32)], fill="white")     # ▶ (계속)
    return img


class App:
    def __init__(self, rt: Runtime):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.rt = rt
        self.root = tk.Tk()
        suffix = (" [TEST MODE]" if rt.test_mode else "") + (" [DRY RUN]" if rt.cfg.dry_run else "")
        self.root.title(APP_NAME + suffix)
        self.root.geometry("520x470")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.vars = {}
        self.tray = None
        self._tray_color = None
        self._build()
        self._start_tray()
        self.thread = threading.Thread(target=rt.engine.run_forever, name="engine", daemon=True)
        self.thread.start()
        self.refresh()

    # ------------------------------------------------------------ 화면 구성
    def _build(self):
        tk, ttk = self.tk, self.ttk
        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=APP_NAME, font=("Malgun Gothic", 14, "bold")).pack(anchor="w")
        grid = ttk.Frame(frm)
        grid.pack(fill="x", pady=8)
        rows = [("프로그램 상태", "program"), ("Codex 상태", "codex"), ("감시 프로젝트", "project"),
                ("감시 대화", "conv"), ("초기화 시간", "reset"), ("자동 재개", "resume"),
                ("남은 시간", "remain"), ("마지막 실행", "last"), ("마지막 전송 시각", "lasttime"),
                ("자동화", "auto"), ("현재 동작", "msg"), ("마지막 오류", "err")]
        for i, (label, key) in enumerate(rows):
            ttk.Label(grid, text=label, width=14).grid(row=i, column=0, sticky="nw", pady=1)
            v = tk.StringVar(value="-")
            self.vars[key] = v
            ttk.Label(grid, textvariable=v, wraplength=340, justify="left").grid(row=i, column=1, sticky="w")
        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=6)
        self.btn_start = ttk.Button(bar, text="자동화 시작", command=self.on_start)
        self.btn_pause = ttk.Button(bar, text="자동화 일시정지", command=self.on_pause)
        self.btn_start.grid(row=0, column=0, padx=2, pady=2)
        self.btn_pause.grid(row=0, column=1, padx=2, pady=2)
        ttk.Button(bar, text="감시 대상 대화 선택", command=self.on_target).grid(row=0, column=2, padx=2)
        ttk.Button(bar, text="실행 로그", command=self.on_log).grid(row=1, column=0, padx=2, pady=2)
        ttk.Button(bar, text="설정", command=self.on_settings).grid(row=1, column=1, padx=2)
        ttk.Button(bar, text="종료", command=self.quit).grid(row=1, column=2, padx=2)

    # ------------------------------------------------------------ 상태 갱신
    def refresh(self):
        try:
            st = self.rt.engine.get_status()
            now = now_kst()
            cfg = self.rt.cfg
            v = self.vars
            v["program"].set(st.program)
            v["codex"].set(st.codex)
            v["project"].set(cfg.target.project or "-")
            v["conv"].set(cfg.target.conversation or "-")
            v["reset"].set(fmt_clock(st.reset_at, now) if st.reset_at else "-")
            v["resume"].set(fmt_clock(st.resume_at, now) if st.resume_at else "-")
            v["remain"].set(fmt_remaining(st.resume_at - now) if st.resume_at else "-")
            v["last"].set(st.last_result)
            v["lasttime"].set(fmt_clock(st.last_send_at, now, seconds=True) if st.last_send_at else "-")
            v["auto"].set("ON" if st.automation_on else "OFF")
            v["msg"].set(st.message or "-")
            v["err"].set(st.last_error or "-")
            paused = self.rt.store.paused
            self.btn_start.state(["!disabled" if paused else "disabled"])
            self.btn_pause.state(["disabled" if paused else "!disabled"])
            self._update_tray(st, paused)
        except Exception:
            log.exception("화면 갱신 오류")
        self.root.after(1000, self.refresh)

    # ------------------------------------------------------------ 동작
    def on_start(self):
        self.rt.engine.resume()

    def on_pause(self):
        self.rt.engine.pause()

    def on_log(self):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("실행 로그")
        win.geometry("760x420")
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
        cfg = self.rt.cfg
        win = tk.Toplevel(self.root)
        win.title("감시 대상 대화 선택")
        win.geometry("460x420")
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        pv, cv = tk.StringVar(value=cfg.target.project), tk.StringVar(value=cfg.target.conversation)
        ttk.Label(frm, text="프로젝트 이름").pack(anchor="w")
        ttk.Entry(frm, textvariable=pv).pack(fill="x")
        ttk.Label(frm, text="대화 이름 (Codex 사이드바에 표시되는 제목과 동일하게)").pack(anchor="w", pady=(8, 0))
        ttk.Entry(frm, textvariable=cv).pack(fill="x")
        ttk.Label(frm, text="Codex 에서 읽은 대화 목록 (선택하면 대화 이름에 채워짐)").pack(anchor="w", pady=(8, 0))
        lb = tk.Listbox(frm, height=10)
        lb.pack(fill="both", expand=True)
        status = tk.StringVar(value="")
        ttk.Label(frm, textvariable=status).pack(anchor="w")

        def pick(_e=None):
            if lb.curselection():
                cv.set(lb.get(lb.curselection()[0]))
        lb.bind("<<ListboxSelect>>", pick)

        def load():
            try:
                names = self.rt.backend.list_conversations()
            except Exception as e:
                names, err = [], str(e)
            else:
                err = ""

            def done():
                if not win.winfo_exists():
                    return
                lb.delete(0, "end")
                for n in names:
                    lb.insert("end", n)
                status.set(f"{len(names)}개 항목" if names else (err or "목록을 읽지 못함 - 직접 입력하세요"))
            self.root.after(0, done)

        def save():
            cfg.target.project, cfg.target.conversation = pv.get().strip(), cv.get().strip()
            if not cfg.target.conversation:
                status.set("대화 이름은 필수입니다")
                return
            save_config(cfg, self.rt.home)
            log.info("감시 대상 변경: %s", cfg.target.label)
            win.destroy()
        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="목록 새로고침", command=lambda: threading.Thread(target=load, daemon=True).start()
                   ).pack(side="left")
        ttk.Button(bar, text="저장", command=save).pack(side="right")
        threading.Thread(target=load, daemon=True).start()

    def on_settings(self):
        tk, ttk = self.tk, self.ttk
        cfg = self.rt.cfg
        win = tk.Toplevel(self.root)
        win.title("설정")
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
                for key, (v, typ) in vars_.items():
                    setattr(cfg, key, typ(v.get().strip()))
                cfg.send_method = mv.get()
                for key, b in bvars.items():
                    setattr(cfg, key, b.get())
                cfg.validate()
                save_config(cfg, self.rt.home)
                if not self.rt.test_mode and av.get() != autostart.is_enabled():
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
            pystray.MenuItem("자동화 시작", lambda: self.root.after(0, self.on_start)),
            pystray.MenuItem("자동화 일시정지", lambda: self.root.after(0, self.on_pause)),
            pystray.MenuItem("실행 로그", lambda: self.root.after(0, self.on_log)),
            pystray.MenuItem("종료", lambda: self.root.after(0, self.quit)),
        )
        self.tray = pystray.Icon("cac", _make_icon("#6b7280"), APP_NAME, menu)
        self.tray.run_detached()

    def _update_tray(self, st, paused: bool):
        if not self.tray:
            return
        color = ("#6b7280" if paused else "#ef4444" if st.last_error and st.codex == "연결 안 됨"
                 else "#f59e0b" if st.limit_detected else "#22c55e")
        if color != self._tray_color:
            self._tray_color = color
            self.tray.icon = _make_icon(color)
        tip = f"{APP_NAME}\n{st.program} | Codex: {st.codex}"
        if st.resume_at:
            tip += f"\n자동 재개 {fmt_clock(st.resume_at, now_kst())}"
        self.tray.title = tip[:120]

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
        self.rt.engine.stop()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()
