"""Recursive chunking packs short parts in near-linear time and keeps every chunk in budget."""
from __future__ import annotations

import time

from features.knowledge import kb_parser_state, parser_chunks, parser_tokens


def test_short_phrases_pack_quickly_and_lose_nothing():
    # Space-separated two-letter words: one tokenizer call per word over the
    # whole growing chunk took ~30 s per MiB.
    text = "ab cd " * 40_000
    parser_tokens.count_tokens("warm up")  # loading the tokenizer is not chunking
    started = time.perf_counter()
    chunks = parser_chunks.recursive_split(text, kb_parser_state.RECURSIVE_SEPARATORS, 512)
    elapsed = time.perf_counter() - started
    assert " ".join(chunks) == text
    assert all(parser_tokens.count_tokens(chunk) <= 512 for chunk in chunks)
    assert elapsed < 3.0, f"chunking took {elapsed:.2f}s"


def test_chunks_are_the_longest_runs_that_fit():
    text = "，".join(f"第{i}条记录内容" for i in range(3000))
    chunks = parser_chunks.recursive_split(text, kb_parser_state.RECURSIVE_SEPARATORS, 64)
    assert "，".join(chunks) == text
    for chunk, following in zip(chunks, chunks[1:]):
        assert parser_tokens.count_tokens(chunk) <= 64
        assert parser_tokens.count_tokens(chunk + "，" + following.split("，")[0]) > 64


def test_oversized_part_recurses_and_empty_leading_parts_are_dropped():
    long_sentence = "，".join(["机构编制"] * 400)
    text = "\n\n\n\n短段落\n\n" + long_sentence
    chunks = parser_chunks.recursive_split(text, kb_parser_state.RECURSIVE_SEPARATORS, 50)
    assert chunks[0].lstrip("\n") == chunks[0]
    assert all(parser_tokens.count_tokens(chunk) <= 50 for chunk in chunks)
    assert "".join(chunks).replace("，", "").replace("\n", "") == text.replace("，", "").replace("\n", "")
