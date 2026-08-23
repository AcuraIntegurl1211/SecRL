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

from secrl_platform.agents.builtin import (
    BUILTIN_AGENT_IDS,
    DeterministicSmokeAgent,
    LegacyGatewayClient,
    builtin_manifest,
    create_approved_builtin,
)
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
from secrl_platform.benchmarks.secrl import (
    SecRLAdapter,
    SecRLMySQLQueryExecutor,
    SecRLRunSpec,
)
from secrl_platform.config import Settings
from secrl_platform.models.gateway import ModelGateway
from secrl_platform.models.evaluator import (
    EvaluatorGatewayClient,
    EvaluatorProfile,
    SecRLEvaluator,
    official_secrl_profile,
)
from secrl_platform.models.pricing import Pricing
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
    secrl_query_executor=None,
    secrl_evaluator_resolver=None,
    builtin_runtime_resolver=None,
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
            builtin_runtime_resolver=builtin_runtime_resolver,
        )
        adapter = _resolve_adapter(
            settings=settings,
            session_factory=sessions,
            task_id=task_id,
            run_id=run_id,
            secrl_query_executor=secrl_query_executor,
            model_provider_resolver=model_provider_resolver,
            secrl_evaluator_resolver=secrl_evaluator_resolver,
        )
        engine = RunnerEngine(
            repository=repository,
            artifact_store=artifacts,
            adapter=adapter,
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
    builtin_runtime_resolver,
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
        model_bundle = None
        if task.model_config_revision_id is not None:
            model_bundle = _resolve_model_provider(
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
        agent_parameters = task_spec.get("agent_parameters", {})
        limits = task_spec.get("limits", {})

    if agent_kind == "BUILT_IN":
        revision = DeterministicSmokeAgent.revision()
        if manifest.agent_id == revision.manifest.agent_id:
            if agent.sha256 != revision.manifest_sha256 or manifest != revision.manifest:
                raise RunnerConfigurationError("built-in agent revision is not allowlisted")
            return DeterministicSmokeAgent, None, None
        if manifest.agent_id not in BUILTIN_AGENT_IDS:
            raise RunnerConfigurationError("built-in agent revision is not allowlisted")
        approved = builtin_manifest(manifest.agent_id)
        if agent.sha256 != approved.sha256() or manifest != approved:
            raise RunnerConfigurationError("built-in agent revision is not allowlisted")
        if model_bundle is None:
            raise RunnerConfigurationError("SecRL built-in agent requires a model config")
        provider, model_name, model_parameters, pricing = model_bundle
        signer = capability_signer(settings)
        token = _issue_capability(signer, run_id, manifest.agent_id, budget)
        gateway = ModelGateway(
            provider=provider,
            pricing=Pricing(
                input_per_million=pricing.get("input_per_million"),
                output_per_million=pricing.get("output_per_million"),
            ),
            capability_signer=signer,
        )
        max_output_tokens = model_parameters.get("max_output_tokens") or model_parameters.get("max_tokens")
        if not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise RunnerConfigurationError("SecRL model requires a positive output token limit")
        model_client = LegacyGatewayClient(
            gateway=gateway,
            model=model_name,
            capability_token=token,
            agent_revision_id=manifest.agent_id,
            max_output_tokens=max_output_tokens,
        )
        parameters = dict(agent_parameters)
        parameters.setdefault("max_steps", limits.get("max_steps", 15))

        def builtin_factory():
            if builtin_runtime_resolver is not None:
                return builtin_runtime_resolver(
                    manifest.agent_id,
                    parameters,
                    model_client,
                    model_name,
                )
            return create_approved_builtin(
                manifest.agent_id,
                parameters,
                model_client=model_client,
                model_name=model_name,
            )

        return builtin_factory, CapabilityBudgetGuard(
            signer=signer,
            token=token,
            run_id=run_id,
            agent_revision_id=manifest.agent_id,
        ), None

    if agent_kind != "SERVICE" or endpoint is None or service_manifest_sha256 is None:
        raise RunnerConfigurationError("Agent Service registration is incomplete")
    signer = capability_signer(settings)
    token = _issue_capability(signer, run_id, manifest.agent_id, budget)
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
) -> tuple[DeferredSecretProvider, str, dict, dict]:
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
    provider = DeferredSecretProvider(
        secret_store=SecretStore(bytes.fromhex(settings.master_key)),
        encrypted_secret=envelope,
        provider_factory=lambda api_key: OpenAICompatibleProvider(
            base_url=model.endpoint,
            api_key=api_key,
            allowed_hosts=settings.model_provider_allowlist,
            resolver=resolver,
        ),
    )
    return provider, model.model, parameters, pricing


def _issue_capability(
    signer: CapabilitySigner,
    run_id: str,
    agent_revision_id: str,
    budget: dict,
    *,
    allowed_model_roles: tuple[str, ...] = ("agent",),
) -> str:
    issued_at = int(time.time())
    return signer.issue(
        CapabilityClaims(
            run_id=run_id,
            agent_revision_id=agent_revision_id,
            allowed_model_roles=allowed_model_roles,
            max_tokens=int(budget.get("max_tokens", 0)),
            max_cost=Decimal(str(budget.get("max_cost", "0"))),
            issued_at=issued_at,
            expires_at=issued_at + 300,
            nonce=secrets.token_urlsafe(18),
        )
    )


def _resolve_adapter(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    task_id: str,
    run_id: str,
    secrl_query_executor,
    model_provider_resolver,
    secrl_evaluator_resolver,
):
    with session_factory() as session:
        task = session.get(EvaluationTaskORM, task_id)
        run = session.get(RunORM, run_id)
        if task is None or run is None:
            raise RunnerConfigurationError("task RunSpec was not found")
        try:
            task_spec = json.loads(task.task_spec_json)
            run_spec = json.loads(run.run_spec_json)
            budget = json.loads(task.budget_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RunnerConfigurationError("task RunSpec is invalid") from exc
        agent = session.get(AgentRevisionORM, task.agent_revision_id)
        if agent is None:
            raise RunnerConfigurationError("task agent revision was not found")
        try:
            agent_manifest = AgentManifest.model_validate_json(agent.manifest_json)
        except ValueError as exc:
            raise RunnerConfigurationError("task agent revision is invalid") from exc
        model_bundle = None
        if task.model_config_revision_id is not None:
            model_bundle = _resolve_model_provider(
                settings=settings,
                session=session,
                task_spec=task_spec,
                model_id=task.model_config_revision_id,
                resolver=model_provider_resolver,
            )
    if hashlib.sha256(run.run_spec_json.encode("utf-8")).hexdigest() != run.run_spec_sha256:
        raise RunnerConfigurationError("task RunSpec hash changed")
    benchmark_id = task_spec.get("benchmark_id")
    if benchmark_id == "protocol-smoke":
        return ProtocolSmokeAdapter.load_default()
    if benchmark_id != "secrl":
        raise RunnerConfigurationError("task benchmark is not allowlisted")
    if model_bundle is None:
        raise RunnerConfigurationError("SecRL evaluator model config is missing")
    limits = run_spec.get("limits", {})
    try:
        frozen_limits = SecRLRunSpec(**limits)
    except (TypeError, ValueError) as exc:
        raise RunnerConfigurationError("SecRL RunSpec limits are invalid") from exc
    executor = secrl_query_executor
    if executor is None:
        if not settings.secrl_runtime_enabled or settings.secrl_mysql_password is None:
            raise RunnerConfigurationError("SecRL runtime is not configured")
        executor = SecRLMySQLQueryExecutor(
            user=settings.secrl_mysql_user,
            password=settings.secrl_mysql_password.get_secret_value(),
            database=settings.secrl_mysql_database,
        ).query_sql
    try:
        frozen_profile = EvaluatorProfile.model_validate(
            task_spec["evaluator_profile"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RunnerConfigurationError("SecRL evaluator profile is invalid") from exc
    expected_profile = official_secrl_profile(
        formal=True,
        model_revision=task_spec.get("model_config_sha256", ""),
    )
    if frozen_profile != expected_profile:
        raise RunnerConfigurationError("SecRL evaluator profile is not approved")
    if secrl_evaluator_resolver is not None:
        evaluator = secrl_evaluator_resolver(frozen_profile)
    else:
        provider, model_name, model_parameters, pricing = model_bundle
        signer = capability_signer(settings)
        token = _issue_capability(
            signer,
            run_id,
            agent_manifest.agent_id,
            budget,
            allowed_model_roles=("evaluator",),
        )
        max_output_tokens = model_parameters.get("max_output_tokens") or model_parameters.get("max_tokens")
        if not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise RunnerConfigurationError("SecRL evaluator requires a positive output token limit")
        gateway = ModelGateway(
            provider=provider,
            pricing=Pricing(
                input_per_million=pricing.get("input_per_million"),
                output_per_million=pricing.get("output_per_million"),
            ),
            capability_signer=signer,
        )
        evaluator = SecRLEvaluator(
            frozen_profile,
            model_client=EvaluatorGatewayClient(
                gateway=gateway,
                model=model_name,
                capability_token=token,
                agent_revision_id=agent_manifest.agent_id,
                max_output_tokens=max_output_tokens,
            ),
        )
    return SecRLAdapter(
        query_executor=executor,
        run_spec=frozen_limits,
        evaluator=evaluator,
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
