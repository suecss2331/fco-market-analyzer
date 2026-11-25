"""4단계 평일·주말 계산 검사.  실행: python -m unittest discover -s tests -t ."""
import unittest
from datetime import date, timedelta

from fco.weekly import compute_weekly

CFG = {
    "windowDays": 28, "minWeekdayPoints": 10, "minWeekendPoints": 4,
    "repetitionWeeks": 8, "minRepetitionWeeks": 4,
    "transientPositiveRatio": 0.5, "weeklyPositiveMinPct": 0.5,
}
END = date(2026, 11, 22)  # 일요일


def series(days: int, fn):
    start = END - timedelta(days=days - 1)
    return [(start + timedelta(days=i), fn(start + timedelta(days=i), i)) for i in range(days)]


class Weekly(unittest.TestCase):
    def test_flat_with_weekend_premium(self):
        pts = series(90, lambda d, i: 110 if d.weekday() >= 5 else 100)
        r = compute_weekly(pts, END, CFG, current_price=95)
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["wd_mean"], 100)
        self.assertAlmostEqual(r["we_median"], 110)
        self.assertAlmostEqual(r["premium_pct"], 10.0)
        self.assertEqual(r["wd_n"], 20)
        self.assertEqual(r["we_n"], 8)
        self.assertEqual(r["weeks_positive"], r["weeks_total"])
        self.assertGreaterEqual(r["weeks_total"], 7)
        self.assertAlmostEqual(r["discount_pct"], 5.0)
        self.assertFalse(r["transient"])

    def test_insufficient_history(self):
        pts = series(9, lambda d, i: 100)
        r = compute_weekly(pts, END, CFG, current_price=100)
        self.assertEqual(r["status"], "insufficient")
        self.assertIsNone(r["premium_pct"])
        self.assertIn("평일", r["note"])

    def test_rising_trend_is_not_counted_as_weekend_effect(self):
        # 매일 1%씩 오르기만 하는 카드: 단순 평균은 주말이 높아 보이지만 주별 비교에서는 0
        pts = series(90, lambda d, i: 1000 * (1.01 ** i))
        r = compute_weekly(pts, END, CFG, current_price=None)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["weeks_positive"], 0)
        self.assertLess(abs(r["weekly_premium_median_pct"]), 0.5)
        self.assertIsNone(r["discount_pct"])

    def test_one_off_spike_is_transient(self):
        spike_sat = END - timedelta(days=8)  # 지난주 토요일
        def fn(d, i):
            if d in (spike_sat, spike_sat + timedelta(days=1)):
                return 200
            return 100
        r = compute_weekly(series(90, fn), END, CFG, current_price=100)
        self.assertGreater(r["premium_pct"], 0)
        self.assertEqual(r["weeks_positive"], 1)
        self.assertTrue(r["transient"])

    def test_window_uses_only_recent_days(self):
        # 오래전 가격은 높았지만 최근 28일은 100 고정
        pts = series(120, lambda d, i: 500 if i < 60 else 100)
        r = compute_weekly(pts, END, CFG, current_price=100)
        self.assertAlmostEqual(r["wd_mean"], 100)
        self.assertEqual(r["window_start"], (END - timedelta(days=27)).isoformat())


if __name__ == "__main__":
    unittest.main()
