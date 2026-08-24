import unittest

from experiments.failure_analysis.features import normalize_entities


class FeatureNormalizationTest(unittest.TestCase):
    def test_security_entities_use_distinct_markers(self):
        cases = [
            ("ip=10.20.30.40", "ip=<ip>"),
            ("url=https://example.com/a?q=1", "url=<url>"),
            ("sha1=" + "a" * 40, "sha1=<sha1>"),
            ("sha256=" + "b" * 64, "sha256=<sha256>"),
            ("guid=550e8400-e29b-41d4-a716-446655440000", "guid=<guid>"),
            ("sid=S-1-5-21-1000-1001-1002-1003", "sid=<sid>"),
            ("time=2026-07-18T06:23:58Z", "time=<timestamp>"),
            ("process=powershell.exe", "process=<process>"),
            ("file=quarterly-report.docx", "file=<file>"),
            ("host=server01.contoso.local", "host=<host>"),
            ("host=server01", "host=<host>"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_entities(raw), expected)

    def test_equivalent_timestamp_offsets_use_the_same_marker(self):
        utc = normalize_entities("2026-07-18T06:23:58Z")
        offset = normalize_entities("2026-07-18T14:23:58+08:00")
        self.assertEqual(utc, "<timestamp>")
        self.assertEqual(offset, utc)

    def test_non_entity_text_is_lowercased_and_whitespace_collapsed(self):
        self.assertEqual(
            normalize_entities("  Investigation   Complete  "),
            "investigation complete",
        )


if __name__ == "__main__":
    unittest.main()
