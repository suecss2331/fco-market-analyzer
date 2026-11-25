"""7단계 매수 적정가·수익률 검사."""
import unittest

from fco.pricing import compute_pricing, discount_factor, net_profit, roi, break_even_sell, max_buy_for_roi

CFG = {"basis": "mean", "discountFactorMin": 0.90, "discountFactorMax": 0.98,
       "cvForMaxFactor": 0.05, "cvForMinFactor": 0.30, "minTargetRoi": 0.05}
FEE = 0.20


def weekly(**kw):
    base = {"status": "ok", "wd_mean": 100.0, "wd_median": 100.0, "premium_pct": 5.0,
            "weekly_premium_median_pct": 3.0, "transient": 0, "weeks_total": 8, "current_price": 90}
    base.update(kw)
    return base


class FeeMath(unittest.TestCase):
    def test_formulas(self):
        self.assertAlmostEqual(net_profit(100, 150, FEE), 20)
        self.assertAlmostEqual(roi(100, 150, FEE), 20)
        self.assertAlmostEqual(break_even_sell(100, FEE), 125)
        self.assertAlmostEqual(max_buy_for_roi(150, FEE, 0.05), 150 * 0.8 / 1.05)


class Factor(unittest.TestCase):
    def test_linear_mapping_and_clamp(self):
        self.assertAlmostEqual(discount_factor(0.05, CFG), 0.98)
        self.assertAlmostEqual(discount_factor(0.30, CFG), 0.90)
        self.assertAlmostEqual(discount_factor(0.175, CFG), 0.94)
        self.assertAlmostEqual(discount_factor(0.0, CFG), 0.98)
        self.assertAlmostEqual(discount_factor(0.9, CFG), 0.90)
        self.assertIsNone(discount_factor(None, CFG))


class Pricing(unittest.TestCase):
    def test_expected_sell_uses_smaller_premium(self):
        r = compute_pricing(weekly(), {"l_cv": 0.05}, CFG, FEE)
        self.assertAlmostEqual(r["premium_used_pct"], 3.0)
        self.assertAlmostEqual(r["sell_expected"], 103)
        self.assertAlmostEqual(r["buy_fair"], 98)

    def test_no_premium_when_transient_or_negative(self):
        self.assertEqual(compute_pricing(weekly(transient=1), {"l_cv": 0.05}, CFG, FEE)["premium_used_pct"], 0)
        self.assertEqual(compute_pricing(weekly(premium_pct=-2), {"l_cv": 0.05}, CFG, FEE)["premium_used_pct"], 0)
        self.assertEqual(compute_pricing(weekly(transient=None), {"l_cv": 0.05}, CFG, FEE)["premium_used_pct"], 0)

    def test_profit_at_current_and_target(self):
        r = compute_pricing(weekly(current_price=90), {"l_cv": 0.05}, CFG, FEE)
        self.assertAlmostEqual(r["fee_amount"], 103 * 0.2)
        self.assertAlmostEqual(r["net_at_current"], 103 * 0.8 - 90)
        self.assertAlmostEqual(r["roi_at_current_pct"], (103 * 0.8 - 90) / 90 * 100)
        self.assertAlmostEqual(r["break_even_sell_at_current"], 90 / 0.8)
        mb = 103 * 0.8 / 1.05
        self.assertAlmostEqual(r["max_buy_for_target"], mb)
        self.assertAlmostEqual(r["target_buy"], min(98, mb))
        self.assertAlmostEqual(r["required_discount_pct"], (100 - mb) / 100 * 100)
        self.assertEqual(r["verdict"], "wait")  # 현재가 90 > 목표 매수가 78.5

    def test_buy_now(self):
        r = compute_pricing(weekly(current_price=70), {"l_cv": 0.05}, CFG, FEE)
        self.assertEqual(r["verdict"], "buy_now")
        self.assertGreaterEqual(r["roi_at_current_pct"], 5)

    def test_buy_now_blocked_by_stability_flags(self):
        for flag in ("sharp_drop", "persistent_decline", "stale"):
            r = compute_pricing(weekly(current_price=70), {"l_cv": 0.05, flag: 1}, CFG, FEE)
            self.assertEqual(r["verdict"], "risky", flag)
            self.assertIn(flag, r["risk_flags"])

    def test_today_gap_vs_last_daily_is_risky(self):
        s = {"l_cv": 0.05, "last_daily_price": 100, "sharp_move_pct": 15}
        r = compute_pricing(weekly(current_price=70), s, CFG, FEE)
        self.assertEqual(r["verdict"], "risky")
        self.assertIn("current_gap", r["risk_flags"])
        self.assertAlmostEqual(r["current_vs_last_daily_pct"], -30)
        s2 = dict(s, last_daily_price=75)
        self.assertEqual(compute_pricing(weekly(current_price=70), s2, CFG, FEE)["verdict"], "buy_now")

    def test_median_basis(self):
        cfg = dict(CFG, basis="min")
        r = compute_pricing(weekly(wd_median=95.0), {"l_cv": 0.05}, cfg, FEE)
        self.assertAlmostEqual(r["basis_price"], 95)

    def test_insufficient_inputs(self):
        r = compute_pricing(weekly(status="insufficient", wd_mean=None), {"l_cv": 0.1}, CFG, FEE)
        self.assertEqual(r["verdict"], "insufficient")
        self.assertIsNone(r["buy_fair"])
        r2 = compute_pricing(weekly(), {"l_cv": None}, CFG, FEE)
        self.assertEqual(r2["verdict"], "insufficient")


if __name__ == "__main__":
    unittest.main()
