"""스크린샷 기반 시나리오: GPT Codex 와 Claude Code 를 각자의 초기화 시각에 맞춰 재개."""
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import Rig, Clock, make_cfg, dt
from codex_auto_continue.backend_fake import FakeBackend
from codex_auto_continue.config import load_settings, save_settings
from codex_auto_continue.engine import Engine
from codex_auto_continue.models import Target
from codex_auto_continue.state import StateStore

CODEX_CARD = ("Codex 및 Work 사용량을 모두 사용했습니다\n"
              "크레딧을 추가하거나 플랜을 업그레이드하세요. 또는 오후 10:47에 사용량이 재설정될 때까지 기다리세요.\n"
              "크레딧 추가")
CODEX_HISTORY = ("사용 한도에 도달했습니다. 계속하려면 요금제를 업그레이드하거나 크레딧을 추가하세요. "
                 "또는 오후 10:47에 다시 시도하세요.")
CLAUDE_TOAST = "사용량 제한을 초과하여 종료됨\n오후 11:50에 초기화\n더 알아보기\n사용량 추가 구매"
D = dict(day=26, month=10)


def codex_rig(start):
    be = FakeBackend(conversation="점검 OCI 이전 준비", project="AI STUDIO")
    return Rig(start=start, backend=be,
               cfg=make_cfg(target=Target("AI STUDIO", "점검 OCI 이전 준비")))


class CodexScreen(unittest.TestCase):
    def test_history_line_after_reset_does_not_block(self):
        r = codex_rig(dt(22, 30, **D))
        r.be.banner_extra = [CODEX_HISTORY]
        r.be.limit_text = CODEX_CARD
        st = r.tick()
        self.assertEqual((st.reset_at, st.resume_at), (dt(22, 47, **D), dt(22, 49, **D)))
        r.clock.set(dt(22, 47, 30, **D)); r.be.limit_text = ""      # 카드는 사라지고 기록 줄만 남음
        r.run_until(dt(22, 48, 59, **D), step=10)
        self.assertEqual(r.be.sent, [])
        r.run_until(dt(22, 49, 5, **D), step=5)
        self.assertEqual(r.be.sent, ["계속"])

    def test_card_still_shown_after_reset_blocks(self):
        r = codex_rig(dt(22, 30, **D))
        r.be.banner_extra = [CODEX_HISTORY]; r.be.limit_text = CODEX_CARD
        r.tick()
        r.run_until(dt(22, 55, **D))
        self.assertEqual(r.be.sent, [])
        self.assertIn("제한이 유지", r.engine.get_status().message)

    def test_old_history_line_alone_creates_no_cycle(self):
        r = codex_rig(dt(22, 55, **D))                       # 이미 초기화된 뒤 프로그램을 켬
        r.be.banner_extra = [CODEX_HISTORY]
        r.tick(); r.tick(60)
        self.assertIsNone(r.store.cycle)
        self.assertEqual(r.be.sent, [])

    def test_other_open_conversation_is_switched_not_typed_into(self):
        r = codex_rig(dt(22, 30, **D))
        r.be.limit_text = CODEX_CARD; r.tick()
        r.be.limit_text = ""
        r.be.active_conversation = "MY AI OFFICE 구축하기"   # 사용자가 다른 대화를 열어 둠
        r.run_until(dt(22, 50, **D))
        self.assertEqual(r.be.select_calls, 1)
        self.assertEqual(r.be.sent, ["계속"])
        self.assertEqual(r.be.active_conversation, "점검 OCI 이전 준비")


class ClaudeScreen(unittest.TestCase):
    def rig(self, start):
        be = FakeBackend(conversation="네이버 블로그 AI 포스팅 생성기 (fork)", project="1_블로그포스팅")
        return Rig(start=start, backend=be, cfg=make_cfg(
            target=Target("1_블로그포스팅", "네이버 블로그 AI 포스팅 생성기 (fork)")))

    def test_toast_dismissed_cycle_survives_and_sends(self):
        r = self.rig(dt(22, 40, **D))
        r.be.limit_text = CLAUDE_TOAST
        st = r.tick()
        self.assertEqual((st.reset_at, st.resume_at), (dt(23, 50, **D), dt(23, 52, **D)))
        r.clock.set(dt(22, 41, **D)); r.be.limit_text = ""        # 사용자가 토스트를 닫음
        r.run_until(dt(23, 51, 59, **D), step=30)
        self.assertEqual(r.be.sent, [])
        r.run_until(dt(23, 52, 30, **D), step=5)
        self.assertEqual(r.be.sent, ["계속"])

    def test_stale_toast_after_reset_ignored(self):
        r = self.rig(dt(23, 55, **D))
        r.be.limit_text = CLAUDE_TOAST
        r.tick()
        self.assertIsNone(r.store.cycle)


