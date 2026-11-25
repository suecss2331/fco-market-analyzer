"""fc-info 팀 페이지의 '인기 급등 선수' · '새로운 선수' 해석."""
import json
import unittest

from fco.rankerpick import parse_team_trends


def page(payload: str) -> str:
    return f'<script>self.__next_f.push([1,{json.dumps(payload)}])</script>'


class Trends(unittest.TestCase):
    def test_rising_and_new(self):
        p = page('{"baseDate":"2026-11-24","compareDate":"2026-11-17",'
                 '"newCards":[{"rank":30,"position":"CB","spId":100,"grades":[{"grade":8,"picks":3}],"picks":3,'
                 '"pickRate":0.003,"prevPicks":0,"prevSample":1500,"rateChange":null,"name":"A","className":"X"}],'
                 '"rising":[{"rank":20,"position":"CB","spId":100201535,"grades":[{"grade":10,"picks":8}],"picks":14,'
                 '"sample":1498,"pickRate":0.0093,"prevPicks":1,"prevSample":1525,"rateChange":13.25,"name":"라파엘 바란","className":"ICON TM"}]}')
        rows = parse_team_trends(p)
        self.assertEqual([r["kind"] for r in rows], ["rising", "new"])
        r = rows[0]
        self.assertEqual((r["spid"], r["prev_picks"], r["picks"]), (100201535, 1, 14))
        self.assertAlmostEqual(r["rate_change"], 13.25)
        self.assertEqual((r["compare_date"], r["base_date"]), ("2026-11-17", "2026-11-24"))
        self.assertIsNone(rows[1]["rate_change"])

    def test_missing_sections(self):
        self.assertEqual(parse_team_trends(page('{"positions":[]}')), [])


if __name__ == "__main__":
    unittest.main()
