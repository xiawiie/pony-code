import unittest

from pricing import clear_cache, regional_price


class PricingIssueTests(unittest.TestCase):
    def tearDown(self):
        clear_cache()

    def test_region_is_part_of_the_cache_identity(self):
        prices = {"us": 10, "eu": 12}
        def loader(sku, region):
            return prices[region]
        self.assertEqual(regional_price("book", "us", loader), 10)
        self.assertEqual(regional_price("book", "eu", loader), 12)


if __name__ == "__main__":
    unittest.main()
