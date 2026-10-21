import json
import unittest
from datetime import timedelta

from helpers import Rig, make_cfg, dt
from codex_auto_continue.backend_fake import FakeBackend
from codex_auto_continue.state import StateStore


def limited_rig(**kw):
    r = Rig(**kw)
    r.limit("오전 11:35")
    return r


class Cycle(unittest.TestCase):
    def test_detect_wait_and_send_after_two_minutes(self):
        r = limited_rig()
        st = r.tick()
        self.assertTrue(st.limit_detected)
        self.assertEqual(st.reset_at, dt(11, 35))
        self.assertEqual(st.resume_at, dt(11, 37))          # 초기화 + 2분
        self.assertEqual(st.codex, "사용량 제한")
        # 11:35 초기화: 안내문 사라짐. 11:36:59 까지는 보내지 않는다
        r.clock.set(dt(11, 35, 1)); r.be.limit_text = ""
        r.tick(); r.clock.set(dt(11, 36, 59)); r.tick()
        self.assertEqual(r.be.sent, [])
        r.clock.set(dt(11, 37, 0)); r.tick()
        self.assertEqual(r.be.sent, ["계속"])
        st = r.engine.get_status()
        self.assertEqual(st.last_result, "정상 전송")
        self.assertFalse(st.limit_detected)

    def test_no_fixed_5h_timer_uses_screen_time(self):
        r = Rig(start=dt(8, 0))
        r.limit("오후 1:20")                    # 5시간이 아닌 임의 시각
        self.assertEqual(r.tick().resume_at, dt(13, 22))

    def test_repeats_for_next_limits(self):
        r = limited_rig()
        r.tick()
        r.clock.set(dt(11, 36)); r.be.limit_text = ""
        r.run_until(dt(11, 40))
        self.assertEqual(len(r.be.sent), 1)
        # 작업 재개 → 다음 제한 (초기화 오후 4:35)
        r.be.busy = False; r.limit("오후 4:35")
        r.run_until(dt(16, 34))
        self.assertEqual(len(r.be.sent), 1)
        r.be.limit_text = ""
        r.run_until(dt(16, 40))
        self.assertEqual(len(r.be.sent), 2)
        self.assertEqual([h["status"] for h in r.store.history], ["sent", "sent"])

    def test_reset_time_changed_later_and_earlier(self):
        r = limited_rig()
        r.tick()
        r.limit("오후 12:00"); st = r.tick(30)
        self.assertEqual(st.resume_at, dt(12, 2))
        r.limit("오전 11:50"); st = r.tick(30)
        self.assertEqual(st.resume_at, dt(11, 52))
        r.be.limit_text = ""
        r.run_until(dt(11, 51)); self.assertEqual(r.be.sent, [])
        r.run_until(dt(11, 53)); self.assertEqual(r.be.sent, ["계속"])

    def test_limit_persists_after_reset(self):
        r = limited_rig()
        r.tick()
        r.run_until(dt(11, 45))                     # 안내문이 계속 남아 있음
        self.assertEqual(r.be.sent, [])
        self.assertIn("제한이 유지", r.engine.get_status().message)
        r.limit("오후 12:30")                       # 새 초기화 시각으로 갱신
        r.run_until(dt(11, 50))
        self.assertEqual(r.store.cycle.resume_at, dt(12, 32))
        r.be.limit_text = ""
        r.run_until(dt(12, 40))
        self.assertEqual(r.be.sent, ["계속"])

    def test_persisting_same_banner_never_sends_and_is_not_pushed_to_tomorrow(self):
        r = limited_rig()
        r.tick()
        r.run_until(dt(14, 0), step=60)             # 같은 안내가 2시간 넘게 유지
        self.assertEqual(r.be.sent, [])
        self.assertEqual(r.store.cycle.reset_at, dt(11, 35))

    def test_midnight_rollover(self):
        r = Rig(start=dt(23, 50))
        r.limit("오전 12:10")
        st = r.tick()
        self.assertEqual(st.resume_at, dt(0, 12, day=22))
        r.be.limit_text = ""
        r.run_until(dt(0, 11, day=22)); self.assertEqual(r.be.sent, [])
        r.run_until(dt(0, 13, day=22)); self.assertEqual(r.be.sent, ["계속"])

    def test_configurable_delay(self):
        r = Rig(cfg=make_cfg(resume_delay_minutes=0.5))
        r.limit("오전 11:35")
        self.assertEqual(r.tick().resume_at, dt(11, 35, 30))


