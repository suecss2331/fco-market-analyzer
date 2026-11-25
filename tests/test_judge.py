"""매수 · 관망 · 매도 판단."""
import unittest

from fco.judge import judge_buy, judge_sell


class Buy(unittest.TestCase):
    def test_buy_without_coupon(self):
        j = judge_buy(100, 140, 0.2, 0.0, 0.05, True)       # 140 × 0.8 = 112 → +12%
        self.assertEqual(j["verdict"], "매수")
        self.assertAlmostEqual(j["buy_fair"], 112 / 1.05)
        self.assertAlmostEqual(j["net"], 12)

    def test_buy_only_with_coupon(self):
        j = judge_buy(100, 110, 0.2, 0.0, 0.05, True)       # 수수료면 −12%, 쿠폰이면 +10%
        self.assertEqual(j["verdict"], "매수")
        self.assertIn("쿠폰", j["reason"])

    def test_hold_reasons(self):
        self.assertEqual(judge_buy(100, 103, 0.2, 0.0, 0.05, True)["verdict"], "관망")   # 쿠폰 써도 +3%
        j = judge_buy(100, 140, 0.2, None, 0.05, False, "2시간 거래 2건 (5건 미만)")
        self.assertEqual((j["verdict"], j["reason"]), ("관망", "2시간 거래 2건 (5건 미만)"))
        self.assertEqual(judge_buy(None, 140, 0.2, None, 0.05, True)["reason"], "현재가 없음")


class Sell(unittest.TestCase):
    def test_sell_at_upper(self):
        j = judge_sell(100, 140, 0.2, None, 0.05)           # 목표 131.25
        self.assertEqual(j["verdict"], "매도")
        self.assertAlmostEqual(j["target_sell"], 131.25)

    def test_sell_with_coupon(self):
        j = judge_sell(198e6, 237e6, 0.2, 0.0, 0.05)        # 카카: 수수료면 목표 2.6억, 쿠폰이면 2.079억
        self.assertEqual(j["verdict"], "매도")
        self.assertIn("쿠폰", j["reason"])

    def test_sell_warns_without_buyers(self):
        j = judge_sell(100, 140, 0.2, None, 0.05, buy_at_upper=0)
        self.assertEqual(j["verdict"], "매도")
        self.assertIn("대기 0건", j["reason"])

    def test_hold(self):
        j = judge_sell(650e6, 559e6, 0.2, 0.0, 0.05)
        self.assertEqual(j["verdict"], "관망")
        self.assertIsNone(judge_sell(100, None, 0.2, None, 0.05)["target_sell_coupon"])


if __name__ == "__main__":
    unittest.main()
