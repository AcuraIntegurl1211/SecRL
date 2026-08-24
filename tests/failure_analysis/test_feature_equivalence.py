import unittest

from experiments.failure_analysis.features import normalized_equivalent


class FeatureEquivalenceTest(unittest.TestCase):
    def test_case_and_whitespace_are_equivalent(self):
        self.assertTrue(normalized_equivalent("  Investigation  Complete ", "investigation complete"))

    def test_equivalent_timestamp_offsets_are_equivalent(self):
        self.assertTrue(
            normalized_equivalent(
                "2026-07-18T06:23:58Z",
                "2026-07-18T14:23:58+08:00",
            )
        )

    def test_fqdn_and_matching_short_hostname_are_equivalent(self):
        self.assertTrue(
            normalized_equivalent(
                "host server01.contoso.local",
                "HOST server01",
            )
        )

    def test_different_ip_addresses_are_not_equivalent(self):
        self.assertFalse(normalized_equivalent("10.0.0.1", "10.0.0.2"))

    def test_different_guids_are_not_equivalent(self):
        self.assertFalse(
            normalized_equivalent(
                "550e8400-e29b-41d4-a716-446655440000",
                "550e8400-e29b-41d4-a716-446655440001",
            )
        )


if __name__ == "__main__":
    unittest.main()
