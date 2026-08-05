import unittest

from commands import build_command
from options import RuntimeOptions


class CommandTests(unittest.TestCase):
    def test_normal_command(self):
        self.assertEqual(build_command("staging"), "deploy staging")

    def test_verbose_command(self):
        self.assertEqual(build_command("staging", RuntimeOptions(verbose=True)), "deploy staging --verbose")

    def test_dry_run_command(self):
        self.assertEqual(build_command("staging", RuntimeOptions(dry_run=True)), "echo deploy staging")


if __name__ == "__main__":
    unittest.main()
