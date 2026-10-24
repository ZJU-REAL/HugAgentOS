"""The user's quote — from a chat message or from a file open in the canvas — reaches the model."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.chat.compaction.history import normalize_rows
from core.chat.context import build_effective_user_message
from core.chat.schemas import ChatRequest, QuoteFileSource, QuoteMessageSource

FILE_QUOTE = {
    "text": "  第二季度营收同比增长 18%  ",
    "source": {
        "kind": "file",
        "file_id": "file_abc",
        "file_name": "经营报告.docx",
        "locator": "第 3 页 · 2.1 营收",
    },
}


def test_request_parses_file_and_message_sources():
    request = ChatRequest(chat_id="c1", message="解释这段内容", quoted_follow_up=FILE_QUOTE)
    quote = request.quoted_follow_up
    assert quote.text == "第二季度营收同比增长 18%"
    assert isinstance(quote.source, QuoteFileSource)
    assert (quote.source.file_name, quote.source.locator) == ("经营报告.docx", "第 3 页 · 2.1 营收")

    request = ChatRequest(
        chat_id="c1",
        message="展开说说",
        quoted_follow_up={"text": "上一轮的结论", "source": {"kind": "message", "ts": 1700}},
    )
    assert isinstance(request.quoted_follow_up.source, QuoteMessageSource)
    assert request.quoted_follow_up.source.ts == 1700


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "file", "file_id": "f1"},  # a file quote must name the file
        {"kind": "page", "page": 3},  # unknown origin
        {"kind": "file", "file_id": "f1", "file_name": "a.md", "locator": "x" * 201},
    ],
)
def test_request_rejects_malformed_sources(source):
    with pytest.raises(ValidationError) as caught:
        ChatRequest(chat_id="c1", message="hi", quoted_follow_up={"text": "原文", "source": source})
    assert all(error["loc"][0] == "quoted_follow_up" for error in caught.value.errors())


def test_file_quote_tells_the_model_which_file_and_where():
    request = ChatRequest(chat_id="c1", message="解释这段内容", quoted_follow_up=FILE_QUOTE)
    sent = build_effective_user_message(request.message, request.quoted_follow_up)

    assert "经营报告.docx" in sent and "file_abc" in sent and "第 3 页 · 2.1 营收" in sent
    assert sent.index("【引用原文】") < sent.index("第二季度营收同比增长 18%")
    assert sent.index("第二季度营收同比增长 18%") < sent.index("【用户提问】")
    assert sent.rstrip().endswith("解释这段内容")
    assert "追问" not in sent


def test_file_quote_without_locator_still_names_the_file():
    quote = {"text": "原文", "source": {"kind": "file", "file_id": "f1", "file_name": "a.md"}}
    sent = build_effective_user_message("改写", quote)
    assert "a.md" in sent and "位置" not in sent


def test_message_quote_keeps_follow_up_framing():
    for quote in ({"text": "上一轮的结论", "source": {"kind": "message", "ts": 1}}, {"text": "上一轮的结论"}):
        sent = build_effective_user_message("为什么", quote)
        assert "【引用原文】\n上一轮的结论" in sent and sent.endswith("【用户追问】\n为什么")


def test_unreadable_stored_quote_is_dropped_not_fatal():
    assert build_effective_user_message("你好", {"source": {"kind": "file"}}) == "你好"
    assert build_effective_user_message("你好", None) == "你好"


def test_history_replay_keeps_the_file_source():
    """The stored turn (extra_data is the request's model_dump) replays with the same framing."""
    request = ChatRequest(chat_id="c1", message="解释这段内容", quoted_follow_up=FILE_QUOTE)
    row = SimpleNamespace(
        role="user",
        content=request.message,
        extra_data={"quoted_follow_up": request.quoted_follow_up.model_dump()},
    )
    [replayed] = normalize_rows([row])
    assert replayed["content"] == build_effective_user_message(
        request.message, request.quoted_follow_up
    )
    assert "经营报告.docx" in replayed["content"]
