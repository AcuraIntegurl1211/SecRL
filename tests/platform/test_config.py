import unittest

from secrl_platform.cli import build_parser


class PlatformCliTest(unittest.TestCase):
    def test_serve_command_is_registered(self):
        args = build_parser().parse_args(["serve", "--host", "0.0.0.0"])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.host, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
