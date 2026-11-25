"""랭커 픽 추천: fc-info 데이터 해석 · 주말 계산 · 점수."""
import json
import unittest
from datetime import date

from fco.rankerpick import last_weekend, parse_team, parse_teams, score


def page(payload: str) -> str:
    """Next.js 처럼 데이터 조각을 self.__next_f.push 로 나눠 싣는다."""
    half = len(payload) // 2
    return "".join(f'<script>self.__next_f.push([1,{json.dumps(p)}])</script>' for p in (payload[:half], payload[half:]))


class Parse(unittest.TestCase):
    def test_teams(self):
        p = page('x:{"totalRankers":1000,"teams":[{"name":"레알 마드리드","rankers":80,"rank":1},'
                 '{"name":"브라질","rankers":50,"rank":2}],"formations":[]}')
        t = parse_teams(p)
        self.assertEqual(t["total"], 1000)
        self.assertEqual([x["name"] for x in t["teams"]], ["레알 마드리드", "브라질"])
        self.assertAlmostEqual(t["teams"][0]["share"], 0.08)

    def test_team_positions(self):
        p = page('{"positions":[{"key":"ST","label":"ST","includes":["ST"]}]}{"positions":[{"position":"ST","sample":100,"items":[{"rank":1,"spId":863231747,"grade":9,'
                 '"grades":[{"grade":9,"picks":7},{"grade":8,"picks":3}],"picks":10,"sample":100,"pickRate":0.1,'
                 '"name":"킬리안 음바페","className":"PTG","price":2920000000}]}]}')
        rows = parse_team(p)
        self.assertEqual(rows[0]["spid"], 863231747)
        self.assertEqual(rows[0]["grades"], [[9, 7], [8, 3]])
        self.assertEqual(rows[0]["position"], "ST")

    def test_no_data(self):
        with self.assertRaises(RuntimeError):
            parse_teams("<html>blocked</html>")


class Weekend(unittest.TestCase):
    def test_last_weekend(self):
        self.assertEqual(last_weekend(date(2026, 11, 24)), ("2026-11-21", "2026-11-22"))  # 화요일
        self.assertEqual(last_weekend(date(2026, 11, 22)), ("2026-11-14", "2026-11-15"))  # 일요일 당일은 지난주
        self.assertEqual(last_weekend(date(2026, 11, 23)), ("2026-11-21", "2026-11-22"))  # 월요일


class Score(unittest.TestCase):
    def test_ratio_to_max(self):
        rows = [{"transfers_2h": 39, "best_pick_rate": 0.2, "drop_pct": 10},
                {"transfers_2h": 5, "best_pick_rate": 0.1, "drop_pct": 20}]
        score(rows, {"transfers": 40, "pickRate": 35, "weekendDrop": 25})
        top = rows[0]
        self.assertAlmostEqual(top["points_transfers"], 40)
        self.assertAlmostEqual(top["points_pick"], 35)
        self.assertAlmostEqual(top["points_drop"], 12.5)
        self.assertAlmostEqual(rows[1]["points_drop"], 25)


if __name__ == "__main__":
    unittest.main()
