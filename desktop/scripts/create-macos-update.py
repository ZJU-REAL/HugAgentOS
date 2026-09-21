#!/usr/bin/env python3
"""Create a Tauri macOS update archive without AppleDouble/xattr sidecars."""
import argparse
import importlib.util
from pathlib import Path
import tarfile

def create_archive(app, output, version):
    app = Path(app).resolve()
    output = Path(output).resolve()
    if not app.is_dir() or app.suffix != ".app":
        raise ValueError("Source must be an existing .app directory")
    if output.is_relative_to(app):
        raise ValueError("Output must be outside the application bundle")
    def keep_member(member):
        return None if any(p.startswith("._") or p == "__MACOSX" for p in member.name.split("/")) else member
    with tarfile.open(output, "w:gz") as archive:
        archive.add(app, arcname=app.name, recursive=True, filter=keep_member)
    spec = importlib.util.spec_from_file_location("publisher", Path(__file__).with_name("publish-desktop.py"))
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    publisher.validate_macos_archive(output, version)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    create_archive(args.app, args.output, args.version)