class TwoAppsTogether(unittest.TestCase):
    def test_each_app_resumes_at_its_own_time_once(self):
        tmp = Path(tempfile.mkdtemp(prefix="cac-two-"))
        clock = Clock(dt(22, 30, **D))
        lock = threading.Lock()
        codex_be = FakeBackend("점검 OCI 이전 준비", "AI STUDIO")
        claude_be = FakeBackend("네이버 블로그 AI 포스팅 생성기 (fork)", "1_블로그포스팅")
        codex_be.limit_text = CODEX_CARD
        claude_be.limit_text = CLAUDE_TOAST
        mk = lambda name, be, t: Engine(make_cfg(target=t), be, StateStore(tmp / f"state_{name}.json"),
                                        clock=clock, sleep=clock.sleep, net_check=lambda: True,
                                        name=name, action_lock=lock)
        e1 = mk("codex", codex_be, Target("AI STUDIO", "점검 OCI 이전 준비"))
        e2 = mk("claude", claude_be, Target("1_블로그포스팅", "네이버 블로그 AI 포스팅 생성기 (fork)"))
        sent_at = {}
        while clock.t < dt(23, 59, **D):
            clock.advance(10)
            for name, e, be in (("codex", e1, codex_be), ("claude", e2, claude_be)):
                if clock.t >= dt(22, 47, **D) and name == "codex":
                    be.limit_text = ""
                if clock.t >= dt(23, 50, **D) and name == "claude":
                    be.limit_text = ""
                e.tick()
                if be.sent and name not in sent_at:
                    sent_at[name] = clock.t
        self.assertEqual(len(codex_be.sent), 1)
        self.assertEqual(len(claude_be.sent), 1)
        self.assertEqual(sent_at["codex"].replace(second=0), dt(22, 49, **D))
        self.assertEqual(sent_at["claude"].replace(second=0), dt(23, 52, **D))


class Profiles(unittest.TestCase):
    def test_default_profiles(self):
        st = load_settings(Path(tempfile.mkdtemp()))
        names = {p.name: p for p in st.profiles}
        self.assertTrue(names["codex"].enabled and names["claude"].enabled)
        self.assertFalse(names["codex-myai"].enabled)
        self.assertEqual(names["claude"].cfg.process_names, ["claude"])
        self.assertEqual(names["codex"].cfg.target.conversation, "점검 OCI 이전 준비")

    def test_roundtrip_and_global_apply(self):
        h = Path(tempfile.mkdtemp())
        st = load_settings(h)
        st.apply_global("resume_delay_minutes", 3)
        st.set_target(st.profiles[1], "프로젝트", "대화")
        save_settings(st, h)
        st2 = load_settings(h)
        self.assertEqual(st2.profiles[0].cfg.resume_delay_minutes, 3.0)
        self.assertEqual(st2.profiles[1].cfg.target.label, "프로젝트 / 대화")

    def test_corrupt_config_recovers(self):
        h = Path(tempfile.mkdtemp())
        (h / "config.json").write_text("{bad", encoding="utf-8")
        st = load_settings(h)
        self.assertEqual(len(st.profiles), 3)
        self.assertTrue((h / "config.bad").exists())

    def test_legacy_single_target_migrated(self):
        h = Path(tempfile.mkdtemp())
        (h / "config.json").write_text('{"target": {"project": "P", "conversation": "C"}, "resume_delay_minutes": 5}',
                                       encoding="utf-8")
        st = load_settings(h)
        self.assertEqual(st.profiles[0].cfg.target.label, "P / C")
        self.assertEqual(st.profiles[0].cfg.resume_delay_minutes, 5.0)


if __name__ == "__main__":
    unittest.main()
