"""Private Windows Bash delivery tested through the staging CLI."""
import hashlib
import os
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).with_name("install-native-tools.py")
REQUIRED = ("bash", "sh", "find", "sort", "head", "cut", "grep", "sed", "awk", "cat", "mkdir", "rm", "cp", "mv", "ls", "wc", "xargs", "tr")

class PrivateBashTests(unittest.TestCase):
    def stage(self, root, missing=None, corrupt=False, target="windows-x86_64"):
        archive = root / "git.tar.bz2"
        with tarfile.open(archive, "w:bz2") as bundle:
            names = ["bin/bash.exe", "bin/sh.exe", "usr/bin/msys-2.0.dll",
                     "usr/share/licenses/bash/COPYING"]
            names += ["usr/bin/" + command + ".exe" for command in REQUIRED]
            for name in names:
                if name == missing:
                    continue
                data = name.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
            # These virtual MSYS mounts must not become absolute Windows symlinks.
            info = tarfile.TarInfo("etc/mtab")
            info.type = tarfile.SYMTYPE
            info.linkname = "/proc/mounts"
            bundle.addfile(info)
        asset = {"url": archive.as_uri(), "size": archive.stat().st_size,
                 "sha256": "0"*64 if corrupt else hashlib.sha256(archive.read_bytes()).hexdigest(),
                 "format": "tar"}
        manifest = {"schema": 1, "git-bash": {"version": "test",
                    "targets": {"windows-x86_64": asset}}}
        path = root / "manifest.json"
        path.write_text(json.dumps(manifest))
        return subprocess.run([sys.executable, str(INSTALLER), "--manifest", str(path),
            "--target", target, "--runtime", str(root / "runtime"),
            "--executable", "python/python.exe"], text=True, capture_output=True)

    def test_windows_bundle_contains_private_bash_commands_and_licenses(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.stage(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            tools = json.loads((root / "runtime/native-tools.json").read_text())
            self.assertEqual(tools["git-bash"]["path"], "native/git-bash/usr/bin/bash.exe")
            for command in REQUIRED:
                self.assertTrue((root / ("runtime/native/git-bash/usr/bin/" + command + ".exe")).is_file())
            self.assertTrue((root / "runtime/native/git-bash/usr/bin/msys-2.0.dll").is_file())
            self.assertTrue((root / "runtime/native/git-bash/usr/share/licenses/bash/COPYING").is_file())
            self.assertFalse((root / "runtime/native/git-bash/etc/mtab").exists())

    def test_missing_command_and_corrupt_archive_fail_staging(self):
        for options in ({"missing": "usr/bin/grep.exe"}, {"corrupt": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                result = self.stage(root, **options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SHA-256" if options.get("corrupt") else "grep.exe", result.stderr)
                self.assertFalse((root / "runtime/native-tools.json").exists())

    def test_runner_prefers_private_runtime_even_with_a_host_bash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "relocated runtime"
            private = root / "native/git-bash/usr/bin/bash.exe"
            private.parent.mkdir(parents=True)
            private.write_bytes(b"private")
            (root / "python").mkdir()
            (root / "native-tools.json").write_text(json.dumps({
                "git-bash": {"path": "native/git-bash/usr/bin/bash.exe"}}))
            runner_dir = INSTALLER.parents[2] / "src/backend/core/sandbox/runner"
            code = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "sys.executable = sys.argv[2]; "
                "from runtime_tools import resolve_bash_executable; "
                "print(resolve_bash_executable())"
            )
            args = [sys.executable, "-c", code, str(runner_dir), str(root / "python/python.exe")]
            env = dict(os.environ, SCRIPT_RUNNER_BASH="/bin/bash")
            result = subprocess.run(args, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(private))
            private.unlink()
            broken = subprocess.run(args, env=env, capture_output=True, text=True)
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn("Bundled Bash", broken.stderr)

    def test_windows_path_prefers_gnu_tools_over_system32(self):
        runner_dir = INSTALLER.parents[2] / "src/backend/core/sandbox/runner"
        code = ("import sys,json; sys.path.insert(0, sys.argv[1]); "
                "from runtime_tools import windows_tool_path_entries; "
                "print(json.dumps(windows_tool_path_entries(sys.argv[2])))")
        result = subprocess.run([sys.executable, "-c", code, str(runner_dir),
            "C:/private runtime/native/git-bash/usr/bin/bash.exe"],
            env=dict(os.environ, SYSTEMROOT="C:/Windows"),
            capture_output=True, text=True, check=True)
        paths = [path.replace(chr(92), "/") for path in json.loads(result.stdout)]
        self.assertLess(paths.index("C:/private runtime/native/git-bash/usr/bin"),
                        paths.index("C:/Windows/System32"))

    def test_non_windows_bundle_does_not_download_windows_bash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.stage(root, corrupt=True, target="linux-x86_64")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / "runtime/native-tools.json").read_text()), {})

if __name__ == "__main__":
    unittest.main()
