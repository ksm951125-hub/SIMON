import unittest
from datetime import timedelta
from helpers import dt
from codex_auto_continue.timeparse import parse_reset_time
from codex_auto_continue.fmt import fmt_clock, fmt_remaining


def P(text, now):
    r = parse_reset_time(text, now)
    return r.reset_at if r else None


class TimeParse(unittest.TestCase):
    def test_sample_message(self):
        t = "요청 한도는 오전 11:35에 재설정됩니다."
        self.assertEqual(P(t, dt(9, 0)), dt(11, 35))
        self.assertEqual(P(t, dt(9, 0)) + timedelta(minutes=2), dt(11, 37))

    def test_am_pm(self):
        self.assertEqual(P("오후 3:05에 재설정", dt(10)), dt(15, 5))
        self.assertEqual(P("오전 12:10에 재설정", dt(23, 0)), dt(0, 10, day=22))   # 자정
        self.assertEqual(P("오후 12:30에 재설정", dt(10)), dt(12, 30))              # 정오
        self.assertEqual(P("오전 12:00", dt(22)), dt(0, 0, day=22))
        self.assertEqual(P("오후 11:59", dt(10)), dt(23, 59))

    def test_korean_and_english_forms(self):
        self.assertEqual(P("오후 3시 5분에 초기화", dt(10)), dt(15, 5))
        self.assertEqual(P("오전 11시에 초기화", dt(9)), dt(11, 0))
        self.assertEqual(P("resets at 11:35 AM", dt(9)), dt(11, 35))
        self.assertEqual(P("resets at 3:05 PM", dt(9)), dt(15, 5))
        self.assertEqual(P("재설정 17:20", dt(9)), dt(17, 20))
        self.assertEqual(P("오전 11：35 재설정", dt(9)), dt(11, 35))              # 전각 콜론

    def test_date_rollover(self):
        # 23:50 에 '오전 12:10' → 다음 날
        self.assertEqual(P("오전 12:10에 재설정", dt(23, 50)), dt(0, 10, day=22))
        # 이미 지난 시각(3시간 초과)은 다음 날
        self.assertEqual(P("오전 9:00에 재설정", dt(14, 30)), dt(9, 0, day=22))
        # 방금 지난 시각(유예 이내)은 오늘 유지 → 제한이 남아 있는 상황
        self.assertEqual(P("오전 11:35에 재설정", dt(11, 40)), dt(11, 35))
        # 월말 넘김
        self.assertEqual(P("오전 1:00에 재설정", dt(23, 0, day=31)), dt(1, 0, day=1, month=11))

    def test_explicit_dates(self):
        self.assertEqual(P("내일 오전 9:00에 재설정", dt(20)), dt(9, 0, day=22))
        self.assertEqual(P("모레 오전 9:00", dt(20)), dt(9, 0, day=23))
        self.assertEqual(P("10월 25일 오전 9:00에 재설정", dt(20)), dt(9, 0, day=25))
        self.assertEqual(P("Oct 25 at 9:00 AM resets", dt(20)), dt(9, 0, day=25))
        self.assertEqual(P("2026-10-26 14:00 재설정", dt(20)), dt(14, 0, day=26))
        # 연말에서 내년 1월
        self.assertEqual(P("1월 2일 오전 9:00", dt(10, day=30, month=12)),
                         dt(9, 0, day=2, month=1).replace(year=2027))
        self.assertIsNone(P("2월 30일 오전 9:00", dt(10)))

    def test_relative(self):
        r = parse_reset_time("2시간 15분 후에 재설정됩니다", dt(10, 0))
        self.assertEqual((r.reset_at, r.kind), (dt(12, 15), "relative"))
        self.assertEqual(P("45분 후에 재설정", dt(10, 0)), dt(10, 45))
        self.assertEqual(P("resets in 1 hour 30 minutes", dt(10, 0)), dt(11, 30))

    def test_invalid(self):
        self.assertIsNone(P("아무 시간도 없음", dt(10)))
        self.assertIsNone(P("오전 25:99", dt(10)))
        self.assertIsNone(P("", dt(10)))

    def test_timezone_conversion(self):
        from datetime import datetime, timezone
        utc_now = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)      # = KST 11:00
        self.assertEqual(P("오전 11:35", utc_now), dt(11, 35))

    def test_fmt(self):
        self.assertEqual(fmt_clock(dt(11, 35)), "오전 11:35")
        self.assertEqual(fmt_clock(dt(0, 5)), "오전 12:05")
        self.assertEqual(fmt_clock(dt(15, 0)), "오후 3:00")
        self.assertEqual(fmt_clock(dt(9, 0, day=22), now=dt(20)), "내일 오전 9:00")
        self.assertEqual(fmt_remaining(timedelta(hours=4, minutes=12, seconds=30)), "4시간 12분")
        self.assertEqual(fmt_remaining(timedelta(seconds=20)), "1분 미만")
        self.assertEqual(fmt_remaining(timedelta(seconds=-1)), "도달")


if __name__ == "__main__":
    unittest.main()
