"""Tests integrity checks without a Docker daemon or remote services."""
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class OfflineBundleTests(unittest.TestCase):
    def test_verified_then_tampered_artifact_is_rejected(self):
        script = Path(__file__).with_name("verify-offline-bundle.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "release.json"
            artifact.write_bytes(b'{"version":1}')
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            (root / "SHA256SUMS").write_text(f"{digest}  release.json\n", encoding="utf-8")
            first = subprocess.run([sys.executable, str(script), str(root)], capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            artifact.write_bytes(b'{"version":2}')
            second = subprocess.run([sys.executable, str(script), str(root)], capture_output=True)
            self.assertNotEqual(second.returncode, 0)

    def test_bundle_path_cannot_escape(self):
        script = Path(__file__).with_name("verify-offline-bundle.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SHA256SUMS").write_text("0" * 64 + "  ../outside\n", encoding="utf-8")
            result = subprocess.run([sys.executable, str(script), str(root)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
