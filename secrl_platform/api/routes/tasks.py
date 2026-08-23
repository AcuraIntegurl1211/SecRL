from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends
from sqlalchemy import select

from secrl_platform.agents.builtin import (
    BUILTIN_AGENT_IDS,
    DeterministicSmokeAgent,
    _safe_config,
    builtin_manifest,
)
from secrl_platform.agents.protocol import AgentManifest, AgentRevisionRef
from secrl_platform.api.dependencies import (
    ApiContext,
    get_context,
    require_csrf_user,
    require_user,
)
from secrl_platform.api.errors import ApiError
from secrl_platform.api.schemas import TaskCreateRequest, TaskCreateResponse
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.benchmarks.secrl import SecRLAdapter, SecRLRunSpec
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.storage.orm import (
    AgentRevisionORM,
    EvaluationTaskORM,
    LocalUserORM,
    ModelConfigRevisionORM,
)


router = APIRouter(tags=["tasks"])


@router.get("/tasks")
def list_tasks(
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> list[dict]:
    with context.session_factory() as session:
        tasks = session.scalars(
            select(EvaluationTaskORM).order_by(
                EvaluationTaskORM.created_at,
                EvaluationTaskORM.id,
            )
        ).all()
        return [
            {
                "id": task.id,
                "name": task.name,
                "status": task.status,
                "task_spec": json.loads(task.task_spec_json),
                "task_spec_sha256": hashlib.sha256(
                    task.task_spec_json.encode("utf-8")
                ).hexdigest(),
            }
            for task in tasks
        ]


@router.post("/tasks", response_model=TaskCreateResponse, status_code=201)
def create_task(
    payload: TaskCreateRequest,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> TaskCreateResponse:
    if payload.benchmark_id == "protocol-smoke":
        adapter = ProtocolSmokeAdapter.load_default()
    elif payload.benchmark_id == "secrl":
        if not context.secrl_runtime_enabled:
            raise ApiError(
                503,
                "SECRL_RUNTIME_UNAVAILABLE",
                "SecRL environment credentials are not configured",
            )
        adapter = SecRLAdapter(
            run_spec=SecRLRunSpec(
                max_steps=payload.max_steps,
                max_str_len=payload.max_str_len,
                max_entry_return=payload.max_entry_return,
            )
        )
    else:
        raise ApiError(422, "INVALID_TASK_SPEC", "Unknown benchmark revision")
    revision = _resolve_agent_revision(context, payload.agent_revision_id)
    model_id, model_sha256 = _resolve_model_revision(
        context, payload.model_config_revision_id
    )
    if revision.manifest.agent_id in BUILTIN_AGENT_IDS:
        if model_id is None:
            raise ApiError(
                422,
                "INVALID_TASK_SPEC",
                "SecRL built-in agents require a model config",
            )
        with context.session_factory() as session:
            model = session.get(ModelConfigRevisionORM, model_id)
            parameters = json.loads(model.parameters_json) if model is not None else {}
            pricing = json.loads(model.pricing_json) if model is not None else {}
        output_limit = parameters.get("max_output_tokens", parameters.get("max_tokens"))
        if (
            not isinstance(output_limit, int)
            or output_limit < 1
            or pricing.get("input_per_million") is None
            or pricing.get("output_per_million") is None
        ):
            raise ApiError(
                422,
                "INVALID_TASK_SPEC",
                "SecRL built-in agents require output limits and frozen pricing",
            )
    budget = payload.budget.model_dump(mode="json", exclude_none=True)
    try:
        parameters = _safe_config(payload.agent_parameters)
        handle = RunnerRepository(context.session_factory).create_benchmark_run(
            name=payload.name,
            adapter=adapter,
            agent_revision=revision,
            case_ids=payload.case_ids,
            budget=budget,
            model_config_revision_id=model_id,
            model_config_sha256=model_sha256,
            run_limits={
                "max_steps": payload.max_steps,
                "max_str_len": payload.max_str_len,
                "max_entry_return": payload.max_entry_return,
            },
            agent_parameters=parameters,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(422, "INVALID_TASK_SPEC", "Invalid benchmark task") from exc
    with context.session_factory() as session:
        task = session.get(EvaluationTaskORM, handle.task_id)
        if task is None:
            raise ApiError(500, "INTERNAL_ERROR", "Task was not persisted")
        task_hash = hashlib.sha256(task.task_spec_json.encode("utf-8")).hexdigest()
        status = task.status
    return TaskCreateResponse(
        id=handle.task_id,
        run_id=handle.run_id,
        status=status,
        task_spec_sha256=task_hash,
    )


def _resolve_model_revision(
    context: ApiContext,
    revision_id: str | None,
) -> tuple[str | None, str | None]:
    if revision_id is None:
        return None, None
    with context.session_factory() as session:
        model = session.get(ModelConfigRevisionORM, revision_id)
        if model is None or model.secret_ref_id is None:
            raise ApiError(
                422,
                "INVALID_TASK_SPEC",
                "Model config is missing an encrypted credential",
            )
        return model.id, model.sha256


def _resolve_agent_revision(
    context: ApiContext,
    revision_id: str,
) -> AgentRevisionRef:
    builtin = DeterministicSmokeAgent.revision()
    if revision_id == builtin.id:
        return builtin
    with context.session_factory() as session:
        stored = session.get(AgentRevisionORM, revision_id)
        if stored is None:
            raise ApiError(422, "INVALID_TASK_SPEC", "Unknown agent revision")
        manifest_json = stored.manifest_json
        digest = stored.sha256
        endpoint = stored.service_endpoint
        service_manifest_sha256 = stored.service_manifest_sha256
    try:
        manifest = AgentManifest.model_validate_json(manifest_json)
        if stored.kind == "BUILT_IN":
            if manifest.agent_id not in BUILTIN_AGENT_IDS:
                raise ValueError("built-in agent is not allowlisted")
            approved = builtin_manifest(manifest.agent_id)
            if manifest != approved or digest != approved.sha256():
                raise ValueError("built-in agent revision changed")
        elif endpoint is None or service_manifest_sha256 is None:
            raise ValueError("Agent Service registration is incomplete")
        return AgentRevisionRef(
            id=manifest.agent_id,
            manifest=manifest,
            manifest_sha256=digest,
        )
    except ValueError as exc:
        raise ApiError(
            422,
            "INVALID_TASK_SPEC",
            "Invalid Agent Service revision",
        ) from exc
