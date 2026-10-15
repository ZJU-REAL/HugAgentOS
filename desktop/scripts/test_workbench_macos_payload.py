import importlib.util
import plistlib
from pathlib import Path
import tempfile
import unittest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


payload = load("workbench-macos-payload")
packer = load("create-macos-update")


class NativePayload(unittest.TestCase):
    def test_archive_matches_actual_app_then_rejects_tampered_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / ".scratch/workbench-25") as folder:
            root = Path(folder)
            app = root / "Example.app"
            contents = app / "Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleShortVersionString": "1.2.3", "CFBundleIdentifier": "com.example.desktop"}))
            binary = contents / "binary"
            binary.write_bytes(b"test")
            binary.chmod(0o755)
            (contents / "link").symlink_to("binary")
            archive = root / "Example.app.tar.gz"
            packer.create_archive(app, archive, "1.2.3")
            self.assertTrue(payload.compare_payload(app, archive, "1.2.3", "com.example.desktop")["nativePayloadMatch"])
            binary.write_bytes(b"Test")
            with self.assertRaisesRegex(ValueError, "bytes differ"):
                payload.compare_payload(app, archive, "1.2.3", "com.example.desktop")
            with self.assertRaisesRegex(ValueError, "product"):
                payload.compare_payload(app, archive, "1.2.3", "com.other.desktop")


if __name__ == "__main__":
    unittest.main()
