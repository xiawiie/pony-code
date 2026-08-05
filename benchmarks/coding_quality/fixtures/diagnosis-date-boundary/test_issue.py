import unittest
from datetime import date

from date_ranges import inclusive_dates


class DateRangeIssueTests(unittest.TestCase):
    def test_includes_the_final_day(self):
        self.assertEqual(
            inclusive_dates(date(2026, 2, 1), date(2026, 2, 2)),
            [date(2026, 2, 1), date(2026, 2, 2)],
        )


if __name__ == "__main__":
    unittest.main()
