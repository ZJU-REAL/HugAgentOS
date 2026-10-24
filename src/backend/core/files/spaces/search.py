"""Name search over provider-neutral directory pages."""
import base64
import hashlib
import json
import time

from fastapi import HTTPException
from core.files.storage.entries import DirectoryPage, page_limit

BATCH_SIZE = 1000
SCAN_SECONDS = 20


def search_directory(storage, prefix, keyword, *, cursor=None, limit=200):
    limit = page_limit(limit)
    context = hashlib.sha256((prefix + "\0" + keyword).encode()).hexdigest()
    source_cursor, offset = None, 0
    if cursor:
        try:
            state = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            source_cursor, offset = state["cursor"], state["offset"]
            if state["context"] != context or not isinstance(offset, int) or not 0 <= offset <= BATCH_SIZE:
                raise ValueError
            if source_cursor is not None and not isinstance(source_cursor, str):
                raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise HTTPException(400, "Invalid search cursor") from exc
    deadline = time.monotonic() + SCAN_SECONDS
    found = []
    seen = set()
    while True:
        if time.monotonic() >= deadline:
            raise HTTPException(504, "Directory search timed out; narrow the keyword and retry")
        page = storage.list_directory(prefix, cursor=source_cursor, limit=BATCH_SIZE)
        for index, item in enumerate(page.entries):
            if not item.key.startswith(prefix):
                raise HTTPException(502, "Storage returned an invalid path")
            if index < offset or keyword not in item.name.lower():
                continue
            found.append(item)
            if len(found) == limit:
                next_source, next_offset = source_cursor, index + 1
                if next_offset == len(page.entries):
                    next_source, next_offset = page.next_cursor, 0
                    if not next_source:
                        return DirectoryPage(tuple(found))
                state = {"context": context, "cursor": next_source, "offset": next_offset}
                token = base64.urlsafe_b64encode(json.dumps(state).encode()).decode()
                return DirectoryPage(tuple(found), token)
        if not page.next_cursor:
            return DirectoryPage(tuple(found))
        if page.next_cursor == source_cursor or page.next_cursor in seen:
            raise HTTPException(502, "Storage returned a non-advancing cursor")
        seen.add(page.next_cursor)
        source_cursor, offset = page.next_cursor, 0
