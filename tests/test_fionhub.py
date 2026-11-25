"""fionhub __data.json 해석 검사 (2026-11-24 실제 응답 형태)."""
import json
import unittest

from fco.fionhub import NotFound, parse_market, unflatten

KAKA_NODE = [{"marketPrice": 1}, {"price": 2, "lowerLimit": 3, "upperLimit": 4, "available": 5, "buyLevels": 6,
             "sellLevels": 18, "completedTransfers": 19, "observedAt": 20},
             "215000000", "194000000", "237000000", True, [7, 9, 12, 15, 17], {"count": 8, "price": 4}, 7,
             {"count": 10, "price": 11}, 1, "222000000", {"count": 13, "price": 14}, 2, "196000000",
             {"count": 10, "price": 16}, "195000000", {"count": 13, "price": 3}, [], 39,
             ["Date", "2026-11-24T11:35:34.694Z"]]


class Parse(unittest.TestCase):
    def test_unflatten_shared_refs(self):
        v = unflatten(KAKA_NODE)
        self.assertEqual(v["marketPrice"]["upperLimit"], "237000000")
        self.assertEqual(v["marketPrice"]["buyLevels"][0], {"count": 7, "price": "237000000"})

    def test_parse_market(self):
        body = json.dumps({"type": "data", "nodes": [{"type": "data", "data": [{"x": 1}, 5]},
                                                     {"type": "data", "data": KAKA_NODE}]})
        m = parse_market(body)
        self.assertEqual((m["price"], m["lower_limit"], m["upper_limit"]), (215000000, 194000000, 237000000))
        self.assertEqual(m["buy_levels"][0], [237000000, 7])
        self.assertEqual(len(m["buy_levels"]), 5)
        self.assertEqual(m["sell_levels"], [])
        self.assertEqual(m["transfers_2h"], 39)
        self.assertEqual(m["observed_at"], "2026-11-24T11:35:34.694Z")

    def test_not_found(self):
        body = json.dumps({"type": "data", "nodes": [{"type": "error", "error": {"status": 404, "message": "Player not found"}}]})
        with self.assertRaises(NotFound):
            parse_market(body)

    def test_html_challenge(self):
        with self.assertRaises(RuntimeError):
            parse_market("<!doctype html><title>Just a moment...</title>")


if __name__ == "__main__":
    unittest.main()
