"""5단계 인기 스쿼드 점수 검사."""
import unittest

from fco.popularity import compute_popularity, compare_snapshots

CFG = {"topN": 20, "subWeight": 0.5, "dropAlertRatio": 0.5}
A, B = (100, 11), (200, 8)


def e(rank, card, sub=False):
    return {"rank": rank, "spid": card[0], "grade": card[1], "is_sub": 1 if sub else 0}


class Popularity(unittest.TestCase):
    def test_rank_weights(self):
        r = compute_popularity([e(1, A), e(20, B)], CFG)
        self.assertEqual(r[A]["score"], 20)
        self.assertEqual(r[B]["score"], 1)

    def test_multiple_squads_sum_and_count(self):
        r = compute_popularity([e(1, A), e(3, A)], CFG)
        self.assertEqual(r[A]["score"], 38)
        self.assertEqual(r[A]["appearances"], 2)
        self.assertEqual(r[A]["best_rank"], 1)
        self.assertEqual(r[A]["ranks"], [1, 3])

    def test_sub_weight(self):
        r = compute_popularity([e(1, A, sub=True)], CFG)
        self.assertEqual(r[A]["score"], 10)
        self.assertEqual(r[A]["starter_appearances"], 0)

    def test_same_card_twice_in_one_squad_counts_once(self):
        r = compute_popularity([e(2, A), e(2, A, sub=True)], CFG)
        self.assertEqual(r[A]["score"], 19)
        self.assertEqual(r[A]["appearances"], 1)
        self.assertEqual(r[A]["starter_appearances"], 1)

    def test_ranks_outside_top_n_ignored(self):
        r = compute_popularity([e(21, A), e(150, A)], CFG)
        self.assertNotIn(A, r)

    def test_drop_alert(self):
        prev = compute_popularity([e(1, A), e(3, A)], CFG)
        now = compute_popularity([e(11, A)], CFG)
        d = compare_snapshots(now, prev, CFG)
        self.assertEqual(d[A]["prev_score"], 38)
        self.assertTrue(d[A]["drop_alert"])
        self.assertAlmostEqual(d[A]["change_pct"], (10 - 38) / 38 * 100)

    def test_card_gone_from_top20_is_alerted(self):
        prev = compute_popularity([e(1, A)], CFG)
        d = compare_snapshots({}, prev, CFG)
        self.assertEqual(d[A]["score"], 0)
        self.assertTrue(d[A]["drop_alert"])

    def test_no_previous_snapshot(self):
        now = compute_popularity([e(1, A)], CFG)
        d = compare_snapshots(now, None, CFG)
        self.assertIsNone(d[A]["prev_score"])
        self.assertIsNone(d[A]["drop_alert"])


if __name__ == "__main__":
    unittest.main()
