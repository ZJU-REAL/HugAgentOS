"""Exercise the native runtime installer over its file/network CLI boundary."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

SCRIPT = Path(__file__).with_name("install-native-tools.py")
BINARY = (
    b'#!/bin/sh\nif [ "$OFFICECLI_SKIP_UPDATE" = 1 ]; then echo 1.0.144; else echo 1.0.149; fi\n'
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(BINARY)

    def log_message(self, *args):
        pass


class NativeToolsTests(unittest.TestCase):
    def test_pandoc_archive_is_private_and_checksum_verified(self):
        import io
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "pandoc.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                payload = b"#!/bin/sh\necho pandoc 3.11\n"
                info = tarfile.TarInfo("pandoc-3.11/bin/pandoc")
                info.size = len(payload)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(payload))
            asset = {"url": archive.as_uri(), "size": archive.stat().st_size,
                     "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "format": "tar"}
            manifest = {"schema": 1, "pandoc": {"version": "3.11", "targets": {"linux-x86_64": asset}}}
            path = root / "tools.json"
            path.write_text(json.dumps(manifest))
            result = subprocess.run([sys.executable, str(SCRIPT), "--manifest", str(path),
                "--target", "linux-x86_64", "--runtime", str(root / "runtime"),
                "--executable", "python/bin/python3.11"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            binary = root / "runtime/python/bin/pandoc"
            self.assertEqual(binary.read_bytes(), payload)
            self.assertIn("3.11", subprocess.check_output([str(binary), "--version"], text=True))

    def test_verified_offline_tool_and_corrupt_download(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                asset = {
                    "url": f"http://127.0.0.1:{server.server_port}/officecli",
                    "size": len(BINARY),
                    "sha256": "0" * 64,
                }
                manifest = {
                    "schema": 1,
                    "officecli": {"version": "1.0.144", "targets": {"linux-x86_64": asset}},
                }
                path = root / "tools.json"
                path.write_text(json.dumps(manifest))
                args = [
                    sys.executable,
                    str(SCRIPT),
                    "--manifest",
                    str(path),
                    "--target",
                    "linux-x86_64",
                    "--runtime",
                    str(root / "runtime"),
                    "--executable",
                    "python/bin/python3.11",
                ]
                bad = subprocess.run(args, capture_output=True, text=True)
                self.assertNotEqual(bad.returncode, 0)
                self.assertIn("SHA-256", bad.stderr)
                self.assertFalse((root / "runtime/python/bin/officecli").exists())
                asset["sha256"] = hashlib.sha256(BINARY).hexdigest()
                path.write_text(json.dumps(manifest))
                good = subprocess.run(args, capture_output=True, text=True)
                self.assertEqual(good.returncode, 0, good.stderr)
                binary = root / "runtime/python/bin/officecli"
                self.assertEqual(binary.read_bytes(), BINARY)
                self.assertEqual(
                    subprocess.check_output(
                        [str(binary), "--version"],
                        text=True,
                        env={**os.environ, "OFFICECLI_SKIP_UPDATE": "1"},
                    ).strip(),
                    "1.0.144",
                )
                # Relocating the completed runtime needs neither network nor a system install.
                runtime = root / "runtime"
                (runtime / "runtime-layout.json").write_text(json.dumps({"schema": 1}))
                shutil.copy2(SCRIPT.with_name("runtime-smoke.py"), runtime / "runtime-smoke.py")
                archive = root / "runtime-core.tar.gz"
                packed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT.with_name("create-runtime-archive.py")),
                        "--source",
                        str(runtime),
                        "--output",
                        str(archive),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(packed.returncode, 0, packed.stderr)
                moved = root / "relocated"
                with tarfile.open(archive) as bundle:
                    self.assertIn("python/bin/officecli", bundle.getnames())
                    bundle.extractall(moved, filter="data")
                smoke = subprocess.run(
                    [sys.executable, str(moved / "runtime-smoke.py"), "--native-only"],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(smoke.returncode, 0)
                self.assertIn("pandoc", smoke.stderr)
                self.assertEqual(
                    subprocess.check_output(
                        [str(moved / "python/bin/officecli"), "--version"],
                        text=True,
                        env={**os.environ, "OFFICECLI_SKIP_UPDATE": "1"},
                    ).strip(),
                    "1.0.144",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_archives_reject_traversal_and_escaping_links(self):
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location("native_installer", SCRIPT)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index, (name, link) in enumerate([
                ("../../escape", None), ("bin/link", "../../escape"),
                ("/absolute", None), ("bin/device", "device"),
            ]):
                archive = root / f"bad-{index}.tar"
                with tarfile.open(archive, "w") as tar:
                    entry = tarfile.TarInfo(name)
                    if link == "device":
                        entry.type = tarfile.CHRTYPE
                    elif link:
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = link
                    else:
                        entry.size = 1
                    tar.addfile(entry, io.BytesIO(b"x") if entry.isfile() else None)
                with self.assertRaises(ValueError):
                    installer.unpack(archive, root / f"out-{index}", "tar")

    def test_backend_prefers_relocated_private_tools_without_host_path(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location(
            "office_helpers", SCRIPT.parents[2] / "src/backend/core/files/content/office.py")
        office = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(office)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "relocated runtime"
            (root / "python/bin").mkdir(parents=True)
            pandoc = root / "python/bin/pandoc"
            pandoc.write_bytes(BINARY)
            pandoc.chmod(0o755)
            lo = root / "native/libreoffice/program/soffice"
            lo.parent.mkdir(parents=True)
            lo.write_bytes(BINARY)
            lo.chmod(0o755)
            manifest = root / "native-tools.json"
            manifest.write_text(json.dumps({
                "pandoc": {"path": "python/bin/pandoc"},
                "libreoffice": {"path": "native/libreoffice/program/soffice"}}))
            for python in (root / "python/bin/python3.11", root / "python/python.exe"):
                with patch.object(sys, "executable", str(python)), patch.dict(os.environ, {"PATH": ""}):
                    self.assertEqual(office.find_pandoc_binary(), str(pandoc))
                    self.assertEqual(office.find_libreoffice_binary(), str(lo))
            with patch.object(sys, "executable", str(root / "python/bin/python3.11")):
                for invalid in ({"pandoc": {"path": "../outside"}}, [], {"pandoc": "invalid"}):
                    manifest.write_text(json.dumps(invalid))
                    self.assertIsNone(office._bundled_tool("pandoc"))

    def test_zip_preserves_safe_links_and_rejects_traversal(self):
        import importlib.util
        import zipfile
        spec = importlib.util.spec_from_file_location("native_installer_zip", SCRIPT)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index, value in enumerate(["pandoc", "../../outside"]):
                archive = root / f"archive-{index}.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    binary = zipfile.ZipInfo("bin/pandoc")
                    binary.external_attr = 0o100755 << 16
                    bundle.writestr(binary, BINARY)
                    link = zipfile.ZipInfo("bin/pandoc-lua")
                    link.external_attr = 0o120777 << 16
                    bundle.writestr(link, value)
                output = root / f"out-{index}"
                if index:
                    with self.assertRaises(ValueError):
                        installer.unpack(archive, output, "zip")
                else:
                    installer.unpack(archive, output, "zip")
                    self.assertEqual((output / "bin/pandoc-lua").read_bytes(), BINARY)


if __name__ == "__main__":
    unittest.main()
