from __future__ import annotations

import hashlib
import json
import uuid

import httpx
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from secrl_platform.agents.builtin import (
    BUILTIN_AGENT_IDS,
    DeterministicSmokeAgent,
    builtin_manifest,
)
from secrl_platform.agents.protocol import AgentManifest
from secrl_platform.agents.service import (
    AgentServiceEndpointPolicy,
    AgentServiceError,
    HttpxAgentServiceTransport,
    inspect_agent_service,
    manifest_sha256,
)
from secrl_platform.api.dependencies import (
    ApiContext,
    get_context,
    require_csrf_user,
    require_user,
)
from secrl_platform.api.errors import ApiError
from secrl_platform.api.schemas import AgentCreateRequest, ModelCreateRequest
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.benchmarks.secrl import SecRLAdapter
from secrl_platform.benchmarks.protocol import Scope
from secrl_platform.models.providers import validate_model_endpoint
from secrl_platform.models.secrets import encrypted_secret_to_json
from secrl_platform.storage.orm import (
    AgentRevisionORM,
    LocalUserORM,
    ModelConfigRevisionORM,
    SecretRefORM,
)
from secrl_platform.storage.repositories import canonical_json


router = APIRouter()


@router.get("/models", tags=["models"])
def list_models(
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> list[dict]:
    with context.session_factory() as session:
        models = session.scalars(
            select(ModelConfigRevisionORM).order_by(
                ModelConfigRevisionORM.created_at,
                ModelConfigRevisionORM.id,
            )
        ).all()
        return [_model_payload(model) for model in models]


@router.post("/models", tags=["models"], status_code=201)
def create_model(
    payload: ModelCreateRequest,
    request: Request,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    if payload.provider != "openai-compatible":
        raise ApiError(422, "INVALID_MODEL_CONFIG", "Model provider is invalid")
    try:
        validate_model_endpoint(
            payload.endpoint,
            allowed_hosts=context.model_provider_allowlist,
            resolver=context.model_provider_resolver,
            insecure_hosts=context.allow_insecure_model_endpoints,
        )
    except ValueError:
        raise ApiError(
            422,
            "INVALID_MODEL_CONFIG",
            "Model endpoint is not an allowed HTTPS OpenAI-compatible URL; use a platform-approved provider host such as api.deepseek.com.",
            details={"next_step": "Use an HTTPS endpoint without embedded credentials and ask an administrator to approve a new host if needed."},
        )
    api_key = request.headers.get("X-Model-API-Key")
    if not api_key:
        raise ApiError(
            422,
            "MODEL_CREDENTIAL_MISSING",
            "Model API key is missing; provide it so the platform can encrypt the credential before saving.",
            details={
                "secret_status": "missing",
                "next_step": "Enter the provider API key and submit the model again.",
            },
        )
    parameters = payload.parameters.model_dump(mode="json", exclude_none=True)
    pricing = payload.pricing.model_dump(mode="json", exclude_none=True)
    secret_ref_id = str(uuid.uuid4())
    frozen = {
        "endpoint": payload.endpoint,
        "model": payload.model,
        "name": payload.name,
        "parameters": parameters,
        "pricing": pricing,
        "provider": payload.provider,
        "secret_ref_id": secret_ref_id,
    }
    digest = hashlib.sha256(canonical_json(frozen).encode("utf-8")).hexdigest()
    with context.session_factory.begin() as session:
        existing = session.scalar(
            select(ModelConfigRevisionORM).where(ModelConfigRevisionORM.sha256 == digest)
        )
        if existing is not None:
            model = existing
        else:
            if secret_ref_id is not None:
                if context.secret_store is None:
                    raise ApiError(
                        503,
                        "SECRET_STORE_UNAVAILABLE",
                        "Model credential storage is unavailable; the platform master key must be configured before saving a model.",
                        details={"next_step": "Configure SECRL_MASTER_KEY and restart the platform."},
                    )
                encrypted = context.secret_store.encrypt(
                    api_key,
                    secret_ref_id=secret_ref_id,
                    owner_id=_user.id,
                    provider=payload.provider,
                )
                session.add(
                    SecretRefORM(
                        id=secret_ref_id,
                        name=f"model-credential-{secret_ref_id}",
                        ciphertext=encrypted_secret_to_json(encrypted),
                        status="UNVERIFIED",
                    )
                )
                session.flush()
            model = ModelConfigRevisionORM(
                name=payload.name,
                provider=payload.provider,
                endpoint=payload.endpoint,
                model=payload.model,
                secret_ref_id=secret_ref_id,
                parameters_json=canonical_json(parameters),
                pricing_json=canonical_json(pricing),
                sha256=digest,
            )
            session.add(model)
            session.flush()
        return _model_payload(model)


@router.get("/agents", tags=["agents"])
def list_agents(
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> list[dict]:
    with context.session_factory() as session:
        agents = session.scalars(
            select(AgentRevisionORM).order_by(
                AgentRevisionORM.created_at,
                AgentRevisionORM.id,
            )
        ).all()
        return [_agent_payload(agent) for agent in agents]


@router.post("/agents", tags=["agents"], status_code=201)
async def create_agent(
    payload: AgentCreateRequest,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    if payload.kind == "SERVICE":
        if payload.endpoint is None or payload.manifest_sha256 is None:
            raise ApiError(
                422,
                "INVALID_AGENT_CONFIG",
                "Agent Service registration is incomplete",
            )
        service_manifest = await _inspect_service(context, payload.endpoint)
        service_payload = service_manifest.model_dump(mode="json")
        if (
            service_manifest.agent_revision_id != payload.revision_id
            or manifest_sha256(service_payload) != payload.manifest_sha256
        ):
            raise ApiError(
                422,
                "AGENT_SERVICE_CHECK_FAILED",
                "Agent Service manifest did not match registration; verify the endpoint and SHA-256 revision.",
                details={"next_step": "Run the service manifest check again and register the exact returned revision and hash."},
            )
        manifest = AgentManifest(
            agent_id=service_manifest.agent_revision_id,
            name=service_manifest.name,
            version=service_manifest.version,
            runtime="service",
            protocol_version="1",
            parameter_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        )
        digest = manifest.sha256()
        with context.session_factory.begin() as session:
            existing = session.scalar(
                select(AgentRevisionORM).where(AgentRevisionORM.sha256 == digest)
            )
            if existing is not None and (
                existing.kind != "SERVICE"
                or existing.service_endpoint != payload.endpoint
                or existing.service_manifest_sha256 != payload.manifest_sha256
            ):
                raise ApiError(
                    409,
                    "AGENT_REVISION_CONFLICT",
                    "Agent revision is already registered differently",
                )
            if existing is None:
                existing = AgentRevisionORM(
                    name=manifest.name,
                    kind="SERVICE",
                    manifest_json=canonical_json(manifest.model_dump(mode="json")),
                    parameter_schema_json=canonical_json(manifest.parameter_schema),
                    sha256=digest,
                    service_endpoint=payload.endpoint,
                    service_manifest_sha256=payload.manifest_sha256,
                )
                session.add(existing)
                session.flush()
            return _agent_payload(existing)

    smoke_revision = DeterministicSmokeAgent.revision()
    if payload.revision_id == smoke_revision.id:
        manifest = smoke_revision.manifest
        digest = smoke_revision.manifest_sha256
    elif payload.revision_id in BUILTIN_AGENT_IDS:
        manifest = builtin_manifest(payload.revision_id)
        digest = manifest.sha256()
    else:
        raise ApiError(
            422,
            "AGENT_REVISION_NOT_ALLOWLISTED",
            "Agent revision is not allowlisted; choose a platform-approved built-in revision or register a checked Agent Service.",
            details={"next_step": "Use a revision shown in the Agents page."},
        )
    with context.session_factory.begin() as session:
        existing = session.scalar(
            select(AgentRevisionORM).where(AgentRevisionORM.sha256 == digest)
        )
        if existing is None:
            existing = AgentRevisionORM(
                name=manifest.name,
                kind="BUILT_IN",
                manifest_json=canonical_json(manifest.model_dump(mode="json")),
                parameter_schema_json=canonical_json(manifest.parameter_schema),
                sha256=digest,
            )
            session.add(existing)
            session.flush()
        return _agent_payload(existing)


@router.post("/agents/{id}:check", tags=["agents"])
async def check_agent(
    id: str,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    with context.session_factory() as session:
        agent = session.get(AgentRevisionORM, id)
        if agent is None:
            raise ApiError(404, "AGENT_NOT_FOUND", "Agent revision was not found")
        kind = agent.kind
        endpoint = agent.service_endpoint
        expected_service_sha256 = agent.service_manifest_sha256
        stored_manifest = json.loads(agent.manifest_json)
        sha256 = agent.sha256
    if kind == "BUILT_IN":
        smoke_revision = DeterministicSmokeAgent.revision()
        agent_id = stored_manifest.get("agent_id")
        if agent_id == smoke_revision.manifest.agent_id:
            expected_manifest = smoke_revision.manifest
            expected_sha256 = smoke_revision.manifest_sha256
        elif agent_id in BUILTIN_AGENT_IDS:
            expected_manifest = builtin_manifest(agent_id)
            expected_sha256 = expected_manifest.sha256()
        else:
            expected_manifest = None
            expected_sha256 = None
        if expected_manifest is None or sha256 != expected_sha256 or stored_manifest != expected_manifest.model_dump(mode="json"):
            raise ApiError(
                409,
                "AGENT_REVISION_INVALID",
                "Built-in agent revision no longer matches registration",
            )
    else:
        if endpoint is None or expected_service_sha256 is None:
            raise ApiError(
                409,
                "AGENT_REVISION_INVALID",
                "Agent Service registration is incomplete",
            )
        service_manifest = await _inspect_service(context, endpoint)
        service_payload = service_manifest.model_dump(mode="json")
        if (
            manifest_sha256(service_payload) != expected_service_sha256
            or service_manifest.agent_revision_id != stored_manifest.get("agent_id")
            or service_manifest.name != stored_manifest.get("name")
            or service_manifest.version != stored_manifest.get("version")
        ):
            raise ApiError(
                409,
                "AGENT_SERVICE_CHECK_FAILED",
                "Agent Service manifest no longer matches registration",
            )
    return {"id": id, "status": "valid", "sha256": sha256}


@router.get("/benchmarks", tags=["benchmarks"])
def list_benchmarks(_user: LocalUserORM = Depends(require_user)) -> list[dict]:
    return [_benchmark_payload(adapter) for adapter in _benchmark_adapters()]


@router.get("/benchmarks/{benchmark_id}/cases", tags=["benchmarks"])
def list_benchmark_cases(
    benchmark_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    scenario: str | None = Query(None, min_length=1, max_length=256),
    _user: LocalUserORM = Depends(require_user),
) -> dict:
    adapter = next(
        (
            candidate
            for candidate in _benchmark_adapters()
            if candidate.manifest().benchmark_id == benchmark_id
        ),
        None,
    )
    if adapter is None:
        raise ApiError(404, "BENCHMARK_NOT_FOUND", "Benchmark revision was not found")
    cases = adapter.enumerate_cases(adapter.dataset_ref(), Scope.all())
    if scenario is not None:
        cases = [case for case in cases if case.scenario.id == scenario]
    items = []
    for index, case in enumerate(cases[offset : offset + limit], start=offset):
        public_input = dict(case.public_input)
        items.append(
            {
                "id": case.id,
                "scenario_id": case.scenario.id,
                "ordinal": int(public_input.get("ordinal", index)),
                "public_input": public_input,
                "public_input_sha256": hashlib.sha256(
                    canonical_json(public_input).encode("utf-8")
                ).hexdigest(),
            }
        )
    return {
        "benchmark_id": benchmark_id,
        "dataset_sha256": adapter.dataset_ref().sha256,
        "total": len(cases),
        "offset": offset,
        "limit": limit,
        "items": items,
    }


def _benchmark_adapters():
    return (ProtocolSmokeAdapter.load_default(), SecRLAdapter())


def _benchmark_payload(adapter) -> dict:
    manifest = adapter.manifest().model_dump(mode="json")
    dataset_ref = adapter.dataset_ref().model_dump(mode="json", exclude={"source"})
    dataset = {
        **dataset_ref,
        "case_count": manifest.get("case_count")
        or len(adapter.enumerate_cases(adapter.dataset_ref(), Scope.all())),
        "split": manifest.get("dataset_split"),
        "schema_version": manifest.get("dataset_schema_version"),
    }
    incident_counts = getattr(adapter, "incident_counts", None)
    if callable(incident_counts):
        dataset["incidents"] = incident_counts()
    return {"manifest": manifest, "dataset": dataset}


def _model_payload(model: ModelConfigRevisionORM) -> dict:
    parameters = json.loads(model.parameters_json)
    pricing = json.loads(model.pricing_json)
    return {
        "id": model.id,
        "name": model.name,
        "provider": model.provider,
        "endpoint": model.endpoint,
        "model": model.model,
        "parameter_names": sorted(parameters),
        "pricing_configured": bool(pricing),
        "sha256": model.sha256,
        "credential_configured": model.secret_ref_id is not None,
    }


def _agent_payload(agent: AgentRevisionORM) -> dict:
    payload = {
        "id": agent.id,
        "name": agent.name,
        "kind": agent.kind,
        "manifest": json.loads(agent.manifest_json),
        "sha256": agent.sha256,
    }
    if agent.kind == "SERVICE":
        payload["endpoint"] = agent.service_endpoint
        payload["service_manifest_sha256"] = agent.service_manifest_sha256
    return payload


async def _inspect_service(context: ApiContext, endpoint: str):
    policy = AgentServiceEndpointPolicy(
        allowed_hosts=context.agent_service_allowlist,
    )
    try:
        if context.agent_service_transport is not None:
            return await inspect_agent_service(
                endpoint=endpoint,
                transport=context.agent_service_transport,
                policy=policy,
                resolver=context.agent_service_resolver,
            )
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await inspect_agent_service(
                endpoint=endpoint,
                transport=HttpxAgentServiceTransport(client),
                policy=policy,
                resolver=context.agent_service_resolver,
            )
    except (AgentServiceError, ValueError) as exc:
        raise ApiError(
            422,
            "AGENT_SERVICE_CHECK_FAILED",
            "Agent Service validation failed; verify the allowlisted endpoint and protocol v1 manifest.",
            details={
                "next_step": "Check that the endpoint is reachable, returns protocol v1, and matches the supplied manifest hash.",
            },
        ) from exc
