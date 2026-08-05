import unittest
from datetime import date

from date_ranges import inclusive_dates


class InclusiveDatesTests(unittest.TestCase):
    def test_rejects_reversed_range(self):
        with self.assertRaises(ValueError):
            inclusive_dates(date(2026, 2, 2), date(2026, 2, 1))


if __name__ == "__main__":
    unittest.main()
