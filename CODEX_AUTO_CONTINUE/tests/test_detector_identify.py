import unittest
from helpers import dt
from codex_auto_continue.detector import detect_limit
from codex_auto_continue.identify import identify
from codex_auto_continue.models import Snapshot, Target

SAMPLE = "Codex 및 Work 사용량을 모두 사용했습니다. 요청 한도는 오전 11:35에 재설정됩니다."


class Detector(unittest.TestCase):
    def test_single_fragment(self):
        d = detect_limit(["안녕", SAMPLE], dt(9))
        self.assertEqual(d.reset.reset_at, dt(11, 35))

    def test_split_fragments(self):
        d = detect_limit(["Codex 및 Work 사용량을 모두 사용했습니다.", "요청 한도는 오전 11:35에", "재설정됩니다."], dt(9))
        self.assertIsNotNone(d.reset)
        self.assertEqual(d.reset.reset_at, dt(11, 35))

    def test_other_timestamps_not_mistaken(self):
        # 메시지 타임스탬프(오전 10:12)가 이웃 조각에 있어도 재설정 문장의 시각만 읽는다
        d = detect_limit(["오전 10:12", "Codex 및 Work 사용량을 모두 사용했습니다.", "요청 한도는 오후 3:05에 재설정됩니다."], dt(9))
        self.assertEqual(d.reset.reset_at, dt(15, 5))

    def test_limit_without_time(self):
        d = detect_limit(["사용량을 모두 사용했습니다."], dt(9))
        self.assertIsNotNone(d)
        self.assertIsNone(d.reset)

    def test_no_limit(self):
        self.assertIsNone(detect_limit(["테스트를 실행했습니다.", "오전 11:35 회의"], dt(9)))
        self.assertIsNone(detect_limit([], dt(9)))

    def test_long_prose_ignored(self):
        self.assertIsNone(detect_limit(["요청 한도 " + "가" * 500], dt(9)))

    def test_english(self):
        d = detect_limit(["You've hit your usage limit. Your limit resets at 11:35 AM."], dt(9))
        self.assertEqual(d.reset.reset_at, dt(11, 35))

    def test_picks_latest(self):
        d = detect_limit([SAMPLE.replace("11:35", "9:00"), "중간 내용", SAMPLE.replace("11:35", "2:10").replace("오전", "오후")], dt(8))
        self.assertEqual(d.reset.reset_at, dt(14, 10))


def snap(**kw):
    base = dict(found=True, conversation_candidates=["MY AI OFFICE 구축하기"], project_candidates=["MY AI OFFICE 구축하기"])
    base.update(kw)
    return Snapshot(**base)


class Identify(unittest.TestCase):
    T = Target("MY AI OFFICE 구축하기", "MY AI OFFICE 구축하기")

    def test_ok(self):
        self.assertTrue(identify(snap(), self.T).ok)

    def test_normalization(self):
        self.assertTrue(identify(snap(conversation_candidates=["my ai  office 구축하기"]), self.T).ok)

    def test_other_conversation(self):
        self.assertFalse(identify(snap(conversation_candidates=["다른 대화"]), self.T).ok)

    def test_no_candidates(self):
        self.assertFalse(identify(snap(conversation_candidates=[]), self.T).ok)

    def test_not_found(self):
        self.assertFalse(identify(snap(found=False), self.T).ok)

    def test_exact_vs_contains(self):
        s = snap(conversation_candidates=["MY AI OFFICE 구축하기 v2"])
        self.assertFalse(identify(s, self.T).ok)
        self.assertTrue(identify(s, self.T, mode="contains").ok)

    def test_project_required(self):
        t = Target("프로젝트X", "대화Y")
        s = snap(conversation_candidates=["대화Y"], project_candidates=["다른 프로젝트"])
        self.assertFalse(identify(s, t).ok)
        self.assertTrue(identify(s, t, require_project=False).ok)
        self.assertTrue(identify(snap(conversation_candidates=["대화Y"], project_candidates=["프로젝트X"]), t).ok)

    def test_empty_target(self):
        self.assertFalse(identify(snap(), Target("", "")).ok)


if __name__ == "__main__":
    unittest.main()
