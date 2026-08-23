from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.agents.capabilities import (
    CapabilityClaims,
    CapabilitySigner,
    FileCapabilityBudgetStore,
)
from secrl_platform.agents.protocol import AgentManifest
from secrl_platform.agents.service import (
    AgentServiceRuntime,
    AgentServiceTransport,
    HttpxAgentServiceTransport,
    ServiceConfig,
)
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.config import Settings
from secrl_platform.models.providers import (
    DeferredSecretProvider,
    OpenAICompatibleProvider,
)
from secrl_platform.models.secrets import (
    SecretStore,
    encrypted_secret_from_json,
)
from secrl_platform.runner.engine import CapabilityBudgetGuard, RunnerEngine
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session
from secrl_platform.storage.orm import (
    AgentRevisionORM,
    EvaluationTaskORM,
    ModelConfigRevisionORM,
    RunORM,
    SecretRefORM,
)


class RunnerConfigurationError(RuntimeError):
    pass


class RunnerProcess:
    """Small process boundary used by CLI/background worker orchestration."""

    def __init__(self, engine: RunnerEngine) -> None:
        self._engine = engine

    async def run_once(self, task_id: str, run_id: str) -> str:
        return await self._engine.run(task_id, run_id)


def capability_signer(settings: Settings) -> CapabilitySigner:
    key = hashlib.sha256(
        b"secrl-lite-capability-v1\0" + settings.session_secret.encode("utf-8")
    ).digest()
    return CapabilitySigner(
        key,
        budget_store=FileCapabilityBudgetStore(settings.data_dir / "capability-ledger"),
    )


async def run_pending_once(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session] | None = None,
    artifact_store: LocalArtifactStore | None = None,
    agent_service_transport: AgentServiceTransport | None = None,
    agent_service_resolver=None,
    model_provider_resolver=None,
) -> str | None:
    sessions = session_factory or create_engine_and_session(settings.database_path)
    artifacts = artifact_store or LocalArtifactStore(settings.artifact_dir)
    with sessions() as session:
        pair = session.execute(
            select(EvaluationTaskORM.id, RunORM.id)
            .join(RunORM, RunORM.task_id == EvaluationTaskORM.id)
            .where(
                EvaluationTaskORM.status == "QUEUED",
                RunORM.status == "QUEUED",
            )
            .order_by(EvaluationTaskORM.created_at, EvaluationTaskORM.id)
            .limit(1)
        ).first()
    if pair is None:
        return None

    task_id, run_id = pair
    repository = RunnerRepository(sessions)
    owned_client: httpx.AsyncClient | None = None
    try:
        runtime_factory, budget_guard, owned_client = _resolve_runtime(
            settings=settings,
            session_factory=sessions,
            task_id=task_id,
            run_id=run_id,
            agent_service_transport=agent_service_transport,
            agent_service_resolver=agent_service_resolver,
            model_provider_resolver=model_provider_resolver,
        )
        engine = RunnerEngine(
            repository=repository,
            artifact_store=artifacts,
            adapter=ProtocolSmokeAdapter.load_default(),
            runtime_factory=runtime_factory,
            model_budget_guard=budget_guard,
        )
        return await engine.run(task_id, run_id)
    finally:
        if owned_client is not None:
            await owned_client.aclose()


