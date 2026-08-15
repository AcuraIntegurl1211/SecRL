from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Column, Integer, Table, inspect, text

from secrl_platform.storage.database import create_engine_and_session
from secrl_platform.storage.orm import Base, EvaluationTaskORM
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

    def test_concurrent_claims_return_a_queued_task_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_factory = create_engine_and_session(
                Path(tmp) / "test.sqlite3",
                create=True,
            )
            expected = TaskRepository(session_factory).create({"name": "only"})
            start = threading.Barrier(2)

            def claim():
                start.wait()
                return TaskRepository(session_factory).claim_next()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: claim(), range(2)))

            claimed_ids = [record.id for record in results if record is not None]
            self.assertEqual(claimed_ids, [expected.id])
            session_factory.kw["bind"].dispose()

    def test_timestamps_remain_utc_aware_after_database_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_factory = create_engine_and_session(
                Path(tmp) / "test.sqlite3",
                create=True,
            )
            created = TaskRepository(session_factory).create({"name": "utc"})
            with session_factory() as session:
                reloaded = session.get(EvaluationTaskORM, created.id)
                self.assertEqual(reloaded.created_at.tzinfo, timezone.utc)
            session_factory.kw["bind"].dispose()

    def test_initial_migration_does_not_include_future_orm_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            future_table = Table(
                "future_milestone_two_table",
                Base.metadata,
                Column("id", Integer, primary_key=True),
            )
            database_path = Path(tmp) / "migration.sqlite3"
            config = Config("alembic.ini")
            config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
            try:
                command.upgrade(config, "head")
                session_factory = create_engine_and_session(database_path)
                table_names = inspect(session_factory.kw["bind"]).get_table_names()
                self.assertNotIn("future_milestone_two_table", table_names)
                session_factory.kw["bind"].dispose()
            finally:
                Base.metadata.remove(future_table)

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

    def test_case_attempt_number_is_unique_within_run_and_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_factory = create_engine_and_session(
                Path(tmp) / "test.sqlite3",
                create=True,
            )
            indexes = inspect(session_factory.kw["bind"]).get_indexes("case_attempt")
            matching = [
                index
                for index in indexes
                if index["column_names"] == ["run_id", "case_id", "attempt_no"]
            ]
            self.assertEqual(len(matching), 1)
            self.assertTrue(matching[0]["unique"])
            session_factory.kw["bind"].dispose()


if __name__ == "__main__":
    unittest.main()
