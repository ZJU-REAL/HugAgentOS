"""Allocation limits are enforced before untrusted decoder/ZIP object creation."""

import io
import lzma
import struct
import zipfile
import zlib

from tests.api.test_archive_files import archive_client
from tests.api.test_archive_safety import make_zip


def test_xz_dictionary_memory_is_limited(archive_client):
    client, upload = archive_client
    data = bytearray(
        lzma.compress(
            b"hello",
            filters=[
                {
                    "id": lzma.FILTER_LZMA2,
                    "dict_size": 1 << 20,
                }
            ],
        )
    )
    # XZ block header: LZMA2 filter property 38 requests a 1 GiB dictionary.
    assert data[14:16] == b"\x21\x01"
    data[16] = 38
    header_size = (data[12] + 1) * 4
    crc_offset = 12 + header_size - 4
    struct.pack_into("<L", data, crc_offset, zlib.crc32(data[12:crc_offset]))
    url = upload(bytes(data), "dictionary.xz")
    response = client.get(url)
    assert response.status_code == 413
    assert response.json()["detail"] == "archive_decoder_memory"


def test_zip_entry_limit_is_checked_before_zipinfo_allocation(archive_client, monkeypatch):
    client, upload = archive_client
    data = bytearray(make_zip([(f"{index}.txt", b"") for index in range(5001)]))
    end = data.rfind(b"PK\x05\x06")
    # A forged count cannot bypass the actual central directory scan.
    struct.pack_into("<2H", data, end + 8, 0, 0)
    url = upload(bytes(data))

    def forbidden(*args, **kwargs):
        raise AssertionError("ZipFile must not allocate untrusted ZipInfo objects")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    assert client.get(url).status_code == 413


def test_small_zip64_archive_can_be_browsed(archive_client):
    client, upload = archive_client
    data = make_zip([("hello.txt", b"hello")])
    end = data.rfind(b"PK\x05\x06")
    fields = struct.unpack_from("<4s4H2LH", data, end)
    count, size, offset = fields[4:7]
    record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, size, offset)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, end, 1)
    legacy = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    url = upload(data[:end] + record + locator + legacy)
    assert client.get(url).status_code == 200
    assert client.get(url + "/member", params={"path": "hello.txt"}).content == b"hello"


def test_concatenated_xz_streams_are_read(archive_client):
    client, upload = archive_client
    url = upload(lzma.compress(b"first") + b"\0" * 4 + lzma.compress(b"second"), "hello.txt.xz")
    assert client.get(url + "/member", params={"path": "hello.txt"}).content == b"firstsecond"


def test_office_member_previews_as_real_pdf(archive_client):
    from docx import Document

    client, upload = archive_client
    document = Document()
    document.add_paragraph("Archive Office preview")
    data = io.BytesIO()
    document.save(data)
    url = upload(make_zip([("docs/report.docx", data.getvalue())]))
    response = client.get(url + "/member/preview", params={"path": "docs/report.docx"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_zip64_footer_cannot_bypass_legacy_entry_count(archive_client, monkeypatch):
    client, upload = archive_client
    data = make_zip([(str(index), b"") for index in range(5001)])
    end = data.rfind(b"PK\x05\x06")
    fields = struct.unpack_from("<4s4H2LH", data, end)
    count, size, offset = fields[4:7]
    fake = bytearray(data[offset : offset + 46])
    struct.pack_into("<3H", fake, 28, 0, 0, 76)
    base = data[:end] + fake
    record = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        count + 1,
        count + 1,
        size + 46,
        offset,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, len(base), 1)
    legacy = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        1,
        1,
        122,
        len(base) - 46,
        0,
    )
    url = upload(base + record + locator + legacy)

    def forbidden(*args, **kwargs):
        raise AssertionError("ZIP64 preflight must reject before ZipFile allocation")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    assert client.get(url).status_code == 413
