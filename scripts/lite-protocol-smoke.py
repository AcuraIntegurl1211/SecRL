#!/usr/bin/env python3
"""Exercise the deployed HTTP boundary without calling an external model."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE_URL = os.environ.get("SECRL_GATE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")


def main() -> int:
    client = _Client(BASE_URL)
    health = client.public_get("/api/v1/health")
    login = client.request(
        "POST",
        "/api/v1/auth/login",
        {
            "username": _required("SECRL_INITIAL_ADMIN_USERNAME"),
            "password": _required("SECRL_INITIAL_ADMIN_PASSWORD"),
        },
    )
    client.csrf = str(login["csrf_token"])

    model_configured = False
    model_api_key = os.environ.get("SECRL_TEST_MODEL_API_KEY")
    if model_api_key:
        model = client.request(
            "POST",
            "/api/v1/models",
            {
                "name": "Release gate no-call model",
                "provider": "openai-compatible",
                "endpoint": "https://api.openai.com/v1",
                "model": "release-gate-no-call",
                "parameters": {"max_output_tokens": 64},
                "pricing": {"input_per_million": "1", "output_per_million": "1"},
            },
            headers={"X-Model-API-Key": model_api_key},
        )
        model_configured = bool(model["credential_configured"])

    builtin = client.request(
        "POST",
        "/api/v1/agents",
        {"kind": "BUILT_IN", "revision_id": "builtin-deterministic-smoke-v1"},
    )
    builtin_task, builtin_run = _run_smoke(client, "Built-in release gate", builtin["id"])

    manifest = json.loads(
        Path("examples/agent_service/manifest.json").read_text(encoding="utf-8")
    )
    manifest_sha256 = hashlib.sha256(_canonical_json(manifest).encode()).hexdigest()
    service = client.request(
        "POST",
        "/api/v1/agents",
        {
            "kind": "SERVICE",
            "revision_id": manifest["agent_revision_id"],
            "endpoint": "http://agent-service-reference:8081",
            "manifest_sha256": manifest_sha256,
        },
    )
    check = client.request("POST", f"/api/v1/agents/{service['id']}:check", {})
    service_task, service_run = _run_smoke(
        client, "Agent Service release gate", service["id"]
    )

    cases = client.request("GET", f"/api/v1/runs/{service_run}/cases")
    trajectory = client.request(
        "GET",
        f"/api/v1/runs/{service_run}/cases/smoke-001/trajectory?step=0",
    )
    artifacts = client.request("GET", f"/api/v1/runs/{service_run}/artifacts")
    analysis = client.request("POST", f"/api/v1/runs/{service_run}:analyze", {})
    analysis_history = client.request("GET", f"/api/v1/runs/{service_run}/analysis")
    attributions = client.request(
        "GET", f"/api/v1/runs/{service_run}/attributions"
    )
    review_revision = None
    if attributions:
        attribution = attributions[0]
        review = client.request(
            "POST",
            f"/api/v1/attributions/{attribution['id']}/reviews",
            {
                "primary": attribution["label"],
                "secondary": [],
                "confidence": "medium",
                "evidence": attribution["evidence"],
                "notes": "automated release gate",
            },
        )
        reviews = client.request(
            "GET", f"/api/v1/attributions/{attribution['id']}/reviews"
        )
        if not reviews or reviews[-1]["id"] != review["id"]:
            raise RuntimeError("HumanReview revision was not queryable")
        review_revision = review["revision"]
    audit = client.request("GET", f"/api/v1/runs/{service_run}/audit")

    tasks = client.request("GET", "/api/v1/tasks")
    expected = {builtin_task: builtin_run, service_task: service_run}
    if any(
        next(item for item in tasks if item["id"] == task_id)["run_id"] != run_id
        for task_id, run_id in expected.items()
    ):
        raise RuntimeError("task list does not preserve run identity")
    if check["status"] != "valid" or health.get("status") != "ok":
        raise RuntimeError("deployment health or Agent Service check failed")
    if not cases or not artifacts or trajectory["step"] != 0:
        raise RuntimeError("run result workflow is incomplete")
    if not analysis_history or analysis_history[-1]["id"] != analysis["id"]:
        raise RuntimeError("analysis history is incomplete")

    print(
        json.dumps(
            {
                "health": health["status"],
                "model_credential_configured": model_configured,
                "builtin_run": {"id": builtin_run, "status": "SUCCEEDED"},
                "service_run": {"id": service_run, "status": "SUCCEEDED"},
                "case_count": len(cases),
                "trajectory_steps": trajectory["total_steps"],
                "artifact_count": len(artifacts),
                "analysis_revision": analysis["revision"],
                "attribution_count": len(attributions),
                "review_revision": review_revision,
                "audit_count": len(audit),
            },
            sort_keys=True,
        )
    )
    return 0


def _run_smoke(client: "_Client", name: str, agent_revision_id: str):
    task = client.request(
        "POST",
        "/api/v1/tasks",
        {
            "name": name,
            "benchmark_id": "protocol-smoke",
            "agent_revision_id": agent_revision_id,
            "case_ids": ["smoke-001"],
            "budget": {},
        },
    )
    run_id = task["run_id"]
    for _ in range(120):
        run = client.request("GET", f"/api/v1/runs/{run_id}")
        if run["status"] in {
            "SUCCEEDED",
            "FAILED",
            "CANCELED",
            "BUDGET_EXHAUSTED",
        }:
            break
        time.sleep(1)
    if run["status"] != "SUCCEEDED":
        raise RuntimeError(f"Protocol-Smoke run finished as {run['status']}")
    return task["id"], run_id


class _Client:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.csrf: str | None = None
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )

    def public_get(self, path: str):
        with urllib.request.urlopen(self.base_url + path, timeout=15) as response:
            return json.load(response)

    def request(self, method: str, path: str, payload=None, headers=None):
        request_headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
        if method not in {"GET", "HEAD", "OPTIONS"} and self.csrf:
            request_headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"{method} {path} returned {exc.code}: {body[:500]}"
            ) from exc


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"release gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
