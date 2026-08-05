import unittest

from errors import AppError
from response import handle


class ResponseTests(unittest.TestCase):
    def test_success(self):
        self.assertEqual(handle(lambda: 7), {"ok": True, "value": 7})

    def test_failure(self):
        def fail():
            raise AppError("not_found", "missing")
        self.assertEqual(handle(fail), {"ok": False, "error": {"code": "not_found", "message": "missing"}})


if __name__ == "__main__":
    unittest.main()
