from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.api.dependencies import ApiContext, get_context, require_user
from secrl_platform.api.errors import ApiError
from secrl_platform.api.scope import (
    AmbiguousScopeError,
    InvalidScopeError,
    ScopeMode,
    canonical_scope_mode,
)
from secrl_platform.benchmarks.protocol import Scope
from secrl_platform.benchmarks.secrl import SecRLAdapter
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.storage.orm import (
    AgentRevisionORM,
    LocalUserORM,
    ModelConfigRevisionORM,
    SecretRefORM,
)


router = APIRouter(tags=["preflight"])


@router.get("/preflight")
def preflight(
    benchmark_id: str = Query("secrl", min_length=1, max_length=64),
    model_config_revision_id: str | None = Query(None, max_length=128),
    agent_revision_id: str | None = Query(None, max_length=128),
    scope_mode: ScopeMode | None = Query(None),
    case_ids: tuple[str, ...] = Query(default=()),
    incident_ids: tuple[str, ...] = Query(default=()),
    all_cases: bool = Query(False),
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    checks: list[dict[str, str]] = []
    scope_summary: dict[str, object] | None = None
    with context.session_factory() as session:
        try:
            session.execute(text("SELECT 1"))
            checks.append(_check("database", "ready", "Incident database connection is available."))
        except Exception:
            checks.append(
                _check(
                    "database",
                    "missing",
                    "The platform database is unavailable; start it or check the configured database path.",
                    code="DATABASE_UNAVAILABLE",
                )
            )

        try:
            canonical_scope_mode(
                scope_mode=scope_mode,
                case_ids=case_ids,
                incident_ids=incident_ids,
                all_cases=all_cases,
                allow_empty_all_benchmark=True,
            )
        except AmbiguousScopeError as exc:
            raise _ambiguous_scope_error() from exc
        except InvalidScopeError as exc:
            raise _invalid_scope_error() from exc

        if benchmark_id in {"secrl", "protocol-smoke"}:
            try:
                selected_scope = _selected_scope(
                    benchmark_id=benchmark_id,
                    scope_mode=scope_mode,
                    case_ids=case_ids,
                    incident_ids=incident_ids,
                    all_cases=all_cases,
                )
                selected_incidents = selected_scope[2]
                scope_summary = {
                    "mode": selected_scope[0],
                    "case_count": len(selected_scope[1]),
                    "incident_count": len(selected_incidents),
                    "incident_ids": list(selected_incidents),
                }
            except AmbiguousScopeError as exc:
                raise _ambiguous_scope_error() from exc
            except (InvalidScopeError, KeyError, TypeError, ValueError) as exc:
                raise _invalid_scope_error() from exc
            if benchmark_id != "secrl":
                checks.append(
                    _check(
                        "environment",
                        "not_applicable",
                        "The selected benchmark does not require the SecRL Incident environment.",
                    )
                )
            elif context.secrl_runtime_enabled:
                environment_status = {incident_id: False for incident_id in selected_incidents}
                if context.secrl_environment_probe is not None:
                    try:
                        probe_result = context.secrl_environment_probe(selected_incidents)
                        if isinstance(probe_result, Mapping):
                            environment_status = {
                                incident_id: probe_result.get(incident_id, False)
                                is True
                                for incident_id in selected_incidents
                            }
                        else:
                            environment_status = {
                                incident_id: probe_result is True
                                for incident_id in selected_incidents
                            }
                    except Exception:
                        environment_status = {
                            incident_id: False for incident_id in selected_incidents
                        }
                unavailable_incidents = [
                    incident_id
                    for incident_id, available in environment_status.items()
                    if not available
                ]
                if unavailable_incidents:
                    checks.append(
                        _check(
                            "environment",
                            "missing",
                            "Selected SecRL Incident service(s) are unavailable; start the required Compose profile(s) and retry preflight.",
                            code="SECRL_ENV_UNAVAILABLE",
                            unavailable_incidents=unavailable_incidents,
                            start_command=_incident_start_command(unavailable_incidents),
                        )
                    )
                else:
                    checks.append(
                        _check(
                            "environment",
                            "ready",
                            "SecRL Incident database connection is available.",
                        )
                    )
            else:
                checks.append(
                    _check(
                        "environment",
                        "missing",
                        "SecRL environment credentials are missing; configure the read-only Incident database credentials.",
                        code="SECRL_ENV_NOT_CONFIGURED",
                        start_command=_incident_start_command(selected_incidents),
                    )
                )
        else:
            checks.append(_check("environment", "not_applicable", "The selected benchmark does not require the SecRL Incident environment."))

        model = (
            session.get(ModelConfigRevisionORM, model_config_revision_id)
            if model_config_revision_id
            else None
        )
        secret = session.get(SecretRefORM, model.secret_ref_id) if model and model.secret_ref_id else None
        if model_config_revision_id is None and benchmark_id != "secrl":
            checks.append(_check("model_secret", "not_applicable", "The selected deterministic run does not require a model credential."))
        elif secret is not None and secret.status != "INVALID":
            model_check = _check("model_secret", "ready", "Model credential is configured (value withheld).")
            model_check["secret_status"] = "configured"
            checks.append(model_check)
        else:
            model_message = (
                "SecRL runs require a model revision with an encrypted credential; select a model before queuing."
                if benchmark_id == "secrl" and model_config_revision_id is None
                else "The selected model credential is marked invalid; save a new API key before queuing a run."
                if secret is not None and secret.status == "INVALID"
                else "The selected model has no configured credential; save an API key before queuing a run."
            )
            model_code = (
                "MODEL_CONFIG_MISSING"
                if benchmark_id == "secrl" and model_config_revision_id is None
                else "MODEL_CREDENTIAL_INVALID"
                if secret is not None and secret.status == "INVALID"
                else "MODEL_CREDENTIAL_MISSING"
            )
            model_check = _check(
                "model_secret",
                "missing",
                model_message,
                code=model_code,
            )
            model_check["secret_status"] = "missing"
            checks.append(model_check)

        agent_exists = False
        if agent_revision_id == DeterministicSmokeAgent.revision().id:
            agent_exists = True
        elif agent_revision_id:
            agent_exists = session.get(AgentRevisionORM, agent_revision_id) is not None
        if agent_exists:
            checks.append(_check("agent_revision", "ready", "Agent revision is available."))
        else:
            checks.append(
                _check(
                    "agent_revision",
                    "missing",
                    "The selected agent revision is unavailable; register or select an approved revision.",
                    code="AGENT_REVISION_MISSING",
                )
            )

        checks.append(
            _check(
                "runner",
                "ready" if context.runner_configured else "missing",
                (
                    "Runner configuration is valid; the worker will acquire a lease when the run starts."
                    if context.runner_configured
                    else "Runner configuration is invalid; set a positive runner poll interval before queuing."
                ),
                code=None if context.runner_configured else "RUNNER_NOT_CONFIGURED",
            )
        )

    return {
        "ready": all(check["status"] in {"ready", "not_applicable"} for check in checks),
        "benchmark_id": benchmark_id,
        "checks": checks,
        "scope": scope_summary,
        "dataset": (
            {"revision": SecRLAdapter().dataset_ref().version, "sha256": SecRLAdapter().dataset_ref().sha256}
            if benchmark_id == "secrl"
            else None
        ),
    }


def _selected_scope(
    *,
    benchmark_id: str,
    scope_mode: ScopeMode | None,
    case_ids: tuple[str, ...],
    incident_ids: tuple[str, ...],
    all_cases: bool,
) -> tuple[ScopeMode, tuple[str, ...], tuple[str, ...]]:
    adapter = SecRLAdapter() if benchmark_id == "secrl" else ProtocolSmokeAdapter.load_default()
    mode = canonical_scope_mode(
        scope_mode=scope_mode,
        case_ids=case_ids,
        incident_ids=incident_ids,
        all_cases=all_cases,
        allow_empty_all_benchmark=True,
    )
    if mode == "INCIDENTS":
        if benchmark_id != "secrl":
            raise InvalidScopeError(
                "Incident selection is only supported by the SecRL benchmark"
            )
        resolved = adapter.resolve_case_ids(incident_ids=incident_ids)
        selected = incident_ids
    elif benchmark_id == "secrl":
        resolved = adapter.resolve_case_ids(
            case_ids=case_ids if mode == "CASES" else (),
            all_cases=mode == "ALL_BENCHMARK",
        )
        selected = adapter.incident_ids_for_case_ids(resolved)
    else:
        smoke_scope = Scope(
            case_ids=case_ids if mode == "CASES" else None,
        )
        resolved = tuple(
            case.id
            for case in adapter.enumerate_cases(adapter.dataset_ref(), smoke_scope)
        )
        selected = ()
    return mode, resolved, tuple(dict.fromkeys(selected))


def _incident_start_command(incident_ids: tuple[str, ...] | list[str]) -> str:
    profiles = " ".join(f"--profile {incident_id}" for incident_id in incident_ids)
    return f"docker compose {profiles} up -d"


def _ambiguous_scope_error() -> ApiError:
    return ApiError(
        422,
        "AMBIGUOUS_SCOPE",
        "Choose exactly one scope mode and provide only its matching selection fields.",
        details={
            "scope_modes": ["CASES", "INCIDENTS", "ALL_BENCHMARK"],
            "next_step": "Select a Scope mode and clear incompatible fields.",
        },
    )


def _invalid_scope_error() -> ApiError:
    return ApiError(
        422,
        "INVALID_TASK_SCOPE",
        "The selected Cases or Incidents are invalid; choose an available Case, Incident, or Benchmark.",
        details={"next_step": "Refresh the benchmark catalog and select a valid scope."},
    )


def _check(name: str, status: str, message: str, *, code: str | None = None, **details: object) -> dict[str, object]:
    result = {"name": name, "status": status, "message": message}
    if code is not None:
        result["code"] = code
    result.update(details)
    return result
