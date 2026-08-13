"""Zero-argument tools must still get a tool_call event before their result.

``_tool_args_ready`` holds the tool card back while streamed args are still
incomplete. It can't tell "not written yet" from "there is nothing to write",
so a tool declared without parameters (its args stay ``{}`` forever) never
cleared the gate and its card was suppressed for the whole run. The result then
arrived orphaned, and the frontend — finding no card for that tool_id — bound it
onto whichever sibling tool was still running, so the call disappeared from the
tool list and briefly overwrote the sibling's output.

``_synthesize_missing_tool_call`` closes that hole at the tool_result stage.
"""

from core.chat.tool_log import attach_tool_result, build_tool_call_event, upsert_tool_call
from core.config.display_names import TOOL_DISPLAY_NAMES
from orchestration.tool_payloads import _tool_args_ready
from orchestration.workflow import _synthesize_missing_tool_call

# The tool this was found on. Its display name is edition-dependent (the industry
# MCP is trimmed out of CE), so the tests resolve it through the same map the
# production code uses rather than pinning a literal.
ZERO_ARG_TOOL = "get_latest_ai_news"


def test_zero_arg_tool_is_gated_out_of_the_tool_call_stage():
    # The precondition this fix exists for: no args, not a fast-emit tool.
    assert _tool_args_ready(ZERO_ARG_TOOL, {}) is False


def test_synthesizes_a_card_for_a_tool_that_never_emitted_one():
    displayed: set = set()
    evt = _synthesize_missing_tool_call("call_1", ZERO_ARG_TOOL, displayed)
    assert evt == {
        "type": "tool_call",
        "tool_name": ZERO_ARG_TOOL,
        "tool_display_name": TOOL_DISPLAY_NAMES.get(ZERO_ARG_TOOL, ZERO_ARG_TOOL),
        "tool_args": {},
        "input": {},
        "tool_id": "call_1",
    }
    # The id is now claimed, so a repeated result can't emit a second card.
    assert displayed == {"call_1"}
    assert _synthesize_missing_tool_call("call_1", ZERO_ARG_TOOL, displayed) is None


def test_no_synthesis_when_the_tool_call_already_streamed():
    assert _synthesize_missing_tool_call("call_1", "bash", {"call_1"}) is None


def test_no_synthesis_without_a_tool_id():
    # Nothing to key the card on — leave the existing name-based matching alone.
    assert _synthesize_missing_tool_call("", ZERO_ARG_TOOL, set()) is None


def test_skill_load_keeps_its_curated_display_name():
    evt = _synthesize_missing_tool_call("call_2", "load_skill", set())
    assert evt is not None
    assert evt["tool_display_name"] == "加载技能"


def test_unknown_tool_falls_back_to_its_raw_name():
    evt = _synthesize_missing_tool_call("call_3", "some_third_party_tool", set())
    assert evt is not None
    assert evt["tool_display_name"] == "some_third_party_tool"


def test_persisted_log_entry_is_complete_instead_of_the_bare_fallback():
    """Without the synthesized call the log entry came from attach_tool_result's
    append branch with no display name or args, leaving an incomplete history
    card that could not be matched to the original call."""
    log: list = []
    upsert_tool_call(log, {"tool_name": "sibling", "tool_display_name": "同伴", "tool_args": {"a": 1}, "tool_id": "sib"})

    # New behaviour: the synthesized call lands in the log before its result.
    evt = _synthesize_missing_tool_call("call_1", ZERO_ARG_TOOL, set())
    assert evt is not None
    build_tool_call_event({**evt, "type": "tool_call"}, "c1", log)
    attach_tool_result(log, "call_1", ZERO_ARG_TOOL, {"items": []})

    entry = next(tc for tc in log if tc["tool_id"] == "call_1")
    assert entry["tool_display_name"] == TOOL_DISPLAY_NAMES.get(ZERO_ARG_TOOL, ZERO_ARG_TOOL)
    assert entry["tool_args"] == {}
    assert entry["result"] == {"items": []}
    assert entry["status"] == "success"
    # The sibling card must not have absorbed the orphan result.
    assert "result" not in next(tc for tc in log if tc["tool_id"] == "sib")
