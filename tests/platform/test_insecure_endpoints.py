"""Explicit per-host HTTP endpoint allowlist for model providers."""

from __future__ import annotations

import unittest

from secrl_platform.models.providers import validate_model_endpoint

INSECURE = ("176.97.70.58",)


def _resolver_factory(recorded: list):
    def resolve(host, port):
        recorded.append((host, port))
        return ("93.184.216.34",)

    return resolve


class InsecureEndpointAllowlistTest(unittest.TestCase):
    def test_http_allowed_for_explicitly_approved_host(self):
        recorded: list = []
        endpoint = validate_model_endpoint(
            "http://176.97.70.58:8080/v1",
            allowed_hosts=("176.97.70.58",),
            resolver=_resolver_factory(recorded),
            insecure_hosts=INSECURE,
        )
        self.assertEqual(endpoint, "http://176.97.70.58:8080/v1")
        self.assertEqual(recorded, [("176.97.70.58", 8080)])

    def test_http_still_rejected_without_explicit_approval(self):
        with self.assertRaises(ValueError) as ctx:
            validate_model_endpoint(
                "http://176.97.70.58:8080/v1",
                allowed_hosts=("176.97.70.58",),
                resolver=_resolver_factory([]),
            )
        self.assertIn("not approved for insecure transport", str(ctx.exception))

    def test_http_rejected_when_host_not_in_insecure_list(self):
        with self.assertRaises(ValueError) as ctx:
            validate_model_endpoint(
                "http://other.example.com/v1",
                allowed_hosts=("other.example.com",),
                resolver=_resolver_factory([]),
                insecure_hosts=INSECURE,
            )
        self.assertIn("not approved for insecure transport", str(ctx.exception))

    def test_https_remains_the_default_without_any_configuration(self):
        endpoint = validate_model_endpoint(
            "https://models.invalid/v1",
            allowed_hosts=("models.invalid",),
            resolver=_resolver_factory([]),
        )
        self.assertEqual(endpoint, "https://models.invalid/v1")

    def test_http_default_port_is_80_when_omitted(self):
        recorded: list = []
        validate_model_endpoint(
            "http://176.97.70.58",
            allowed_hosts=("176.97.70.58",),
            resolver=_resolver_factory(recorded),
            insecure_hosts=INSECURE,
        )
        self.assertEqual(recorded, [("176.97.70.58", 80)])

    def test_private_ip_rejected_even_when_host_is_approved(self):
        with self.assertRaises(ValueError) as ctx:
            validate_model_endpoint(
                "http://10.0.0.5/v1",
                allowed_hosts=("10.0.0.5",),
                resolver=_resolver_factory([]),
                insecure_hosts=("10.0.0.5",),
            )
        self.assertIn("private address", str(ctx.exception))

    def test_allowlist_still_enforced_for_insecure_hosts(self):
        with self.assertRaises(ValueError) as ctx:
            validate_model_endpoint(
                "http://176.97.70.58:8080/v1",
                allowed_hosts=("api.deepseek.com",),
                resolver=_resolver_factory([]),
                insecure_hosts=INSECURE,
            )
        self.assertIn("not allowlisted", str(ctx.exception))

    def test_userinfo_still_rejected_for_insecure_hosts(self):
        with self.assertRaises(ValueError) as ctx:
            validate_model_endpoint(
                "http://user:pass@176.97.70.58:8080/v1",
                allowed_hosts=("176.97.70.58",),
                resolver=_resolver_factory([]),
                insecure_hosts=INSECURE,
            )
        self.assertIn("user information", str(ctx.exception))

    def test_query_still_rejected_for_insecure_hosts(self):
        with self.assertRaises(ValueError) as ctx:
            validate_model_endpoint(
                "http://176.97.70.58:8080/v1?x=1",
                allowed_hosts=("176.97.70.58",),
                resolver=_resolver_factory([]),
                insecure_hosts=INSECURE,
            )
        self.assertIn("query or fragment", str(ctx.exception))


class OpenAICompatibleProviderInsecureTest(unittest.TestCase):
    def test_provider_accepts_approved_http_endpoint(self):
        from secrl_platform.models.providers import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider(
            base_url="http://176.97.70.58:8080/v1",
            api_key="test-key",
            allowed_hosts=("176.97.70.58",),
            resolver=lambda _host, _port: ("93.184.216.34",),
            insecure_hosts=("176.97.70.58",),
        )
        self.assertEqual(provider._base_url, "http://176.97.70.58:8080/v1")

    def test_provider_rejects_http_endpoint_without_approval(self):
        from secrl_platform.models.providers import OpenAICompatibleProvider

        with self.assertRaises(ValueError):
            OpenAICompatibleProvider(
                base_url="http://176.97.70.58:8080/v1",
                api_key="test-key",
                allowed_hosts=("176.97.70.58",),
                resolver=lambda _host, _port: ("93.184.216.34",),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
