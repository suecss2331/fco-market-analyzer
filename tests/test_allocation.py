"""9·10단계 자금 배분 검사 (순 BP 에서만 매수, 한도는 총 운용 BP 기준)."""
import unittest

from fco.allocation import allocate


def c(key, price, sell, kind="buy_now"):
    return {"key": key, "kind": kind, "unit_price": price, "sell_expected": sell}


class Allocation(unittest.TestCase):
    def test_cap_per_card_and_remaining(self):
        plan = allocate([c("a", 30, 50), c("b", 70, 100)], total_bp=1000, cash_bp=1000, max_ratio=0.2, fee=0.2)
        a, b = plan["rows"]
        self.assertEqual(a["qty"], 6)            # 200 / 30 -> 6장
        self.assertEqual(a["total_cost"], 180)
        self.assertAlmostEqual(a["net_total"], 6 * (50 * 0.8 - 30))
        self.assertAlmostEqual(a["weight"], 0.18)
        self.assertEqual(a["remaining_after"], 820)
        self.assertEqual(b["qty"], 2)
        self.assertEqual(plan["remaining"], 820 - 140)
        self.assertEqual(plan["invested"], 320)

    def test_spending_limited_by_cash_but_cap_by_total(self):
        # 총 1000 (보유 매물 800 + 순 BP 200). 카드 한도는 200 이지만 쓸 수 있는 돈은 순 BP 200 뿐
        plan = allocate([c("a", 50, 80), c("b", 50, 80)], total_bp=1000, cash_bp=200, max_ratio=0.2, fee=0.2)
        self.assertEqual([r["qty"] for r in plan["rows"]], [4, 0])
        self.assertEqual(plan["remaining"], 0)
        self.assertEqual(plan["rows"][1]["note"], "순 BP 부족")

    def test_existing_holding_reduces_card_cap(self):
        plan = allocate([c("a", 50, 80)], total_bp=1000, cash_bp=1000, max_ratio=0.2, fee=0.2,
                        held_value={"a": 150})
        self.assertEqual(plan["rows"][0]["qty"], 1)   # 한도 200 - 보유 150 = 50
        plan2 = allocate([c("a", 50, 80)], total_bp=1000, cash_bp=1000, max_ratio=0.2, fee=0.2,
                         held_value={"a": 250})
        self.assertEqual(plan2["rows"][0]["qty"], 0)
        self.assertIn("이미 보유", plan2["rows"][0]["note"])

    def test_card_more_expensive_than_cap_gets_zero(self):
        plan = allocate([c("a", 300, 400)], total_bp=1000, cash_bp=1000, max_ratio=0.2, fee=0.2)
        self.assertEqual(plan["rows"][0]["qty"], 0)
        self.assertIn("한도", plan["rows"][0]["note"])

    def test_max_qty_per_card(self):
        plan = allocate([c("a", 10, 20)], total_bp=1000, cash_bp=1000, max_ratio=0.2, fee=0.2, max_qty=5)
        self.assertEqual(plan["rows"][0]["qty"], 5)
        self.assertIn("최대 수량", plan["rows"][0]["note"])

    def test_kinds_are_kept(self):
        plan = allocate([c("a", 10, 20), c("b", 10, 20, kind="target_order")], 1000, 1000, 0.2, 0.2)
        self.assertEqual([r["kind"] for r in plan["rows"]], ["buy_now", "target_order"])


class RankerPickAllocation(unittest.TestCase):
    def rp(self, key, price, sell, tr):
        return {"key": key, "unit_price": price, "sell_expected": sell, "transfers_2h": tr}

    def test_coupon_needed_and_volume_cap(self):
        from fco.allocation import allocate_rankerpick
        cands = [self.rp((1, 8), 100, 110, 12),   # 수수료 20%: 88-100 손해, 쿠폰 0%: +10 ≥ 5 → 쿠폰 매수
                 self.rp((2, 8), 100, 140, 40),   # 수수료 20%로도 +12 → 쿠폰 없이
                 self.rp((3, 8), 100, 103, 40)]   # 쿠폰 써도 +3 < 5 → 건너뜀
        r = allocate_rankerpick(cands, total_bp=10000, cash_bp=10000, max_ratio=0.2, fee=0.2, fee_coupon=0.0,
                                coupons=2, min_roi=0.05, max_qty=5, transfers_per_unit=5)
        a, b = r["rows"]
        self.assertTrue(a["coupon"])
        self.assertEqual(a["qty"], 2)                  # 거래 12건 → 2장, 쿠폰 2장
        self.assertFalse(b["coupon"])
        self.assertEqual(b["qty"], 5)                  # 카드당 최대 5장
        self.assertEqual(r["coupons_left"], 0)
        self.assertEqual(len(r["skipped"]), 1)
        self.assertAlmostEqual(r["remaining"], 10000 - 200 - 500)

    def test_no_coupons_left(self):
        from fco.allocation import allocate_rankerpick
        r = allocate_rankerpick([self.rp((1, 8), 100, 110, 50)], 10000, 10000, 0.2, 0.2, 0.0, 0, 0.05, 5, 5)
        self.assertEqual(r["rows"], [])
        self.assertIn("쿠폰", r["skipped"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
