from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
import time
import pytest
from core.content.office_preview_cache import cached_office_pdf


def test_preview_cache_deduplicates_and_invalidates(tmp_path):
    source = tmp_path / "document.docx"
    source.write_bytes(b"first")
    calls = []
    lock = Lock()

    def convert(path, file_id):
        with lock:
            calls.append(Path(path).read_bytes())
        time.sleep(0.03)
        target = Path(path).parent / "converted"
        target.mkdir()
        pdf = target / "preview.pdf"
        pdf.write_bytes(b"%PDF-1.4 " + Path(path).read_bytes())
        return str(pdf), str(target)

    def render(_):
        path, directory = cached_office_pdf(
            str(source), "doc", convert, root=tmp_path / "cache", version="v1"
        )
        content = Path(path).read_bytes()
        import shutil

        shutil.rmtree(directory)
        return content

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(render, range(6))) == [b"%PDF-1.4 first"] * 6
    assert calls == [b"first"]
    source.write_bytes(b"second")
    assert render(0) == b"%PDF-1.4 second"
    assert calls == [b"first", b"second"]
    path, _ = cached_office_pdf(str(source), "doc", convert, root=tmp_path / "cache", version="v2")
    assert Path(path).read_bytes() == b"%PDF-1.4 second"
    assert len(calls) == 3


def test_failed_conversion_is_not_cached(tmp_path):
    source = tmp_path / "bad.docx"
    source.write_bytes(b"invalid")

    def fail(*args):
        raise RuntimeError("conversion failed")

    with pytest.raises(RuntimeError):
        cached_office_pdf(str(source), "doc", fail, root=tmp_path / "cache", version="v1")
    assert not list((tmp_path / "cache").glob("*.pdf"))


def test_unexpected_lock_errors_are_not_retried(monkeypatch, tmp_path):
    import errno
    import fcntl
    from core.content.office_preview_cache import _try_lock

    def fail(*args):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(fcntl, "flock", fail)
    with (tmp_path / "lock").open("w+b") as handle:
        with pytest.raises(OSError) as error:
            _try_lock(handle)
        assert error.value.errno == errno.ENOLCK


def _process_preview(args):
    source, root, calls = args

    def convert(path, file_id):
        with open(calls, "a") as log:
            log.write("conversion\n")
        time.sleep(0.1)
        target = Path(path).parent / "converted"
        target.mkdir()
        pdf = target / "preview.pdf"
        pdf.write_bytes(b"%PDF-1.4 process")
        return str(pdf), str(target)

    pdf, work = cached_office_pdf(source, "doc", convert, root=root, version="process-test")
    data = Path(pdf).read_bytes()
    import shutil

    shutil.rmtree(work)
    return data


def test_preview_deduplicates_across_worker_processes(tmp_path):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    source = tmp_path / "source.docx"
    source.write_bytes(b"process source")
    args = (str(source), str(tmp_path / "cache"), str(tmp_path / "calls"))
    with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("fork")) as pool:
        assert list(pool.map(_process_preview, [args] * 3)) == [b"%PDF-1.4 process"] * 3
    assert (tmp_path / "calls").read_text().splitlines() == ["conversion"]


def test_eviction_keeps_response_file_available(monkeypatch, tmp_path):
    import core.content.office_preview_cache as cache

    monkeypatch.setattr(cache, "CACHE_ENTRIES", 0)
    source = tmp_path / "source.docx"
    source.write_bytes(b"evict")

    def convert(path, fid):
        out = Path(path).parent / "out"
        out.mkdir()
        pdf = out / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4 pinned")
        return str(pdf), str(out)

    pdf, work = cache.cached_office_pdf(
        source, "doc", convert, root=tmp_path / "cache", version="evict"
    )
    assert not list((tmp_path / "cache").glob("*.pdf"))
    assert Path(pdf).read_bytes() == b"%PDF-1.4 pinned"
    import shutil

    shutil.rmtree(work)
