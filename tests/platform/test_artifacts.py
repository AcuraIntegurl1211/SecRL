import tempfile
import unittest
from pathlib import Path

from secrl_platform.storage.artifacts import (
    ArtifactIntegrityError,
    LocalArtifactStore,
    verify_all_artifacts,
)


class ArtifactStoreTest(unittest.TestCase):
    def test_put_is_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalArtifactStore(Path(tmp))
            first = store.put_bytes("trajectory", b'{"steps":[]}')
            second = store.put_bytes("trajectory", b'{"steps":[]}')
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.path, second.path)
            self.assertEqual(
                first.path.relative_to(Path(tmp)),
                Path("sha256")
                / first.sha256[:2]
                / first.sha256[2:4]
                / first.sha256,
            )
            self.assertTrue(verify_all_artifacts(Path(tmp)))

    def test_verify_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalArtifactStore(Path(tmp))
            ref = store.put_bytes("log", b"safe")
            ref.path.write_bytes(b"changed")
            with self.assertRaises(ArtifactIntegrityError):
                store.verify(ref)
            self.assertFalse(verify_all_artifacts(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
