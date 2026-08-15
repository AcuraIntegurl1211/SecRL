from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.agents.capabilities import CapabilitySigner, InvalidCapability
from secrl_platform.agents.protocol import EpisodeContext
from secrl_platform.benchmarks.protocol import Observation


_MANIFEST = json.loads(
    (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
)


class ServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSessionRequest(ServiceRequest):
    request_id: str
    sequence: int
    episode: EpisodeContext


class ActRequest(ServiceRequest):
    request_id: str
    sequence: int
    observation: Observation


class CloseRequest(ServiceRequest):
    request_id: str


@dataclass
class _Session:
    runtime: DeterministicSmokeAgent
    episode: EpisodeContext
    sequence: int = 0
    responses: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)


def create_app(signer: CapabilitySigner | None = None) -> FastAPI:
    signer = signer or _signer_from_environment()
    app = FastAPI(title="Agent Service Protocol v1 Reference", version="1.0.0")
    sessions: dict[str, _Session] = {}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/manifest")
    async def manifest():
        return _MANIFEST

    @app.post("/v1/sessions")
    async def create_session(
        request: CreateSessionRequest,
        authorization: str = Header(default=""),
    ):
        _verify_authorization(
            signer,
            authorization,
            run_id=request.episode.run_id,
        )
        if request.sequence != 0:
            raise HTTPException(status_code=409, detail="invalid sequence")
        runtime = DeterministicSmokeAgent()
        await runtime.reset(request.episode)
        session_id = str(uuid.uuid4())
        sessions[session_id] = _Session(runtime=runtime, episode=request.episode)
        return {"session_id": session_id}

    @app.post("/v1/sessions/{session_id}:act")
    async def act(
        session_id: str,
        request: ActRequest,
        authorization: str = Header(default=""),
    ):
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        _verify_authorization(
            signer,
            authorization,
            run_id=session.episode.run_id,
        )
        key = (request.request_id, request.sequence)
        if key in session.responses:
            return session.responses[key]
        if request.sequence != session.sequence + 1:
            raise HTTPException(status_code=409, detail="invalid sequence")
        action = await session.runtime.act(request.observation)
        usage = session.runtime.usage()
        response = {
            "action": action.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
        }
        session.responses[key] = response
        session.sequence = request.sequence
        return response

    @app.post("/v1/sessions/{session_id}:close")
    async def close(
        session_id: str,
        _request: CloseRequest,
        authorization: str = Header(default=""),
    ):
        session = sessions.get(session_id)
        if session is None:
            return {"closed": True}
        _verify_authorization(
            signer,
            authorization,
            run_id=session.episode.run_id,
        )
        try:
            await session.runtime.close()
        finally:
            del sessions[session_id]
        return {"closed": True}

    return app


def _verify_authorization(
    signer: CapabilitySigner,
    authorization: str,
    *,
    run_id: str,
) -> None:
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="capability required")
    try:
        signer.verify(
            token,
            expected_run=run_id,
            expected_agent=DeterministicSmokeAgent.revision().id,
        )
    except InvalidCapability as exc:
        raise HTTPException(status_code=401, detail="invalid capability") from exc


def _signer_from_environment() -> CapabilitySigner:
    encoded = os.environ.get("AGENT_SERVICE_CAPABILITY_SECRET", "")
    try:
        secret = bytes.fromhex(encoded)
    except ValueError as exc:
        raise RuntimeError("capability signing secret is not configured") from exc
    if len(secret) < 32:
        raise RuntimeError("capability signing secret is not configured")
    return CapabilitySigner(secret)
