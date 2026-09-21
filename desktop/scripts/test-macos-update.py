import importlib.util
import io
import plistlib
from pathlib import Path
import tarfile
import tempfile
import unittest

def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

publisher = load("publish-desktop")
packer = load("create-macos-update")

class MacUpdateArchives(unittest.TestCase):
    def test_bundle_round_trip_preserves_mode_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); app = root / "Example.app"
            contents = app / "Contents"; contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.2.3"}))
            binary = contents / "run"; binary.write_bytes(b"binary"); binary.chmod(0o755)
            (contents / "link").symlink_to("run")
            (app / "._metadata").write_bytes(b"AppleDouble")
            archive = root / "Example.app.tar.gz"
            packer.create_archive(app, archive, "1.2.3")
            publisher.validate_macos_archive(archive, "1.2.3")
            with tarfile.open(archive) as bundle:
                self.assertNotIn("Example.app/._metadata", bundle.getnames())
                self.assertEqual(bundle.getmember("Example.app/Contents/run").mode & 0o777, 0o755)
                self.assertTrue(bundle.getmember("Example.app/Contents/link").issym())
            with self.assertRaisesRegex(ValueError, "version"):
                publisher.validate_macos_archive(archive, "1.2.4")

    def test_rejects_appledouble_multiple_roots_and_unsafe_paths(self):
        for bad in ["._Example.app", "Example.app/Contents/._Info.plist", "__MACOSX/x", "Other.app/x", "../escape"]:
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as folder:
                archive = Path(folder) / "Example.app.tar.gz"
                with tarfile.open(archive, "w:gz") as bundle:
                    for name in ["Example.app/Contents/Info.plist", bad]:
                        content = plistlib.dumps({"CFBundleShortVersionString": "1.2.3"})
                        member = tarfile.TarInfo(name); member.size = len(content)
                        bundle.addfile(member, io.BytesIO(content))
                with self.assertRaises(ValueError):
                    publisher.select_artifact(Path(folder), "darwin-aarch64", "1.2.3")

if __name__ == "__main__":
    unittest.main()
