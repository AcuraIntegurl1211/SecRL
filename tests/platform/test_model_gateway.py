import unittest
from decimal import Decimal

import httpx

from secrl_platform.models.gateway import ModelGateway
from secrl_platform.models.pricing import Pricing
from secrl_platform.models.providers import (
    ModelRequest,
    ModelResponse,
    OpenAICompatibleProvider,
    ProviderError,
    Usage,
)


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


class ModelGatewayTest(unittest.IsolatedAsyncioTestCase):
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
            )
            response = await provider.complete(model_request())

        self.assertEqual(response.text, "answer")
        self.assertEqual(response.usage.total, 6)
        self.assertEqual(response.usage.cached, 1)
        self.assertEqual(response.usage.reasoning, 1)
        self.assertEqual(seen_authorization, ["Bearer test-provider-key"])

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
                    )
                    with self.assertRaises(ProviderError) as raised:
                        await provider.complete(model_request())
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retry_after, retry_after)
                self.assertNotIn("test-provider-key", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
