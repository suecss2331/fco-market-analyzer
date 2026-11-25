"""8단계 Deal Score 검사."""
import unittest

from fco.deal import percentile, compute_deal_scores

CFG = {"weights": {"popularity": 30, "discount": 25, "weekend": 25, "stability": 20},
       "priceBandEdges": [100, 1000], "minGroupSize": 3,
       "riskPenalty": {"sharp_drop": 10, "persistent_decline": 15, "current_gap": 10},
       "popularityBlendTop200": 0.0, "popularityNormalization": "percentile"}
RATIO = dict(CFG, popularityNormalization="ratio_to_max")


def card(key, price=500, pop=0, users=0, disc=0.0, wk=0.0, cv=0.1, **kw):
    c = {"key": key, "current_price": price, "pop_score": pop, "users_top_universe": users,
         "discount_pct": disc, "premium_used_pct": wk, "weekly_status": "ok", "transient": 0,
         "l_cv": cv, "stale": 0, "risk_flags": "", "drop_alert": None, "buy_fair": price * 2,
         "roi_at_current_pct": 30.0, "price_days": 365}
    c.update(kw)
    return c


class Percentile(unittest.TestCase):
    def test_basic_and_ties(self):
        self.assertEqual(percentile({"a": 1, "b": 2, "c": 3}), {"a": 0, "b": 0.5, "c": 1})
        self.assertEqual(percentile({"a": 0, "b": 0, "c": 5}), {"a": 0, "b": 0, "c": 1})
        self.assertEqual(percentile({"a": 7}), {"a": 1.0})


class Deal(unittest.TestCase):
    def test_best_and_worst(self):
        cs = [card("best", pop=50, disc=20, wk=8, cv=0.05),
              card("mid", pop=10, disc=5, wk=2, cv=0.15),
              card("worst", pop=0, disc=-10, wk=0, cv=0.40)]
        r = compute_deal_scores(cs, CFG, fee=0.2)
        self.assertAlmostEqual(r["best"]["final"], 100)
        self.assertAlmostEqual(r["worst"]["final"], 0)
        self.assertAlmostEqual(r["mid"]["final"], 50)
        self.assertEqual(r["best"]["reliability_pct"], 100)

    def test_popularity_ratio_to_max(self):
        # 인기 점수 대부분 0, 소수만 1점/26점 -> 백분위면 1점과 26점이 거의 같지만 비율이면 1/26
        cs = [card("top", pop=26), card("one", pop=1)] + [card(f"z{i}", pop=0) for i in range(8)]
        pr = compute_deal_scores(cs, CFG, fee=0.2)
        rr = compute_deal_scores(cs, RATIO, fee=0.2)
        self.assertGreater(pr["one"]["parts"]["popularity"]["percentile"], 0.85)
        self.assertAlmostEqual(rr["one"]["parts"]["popularity"]["percentile"], 1 / 26)
        self.assertAlmostEqual(rr["top"]["parts"]["popularity"]["percentile"], 1.0)
        self.assertEqual(rr["z0"]["parts"]["popularity"]["percentile"], 0)

    def test_ratio_when_group_has_no_popular_card(self):
        cs = [card("a", pop=0), card("b", pop=0), card("c", pop=0)]
        rr = compute_deal_scores(cs, RATIO, fee=0.2)
        self.assertEqual(rr["a"]["parts"]["popularity"]["percentile"], 0)
        self.assertEqual(rr["a"]["reliability_pct"], 100)

    def test_missing_metric_is_reweighted_not_zero(self):
        cs = [card("a", pop=50, disc=20, cv=0.05, wk=None, transient=None),
              card("b", pop=10, disc=5, wk=2, cv=0.15), card("c", pop=0, disc=-10, wk=0, cv=0.4)]
        r = compute_deal_scores(cs, CFG, fee=0.2)
        self.assertIsNone(r["a"]["parts"]["weekend"]["points"])
        self.assertAlmostEqual(r["a"]["final"], 100)
        self.assertEqual(r["a"]["reliability_pct"], 75)

    def test_stale_card_has_no_stability(self):
        cs = [card("a", cv=0.0, stale=1), card("b", cv=0.1), card("c", cv=0.2)]
        r = compute_deal_scores(cs, CFG, fee=0.2)
        self.assertIsNone(r["a"]["parts"]["stability"]["points"])
        self.assertIn("가격 정체", r["a"]["warnings"])

    def test_risk_penalty(self):
        cs = [card("a", pop=50, disc=20, wk=8, cv=0.05, risk_flags="sharp_drop,current_gap"),
              card("b"), card("c")]
        r = compute_deal_scores(cs, CFG, fee=0.2)
        self.assertEqual(r["a"]["penalty"], 20)
        self.assertAlmostEqual(r["a"]["final"], r["a"]["base"] - 20)

    def test_warnings(self):
        cs = [card("a", transient=1, drop_alert=1, buy_fair=100, price=500, roi_at_current_pct=5.0),
              card("b"), card("c")]
        w = compute_deal_scores(cs, CFG, fee=0.2)["a"]["warnings"]
        for s in ("일시적 주말 차이", "인기 순위 급락", "현재가 > 매수 적정가", "예상 수익률 < 수수료율"):
            self.assertIn(s, w)

    def test_small_group_falls_back_to_all(self):
        cs = [card("x", price=50), card("a", price=500), card("b", price=600), card("c", price=700)]
        r = compute_deal_scores(cs, CFG, fee=0.2)
        self.assertEqual(r["x"]["group"], "전체")
        self.assertEqual(r["a"]["group"], "100~1000")


if __name__ == "__main__":
    unittest.main()
