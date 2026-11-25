"""6단계 가격 안정성 검사."""
import unittest
from datetime import date, timedelta

from fco.stability import compute_stability

CFG = {"shortDays": 7, "longDays": 30, "minShortPoints": 5, "minLongPoints": 20,
       "sharpMovePct": 15, "sharpLookbackDays": 7, "trendFlatPctPerDay": 0.5,
       "persistentDeclinePct": -10, "staleDays": 5}
END = date(2026, 11, 22)


def series(days, fn, skip=()):
    start = END - timedelta(days=days - 1)
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        if d not in skip:
            out.append((d, fn(i)))
    return out


class Stability(unittest.TestCase):
    def test_constant_price(self):
        r = compute_stability(series(40, lambda i: 100), END, CFG)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["long"]["cv"], 0)
        self.assertEqual(r["long"]["trend"], "보합")
        self.assertEqual(r["long"]["coverage"], 1.0)
        self.assertFalse(r["sharp_drop"])
        self.assertFalse(r["persistent_decline"])

    def test_cv_uses_population_std(self):
        r = compute_stability(series(40, lambda i: 100 if i % 2 else 110), END, CFG)
        self.assertAlmostEqual(r["long"]["mean"], 105)
        self.assertAlmostEqual(r["long"]["std"], 5)
        self.assertAlmostEqual(r["long"]["cv"], 5 / 105)
        self.assertAlmostEqual(r["long"]["range_pct"], 10 / 105 * 100)

    def test_steady_decline_is_persistent(self):
        r = compute_stability(series(40, lambda i: 1000 * 0.99 ** i), END, CFG)
        self.assertEqual(r["long"]["trend"], "하락")
        self.assertAlmostEqual(r["long"]["trend_pct_per_day"], -1.0, places=6)
        self.assertAlmostEqual(r["long"]["change_pct"], (0.99 ** 29 - 1) * 100)
        self.assertTrue(r["persistent_decline"])
        self.assertFalse(r["sharp_drop"])

    def test_one_day_crash(self):
        r = compute_stability(series(40, lambda i: 50 if i >= 37 else 100), END, CFG)
        self.assertTrue(r["sharp_drop"])
        self.assertAlmostEqual(r["max_daily_drop_pct"], -50)
        self.assertFalse(r["sharp_rise"])

    def test_old_crash_outside_lookback_not_flagged(self):
        r = compute_stability(series(40, lambda i: 50 if i >= 20 else 100), END, CFG)
        self.assertFalse(r["sharp_drop"])

    def test_gaps_reduce_coverage(self):
        skip = {END - timedelta(days=k) for k in (3, 4, 5, 10)}
        r = compute_stability(series(40, lambda i: 100, skip=skip), END, CFG)
        self.assertAlmostEqual(r["long"]["coverage"], 26 / 30)
        self.assertEqual(r["long"]["max_gap_days"], 3)
        self.assertEqual(r["short"]["n"], 4)
        self.assertEqual(r["status"], "partial")  # 7일 기록이 4일뿐 -> 단기 지표 계산 불가

    def test_stale_price_run(self):
        r = compute_stability(series(40, lambda i: 120 if i >= 34 else 100 + i), END, CFG)
        self.assertEqual(r["flat_run_days"], 6)
        self.assertTrue(r["stale"])
        r2 = compute_stability(series(40, lambda i: 100 + i), END, CFG)
        self.assertEqual(r2["flat_run_days"], 1)
        self.assertFalse(r2["stale"])

    def test_insufficient(self):
        r = compute_stability(series(4, lambda i: 100), END, CFG)
        self.assertEqual(r["status"], "insufficient")
        self.assertIsNone(r["long"]["cv"])
        self.assertIn("데이터 부족", r["note"])


if __name__ == "__main__":
    unittest.main()
