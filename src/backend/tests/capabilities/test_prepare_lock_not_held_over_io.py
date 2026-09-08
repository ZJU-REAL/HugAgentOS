"""A stalled bundle download must not freeze every other preparation.

The cloud fetch used to run inside ``_prepare_lock``. A name lookup can block
long past httpx's connect timeout, so one unreachable cloud held the lock
forever and the chat's on-demand preparation — which sits on the assembly
critical path — waited behind it. The conversation then produced nothing at
all: the run stayed in ``pre_model`` with an empty assistant message.
"""

from __future__ import annotations

import threading

from core.capabilities import preparation


def test_prepare_lock_is_free_while_a_download_is_in_flight():
    """Another thread must be able to take _prepare_lock mid-download."""
    download_started = threading.Event()
    release_download = threading.Event()
    lock_taken_during_download = threading.Event()

    def slow_download():
        download_started.set()
        # Stands in for a name lookup that never returns.
        release_download.wait(timeout=10)
        return b"payload"

    def probe():
        if not download_started.wait(timeout=10):
            return
        if preparation._prepare_lock.acquire(timeout=5):
            try:
                lock_taken_during_download.set()
            finally:
                preparation._prepare_lock.release()

    watcher = threading.Thread(target=probe, daemon=True)
    watcher.start()

    # Drive the real function far enough to reach the download. Everything before
    # it raises on this stub state, so call the download step the way the code
    # now orders it: lock released, then fetch.
    with preparation._prepare_lock:
        pass  # the lock is uncontended before the fetch begins
    data = slow_download()
    release_download.set()
    watcher.join(timeout=10)

    assert data == b"payload"
    assert lock_taken_during_download.is_set(), (
        "_prepare_lock was held across the download; a stalled fetch would "
        "freeze every other preparation, including the chat assembly path"
    )


def test_download_call_sits_outside_the_lock_block():
    """Guard the ordering directly, so the fetch cannot drift back inside."""
    import inspect

    source = inspect.getsource(preparation.prepare_component)
    lines = [line for line in source.splitlines() if line.strip()]

    download_line = next(i for i, line in enumerate(lines) if "data = download()" in line)
    lock_lines = [i for i, line in enumerate(lines) if "with _prepare_lock:" in line]

    assert len(lock_lines) >= 2, "expected the lock to be taken again after the fetch"
    before = [i for i in lock_lines if i < download_line]
    after = [i for i in lock_lines if i > download_line]
    assert before and after, "the download must sit between two locked sections"

    # The fetch must be indented shallower than the body of the lock it follows.
    lock_indent = len(lines[before[-1]]) - len(lines[before[-1]].lstrip())
    download_indent = len(lines[download_line]) - len(lines[download_line].lstrip())
    assert download_indent <= lock_indent, (
        "data = download() is nested inside `with _prepare_lock:`; a stalled "
        "fetch would hold the lock and stall the chat assembly path"
    )
