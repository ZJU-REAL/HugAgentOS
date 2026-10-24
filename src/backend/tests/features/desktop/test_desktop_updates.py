import json
import re
from fastapi import FastAPI
from fastapi.testclient import TestClient
from features.desktop.api.desktop import router


def test_unpublished_platform_is_no_update(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKTOP_RELEASE_DIR", str(tmp_path))
    (tmp_path / "latest.json").write_text(json.dumps({
        "version": "0.5.64", "platforms": {
            "windows-x86_64": {"url": "client.exe", "signature": "signature"}
        }
    }))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/v1/desktop/latest.json?target=darwin&arch=aarch64")
    assert response.status_code == 204

import subprocess
import sys
from pathlib import Path

PUBLISH = Path(__file__).resolve().parents[5] / "desktop" / "scripts" / "publish-desktop.py"


def artifact_name(version, target):
    if target.startswith("darwin"):
        return "client.app.tar.gz"
    if target.startswith("linux"):
        # electron-builder names the UOS package after the product, spaces included.
        return f"Example UOS_{version}_uos1070_arm64.deb"
    return f"client_{version}-setup.exe"


def publish(directory, bundle, version, target):
    bundle.mkdir(exist_ok=True)
    name = artifact_name(version, target)
    for child in bundle.iterdir():
        child.unlink()
    (bundle / name).write_bytes(f"{version}-{target}".encode())
    (bundle / (name + ".sig")).write_text("fixture-signature")
    return subprocess.run([
        sys.executable, str(PUBLISH), "--version", version, "--target", target,
        "--bundle", str(bundle), "--local-dir", str(directory),
    ], capture_output=True, text=True)


def test_staggered_publication_preserves_platforms_and_legacy_updates(tmp_path, monkeypatch):
    directory, bundle = tmp_path / "release", tmp_path / "bundle"
    monkeypatch.setenv("DESKTOP_RELEASE_DIR", str(directory))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        def release(target=None):
            params = dict(zip(("target", "arch"), target.split("-", 1))) if target else {}
            r = client.get("/v1/desktop/latest.json", params=params)
            assert r.status_code == 200, r.text
            return r.json()

        for target in ("windows-x86_64", "darwin-aarch64"):
            result = publish(directory, bundle, "0.5.69", target)
            assert result.returncode == 0, result.stderr
        common = release()
        assert common["version"] == "0.5.69"
        assert set(common["platforms"]) == {"windows-x86_64", "darwin-aarch64"}
        old_mac_url = common["platforms"]["darwin-aarch64"]["url"]

        result = publish(directory, bundle, "0.5.70", "darwin-aarch64")
        assert result.returncode == 0, result.stderr
        assert release("darwin-aarch64")["version"] == "0.5.70"
        assert release("windows-x86_64")["version"] == "0.5.69"
        assert release() == common
        assert client.get(old_mac_url.replace("/api/", "/")).content == b"0.5.69-darwin-aarch64"

        result = publish(directory, bundle, "0.5.70", "windows-x86_64")
        assert result.returncode == 0, result.stderr
        common = release()
        assert common["version"] == "0.5.70"
        assert set(common["platforms"]) == {"windows-x86_64", "darwin-aarch64"}
        for target, spec in common["platforms"].items():
            r = client.get(spec["url"].replace("/api/", "/"))
            assert r.content == f"0.5.70-{target}".encode()


def test_legacy_manifest_is_migrated_without_relabelling_windows(tmp_path, monkeypatch):
    directory, bundle = tmp_path / "release", tmp_path / "bundle"
    directory.mkdir()
    legacy = {"version": "0.5.64", "platforms": {
        "windows-x86_64": {"url": "legacy.exe", "signature": "legacy-signature"}
    }}
    (directory / "latest.json").write_text(json.dumps(legacy))
    (directory / "legacy.exe").write_bytes(b"windows-0.5.64")
    monkeypatch.setenv("DESKTOP_RELEASE_DIR", str(directory))
    result = publish(directory, bundle, "0.5.69", "darwin-aarch64")
    assert result.returncode == 0, result.stderr
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/v1/desktop/latest.json").json()["version"] == "0.5.64"
        assert client.get("/v1/desktop/latest.json?target=windows&arch=x86_64").json()["version"] == "0.5.64"
        assert client.get("/v1/desktop/latest.json?target=darwin&arch=aarch64").json()["version"] == "0.5.69"
        assert client.get("/v1/desktop/latest.json?target=darwin&arch=x86_64").status_code == 204


def test_failed_or_conflicting_publication_keeps_the_available_release(tmp_path, monkeypatch):
    directory, bundle = tmp_path / "release", tmp_path / "bundle"
    assert publish(directory, bundle, "0.5.69", "darwin-aarch64").returncode == 0
    before = (directory / "release-index.json").read_bytes()
    (bundle / "client.app.tar.gz").write_bytes(b"changed-content")
    result = subprocess.run([
        sys.executable, str(PUBLISH), "--version", "0.5.69", "--target", "darwin-aarch64",
        "--bundle", str(bundle), "--local-dir", str(directory),
    ], capture_output=True, text=True)
    assert result.returncode != 0
    assert (directory / "release-index.json").read_bytes() == before
    assert publish(directory, bundle, "0.5.68", "darwin-aarch64").returncode != 0
    assert (directory / "release-index.json").read_bytes() == before


def test_uos_package_shares_the_index_and_keeps_its_signed_name(tmp_path, monkeypatch):
    directory, bundle = tmp_path / "release", tmp_path / "bundle"
    monkeypatch.setenv("DESKTOP_RELEASE_DIR", str(directory))
    for target in ("windows-x86_64", "linux-aarch64"):
        assert publish(directory, bundle, "0.5.69", target).returncode == 0
    # The UOS signature covers the published name, so planning and publishing must agree.
    plan = subprocess.run([
        sys.executable, str(PUBLISH), "--version", "0.5.69", "--target", "linux-aarch64",
        "--bundle", str(bundle), "--plan",
    ], capture_output=True, text=True)
    assert plan.returncode == 0, plan.stderr
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        manifest = client.get("/v1/desktop/latest.json?target=linux&arch=aarch64").json()
        spec = manifest["platforms"]["linux-aarch64"]
        name = spec["url"].rsplit("/", 1)[-1]
        assert name == json.loads(plan.stdout)["filename"]
        # The client rejects entries without a hash, and a percent-escaped name would not
        # match the one the signature covers.
        assert re.fullmatch(r"[a-f0-9]{64}", spec["sha256"])
        assert " " not in name and "%" not in name and name.endswith(".deb")
        assert spec["size"] == len(b"0.5.69-linux-aarch64")
        assert client.get(spec["url"].replace("/api/", "/")).content == b"0.5.69-linux-aarch64"
        # Publishing every platform advances the common release older clients still read.
        common = client.get("/v1/desktop/latest.json").json()
        assert common["version"] == "0.5.69"
        assert set(common["platforms"]) == {"windows-x86_64", "linux-aarch64"}


def test_required_platforms_wait_for_both_artifacts(tmp_path):
    directory, bundle = tmp_path / "release", tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "client.app.tar.gz").write_bytes(b"mac")
    (bundle / "client.app.tar.gz.sig").write_text("fixture-signature")
    result = subprocess.run([
        sys.executable, str(PUBLISH), "--version", "0.5.69", "--target", "darwin-aarch64",
        "--bundle", str(bundle), "--local-dir", str(directory),
        "--required-target", "darwin-aarch64", "--required-target", "windows-x86_64",
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not (directory / "latest.json").exists()
    assert json.loads((directory / "release-index.json").read_text())["legacy"] is None
