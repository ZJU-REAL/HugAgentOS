"""Stateless, path-ordered file traversal. Runs inside the caller's sandbox."""
import base64
import fnmatch
import hashlib
import json
import os
import sys


def matches(parts, patterns):
    previous = [True] + [False] * len(patterns)
    for j, pattern in enumerate(patterns, 1):
        previous[j] = previous[j - 1] and pattern == "**"
    for part in parts:
        current = [False] * (len(patterns) + 1)
        for j, pattern in enumerate(patterns, 1):
            current[j] = ((current[j - 1] or previous[j]) if pattern == "**"
                          else previous[j - 1] and fnmatch.fnmatchcase(part, pattern))
        previous = current
    return previous[-1]


def scan(root, pattern, limit=100, cursor=None):
    root = os.path.abspath(root)
    patterns = pattern.split("/")
    if len(patterns) > 64 or any(part in ("", ".", "..") for part in patterns):
        raise ValueError("Invalid glob pattern")
    identity = hashlib.sha256(json.dumps([root, pattern]).encode()).hexdigest()
    after = ()
    count = 0
    if cursor:
        if len(cursor) > 16384:
            raise ValueError("Invalid cursor")
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if value["query"] != identity:
                raise ValueError("Cursor belongs to another query")
            after = tuple(value["after"])
            count = value["count"]
            if (not after or len(after) > 128 or not isinstance(count, int) or count < 0
                    or any(not isinstance(p, str) or p in ("", ".", "..")
                           or "/" in p or "\x00" in p for p in after)):
                raise ValueError("Invalid cursor")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid cursor; start a new search") from exc

    def walk(directory, parents=()):
        if len(parents) > 128:
            raise ValueError("Directory depth exceeds search limit")
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            parts = parents + (entry.name,)
            # Only reopen the ancestor directories of the continuation point.
            if after and parts <= after and after[:len(parts)] != parts:
                continue
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if "**" in patterns or len(parts) < len(patterns):
                    yield from walk(entry.path, parts)
            elif parts > after and entry.is_file(follow_symlinks=False) and matches(parts, patterns):
                yield parts

    found = []
    more = False
    for parts in walk(root):
        if limit is not None and len(found) >= limit:
            more = True
            break
        found.append(parts)
    count += len(found)
    next_cursor = None
    if more:
        value = {"query": identity, "after": found[-1], "count": count}
        next_cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return {"ok": True, "filenames": [os.path.join(root, *parts) for parts in found],
            "num_files": len(found), "has_more": more, "truncated": more,
            "next_cursor": next_cursor, "page_size": limit,
            "total": None if more else count, "order": "path", "path": root, "pattern": pattern}


if __name__ == "__main__":
    try:
        print(json.dumps(scan(**json.loads(sys.argv[1])), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
