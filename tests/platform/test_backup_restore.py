import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from secrl_platform.storage.backup import BackupIntegrityError, create_backup, restore_backup
from secrl_platform.storage.artifacts import LocalArtifactStore


class BackupRestoreTest(unittest.TestCase):
    @staticmethod
    def _seed_database(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO fixture (value) VALUES ('stable')")

    def test_round_trip_preserves_database_and_artifact_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            backup = Path(tmp) / "backup"
            restored = Path(tmp) / "restored"
            data.mkdir()
            self._seed_database(data / "secrl-lite.sqlite3")
            LocalArtifactStore(data / "artifacts").put_bytes("trajectory", b"trace")
            result = create_backup(data, backup)
            restored_result = restore_backup(backup, restored)
            self.assertEqual(result.database_sha256, restored_result.database_sha256)
            self.assertEqual(result.artifact_manifest_sha256, restored_result.artifact_manifest_sha256)
            self.assertTrue((restored / "artifacts").exists())

    def test_restore_rejects_tampered_artifact_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            backup = Path(tmp) / "backup"
            target = Path(tmp) / "target"
            data.mkdir()
            self._seed_database(data / "secrl-lite.sqlite3")
            LocalArtifactStore(data / "artifacts").put_bytes("trajectory", b"trace")
            create_backup(data, backup)
            artifact = next(path for path in (backup / "artifacts").rglob("*") if path.is_file())
            artifact.write_bytes(b"tampered")
            target.mkdir()
            with self.assertRaises(BackupIntegrityError):
                restore_backup(backup, target)
            self.assertEqual(list(target.iterdir()), [])

    def test_restore_rejects_path_traversal_and_newer_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            backup = Path(tmp) / "backup"
            data.mkdir()
            self._seed_database(data / "secrl-lite.sqlite3")
            create_backup(data, backup)
            manifest_path = backup / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["schema_version"] = 2
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(BackupIntegrityError):
                restore_backup(backup, Path(tmp) / "newer")


if __name__ == "__main__":
    unittest.main()
