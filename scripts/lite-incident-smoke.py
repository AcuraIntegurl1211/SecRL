#!/usr/bin/env python3
"""Verify one explicitly started SecRL Incident without calling an LLM."""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.benchmarks.secrl import SECRL_EXPECTED_SCENARIO_COUNTS


BASE_URL = os.environ.get("SECRL_GATE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
INCIDENT_ID = os.environ.get("SECRL_INCIDENT_SMOKE_ID", "incident_34")


def main() -> int:
    expected_count = SECRL_EXPECTED_SCENARIO_COUNTS[INCIDENT_ID]
    client = _Client(BASE_URL)
    _authenticate(client)
    cases = client.request(
        "GET",
        f"/api/v1/benchmarks/secrl/cases?scenario={INCIDENT_ID}&limit=1",
    )
    if cases.get("total") != expected_count or len(cases.get("items", [])) != 1:
        raise RuntimeError("Incident case catalog count is not frozen")
    case_id = cases["items"][0]["id"]
    if not case_id.startswith(f"{INCIDENT_ID}:"):
        raise RuntimeError("Incident case identity is not scoped to the selected Incident")

    preflight = client.request(
        "GET",
        "/api/v1/preflight"
        f"?benchmark_id=secrl&agent_revision_id={DeterministicSmokeAgent.revision().id}"
        f"&incident_ids={INCIDENT_ID}&case_ids={case_id}",
    )
    environment = next(
        check for check in preflight["checks"] if check["name"] == "environment"
    )
    if environment["status"] != "ready":
        raise RuntimeError("selected Incident is not available")
    print(json.dumps({"incident": INCIDENT_ID, "case_count": cases["total"], "environment": "ready"}, sort_keys=True))
    return 0


def _authenticate(client: "_Client") -> None:
    initial_password = _required("SECRL_INITIAL_ADMIN_PASSWORD")
    current_password = os.environ.get("SECRL_ADMIN_PASSWORD", initial_password)
    login = client.request(
        "POST",
        "/api/v1/auth/login",
        {
            "username": _required("SECRL_INITIAL_ADMIN_USERNAME"),
            "password": current_password,
        },
    )
    client.csrf = str(login["csrf_token"])
    if login.get("password_change_required"):
        client.request(
            "POST",
            "/api/v1/auth/password",
            {
                "current_password": current_password,
                "new_password": _required("SECRL_TEST_ADMIN_PASSWORD"),
            },
        )


class _Client:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.csrf: str | None = None
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def request(self, method: str, path: str, payload=None):
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if method not in {"GET", "HEAD", "OPTIONS"} and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path} returned {exc.code}") from exc


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"incident smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
