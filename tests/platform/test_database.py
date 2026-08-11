import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import inspect, text

from secrl_platform.storage.database import create_engine_and_session
from secrl_platform.storage.repositories import TaskRepository


class DatabaseTest(unittest.TestCase):
    def test_only_one_task_can_be_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_factory = create_engine_and_session(
                Path(tmp) / "test.sqlite3",
                create=True,
            )
            repo = TaskRepository(session_factory)
            first = repo.create({"name": "first"})
            second = repo.create({"name": "second"})
            self.assertEqual(uuid.UUID(first.id).version, 4)
            self.assertEqual(first.task_spec_json, '{"name":"first"}')
            self.assertIsNotNone(first.created_at.tzinfo)
            self.assertEqual(repo.claim_next().id, first.id)
            self.assertIsNone(repo.claim_next())
            repo.finish(first.id, "SUCCEEDED")
            self.assertEqual(repo.claim_next().id, second.id)
            session_factory.kw["bind"].dispose()

    def test_schema_has_all_sixteen_lite_tables(self):
        expected = {
            "agent_revision",
            "app_setting",
            "artifact",
            "attribution",
            "audit_event",
            "benchmark_revision",
            "case_attempt",
            "case_record",
            "dataset_version",
            "evaluation_task",
            "human_review",
            "local_user",
            "model_config_revision",
            "run",
            "scenario",
            "secret_ref",
        }
        with tempfile.TemporaryDirectory() as tmp:
            session_factory = create_engine_and_session(
                Path(tmp) / "test.sqlite3",
                create=True,
            )
            engine = session_factory.kw["bind"]
            self.assertEqual(set(inspect(engine).get_table_names()), expected)
            engine.dispose()

    def test_platform_connections_enable_required_pragmas(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_factory = create_engine_and_session(
                Path(tmp) / "test.sqlite3",
                create=True,
            )
            with session_factory() as session:
                self.assertEqual(session.scalar(text("PRAGMA journal_mode")), "wal")
                self.assertEqual(session.scalar(text("PRAGMA foreign_keys")), 1)
                self.assertEqual(session.scalar(text("PRAGMA busy_timeout")), 5000)
            session_factory.kw["bind"].dispose()


if __name__ == "__main__":
    unittest.main()
