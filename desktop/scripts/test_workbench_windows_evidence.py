"""Evidence CLI boundaries only; these do not pass Windows native acceptance."""
import hashlib
import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("workbench-windows-evidence.py")
ROOT = Path(__file__).resolve().parents[2]
from workbench_windows_source import source_snapshot, verify


class EvidenceCommands(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              cwd=ROOT, capture_output=True, text=True)

    def test_prepare_preserves_existing_directory_and_rejects_pending(self):
        scratch = ROOT / ".scratch" / "workbench-24"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            output = Path(temp) / "batch"
            prepared = self.run_cli("prepare", "--output", str(output))
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            manifest = json.loads((output / "source.json").read_text())
            self.assertEqual(manifest["platform"], "windows-x86_64")
            self.assertEqual(manifest["protocol"], "1.0.0")
            self.assertEqual(manifest["sourceTreeHash"], manifest["sourceSha256"])
            self.assertGreater(len(manifest["files"]), 10)
            rows = json.loads((output / "results.json").read_text())["cases"]
            self.assertEqual({r["mode"] for r in rows}, {"local", "cloud", "dual"})
            self.assertTrue(all(r["status"] == "not-run" for r in rows))
            refused = self.run_cli("prepare", "--output", str(output))
            self.assertNotEqual(refused.returncode, 0)
            incomplete = self.run_cli("check", "--batch", str(output))
            self.assertNotEqual(incomplete.returncode, 0)
            self.assertIn("incomplete", incomplete.stderr)
            report_path = output / "results.json"
            report = json.loads(report_path.read_text())
            report["sourceReceiptSha256"] = "0" * 64
            report_path.write_text(json.dumps(report))
            tampered = self.run_cli("check", "--batch", str(output))
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("Source receipt changed", tampered.stderr)
            report = json.loads((output / "results.json").read_text())
            report["sourceReceiptSha256"] = hashlib.sha256(
                (output / "source.json").read_bytes()).hexdigest()
            for row in report["cases"]:
                row["status"] = "pass"
                row["actual"] = "claimed pass without IPC/sentinel records"
            report_path.write_text(json.dumps(report))
            missing_ipc = self.run_cli("check", "--batch", str(output))
            self.assertNotEqual(missing_ipc.returncode, 0)
            self.assertIn("Missing actual IPC denial", missing_ipc.stderr)


    def test_complete_receipt_tracks_modes_deletions_and_rejects_resurrection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def git(*args):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            git("init")
            git("config", "user.name", "Receipt Fixture")
            git("config", "user.email", "receipt@example.invalid")
            (root / "retired.py").write_text("old batch execution\n")
            (root / "run.sh").write_text("#!/bin/sh\n")
            os.chmod(root / "run.sh", 0o755)
            git("add", ".")
            git("commit", "-m", "fixture")
            (root / "retired.py").unlink()
            (root / "new.py").write_text("new source\n")
            (root / ".env").write_text("fixture-private\n")
            (root / "storage").mkdir()
            (root / "storage/private").write_text("fixture-data\n")
            receipt = source_snapshot(root)
            entries = {row["path"]: row for row in receipt["files"]}
            self.assertTrue(entries["retired.py"]["deleted"])
            self.assertEqual(entries["run.sh"]["mode"], "100755")
            self.assertIn("new.py", entries)
            self.assertNotIn(".env", entries)
            self.assertNotIn("storage/private", entries)
            verify(root, receipt)
            (root / "retired.py").write_text("resurrected\n")
            with self.assertRaisesRegex(ValueError, "Frozen source differs"):
                verify(root, receipt)


if __name__ == "__main__":
    unittest.main()
