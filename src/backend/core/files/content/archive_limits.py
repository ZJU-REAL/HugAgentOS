"""Preflight ZIP metadata and limit XZ decoder memory before allocating objects."""

from contextlib import contextmanager
import lzma
from pathlib import Path
import struct


# Limits are passed by the caller so the browser owns its resource policy.
def check_zip_directory(path, max_entries, max_directory_bytes):
    with open(path, "rb") as stream:
        size = Path(path).stat().st_size
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
        end = tail.rfind(b"PK\x05\x06")
        if end < 0 or end + 22 > len(tail):
            raise ValueError("invalid")
        fields = struct.unpack_from("<4s4H2LH", tail, end)
        _, disk, directory_disk, disk_count, count, directory_size, offset, comment = fields
        if end + 22 + comment != len(tail):
            raise ValueError("invalid")
        end_position = size - len(tail) + end
        if disk or directory_disk or disk_count != count:
            raise ValueError("unsupported")
        # ZipFile checks for ZIP64 even when the legacy fields are not sentinels.
        # Read the physical footer, not the attacker-provided locator offset.
        locator = b""
        if end_position >= 20:
            stream.seek(end_position - 20)
            locator = stream.read(20)
        if locator[:4] == b"PK\x06\x07":
            _, zip64_disk, zip64_offset, disks = struct.unpack("<4sLQL", locator)
            if zip64_disk or disks != 1 or end_position < 76:
                raise ValueError("unsupported")
            zip64_position = end_position - 76
            stream.seek(zip64_position)
            record = stream.read(56)
            if len(record) != 56 or record[:4] != b"PK\x06\x06":
                raise ValueError("invalid")
            values = struct.unpack("<4sQ2H2L4Q", record)
            if values[1] != 44 or values[4] or values[5] or values[6] != values[7]:
                raise ValueError("invalid")
            count, directory_size, offset = values[7:10]
            if zip64_offset != offset + directory_size:
                raise ValueError("invalid")
            end_position = zip64_position
        elif count == 0xFFFF or directory_size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
            raise ValueError("invalid")
        if count > max_entries:
            raise ValueError("entries")
        if directory_size > max_directory_bytes:
            raise ValueError("directory")
        # Derive the physical offset to support ordinary self-extracting ZIPs.
        start = end_position - directory_size
        if start < 0 or offset > start:
            raise ValueError("invalid")
        stream.seek(start)
        remaining, actual = directory_size, 0
        while remaining:
            header = stream.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise ValueError("invalid")
            actual += 1
            if actual > max_entries:
                raise ValueError("entries")
            name_size, extra_size, comment_size = struct.unpack_from("<3H", header, 28)
            record_size = 46 + name_size + extra_size + comment_size
            if record_size > remaining:
                raise ValueError("invalid")
            stream.seek(record_size - 46, 1)
            remaining -= record_size
        if actual != count:
            raise ValueError("invalid")


class _XzStream:
    def __init__(self, source, memory_limit):
        self.source = source
        self.memory_limit = memory_limit
        self.decoder = lzma.LZMADecompressor(memlimit=memory_limit)
        self.pending = b""
        self.finished = False

    def read(self, size):
        chunks, remaining = [], size
        while remaining and not self.finished:
            if self.decoder.eof:
                self.pending = self.decoder.unused_data
                padding = 0
                while True:
                    if not self.pending:
                        self.pending = self.source.read(64 * 1024)
                    if not self.pending:
                        self.finished = True
                        break
                    stripped = self.pending.lstrip(b"\0")
                    padding += len(self.pending) - len(stripped)
                    self.pending = stripped
                    if self.pending:
                        break
                if padding % 4:
                    raise lzma.LZMAError("Invalid XZ padding")
                if self.finished:
                    break
                self.decoder = lzma.LZMADecompressor(memlimit=self.memory_limit)
            if self.decoder.needs_input:
                block = self.pending or self.source.read(64 * 1024)
                self.pending = b""
                if not block:
                    raise EOFError("Incomplete XZ stream")
            else:
                block = b""
            decoded = self.decoder.decompress(block, max_length=remaining)
            chunks.append(decoded)
            remaining -= len(decoded)
        return b"".join(chunks)


@contextmanager
def open_bounded_xz(path, mode="rb", *, memory_limit=64 * 1024 * 1024):
    with open(path, mode) as source:
        yield _XzStream(source, memory_limit)