def _resolve_runtime(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    task_id: str,
    run_id: str,
    agent_service_transport: AgentServiceTransport | None,
    agent_service_resolver,
    model_provider_resolver,
):
    with session_factory() as session:
        task = session.get(EvaluationTaskORM, task_id)
        if task is None or task.agent_revision_id is None:
            raise RunnerConfigurationError("task agent revision is missing")
        agent = session.get(AgentRevisionORM, task.agent_revision_id)
        if agent is None:
            raise RunnerConfigurationError("task agent revision was not found")
        try:
            task_spec = json.loads(task.task_spec_json)
            manifest = AgentManifest.model_validate_json(agent.manifest_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RunnerConfigurationError("task runtime metadata is invalid") from exc
        if task_spec.get("agent_revision_sha256") != agent.sha256:
            raise RunnerConfigurationError("task agent revision hash changed")
        if task.model_config_revision_id is not None:
            _resolve_model_provider(
                settings=settings,
                session=session,
                task_spec=task_spec,
                model_id=task.model_config_revision_id,
                resolver=model_provider_resolver,
            )
        agent_kind = agent.kind
        endpoint = agent.service_endpoint
        service_manifest_sha256 = agent.service_manifest_sha256
        budget = json.loads(task.budget_json)

    if agent_kind == "BUILT_IN":
        revision = DeterministicSmokeAgent.revision()
        if agent.sha256 != revision.manifest_sha256 or manifest != revision.manifest:
            raise RunnerConfigurationError("built-in agent revision is not allowlisted")
        return DeterministicSmokeAgent, None, None

    if agent_kind != "SERVICE" or endpoint is None or service_manifest_sha256 is None:
        raise RunnerConfigurationError("Agent Service registration is incomplete")
    signer = capability_signer(settings)
    issued_at = int(time.time())
    claims = CapabilityClaims(
        run_id=run_id,
        agent_revision_id=manifest.agent_id,
        allowed_model_roles=("agent",),
        max_tokens=int(budget.get("max_tokens", 0)),
        max_cost=Decimal(str(budget.get("max_cost", "0"))),
        issued_at=issued_at,
        expires_at=issued_at + 300,
        nonce=secrets.token_urlsafe(18),
    )
    token = signer.issue(claims)
    owned_client = None
    transport = agent_service_transport
    if transport is None:
        owned_client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        transport = HttpxAgentServiceTransport(owned_client)

    def service_factory():
        return AgentServiceRuntime.from_settings(
            config=ServiceConfig(
                endpoint=endpoint,
                expected_manifest_sha256=service_manifest_sha256,
                agent_revision_id=manifest.agent_id,
                capability_token=token,
            ),
            transport=transport,
            settings=settings,
            resolver=agent_service_resolver,
        )

    guard = CapabilityBudgetGuard(
        signer=signer,
        token=token,
        run_id=run_id,
        agent_revision_id=manifest.agent_id,
    )
    return service_factory, guard, owned_client


def _resolve_model_provider(
    *,
    settings: Settings,
    session: Session,
    task_spec: dict,
    model_id: str,
    resolver,
) -> DeferredSecretProvider:
    model = session.get(ModelConfigRevisionORM, model_id)
    if (
        model is None
        or model.secret_ref_id is None
        or task_spec.get("model_config_sha256") != model.sha256
    ):
        raise RunnerConfigurationError("task model config is invalid")
    secret = session.get(SecretRefORM, model.secret_ref_id)
    if secret is None:
        raise RunnerConfigurationError("task model credential was not found")
    try:
        envelope = encrypted_secret_from_json(secret.ciphertext)
        parameters = json.loads(model.parameters_json)
        pricing = json.loads(model.pricing_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RunnerConfigurationError("task model credential is invalid") from exc
    if not isinstance(parameters, dict) or not isinstance(pricing, dict):
        raise RunnerConfigurationError("task model config is invalid")
    if envelope.secret_ref_id != secret.id or envelope.provider != model.provider:
        raise RunnerConfigurationError("task model credential binding is invalid")
    if model.provider != "openai-compatible":
        raise RunnerConfigurationError("task model provider is not allowlisted")
    return DeferredSecretProvider(
        secret_store=SecretStore(bytes.fromhex(settings.master_key)),
        encrypted_secret=envelope,
        provider_factory=lambda api_key: OpenAICompatibleProvider(
            base_url=model.endpoint,
            api_key=api_key,
            allowed_hosts=settings.model_provider_allowlist,
            resolver=resolver,
        ),
    )


async def _worker_loop(settings: Settings) -> None:
    while True:
        status = await run_pending_once(settings=settings)
        if status is None:
            await asyncio.sleep(settings.runner_poll_seconds)


def run_forever() -> int:
    settings = Settings()
    try:
        asyncio.run(_worker_loop(settings))
    except KeyboardInterrupt:
        return 0
    return 0