class Safety(unittest.TestCase):
    def ready(self, **kw):
        r = limited_rig(**kw)
        r.tick()
        r.clock.set(dt(11, 36)); r.be.limit_text = ""; r.tick()
        r.clock.set(dt(11, 37, 5))
        return r

    def test_no_duplicate_when_banner_lingers(self):
        r = self.ready(); r.tick()
        self.assertEqual(len(r.be.sent), 1)
        r.limit("오전 11:35")                      # 같은 안내가 다시 보임
        r.be.busy = False
        r.run_until(dt(12, 30))
        self.assertEqual(len(r.be.sent), 1)

    def test_same_reset_cycle_never_processed_twice(self):
        r = self.ready(); r.tick()
        self.assertEqual(len(r.be.sent), 1)
        # 전송 13분 뒤 같은 초기화 시각 안내가 잠깐 보였다가 사라지고, 작업은 멈춘 상태
        r.be.busy = False; r.clock.set(dt(11, 50)); r.limit("오전 11:35"); r.tick()
        self.assertIsNone(r.store.cycle)
        r.be.limit_text = ""
        r.run_until(dt(12, 20))
        self.assertEqual(len(r.be.sent), 1)

    def test_wrong_conversation_not_sent(self):
        r = self.ready()
        r.be.active_conversation = "다른 대화 A"; r.be.select_behavior = "fail"
        r.tick()
        self.assertEqual(r.be.sent, [])
        self.assertIn("대상 대화 식별 실패", r.engine.get_status().last_error)

    def test_wrong_conversation_gets_selected_then_sent(self):
        r = self.ready()
        r.be.active_conversation = "다른 대화 A"; r.be.active_project = "다른"
        r.tick()
        self.assertEqual(r.be.select_calls, 1)
        self.assertEqual(r.be.sent, ["계속"])
        self.assertEqual(r.be.active_conversation, "MY AI OFFICE 구축하기")

    def test_wrong_conversation_user_active_defers(self):
        r = self.ready()
        r.be.active_conversation = "다른 대화 A"; r.be.select_behavior = "deferred"
        r.tick()
        self.assertEqual(r.be.sent, [])
        self.assertIsNotNone(r.store.cycle)                    # 포기하지 않고 보류
        r.be.select_behavior = "ok"
        r.run_until(dt(11, 40))
        self.assertEqual(r.be.sent, ["계속"])

    def test_gives_up_after_max_attempts(self):
        r = self.ready()
        r.be.active_conversation = "다른"; r.be.select_behavior = "fail"
        r.run_until(dt(11, 37, 5) + timedelta(minutes=10), step=30)
        self.assertEqual(r.be.sent, [])
        self.assertIsNone(r.store.cycle)
        self.assertEqual(r.store.history[-1]["status"], "failed")

    def test_user_already_resumed(self):
        r = self.ready()
        r.be.busy = True
        r.tick()
        self.assertEqual(r.be.sent, [])
        self.assertEqual(r.store.history[-1]["status"], "cancelled")

    def test_user_resumed_before_resume_time(self):
        r = limited_rig(); r.tick()
        r.clock.set(dt(11, 36)); r.be.limit_text = ""; r.be.busy = True; r.tick()
        self.assertIsNone(r.store.cycle)

    def test_user_draft_not_overwritten(self):
        r = self.ready()
        r.be.input_text = "사용자가 쓰는 중"
        r.tick()
        self.assertEqual(r.be.sent, [])
        r.be.input_text = ""
        r.run_until(dt(11, 40))
        self.assertEqual(r.be.sent, ["계속"])

    def test_completion_marker(self):
        r = self.ready()
        r.cfg.completion_markers = ["모든 작업을 완료"]
        r.be.body_texts.append("모든 작업을 완료했습니다.")
        r.tick()
        self.assertEqual(r.be.sent, [])
        self.assertEqual(r.store.history[-1]["status"], "cancelled")

    def test_input_missing(self):
        r = self.ready(); r.be.input_found = False
        r.tick()
        self.assertEqual(r.be.sent, [])

    def test_network_down_defers(self):
        r = self.ready(); r.net = False
        r.tick(); r.tick(60)
        self.assertEqual(r.be.sent, [])
        r.net = True
        r.run_until(dt(11, 42))
        self.assertEqual(r.be.sent, ["계속"])

    def test_codex_not_running_then_started(self):
        r = limited_rig(); r.tick()
        r.be.window_found = False
        r.run_until(dt(11, 45), step=30)
        self.assertEqual(r.engine.get_status().codex, "연결 안 됨")
        self.assertEqual(r.be.sent, [])
        r.be.window_found = True; r.be.limit_text = ""
        r.run_until(dt(11, 50))
        self.assertEqual(r.be.sent, ["계속"])

    def test_minimized_is_restored(self):
        r = self.ready(); r.be.minimized = True
        r.tick()
        self.assertEqual(r.be.restore_calls, 1)
        self.assertEqual(r.be.sent, ["계속"])

    def test_locked_screen_no_action(self):
        r = self.ready(); r.be.ui_available = False
        r.tick(); r.tick(60)
        self.assertEqual(r.be.sent, [])
        self.assertEqual(r.engine.get_status().codex, "UI 사용 불가")
        r.be.ui_available = True
        r.run_until(dt(11, 42))
        self.assertEqual(r.be.sent, ["계속"])

    def test_paused_never_sends(self):
        r = self.ready()
        r.engine.pause()
        r.tick(); r.tick(600)
        self.assertEqual(r.be.sent, [])
        self.assertEqual(r.engine.get_status().program, "일시정지")
        r.engine.resume(); r.tick()
        self.assertEqual(r.be.sent, ["계속"])

    def test_dry_run_sends_nothing(self):
        r = self.ready(cfg=make_cfg(dry_run=True))
        r.tick()
        self.assertEqual(r.be.sent, [])
        self.assertEqual(r.store.history[-1]["status"], "dry_run")
        r.tick(60); r.tick(60)
        self.assertEqual(len(r.store.history), 1)               # dry-run 에서도 반복 기록 없음

    def test_dry_run_history_does_not_block_real_send(self):
        r = self.ready(cfg=make_cfg(dry_run=True)); r.tick()
        r.cfg.dry_run = False
        r.limit("오전 11:35"); r.be.busy = False
        r.tick(5); r.be.limit_text = ""
        r.run_until(dt(11, 45))
        self.assertEqual(r.be.sent, ["계속"])


