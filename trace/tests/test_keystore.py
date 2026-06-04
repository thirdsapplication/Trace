import os
import tempfile
import unittest

from trace.ui import keystore


class KeystoreFileFallbackTest(unittest.TestCase):
    """Exercises the file-fallback backend (forces keyring off) so the test is
    deterministic regardless of whether keyring is installed."""

    def setUp(self):
        self._orig_keyring = keystore._keyring
        self._orig_dir = keystore._FALLBACK_DIR
        self._orig_path = keystore._FALLBACK_PATH
        keystore._keyring = lambda: None
        self._tmp = tempfile.TemporaryDirectory()
        keystore._FALLBACK_DIR = self._tmp.name
        keystore._FALLBACK_PATH = os.path.join(self._tmp.name, "config.json")

    def tearDown(self):
        keystore._keyring = self._orig_keyring
        keystore._FALLBACK_DIR = self._orig_dir
        keystore._FALLBACK_PATH = self._orig_path
        self._tmp.cleanup()

    def test_save_and_get(self):
        self.assertEqual(keystore.save_key("sk-ant-abc123"), "file")
        self.assertEqual(keystore.get_key(), "sk-ant-abc123")

    def test_file_is_0600(self):
        keystore.save_key("sk-ant-xyz")
        mode = os.stat(keystore._FALLBACK_PATH).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_info_never_exposes_full_key(self):
        keystore.save_key("sk-ant-secret-tail9999")
        info = keystore.info()
        self.assertTrue(info["saved"])
        self.assertEqual(info["backend"], "file")
        self.assertEqual(info["masked_tail"], "…9999")
        self.assertNotIn("secret", str(info))

    def test_empty_key_rejected(self):
        with self.assertRaises(ValueError):
            keystore.save_key("   ")

    def test_clear(self):
        keystore.save_key("sk-ant-abc")
        keystore.clear_key()
        self.assertIsNone(keystore.get_key())
        self.assertFalse(keystore.info()["saved"])


if __name__ == "__main__":
    unittest.main()
