"""Security and failure behavior at the archive HTTP boundary."""

import io
import stat
import struct
import tarfile
import zipfile
from datetime import datetime, timezone

import pytest

from core.files.content.archive_browser import MAX_MEMBER_BYTES
from tests.api.test_archive_files import archive_client


def make_zip(members):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return data.getvalue()


@pytest.mark.parametrize("suffix", ["", "/member", "/member/preview"])
def test_another_users_archive_cannot_be_accessed(archive_client, suffix):
    client, upload = archive_client
    url = upload(make_zip([("secret.txt", b"private")]), owner="another")
    response = client.get(url + suffix, params={"path": "secret.txt"})
    assert response.status_code == 403
    assert b"private" not in response.content


@pytest.mark.parametrize("suffix", ["", "/member", "/member/preview"])
def test_deleted_archive_cannot_be_accessed(archive_client, suffix):
    client, upload = archive_client
    url = upload(make_zip([("secret.txt", b"private")]), deleted_at=datetime.now(timezone.utc))
    assert client.get(url + suffix, params={"path": "secret.txt"}).status_code == 404


def test_unsafe_paths_are_hidden_and_cannot_be_requested(archive_client):
    client, upload = archive_client
    url = upload(
        make_zip(
            [
                ("../outside.txt", b"no"),
                ("/absolute.txt", b"no"),
                ("C:/drive.txt", b"no"),
                ("folder\\evil.txt", b"no"),
                ("safe.txt", b"yes"),
            ]
        )
    )
    listing = client.get(url).json()["data"]
    assert listing["blocked_count"] == 4
    assert [entry["path"] for entry in listing["entries"]] == ["safe.txt"]
    assert client.get(url + "/member", params={"path": "../outside.txt"}).status_code == 400


def test_duplicate_paths_are_unreadable(archive_client):
    client, upload = archive_client
    with pytest.warns(UserWarning, match="Duplicate name"):
        url = upload(make_zip([("a.txt", b"first"), ("a.txt", b"last")]))
    entries = client.get(url).json()["data"]["entries"]
    assert len(entries) == 1
    assert entries[0]["reason"] == "archive_ambiguous_path"
    assert client.get(url + "/member", params={"path": "a.txt"}).status_code == 415


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_symbolic_links_are_not_followed(archive_client, kind):
    client, upload = archive_client
    data = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(data, "w") as archive:
            info = zipfile.ZipInfo("link.txt")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
    else:
        with tarfile.open(fileobj=data, mode="w") as archive:
            info = tarfile.TarInfo("link.txt")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
    url = upload(data.getvalue(), "links." + kind)
    entry = client.get(url).json()["data"]["entries"][0]
    assert entry["reason"] == "archive_special_file"
    assert client.get(url + "/member", params={"path": "link.txt"}).status_code == 415


def test_encrypted_zip_is_listed_but_not_read(archive_client):
    client, upload = archive_client
    data = bytearray(make_zip([("locked.txt", b"secret")]))
    central = data.index(b"PK\x01\x02")
    local = data.index(b"PK\x03\x04")
    struct.pack_into("<H", data, central + 8, struct.unpack_from("<H", data, central + 8)[0] | 1)
    struct.pack_into("<H", data, local + 6, struct.unpack_from("<H", data, local + 6)[0] | 1)
    url = upload(bytes(data))
    assert client.get(url).json()["data"]["entries"][0]["reason"] == "archive_encrypted"
    assert client.get(url + "/member", params={"path": "locked.txt"}).status_code == 415


def test_large_zip_member_cannot_be_expanded(archive_client):
    client, upload = archive_client
    url = upload(make_zip([("large.txt", b"0" * (MAX_MEMBER_BYTES + 1))]))
    assert client.get(url).json()["data"]["entries"][0]["reason"] == "archive_member_too_large"
    assert client.get(url + "/member", params={"path": "large.txt"}).status_code == 413


def test_expanded_tar_scan_has_a_hard_budget(archive_client, monkeypatch):
    client, upload = archive_client
    monkeypatch.setattr('core.files.content.archive_browser.MAX_SCAN_BYTES', 32 * 1024)
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        info = tarfile.TarInfo("file.txt")
        info.size = 64 * 1024
        archive.addfile(info, io.BytesIO(b"0" * info.size))
    url = upload(data.getvalue(), "data.tar.gz")
    assert client.get(url).status_code == 413


def test_too_many_members_are_rejected(archive_client):
    client, upload = archive_client
    url = upload(make_zip([(f"{index}.txt", b"") for index in range(5001)]))
    assert client.get(url).status_code == 413


@pytest.mark.parametrize("name", ["broken.zip", "broken.tar.gz", "broken.gz", "broken.xz"])
def test_corrupt_archive_explains_failure(archive_client, name):
    client, upload = archive_client
    url = upload(b"broken", name)
    assert client.get(url).status_code == 422


@pytest.mark.parametrize("name", ["sample.rar", "sample.7z"])
def test_unsupported_archives_return_an_explicit_format_error(archive_client, name):
    client, upload = archive_client
    url = upload(b"unsupported", name)
    response = client.get(url)
    assert response.status_code == 415
    assert response.json()["detail"] == "archive_format_unsupported"


@pytest.mark.parametrize("filename", ["page.html", "image.svg"])
def test_active_member_content_is_served_as_plain_text(archive_client, filename):
    client, upload = archive_client
    url = upload(make_zip([(filename, b"<script>alert(1)</script>")]))
    response = client.get(url + "/member", params={"path": filename, "inline": "true"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_missing_member_returns_404(archive_client):
    client, upload = archive_client
    url = upload(make_zip([("file.txt", b"hello")]))
    assert client.get(url + "/member", params={"path": "missing.txt"}).status_code == 404
