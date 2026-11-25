"""연도 없는 'M.DD' 라벨 날짜 역산 검사.  실행: python -m unittest discover tests"""
import unittest
from datetime import date, timedelta

from fco.datacenter import assign_dates


def labels_for(last: date, n: int):
    return [f"{d.month}.{d.day:02d}" for d in (last - timedelta(days=n - 1 - i) for i in range(n))]


class AssignDates(unittest.TestCase):
    def test_365_days_across_new_year(self):
        today = date(2026, 11, 23)
        last = date(2026, 11, 22)
        days, check = assign_dates(labels_for(last, 365), today)
        self.assertEqual(check, "ok")
        self.assertEqual(days[-1], last)
        self.assertEqual(days[0], date(2025, 11, 23))

    def test_label_equal_today(self):
        days, check = assign_dates(["11.23"], date(2026, 11, 23))
        self.assertEqual(days, [date(2026, 11, 23)])

    def test_early_january_wraps_to_previous_year(self):
        today = date(2027, 1, 2)
        days, check = assign_dates(labels_for(date(2027, 1, 1), 10), today)
        self.assertEqual(check, "ok")
        self.assertEqual(days[0], date(2026, 12, 23))

    def test_leap_day(self):
        today = date(2028, 3, 5)
        days, check = assign_dates(labels_for(date(2028, 3, 4), 10), today)
        self.assertEqual(check, "ok")
        self.assertIn(date(2028, 2, 29), days)

    def test_gap_is_flagged(self):
        lbls = labels_for(date(2026, 11, 22), 5)
        del lbls[2]
        days, check = assign_dates(lbls, date(2026, 11, 23))
        self.assertNotEqual(check, "ok")
        self.assertEqual(days[-1], date(2026, 11, 22))
        self.assertEqual(days[1], date(2026, 11, 19))


if __name__ == "__main__":
    unittest.main()