class SendOutcomes(unittest.TestCase):
    def ready(self, behavior):
        r = limited_rig(); r.be.send_behavior = behavior
        r.tick(); r.clock.set(dt(11, 36)); r.be.limit_text = ""; r.tick()
        r.clock.set(dt(11, 37, 5))
        return r

    def test_send_fail_retries_then_gives_up(self):
        r = self.ready("fail")
        r.run_until(dt(11, 37, 5) + timedelta(minutes=8), step=30)
        self.assertEqual(r.be.sent, [])
        self.assertEqual(r.store.history[-1]["status"], "failed")

    def test_not_submitted_retries_without_duplicates_in_box(self):
        r = self.ready("not_submitted")
        r.tick()
        self.assertEqual(r.store.cycle.attempts, 1)
        r.be.send_behavior = "ok"
        r.run_until(dt(11, 41))
        self.assertEqual(r.store.history[-1]["status"], "sent")

    def test_silent_send_unconfirmed_is_not_resent(self):
        r = self.ready("silent")
        r.tick()
        self.assertEqual(len(r.be.sent), 1)
        self.assertEqual(r.store.history[-1]["status"], "sent_unconfirmed")
        r.run_until(dt(12, 30))
        self.assertEqual(len(r.be.sent), 1)

    def test_deferred_send_does_not_count_as_attempt(self):
        r = self.ready("deferred")
        r.run_until(dt(11, 37, 5) + timedelta(minutes=10), step=30)
        self.assertEqual(r.be.sent, [])
        self.assertIsNotNone(r.store.cycle)
        self.assertEqual(r.store.cycle.attempts, 0)
        r.be.send_behavior = "ok"
        r.run_until(dt(11, 55))
        self.assertEqual(r.be.sent, ["계속"])

    def test_min_send_interval(self):
        r = self.ready("ok"); r.tick()
        self.assertEqual(len(r.be.sent), 1)
        # 곧바로 새 제한(초기화 11:45)이 와도 직전 전송 후 10분 안에는 보내지 않는다
        r.be.busy = False; r.limit("오전 11:45"); r.tick(1); r.be.limit_text = ""
        r.run_until(dt(11, 47, 30), step=5)
        self.assertEqual(len(r.be.sent), 1)
        r.run_until(dt(11, 52), step=15)
        self.assertEqual(len(r.be.sent), 2)


