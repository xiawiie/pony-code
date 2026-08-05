import unittest

from response import handle


class ResponseTests(unittest.TestCase):
    def test_success(self):
        self.assertEqual(handle(lambda: 7), {"ok": True, "value": 7})


if __name__ == "__main__":
    unittest.main()
