import unittest
from decimal import Decimal
from math import isfinite

import httpx
from pydantic import ValidationError

from secrl_platform.models.gateway import ModelGateway
from secrl_platform.models.pricing import Pricing
from secrl_platform.models.providers import (
    DeferredSecretProvider,
    ModelRequest,
    ModelResponse,
    OpenAICompatibleProvider,
    ProviderError,
    Usage,
    _PinnedNetworkBackend,
)
from secrl_platform.models.secrets import SecretStore


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def complete(self, _request):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def model_request(**overrides):
    values = {
        "provider_adapter_version": "openai-compatible-v1",
        "model_role": "agent",
        "model": "fixture-model",
        "messages": ({"role": "user", "content": "hello"},),
        "requested_parameters": {"temperature": 0},
        "effective_parameters": {"temperature": 0},
        "timeout_seconds": 5,
        "run_id": "run-1",
        "case_id": "case-1",
        "attempt_id": "attempt-1",
        "max_attempts": 3,
    }
    values.update(overrides)
    return ModelRequest(**values)


async def no_wait(_seconds):
    return None


def public_resolver(_host, _port):
    return ("93.184.216.34",)


class ModelGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_model_secret_is_decrypted_only_inside_provider_call(self):
        class RecordingSecretStore(SecretStore):
            def __init__(self):
                super().__init__(bytes.fromhex("22" * 32))
                self.decrypt_calls = 0

            def decrypt(self, secret):
                self.decrypt_calls += 1
                return super().decrypt(secret)

        store = RecordingSecretStore()
        envelope = store.encrypt("sk-call-scoped")
        seen_keys = []

        class Provider:
            async def complete(self, _request):
                return ModelResponse(text="ok", usage=Usage(prompt=1, completion=1))

        provider = DeferredSecretProvider(
            secret_store=store,
            encrypted_secret=envelope,
            provider_factory=lambda api_key: seen_keys.append(api_key) or Provider(),
        )
        self.assertEqual(store.decrypt_calls, 0)

        await provider.complete(model_request())

        self.assertEqual(store.decrypt_calls, 1)
        self.assertEqual(seen_keys, ["sk-call-scoped"])

    async def test_429_retries_and_records_one_successful_usage(self):
        provider = FakeProvider(
            [
                ProviderError("RATE_LIMITED", retry_after=0),
                ModelResponse(
                    text="ok",
                    usage=Usage(prompt=10, completion=2),
                    raw_usage={"prompt_tokens": 10, "completion_tokens": 2},
                ),
            ]
        )
        gateway = ModelGateway(
            provider=provider,
            pricing=Pricing(input_per_million=1, output_per_million=2),
            sleep=no_wait,
        )

        result = await gateway.complete(model_request())

        self.assertEqual(provider.calls, 2)
        self.assertTrue(ProviderError("RATE_LIMITED").safe_to_retry)
        self.assertEqual(result.usage.total, 12)
        self.assertEqual(result.estimated_cost, Decimal("0.000014"))
        self.assertEqual(
            result.raw_usage,
            {"prompt_tokens": 10, "completion_tokens": 2},
        )
        self.assertRegex(result.pricing_profile_sha256, r"^[0-9a-f]{64}$")

    async def test_permanent_error_is_not_retried(self):
        provider = FakeProvider([ProviderError("AUTHENTICATION_FAILED")])
        gateway = ModelGateway(provider=provider, pricing=Pricing(), sleep=no_wait)

        with self.assertRaises(ProviderError) as raised:
            await gateway.complete(model_request())

        self.assertEqual(raised.exception.code, "AUTHENTICATION_FAILED")
        self.assertEqual(provider.calls, 1)

    async def test_retry_count_is_bounded_by_request(self):
        provider = FakeProvider(
            [ProviderError("RATE_LIMITED", retry_after=0) for _ in range(2)]
        )
        gateway = ModelGateway(provider=provider, pricing=Pricing(), sleep=no_wait)

        with self.assertRaises(ProviderError):
            await gateway.complete(model_request(max_attempts=2))

        self.assertEqual(provider.calls, 2)

    async def test_retry_delay_is_finite_and_capped(self):
        delays = []

        async def record_delay(value):
            delays.append(value)

        provider = FakeProvider(
            [
                ProviderError("RATE_LIMITED", retry_after=float("inf")),
                ModelResponse(text="ok", usage=Usage(prompt=1, completion=1)),
            ]
        )
        gateway = ModelGateway(
            provider=provider,
            pricing=Pricing(input_per_million=1, output_per_million=1),
            sleep=record_delay,
        )

        await gateway.complete(model_request())

        self.assertEqual(len(delays), 1)
        self.assertTrue(isfinite(delays[0]))
        self.assertLessEqual(delays[0], 30)

    def test_effective_parameters_cannot_replace_authoritative_request_fields(self):
        for reserved in ("model", "messages", "max_tokens"):
            with self.subTest(reserved=reserved):
                with self.assertRaises(ValidationError):
                    model_request(effective_parameters={reserved: "changed"})

    async def test_missing_usage_or_price_is_unknown_not_zero(self):
        without_usage = ModelGateway(
            provider=FakeProvider([ModelResponse(text="ok", usage=None)]),
            pricing=Pricing(input_per_million=1, output_per_million=2),
        )
        without_price = ModelGateway(
            provider=FakeProvider(
                [ModelResponse(text="ok", usage=Usage(prompt=1, completion=1))]
            ),
            pricing=Pricing(),
        )

        self.assertIsNone((await without_usage.complete(model_request())).estimated_cost)
        self.assertIsNone((await without_price.complete(model_request())).estimated_cost)


class OpenAICompatibleProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_backend_connects_to_validated_ip_not_hostname(self):
        class RecordingBackend:
            def __init__(self):
                self.hosts = []

            async def connect_tcp(self, host, port, **kwargs):
                self.hosts.append((host, port))
                return object()

            async def connect_unix_socket(self, path, **kwargs):
                raise AssertionError(path)

            async def sleep(self, seconds):
                return None

        backend = RecordingBackend()
        pinned = _PinnedNetworkBackend(
            hostname="provider.invalid",
            address="93.184.216.34",
            backend=backend,
        )

        await pinned.connect_tcp("provider.invalid", 443)

        self.assertEqual(backend.hosts, [("93.184.216.34", 443)])

    def test_endpoint_policy_rejects_unallowlisted_and_private_hosts(self):
        def provider_for(base_url, *, allowed_hosts, resolver):
            try:
                return OpenAICompatibleProvider(
                    base_url=base_url,
                    api_key="test-provider-key",
                    allowed_hosts=allowed_hosts,
                    resolver=resolver,
                )
            except TypeError as exc:
                self.fail(f"provider endpoint policy is unavailable: {exc}")

        with self.assertRaises(ValueError):
            provider_for(
                "https://unapproved.example/v1",
                allowed_hosts=("provider.example",),
                resolver=lambda _host, _port: ("93.184.216.34",),
            )
        with self.assertRaises(ValueError):
            provider_for(
                "https://provider.example/v1",
                allowed_hosts=("provider.example",),
                resolver=lambda _host, _port: ("127.0.0.1",),
            )

    async def test_injected_client_cannot_follow_provider_redirect(self):
        seen_hosts = []

        def handler(request):
            seen_hosts.append(request.url.host)
            if request.url.host == "provider.example":
                return httpx.Response(
                    302,
                    headers={"Location": "http://169.254.169.254/latest"},
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "unsafe"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            provider = OpenAICompatibleProvider(
                base_url="https://provider.example/v1",
                api_key="test-provider-key",
                client=client,
                allowed_hosts=("provider.example",),
                resolver=public_resolver,
            )
            with self.assertRaises(ProviderError) as raised:
                await provider.complete(model_request())

        self.assertEqual(raised.exception.code, "PROVIDER_REDIRECT")
        self.assertEqual(seen_hosts, ["provider.example"])

    async def test_normalizes_successful_response_and_sends_bearer_token(self):
        seen_authorization = []

        def handler(request):
            seen_authorization.append(request.headers["authorization"])
            return httpx.Response(
                200,
                json={
                    "id": "response-1",
                    "choices": [{"message": {"content": "answer"}}],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "prompt_tokens_details": {"cached_tokens": 1},
                        "completion_tokens_details": {"reasoning_tokens": 1},
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                base_url="https://provider.invalid/v1",
                api_key="test-provider-key",
                client=client,
                allowed_hosts=("provider.invalid",),
                resolver=public_resolver,
            )
            response = await provider.complete(model_request())

        self.assertEqual(response.text, "answer")
        self.assertEqual(response.usage.total, 6)
        self.assertEqual(response.usage.cached, 1)
        self.assertEqual(response.usage.reasoning, 1)
        self.assertEqual(seen_authorization, ["Bearer test-provider-key"])

    async def test_accepts_reasoning_content_when_compatible_provider_omits_content(self):
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "id": "response-reasoning-only",
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": None, "reasoning_content": "Action: SELECT 1"},
                    }],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                },
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            provider = OpenAICompatibleProvider(
                base_url="https://api.deepseek.com",
                api_key="test-provider-key",
                client=client,
                allowed_hosts=("api.deepseek.com",),
                resolver=public_resolver,
            )
            response = await provider.complete(model_request())

        self.assertEqual(response.text, "Action: SELECT 1")
        self.assertEqual(response.provider_request_id, "response-reasoning-only")

    async def test_invalid_usage_is_reported_without_echoing_response_body(self):
        marker = "do-not-log-provider-answer"
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": marker}}],
                    "usage": {"prompt_tokens": "not-an-integer", "completion_tokens": 1},
                },
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            provider = OpenAICompatibleProvider(
                base_url="https://provider.invalid/v1",
                api_key="test-provider-key",
                client=client,
                allowed_hosts=("provider.invalid",),
                resolver=public_resolver,
            )
            with self.assertLogs("secrl_platform.models.providers", level="WARNING") as captured:
                with self.assertRaises(ProviderError) as raised:
                    await provider.complete(model_request())

        self.assertEqual(raised.exception.code, "INVALID_PROVIDER_RESPONSE")
        self.assertTrue(raised.exception.usage_may_have_occurred)
        self.assertNotIn(marker, "\n".join(captured.output))
        self.assertNotIn("test-provider-key", "\n".join(captured.output))

    async def test_invalid_success_response_is_treated_as_ambiguous_usage(self):
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"choices": []})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            provider = OpenAICompatibleProvider(
                base_url="https://provider.invalid/v1",
                api_key="test-provider-key",
                client=client,
                allowed_hosts=("provider.invalid",),
                resolver=public_resolver,
            )
            with self.assertRaises(ProviderError) as raised:
                await provider.complete(model_request())

        self.assertEqual(raised.exception.code, "INVALID_PROVIDER_RESPONSE")
        self.assertTrue(raised.exception.usage_may_have_occurred)
        self.assertFalse(raised.exception.safe_to_retry)

    async def test_malformed_json_and_empty_content_are_ambiguous_provider_failures(self):
        responses = (
            httpx.Response(200, headers={"content-type": "application/json"}, content=b"not-json"),
            httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
            httpx.Response(200, json={"choices": [{"finish_reason": 7, "message": {"content": "answer"}}]}),
        )
        for response in responses:
            with self.subTest(response=response):
                transport = httpx.MockTransport(lambda _request, response=response: response)
                async with httpx.AsyncClient(transport=transport) as client:
                    provider = OpenAICompatibleProvider(
                        base_url="https://provider.invalid/v1",
                        api_key="test-provider-key",
                        client=client,
                        allowed_hosts=("provider.invalid",),
                        resolver=public_resolver,
                    )
                    with self.assertRaises(ProviderError) as raised:
                        await provider.complete(model_request())
                self.assertEqual(raised.exception.code, "INVALID_PROVIDER_RESPONSE")
                self.assertTrue(raised.exception.usage_may_have_occurred)
                self.assertFalse(raised.exception.safe_to_retry)

    async def test_classifies_permanent_and_transient_statuses(self):
        statuses = [
            (401, "AUTHENTICATION_FAILED", None),
            (404, "MODEL_NOT_FOUND", None),
            (429, "RATE_LIMITED", 2.0),
            (503, "PROVIDER_UNAVAILABLE", None),
        ]
        for status, code, retry_after in statuses:
            with self.subTest(status=status):
                headers = {"Retry-After": "2"} if retry_after is not None else {}
                transport = httpx.MockTransport(
                    lambda _request, status=status, headers=headers: httpx.Response(
                        status, headers=headers, json={"error": {"message": "hidden"}}
                    )
                )
                async with httpx.AsyncClient(transport=transport) as client:
                    provider = OpenAICompatibleProvider(
                        base_url="https://provider.invalid/v1",
                        api_key="test-provider-key",
                        client=client,
                        allowed_hosts=("provider.invalid",),
                        resolver=public_resolver,
                    )
                    with self.assertRaises(ProviderError) as raised:
                        await provider.complete(model_request())
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retry_after, retry_after)
                self.assertNotIn("test-provider-key", str(raised.exception))

    async def test_timeout_and_server_errors_are_ambiguous_usage(self):
        for status in (408, 500, 503):
            with self.subTest(status=status):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _request, value=status: httpx.Response(value)
                    )
                ) as client:
                    provider = OpenAICompatibleProvider(
                        base_url="https://provider.invalid/v1",
                        api_key="test-provider-key",
                        client=client,
                        allowed_hosts=("provider.invalid",),
                        resolver=public_resolver,
                    )
                    with self.assertRaises(ProviderError) as raised:
                        await provider.complete(model_request(max_attempts=1))
                self.assertTrue(raised.exception.usage_may_have_occurred)


if __name__ == "__main__":
    unittest.main()
