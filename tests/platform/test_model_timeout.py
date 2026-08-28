import unittest
from decimal import Decimal

from secrl_platform.agents.builtin import LegacyGatewayClient
from secrl_platform.models.evaluator import EvaluatorGatewayClient
from secrl_platform.models.gateway import GatewayResponse
from secrl_platform.models.providers import Usage
from secrl_platform.runner.process import (
    RunnerConfigurationError,
    _model_timeout_from_parameters,
)


class _RecordingGateway:
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return GatewayResponse(
            text="ok",
            usage=Usage(prompt=3, completion=2),
            estimated_cost=Decimal("0"),
            pricing_profile_sha256="0" * 64,
        )


class _Episode:
    run_id = "run-1"
    case_id = "case-1"
    attempt_id = "attempt-1"


class LegacyGatewayClientTimeoutTest(unittest.TestCase):
    def test_configured_timeout_reaches_model_request(self):
        gateway = _RecordingGateway()
        client = LegacyGatewayClient(
            gateway=gateway,
            model="fixture-model",
            capability_token="capability",
            agent_revision_id="agent-1",
            max_output_tokens=16,
            timeout_seconds=120,
        )
        client.bind_episode(_Episode())

        client.create(messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(gateway.requests[0].timeout_seconds, 120.0)

    def test_default_timeout_is_preserved_when_unset(self):
        gateway = _RecordingGateway()
        client = LegacyGatewayClient(
            gateway=gateway,
            model="fixture-model",
            capability_token="capability",
            agent_revision_id="agent-1",
            max_output_tokens=16,
        )
        client.bind_episode(_Episode())

        client.create(messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(gateway.requests[0].timeout_seconds, 30.0)


class EvaluatorGatewayClientTimeoutTest(unittest.TestCase):
    def test_configured_timeout_reaches_model_request(self):
        gateway = _RecordingGateway()
        client = EvaluatorGatewayClient(
            gateway=gateway,
            model="fixture-model",
            capability_token="evaluator-capability",
            agent_revision_id="agent-1",
            max_output_tokens=16,
            timeout_seconds=90,
        )
        client.bind_attempt(run_id="run-1", case_id="case-1", attempt_id="attempt-1")

        client.complete(prompt="private evaluator prompt", parameters={})

        request = gateway.requests[0]
        self.assertEqual(request.timeout_seconds, 90.0)
        self.assertEqual(request.model_role, "evaluator")


class ModelTimeoutParameterTest(unittest.TestCase):
    def test_accepts_configured_timeout(self):
        self.assertEqual(_model_timeout_from_parameters({"timeout_seconds": 120}), 120)

    def test_missing_timeout_is_none(self):
        self.assertIsNone(_model_timeout_from_parameters({}))

    def test_rejects_non_numeric_timeout(self):
        with self.assertRaises(RunnerConfigurationError):
            _model_timeout_from_parameters({"timeout_seconds": "fast"})

    def test_rejects_out_of_range_timeout(self):
        for value in (0, 601):
            with self.assertRaises(RunnerConfigurationError):
                _model_timeout_from_parameters({"timeout_seconds": value})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
