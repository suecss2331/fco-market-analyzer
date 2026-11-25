"""10단계 보유 매물 평가 · 추천 검증 검사."""
import unittest
from datetime import date

from fco.portfolio import flip_roi, limit_metrics, lot_metrics, realized
from fco.validation import evaluate_recommendation


class Lot(unittest.TestCase):
    def test_unrealized_after_fee(self):
        m = lot_metrics(qty=2, buy_price=100, current=150, fee=0.2, sell_expected=160, prev_price=140)
        self.assertEqual(m["cost"], 200)
        self.assertEqual(m["market_value"], 300)
        self.assertAlmostEqual(m["liquidation_value"], 240)        # 150 × 0.8 × 2
        self.assertAlmostEqual(m["unrealized"], 40)
        self.assertAlmostEqual(m["unrealized_roi_pct"], 20)
        self.assertAlmostEqual(m["break_even_sell"], 125)
        self.assertAlmostEqual(m["profit_at_expected"], (160 * 0.8 - 100) * 2)
        self.assertAlmostEqual(m["change_vs_prev_pct"], (150 - 140) / 140 * 100)

    def test_no_price(self):
        m = lot_metrics(qty=1, buy_price=100, current=None, fee=0.2, sell_expected=None, prev_price=None)
        self.assertIsNone(m["market_value"])
        self.assertIsNone(m["unrealized"])
        self.assertEqual(m["break_even_sell"], 125)

    def test_realized(self):
        r = realized(qty=3, buy_price=100, sell_price=150, fee=0.2)
        self.assertAlmostEqual(r["net_profit"], (150 * 0.8 - 100) * 3)
        self.assertAlmostEqual(r["roi_pct"], 20)
        self.assertAlmostEqual(r["fee_paid"], 150 * 0.2 * 3)


class Limits(unittest.TestCase):
    def test_kaka_like(self):
        # 1억 9,800만에 산 카드, 상한가 2억 3,700만, 하한가 1억 9,400만, 수수료 20%, 100% 쿠폰 → 0%
        m = limit_metrics(1, 198e6, 237e6, 194e6, 0.2, 0.0)
        self.assertAlmostEqual(m["profit_at_upper"], 237e6 * 0.8 - 198e6)       # −840만
        self.assertAlmostEqual(m["profit_at_upper_coupon"], 237e6 - 198e6)      # +3,900만
        self.assertAlmostEqual(m["profit_at_lower"], 194e6 * 0.8 - 198e6)
        self.assertAlmostEqual(m["upper_gap_to_break_even"], 198e6 / 0.8 - 237e6)
        self.assertAlmostEqual(m["upper_gap_to_break_even_coupon"], 198e6 - 237e6)

    def test_no_market(self):
        m = limit_metrics(1, 100, None, None, 0.2, None)
        self.assertIsNone(m["profit_at_upper"])
        self.assertIsNone(m["profit_at_lower"])

    def test_flip(self):
        self.assertAlmostEqual(flip_roi(90, 110, 0.2), (88 - 90) / 90)   # 수수료 20% 면 같은 시세 왕복은 손해
        self.assertAlmostEqual(flip_roi(90, 110, 0.0), 20 / 90)


class RankerPickValidation(unittest.TestCase):
    # 2026-11-25(수) 추천 → 11/28(토)·11/29(일) 평균에 매도
    daily = {date(2026, 11, d): p for d, p in [(25, 100), (26, 98), (27, 99), (28, 110), (29, 120), (30, 105)]}

    def test_graded_after_sunday(self):
        from fco.validation import evaluate_rankerpick
        rec = {"day": "2026-11-25", "current_price": 100}
        r = evaluate_rankerpick(rec, self.daily, fee=0.2, fee_coupon=0.0, today=date(2026, 11, 30))
        self.assertEqual(r["status"], "evaluated")
        self.assertEqual(r["sell_weekend"], "2026-11-28")
        self.assertAlmostEqual(r["exit_price"], 115)
        self.assertAlmostEqual(r["roi_pct"], (115 * 0.8 / 100 - 1) * 100)   # −8%
        self.assertAlmostEqual(r["roi_coupon_pct"], 15)

    def test_pending_through_sunday(self):
        from fco.validation import evaluate_rankerpick
        r = evaluate_rankerpick({"day": "2026-11-25", "current_price": 100}, self.daily, 0.2, None, date(2026, 11, 29))
        self.assertEqual(r["status"], "pending")

    def test_saturday_rec_uses_next_weekend(self):
        from fco.validation import next_weekend
        self.assertEqual(next_weekend(date(2026, 11, 28)), (date(2026, 12, 5), date(2026, 12, 6)))


class Validation(unittest.TestCase):
    daily = {date(2026, 11, d): p for d, p in [(24, 100), (25, 95), (26, 90), (27, 92), (28, 120), (29, 130), (30, 99)]}

    def test_buy_now_sold_next_weekend(self):
        rec = {"day": "2026-11-24", "verdict": "buy_now", "current_price": 100, "target_buy": 105}
        r = evaluate_recommendation(rec, self.daily, fee=0.2, horizon=7, today=date(2026, 12, 2))
        self.assertEqual(r["status"], "evaluated")
        self.assertEqual(r["entry_price"], 100)
        self.assertAlmostEqual(r["exit_price"], 125)          # 11/28 토 120, 11/29 일 130 평균
        self.assertAlmostEqual(r["roi_pct"], (125 * 0.8 / 100 - 1) * 100)

    def test_wait_fills_only_if_target_reached(self):
        rec = {"day": "2026-11-24", "verdict": "wait", "current_price": 100, "target_buy": 91}
        r = evaluate_recommendation(rec, self.daily, fee=0.2, horizon=7, today=date(2026, 12, 2))
        self.assertEqual(r["entry_price"], 91)
        self.assertEqual(r["filled_on"], "2026-11-26")
        rec2 = dict(rec, target_buy=80)
        r2 = evaluate_recommendation(rec2, self.daily, fee=0.2, horizon=7, today=date(2026, 12, 2))
        self.assertEqual(r2["status"], "not_filled")
        self.assertIsNone(r2["roi_pct"])

    def test_pending_until_horizon_passes(self):
        rec = {"day": "2026-11-24", "verdict": "buy_now", "current_price": 100, "target_buy": 105}
        r = evaluate_recommendation(rec, self.daily, fee=0.2, horizon=7, today=date(2026, 11, 27))
        self.assertEqual(r["status"], "pending")

    def test_no_weekend_prices(self):
        rec = {"day": "2026-11-24", "verdict": "buy_now", "current_price": 100, "target_buy": 105}
        d = {k: v for k, v in self.daily.items() if k.weekday() < 5}
        r = evaluate_recommendation(rec, d, fee=0.2, horizon=7, today=date(2026, 12, 2))
        self.assertEqual(r["status"], "no_data")


if __name__ == "__main__":
    unittest.main()
