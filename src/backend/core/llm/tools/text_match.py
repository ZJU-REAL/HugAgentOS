"""Text replacement used by ``Edit``, ported from pi-mono's coding-agent ``edit-diff.ts``.

Matching runs on LF-normalized text (BOM stripped, original line endings restored on
write). An exact match is tried first; if it fails, both sides are fuzzy-normalized
(NFKC, trailing whitespace per line, smart quotes / dashes / special spaces → ASCII) and
the replacement is overlaid on the original so untouched lines keep their exact bytes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_BOM = "﻿"
_SINGLE_QUOTES = re.compile("[‘’‚‛]")
_DOUBLE_QUOTES = re.compile("[“”„‟]")
_DASHES = re.compile("[‐‑‒–—―−]")
_SPACES = re.compile("[  -   　]")
_LINES_WITH_ENDINGS = re.compile(r"[^\n]*\n|[^\n]+")


class EditMatchError(ValueError):
    """``old_string`` could not be applied; the message is shown to the model."""


@dataclass
class EditResult:
    old_text: str  # LF-normalized original, for the diff
    new_text: str  # LF-normalized result, for the diff
    new_raw: str  # what gets written: BOM + original line endings restored
    replaced: int
    fuzzy: bool


def split_bom(text: str) -> tuple[str, str]:
    return (_BOM, text[1:]) if text.startswith(_BOM) else ("", text)


def detect_line_ending(text: str) -> str:
    crlf, lf = text.find("\r\n"), text.find("\n")
    return "\r\n" if lf != -1 and crlf != -1 and crlf < lf else "\n"


def normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_for_fuzzy_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _SINGLE_QUOTES.sub("'", text)
    text = _DOUBLE_QUOTES.sub('"', text)
    text = _DASHES.sub("-", text)
    return _SPACES.sub(" ", text)


def _line_spans(text: str) -> list[tuple[int, int]]:
    spans, offset = [], 0
    for line in _LINES_WITH_ENDINGS.findall(text):
        spans.append((offset, offset + len(line)))
        offset += len(line)
    return spans


def _overlay_on_original(original: str, base: str, starts: list[int], old_len: int, new: str) -> str:
    """Rewrite only the lines the fuzzy matches touch; copy every other line from ``original``."""
    original_lines = _LINES_WITH_ENDINGS.findall(original)
    spans = _line_spans(base)
    if len(original_lines) != len(spans):
        raise EditMatchError("模糊匹配后行数不一致，无法安全替换。请让 old_string 与文件内容精确一致。")
    groups: list[list] = []  # [start_line, end_line, [match starts]]
    for start in starts:
        end = start + old_len
        first = next(i for i, (s, e) in enumerate(spans) if s <= start < e)
        last = first
        while spans[last][1] < end:
            last += 1
        if groups and first < groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], last + 1)
            groups[-1][2].append(start)
        else:
            groups.append([first, last + 1, [start]])
    out, line = [], 0
    for first, end_line, group_starts in groups:
        out.extend(original_lines[line:first])
        seg_start, seg_end = spans[first][0], spans[end_line - 1][1]
        segment = base[seg_start:seg_end]
        for start in reversed(group_starts):
            rel = start - seg_start
            segment = segment[:rel] + new + segment[rel + old_len :]
        out.append(segment)
        line = end_line
    out.extend(original_lines[line:])
    return "".join(out)


def _occurrences(haystack: str, needle: str) -> list[int]:
    starts, i = [], haystack.find(needle)
    while i != -1:
        starts.append(i)
        i = haystack.find(needle, i + len(needle))
    return starts


def apply_edit(raw: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
    bom, body = split_bom(raw)
    ending = detect_line_ending(body)
    content = normalize_to_lf(body)
    old, new = normalize_to_lf(old_string), normalize_to_lf(new_string)
    if not old:
        raise EditMatchError("old_string 不能为空。")

    base, fuzzy = content, False
    starts = _occurrences(base, old)
    if not starts:
        base, old_fuzzy = normalize_for_fuzzy_match(content), normalize_for_fuzzy_match(old)
        starts, fuzzy = _occurrences(base, old_fuzzy), True
        old = old_fuzzy
    if not starts:
        raise EditMatchError(
            "old_string 在文件中找不到（已尝试忽略行尾空白与中英文引号/破折号差异）。"
            "请检查缩进与换行（行号前缀不属于内容本身）。"
        )
    if len(starts) > 1 and not replace_all:
        raise EditMatchError(
            f"old_string 在文件中出现 {len(starts)} 次，无法唯一定位。"
            "请扩大 old_string 的上下文使其唯一，或传 replace_all=true 替换所有匹配。"
        )
    if not replace_all:
        starts = starts[:1]

    if fuzzy:
        result = _overlay_on_original(content, base, starts, len(old), new)
    else:
        result = content
        for start in reversed(starts):
            result = result[:start] + new + result[start + len(old) :]
    if result == content:
        raise EditMatchError("替换后内容没有变化。")
    new_raw = bom + (result.replace("\n", "\r\n") if ending == "\r\n" else result)
    return EditResult(content, result, new_raw, len(starts), fuzzy)
