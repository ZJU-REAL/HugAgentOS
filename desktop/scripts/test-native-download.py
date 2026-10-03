"""Regression tests for bounded, verified native downloads."""
import hashlib
import importlib.util
import io
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("native_installer", Path(__file__).with_name("install-native-tools.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
DATA = b"verified native executable"


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.destination = Path(self.folder.name) / "download"
        self.asset = {"url": "https://primary.example/tool", "fallback_urls": ["https://archive.example/tool"],
                      "size": len(DATA), "sha256": hashlib.sha256(DATA).hexdigest()}

    def run_download(self, outcomes):
        with patch.object(installer.urllib.request, "urlopen", side_effect=outcomes) as opened, \
             patch.object(installer.time, "sleep"):
            installer.download(self.asset, self.destination)
        return [call.args[0].full_url for call in opened.call_args_list]

    def test_tls_disconnect_uses_pinned_fallback(self):
        failure = urllib.error.URLError(ssl.SSLEOFError("connection interrupted"))
        urls = self.run_download([failure] * 3 + [io.BytesIO(DATA)])
        self.assertEqual(urls, [self.asset["url"]] * 3 + self.asset["fallback_urls"])
        self.assertEqual(self.destination.read_bytes(), DATA)

    def test_partial_response_is_retried_from_start(self):
        self.run_download([io.BytesIO(DATA[:4]), io.BytesIO(DATA)])
        self.assertEqual(self.destination.read_bytes(), DATA)

    def test_bad_checksum_fails_closed_without_fallback(self):
        with patch.object(installer.urllib.request, "urlopen", return_value=io.BytesIO(b"x" * len(DATA))) as opened:
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                installer.download(self.asset, self.destination)
        self.assertEqual(opened.call_count, 1)
        self.assertFalse(self.destination.exists())

    def test_oversized_response_is_not_published(self):
        with patch.object(installer.urllib.request, "urlopen", return_value=io.BytesIO(DATA + b"x")):
            with self.assertRaisesRegex(ValueError, "pinned size"):
                installer.download(self.asset, self.destination)
        self.assertFalse(self.destination.exists())

    def test_certificate_failure_is_not_retried(self):
        failure = urllib.error.URLError(ssl.SSLCertVerificationError("invalid certificate"))
        with patch.object(installer.urllib.request, "urlopen", side_effect=failure) as opened, patch.object(installer.time, "sleep"):
            with self.assertRaises(urllib.error.URLError):
                installer.download(self.asset, self.destination)
        self.assertEqual(opened.call_count, 1)

    def test_exhaustion_is_bounded_and_removes_partial_file(self):
        with patch.object(installer.urllib.request, "urlopen", side_effect=ConnectionResetError("reset")) as opened, patch.object(installer.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Native tool download failed"):
                installer.download(self.asset, self.destination)
        self.assertEqual(opened.call_count, 6)
        self.assertFalse(self.destination.exists())

    def test_missing_primary_asset_uses_archive(self):
        error = urllib.error.HTTPError(self.asset["url"], 404, "Not found", {}, None)
        urls = self.run_download([error, io.BytesIO(DATA)])
        self.assertEqual(urls, [self.asset["url"], *self.asset["fallback_urls"]])


if __name__ == "__main__":
    unittest.main()
