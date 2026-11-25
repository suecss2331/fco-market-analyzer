"""탭3 장기투자 점수 (상승 추세형 · 저평가 선점형)."""
import unittest
from datetime import date, timedelta

from fco.longterm import (grade, metrics, momentum, pop_fraction, pop_with_change, value, volume_fraction,
                          weighted)

MW = {"r7": 25, "r30": 25, "persist": 20, "pop": 15, "volume": 10, "risk": 5}
VW = {"pop": 25, "discount": 25, "unpriced": 20, "stability": 15, "volume": 10, "rebound": 5}
T = date(2026, 11, 25)


def series(prices):
    """마지막이 어제인 일별 가격."""
    n = len(prices)
    return [(T - timedelta(days=n - i), p) for i, p in enumerate(prices)]


class Metrics(unittest.TestCase):
    def test_changes_and_discount(self):
        s = series([100] * 30 + [100 + i for i in range(1, 31)])   # 60일, 마지막 30일 꾸준히 상승
        m = metrics(s, 130, T)
        self.assertAlmostEqual(m["r30"], 130 / 100 - 1)
        self.assertAlmostEqual(m["r7"], 130 / 123 - 1)
        self.assertEqual(m["disc"], 0)                               # 현재가가 최고가
        self.assertGreater(m["slope30"], 0)
        self.assertEqual(m["up_weeks"], 4)

    def test_short_history(self):
        m = metrics(series([100] * 10), 100, T)
        self.assertIsNone(m["r30"])                                  # 30일 기록이 없으면 만들지 않음


class Scores(unittest.TestCase):
    def test_weighted_renormalizes_missing(self):
        s, d = weighted({"a": 1.0, "b": None}, {"a": 30, "b": 70})
        self.assertAlmostEqual(s, 100)                               # 없는 항목은 빼고 환산
        self.assertIsNone(d["b"]["points"])

    def test_grades(self):
        self.assertEqual([grade(x) for x in (90, 80, 70, 55, 10, None)], ["S", "A", "B", "C", "D", "-"])

    def test_pop(self):
        self.assertEqual(pop_fraction(1), 1.0)
        self.assertGreater(pop_fraction(10), pop_fraction(11))      # 10위 이내 추가 가점
        self.assertEqual(pop_fraction(None), 0.15)
        self.assertIn("하락", pop_with_change(8, 3)[1])

    def test_volume_unknown(self):
        self.assertIsNone(volume_fraction(None))
        self.assertIsNone(volume_fraction({"transfers_2h": None}))
        self.assertGreater(volume_fraction({"transfers_2h": 20, "buy_levels": [[1, 5]], "sell_levels": []}), 0.9)

    def test_momentum_steady_beats_spike(self):
        steady = metrics(series([100] * 30 + [100 + i for i in range(1, 31)]), 131, T)
        steady["cur"] = 131
        spike = metrics(series([100] * 53 + [100, 100, 100, 100, 100, 130, 160]), 165, T)
        spike["cur"] = 165
        a = momentum(steady, (1.0, None), None, {}, MW)
        b = momentum(spike, (1.0, None), None, {"sharp_rise": 1}, MW)
        self.assertGreater(a["score"], b["score"])                   # 단기 급등보다 꾸준한 상승
        self.assertTrue(b["overheated"] and b["wait"])               # 급등이면 매수 대기
        self.assertLessEqual(a["fair"], 131 * 0.97 + 1e-9)          # 3~5% 아래 대기가
        self.assertGreaterEqual(a["fair"], 131 * 0.95 - 1e-9)

    def test_value_needs_popularity_not_just_drop(self):
        base = [200] * 30 + [150] * 30                               # 고점 200 → 150 에서 횡보
        m = metrics(series(base), 150, T)
        m["cur"] = 150
        popular = value(m, (1.0, None), None, {}, False, VW)
        unpopular = value(m, (0.15, None), None, {}, False, VW)
        self.assertGreater(popular["score"], unpopular["score"] + 20)
        self.assertAlmostEqual(popular["fair"], 150)                 # 지지선 150 = 중앙값 → 현재가 이하
        decline = value(m, (0.6, "인기 순위 하락 (3→9위)"), None, {"persistent_decline": 1}, False, VW)
        self.assertIn("지속 하락 중", decline["risks"])


if __name__ == "__main__":
    unittest.main()
