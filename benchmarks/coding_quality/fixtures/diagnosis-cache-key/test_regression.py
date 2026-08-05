import unittest

from pricing import clear_cache, regional_price


class PricingTests(unittest.TestCase):
    def tearDown(self):
        clear_cache()

    def test_reuses_same_region_value(self):
        calls = []
        def loader(sku, region):
            calls.append((sku, region))
            return 10
        self.assertEqual(regional_price("book", "us", loader), 10)
        self.assertEqual(regional_price("book", "us", loader), 10)
        self.assertEqual(calls, [("book", "us")])


if __name__ == "__main__":
    unittest.main()