class Persistence(unittest.TestCase):
    def test_restart_restores_cycle_and_sends_once(self):
        r = limited_rig(); r.tick()
        eng = r.restart()                               # 프로그램 재시작
        self.assertEqual(r.store.cycle.resume_at, dt(11, 37))
        r.be.limit_text = ""
        r.clock.set(dt(11, 40)); eng.tick()
        self.assertEqual(r.be.sent, ["계속"])
        eng = r.restart()                               # 전송 후 재시작
        r.be.busy = False; r.limit("오전 11:35")
        r.run_until(dt(12, 30))
        self.assertEqual(len(r.be.sent), 1)

    def test_restart_after_manual_resume_cancels(self):
        r = limited_rig(); r.tick()
        eng = r.restart()
        r.be.limit_text = ""; r.be.busy = True           # 그 사이 사용자가 직접 재개
        r.clock.set(dt(11, 40)); eng.tick()
        self.assertEqual(r.be.sent, [])
        self.assertEqual(r.store.history[-1]["status"], "cancelled")

    def test_crash_while_sending_never_resends(self):
        r = limited_rig(); r.tick()
        r.store.cycle.status = "sending"; r.store.touch()
        eng = r.restart()
        self.assertIsNone(r.store.cycle)
        self.assertEqual(r.store.history[-1]["status"], "sent_unconfirmed")
        r.be.limit_text = ""; r.clock.set(dt(11, 40)); eng.tick()
        self.assertEqual(r.be.sent, [])

    def test_paused_flag_survives_restart(self):
        r = Rig(); r.engine.pause()
        r.restart()
        self.assertTrue(r.store.paused)

    def test_corrupted_state_file(self):
        r = Rig()
        r.path.write_text("{ not json", encoding="utf-8")
        s = StateStore(r.path)
        self.assertIsNone(s.cycle)
        self.assertTrue(s.load_error)
        self.assertTrue(r.path.with_suffix(".bad").exists())

    def test_state_file_is_valid_json(self):
        r = limited_rig(); r.tick()
        d = json.loads(r.path.read_text(encoding="utf-8"))
        self.assertEqual(d["cycle"]["resume_at"], dt(11, 37).isoformat())

    def test_stale_cycle_discarded(self):
        r = limited_rig(); r.tick()
        eng = r.restart()
        r.be.limit_text = ""
        r.clock.set(dt(11, 37) + timedelta(hours=13)); eng.tick()
        self.assertEqual(r.be.sent, [])
        self.assertEqual(r.store.history[-1]["status"], "cancelled")


class WakeAndMisc(unittest.TestCase):
    def test_wake_from_sleep_waits_grace(self):
        r = Rig(cfg=make_cfg(wake_grace_seconds=30)); r.limit("오전 11:35")
        r.clock.advance(60); r.tick()
        r.be.limit_text = ""
        r.clock.set(dt(11, 40)); r.tick()                  # 절전에서 복귀(큰 틱 간격)
        self.assertEqual(r.be.sent, [])
        r.clock.set(dt(11, 40, 31)); r.tick()
        self.assertEqual(r.be.sent, ["계속"])

    def test_unknown_reset_time_waits_for_banner_to_disappear(self):
        r = Rig(); r.be.limit_text = "사용량을 모두 사용했습니다."
        st = r.tick()
        self.assertTrue(st.limit_detected)
        self.assertIsNone(st.resume_at)
        r.run_until(dt(11, 30)); self.assertEqual(r.be.sent, [])
        r.be.limit_text = ""; r.tick(15)
        self.assertEqual(r.store.cycle.resume_at, dt(11, 32, 15))   # 안내가 사라진 시각 + 2분
        r.run_until(dt(11, 40))
        self.assertEqual(r.be.sent, ["계속"])

    def test_tail_detection_when_no_banner_zone(self):
        r = Rig(); r.be.banner_zone = False; r.limit("오전 11:35")
        self.assertEqual(r.tick().resume_at, dt(11, 37))

    def test_next_delay_hits_resume_time(self):
        r = limited_rig(); r.tick()
        self.assertEqual(r.engine.next_delay(), r.cfg.waiting_poll_seconds)
        r.clock.set(dt(11, 36, 50))
        self.assertAlmostEqual(r.engine.next_delay(), 10, delta=1)

    def test_loop_survives_backend_exception(self):
        r = Rig()
        calls = []
        def boom():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("uia crash")
            r.engine.stop()
            return FakeBackend().snapshot()
        r.be.snapshot = boom
        r.cfg.poll_interval_seconds = 1
        r.engine._stop.wait = lambda t: None
        r.engine.run_forever()
        self.assertEqual(len(calls), 2)
        self.assertIn("내부 오류", r.engine.get_status().last_error)


if __name__ == "__main__":
    unittest.main()
