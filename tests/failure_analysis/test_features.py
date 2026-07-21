import unittest

from experiments.failure_analysis.features import normalize_sql


class FeaturesTest(unittest.TestCase):
    def test_sql_whitespace_and_trailing_semicolon_are_normalized(self):
        self.assertEqual(
            normalize_sql(" SELECT  *\nFROM Alerts;  "),
            "select * from alerts",
        )


if __name__ == "__main__":
    unittest.main()
