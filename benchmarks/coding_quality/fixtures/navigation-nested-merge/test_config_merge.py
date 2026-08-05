import unittest

from config_merge import merge_config


class MergeConfigTests(unittest.TestCase):
    def test_scalar_override(self):
        self.assertEqual(merge_config({"mode": "safe"}, {"mode": "fast"}), {"mode": "fast"})


if __name__ == "__main__":
    unittest.main()
