from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.api.dependencies import ApiContext, get_context, require_user
from secrl_platform.benchmarks.secrl import SecRLAdapter
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
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    checks: list[dict[str, str]] = []
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

        if benchmark_id == "secrl":
            if context.secrl_runtime_enabled:
                environment_ready = True
                if context.secrl_environment_probe is not None:
                    try:
                        environment_ready = context.secrl_environment_probe()
                    except Exception:
                        environment_ready = False
                checks.append(
                    _check(
                        "environment",
                        "ready" if environment_ready else "missing",
                        (
                            "SecRL Incident database connection is available."
                            if environment_ready
                            else "SecRL Incident database is unreachable; verify the read-only credentials and Incident service."
                        ),
                        code=None if environment_ready else "SECRL_ENV_UNAVAILABLE",
                    )
                )
            else:
                checks.append(
                    _check(
                        "environment",
                        "missing",
                        "SecRL environment credentials are missing; configure the read-only Incident database credentials.",
                        code="SECRL_ENV_NOT_CONFIGURED",
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
        elif secret is not None:
            model_check = _check("model_secret", "ready", "Model credential is configured (value withheld).")
            model_check["secret_status"] = "configured"
            checks.append(model_check)
        else:
            model_message = (
                "SecRL runs require a model revision with an encrypted credential; select a model before queuing."
                if benchmark_id == "secrl" and model_config_revision_id is None
                else "The selected model has no configured credential; save an API key before queuing a run."
            )
            model_code = (
                "MODEL_CONFIG_MISSING"
                if benchmark_id == "secrl" and model_config_revision_id is None
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
        "dataset": (
            {"revision": SecRLAdapter().dataset_ref().version, "sha256": SecRLAdapter().dataset_ref().sha256}
            if benchmark_id == "secrl"
            else None
        ),
    }


def _check(name: str, status: str, message: str, *, code: str | None = None) -> dict[str, str]:
    result = {"name": name, "status": status, "message": message}
    if code is not None:
        result["code"] = code
    return result
