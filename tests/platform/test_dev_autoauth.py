"""Local development auto-authentication fence tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from secrl_platform.api.app import create_app
from secrl_platform.auth.passwords import hash_password
from secrl_platform.auth.sessions import password_change_key
from secrl_platform.config import Settings
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session
from secrl_platform.storage.orm import AppSettingORM, LocalUserORM


def _settings(root: Path, *, autoauth: bool, confirm: str | None = None) -> Settings:
    return Settings(
        data_dir=root,
        master_key="00" * 32,
        session_secret="s" * 32,
        model_provider_allowlist=("api.deepseek.com",),
        dev_autoauth=autoauth,
        dev_autoauth_confirm=confirm,
    )


class DevAutoAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.session_factory = create_engine_and_session(
            root / "platform.sqlite3",
            create=True,
        )
        self.artifact_store = LocalArtifactStore(root / "artifacts")
        self.root = root

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _add_admin(self) -> None:
        with self.session_factory.begin() as session:
            session.add(
                LocalUserORM(
                    username="admin",
                    password_hash=hash_password("correct horse battery staple"),
                    status="ACTIVE",
                )
            )

    def test_autoauth_disabled_requires_authentication(self) -> None:
        self._add_admin()
        app = create_app(
            settings=_settings(self.root, autoauth=False),
            session_factory=self.session_factory,
            artifact_store=self.artifact_store,
        )
        with TestClient(app) as client:
            response = client.get("/api/v1/tasks")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_autoauth_serves_requests_without_cookie_or_csrf(self) -> None:
        self._add_admin()
        app = create_app(
            settings=_settings(
                self.root, autoauth=True, confirm="yes"
            ),
            session_factory=self.session_factory,
            artifact_store=self.artifact_store,
            model_provider_resolver=lambda _host, _port: ("93.184.216.34",),
        )
        with TestClient(app) as client:
            listed = client.get("/api/v1/tasks")
            self.assertEqual(listed.status_code, 200, listed.text)
            models = client.get("/api/v1/models")
            self.assertEqual(models.status_code, 200, models.text)
            created = client.post(
                "/api/v1/models",
                json={
                    "name": "autoauth model",
                    "provider": "openai-compatible",
                    "endpoint": "https://api.deepseek.com",
                    "model": "fixture-model",
                },
                headers={"X-Model-API-Key": "encrypted-test-key"},
            )
            self.assertEqual(created.status_code, 201, created.text)

    def test_autoauth_requires_confirmation_flag(self) -> None:
        with self.assertRaises(RuntimeError):
            create_app(
                settings=_settings(self.root, autoauth=True),
                session_factory=self.session_factory,
                artifact_store=self.artifact_store,
            )

    def test_autoauth_confirmation_enforced_on_noarg_factory_path(self) -> None:
        """The uvicorn factory path calls create_app() without settings; the
        confirmation fence must still fire via lifespan-time settings."""
        from unittest import mock

        env = {
            "SECRL_DATA_DIR": str(self.root),
            "SECRL_MASTER_KEY": "00" * 32,
            "SECRL_SESSION_SECRET": "s" * 32,
            "SECRL_DEV_AUTOAUTH": "true",
            "SECRL_MODEL_PROVIDER_ALLOWLIST": '["api.deepseek.com"]',
        }
        with mock.patch.dict(os.environ, env, clear=False):
            app = create_app()
            with self.assertRaises(RuntimeError):
                with TestClient(app):
                    pass

    def test_autoauth_without_admin_user_fails_closed(self) -> None:
        app = create_app(
            settings=_settings(self.root, autoauth=True, confirm="yes"),
            session_factory=self.session_factory,
            artifact_store=self.artifact_store,
        )
        with TestClient(app) as client:
            response = client.get("/api/v1/tasks")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "AUTOAUTH_UNAVAILABLE")

    def test_autoauth_still_enforces_initial_password_rotation(self) -> None:
        self._add_admin()
        with self.session_factory.begin() as session:
            user_id = (
                session.query(LocalUserORM.id)
                .filter(LocalUserORM.username == "admin")
                .scalar()
            )
            session.add(
                AppSettingORM(
                    key=password_change_key(user_id),
                    value_json="true",
                )
            )
        app = create_app(
            settings=_settings(
                self.root, autoauth=True, confirm="yes"
            ),
            session_factory=self.session_factory,
            artifact_store=self.artifact_store,
        )
        with TestClient(app) as client:
            response = client.get("/api/v1/tasks")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["error"]["code"], "PASSWORD_CHANGE_REQUIRED"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
