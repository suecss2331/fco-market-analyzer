"""수수료 · 쿠폰 배정 검사."""
import unittest

from fco.coupons import assign_coupons, coupon_saving, effective_fee


class Fee(unittest.TestCase):
    def test_additive_discounts(self):
        self.assertAlmostEqual(effective_fee(0.40, 0.50), 0.20)            # PC방 30 + TOP CLASS 20
        self.assertAlmostEqual(effective_fee(0.40, 0.50, 0.30), 0.08)
        self.assertAlmostEqual(effective_fee(0.40, 0.30, 0.40), 0.12)      # FC STATS 예시: PC방 + 40% 쿠폰 = 12%
        self.assertAlmostEqual(effective_fee(0.40, 0.50, 0.50), 0.0)
        self.assertAlmostEqual(effective_fee(0.40, 0.50, 0.70), 0.0)       # 100% 넘으면 0 에서 멈춤
        self.assertAlmostEqual(effective_fee(0.40, 0.0), 0.40)

    def test_saving_is_capped(self):
        self.assertAlmostEqual(coupon_saving(100_000_000, 0.40, 0.50, 0.30), 100_000_000 * 0.12)
        # 50% 쿠폰이든 70% 쿠폰이든 남은 할인 여지(50%)까지만 아낀다
        self.assertAlmostEqual(coupon_saving(100_000_000, 0.40, 0.50, 0.70), 100_000_000 * 0.20)


class Assign(unittest.TestCase):
    def test_biggest_coupon_on_most_expensive_sale(self):
        sales = [{"key": "cheap", "unit_price": 100, "qty": 1}, {"key": "pricey", "unit_price": 1000, "qty": 1}]
        coupons = [{"discount": 0.30, "count": 1}, {"discount": 0.50, "count": 1}]
        r = assign_coupons(sales, coupons, base=0.40, other=0.50)
        by = {a["key"]: a for a in r["assignments"]}
        self.assertEqual(by["pricey"]["coupon"], 0.50)
        self.assertEqual(by["cheap"]["coupon"], 0.30)
        self.assertAlmostEqual(r["total_saving"], 1000 * 0.4 * 0.5 + 100 * 0.4 * 0.3)

    def test_units_are_separate_sales_and_leftovers_reported(self):
        sales = [{"key": "a", "unit_price": 500, "qty": 3}]
        coupons = [{"discount": 0.30, "count": 5}]
        r = assign_coupons(sales, coupons, base=0.40, other=0.50)
        self.assertEqual(len(r["assignments"]), 3)
        self.assertEqual(r["unused"], [{"discount": 0.30, "count": 2}])

    def test_no_value_when_fee_already_zero(self):
        sales = [{"key": "a", "unit_price": 500, "qty": 1}]
        r = assign_coupons(sales, [{"discount": 0.30, "count": 1}], base=0.40, other=1.0)
        self.assertEqual(r["assignments"], [])
        self.assertEqual(r["total_saving"], 0)

    def test_oversized_coupon_prefers_where_it_is_not_wasted(self):
        # 남은 할인 여지가 50% 인데 70% 쿠폰 1장, 30% 쿠폰 1장: 비싼 매물에 70%(실효 50%), 싼 매물에 30%
        sales = [{"key": "x", "unit_price": 1000, "qty": 1}, {"key": "y", "unit_price": 900, "qty": 1}]
        coupons = [{"discount": 0.70, "count": 1}, {"discount": 0.30, "count": 1}]
        r = assign_coupons(sales, coupons, base=0.40, other=0.50)
        by = {a["key"]: a for a in r["assignments"]}
        self.assertEqual(by["x"]["coupon"], 0.70)
        self.assertAlmostEqual(by["x"]["effective_discount"], 0.50)


if __name__ == "__main__":
    unittest.main()
