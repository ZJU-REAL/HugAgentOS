"""Additional formats through the archive HTTP contract."""

import bz2
import gzip
import io
import lzma
import tarfile

import pytest

from tests.api.test_archive_files import archive_client


@pytest.mark.parametrize(
    "suffix,mode",
    [
        ("tar", "w"),
        ("tar.gz", "w:gz"),
        ("tgz", "w:gz"),
        ("tar.bz2", "w:bz2"),
        ("tbz2", "w:bz2"),
        ("tar.xz", "w:xz"),
        ("txz", "w:xz"),
    ],
)
def test_tar_archives_list_and_download_members(archive_client, suffix, mode):
    client, upload = archive_client
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode=mode) as archive:
        info = tarfile.TarInfo("nested/hello.txt")
        info.size = 5
        archive.addfile(info, io.BytesIO(b"hello"))
    url = upload(data.getvalue(), "sample." + suffix)
    listing = client.get(url)
    assert listing.status_code == 200
    assert listing.json()["data"]["entries"][0]["path"] == "nested/hello.txt"
    assert client.get(url + "/member", params={"path": "nested/hello.txt"}).content == b"hello"


@pytest.mark.parametrize(
    "suffix,compress", [("gz", gzip.compress), ("bz2", bz2.compress), ("xz", lzma.compress)]
)
def test_single_compressed_file_has_one_readable_member(archive_client, suffix, compress):
    client, upload = archive_client
    url = upload(compress(b"single file"), "hello.txt." + suffix)
    listing = client.get(url)
    assert listing.status_code == 200
    assert listing.json()["data"]["entries"][0]["path"] == "hello.txt"
    assert client.get(url + "/member", params={"path": "hello.txt"}).content == b"single file"
