"""Model-facing ``ask_user_question`` tool over the suspended question registry."""

import logging
from typing import Annotated, List, Optional

from agentscope.message import TextBlock
from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk as ToolResponse
from agentscope.tool._response import ToolResultState
from core.llm.tools import user_questions
from core.llm.tools._common import resp_json
from pydantic import ConfigDict, Field
from typing_extensions import NotRequired, TypedDict

logger = logging.getLogger(__name__)

_TOOL_DESCRIPTION = (
    "Ask the user a concise question when you need confirmation, a choice, or missing "
    "information before proceeding. Send one or more questions, each with a stable id "
    "that will be echoed in the answer."
)


class AskUserQuestionOption(TypedDict):
    __pydantic_config__ = ConfigDict(extra="allow")

    label: Annotated[str, Field(description="Short user-facing option label.")]
    description: NotRequired[
        Annotated[
            str,
            Field(description="One sentence explaining the tradeoff or impact."),
        ]
    ]


class AskUserQuestionItem(TypedDict):
    __pydantic_config__ = ConfigDict(extra="allow")

    id: Annotated[
        str,
        Field(description="Stable id for this question; echoed in the answer."),
    ]
    question: Annotated[
        str,
        Field(description="The specific question to ask the user."),
    ]
    header: NotRequired[
        Annotated[
            str,
            Field(
                description=(
                    'Optional short heading for the question, such as "Confirm" or '
                    '"Choose Mode".'
                ),
            ),
        ]
    ]
    options: NotRequired[
        Annotated[
            List[AskUserQuestionOption],
            Field(
                description=(
                    "Optional choices to show the user. If you recommend one, put it "
                    'first and append "(Recommended)" to that label.'
                ),
            ),
        ]
    ]
    multi_select: NotRequired[
        Annotated[
            bool,
            Field(
                description=(
                    "Whether the user may select more than one option. Defaults to false."
                ),
            ),
        ]
    ]


def _error_response(status: str) -> ToolResponse:
    """Return DSH-style tool errors while retaining local timeout semantics."""

    errors = {
        "cancelled": ("ASK_CANCELLED", "the user cancelled ask_user_question"),
        "timeout": (
            "ASK_TIMEOUT",
            "ask_user_question timed out before the user answered; do not repeat the same "
            "question and continue with the safest reasonable default",
        ),
        user_questions.STATUS_BLOCKED: (
            "NO_PROVIDER",
            "no user-questions provider is registered",
        ),
    }
    code, message = errors.get(
        status,
        ("ASK_ABORTED", "ask_user_question was aborted before the user answered"),
    )
    return ToolResponse(
        content=[TextBlock(type="text", text=f"Error: {message}")],
        state=ToolResultState.ERROR,
        metadata={
            "error": {"name": "UserQuestionError", "code": code},
            "status": status,
        },
    )


def should_register_ask_user_question(
    *,
    top_level_chat: bool,
    turbo_mode: bool,
    disable_tools: bool,
    chat_id: Optional[str],
) -> bool:
    """Central capability boundary for browser-backed user questions."""

    return bool(top_level_chat and chat_id and not turbo_mode and not disable_tools)


def register_ask_user_question(
    toolkit: Toolkit,
    *,
    chat_id: Optional[str] = None,
    interactive: bool = True,
) -> None:
    """Register the question tool for an interactive top-level chat."""

    async def ask_user_question(
        questions: Annotated[
            List[AskUserQuestionItem],
            Field(description="Questions to ask the user before continuing."),
        ],
    ) -> ToolResponse:
        """Ask the user one or more questions and wait for the human answer."""

        result = await user_questions.ask(
            chat_id=chat_id,
            questions=questions,
            interactive=interactive,
        )
        status = result.get("status")
        if status == "answered":
            answers = []
            for answer in result.get("answers", []):
                projected = {
                    "id": answer.get("id", ""),
                    "selected": list(answer.get("selected_labels", [])),
                }
                custom = answer.get("custom")
                if custom is not None:
                    projected["custom"] = custom
                answers.append(projected)
            return resp_json({"answers": answers})

        return _error_response(str(status or "aborted"))

    # AgentScope derives the model-visible description from ``__doc__``. Set it
    # to DSH's exact two-sentence contract instead of leaking local policy or
    # transport details into the fixed tool-schema prefix.
    ask_user_question.__doc__ = _TOOL_DESCRIPTION
    toolkit.register_tool_function(ask_user_question, namesake_strategy="override")
    logger.info("[factory] Registered ask_user_question tool (interactive top-level chat)")


__all__ = [
    "AskUserQuestionItem",
    "AskUserQuestionOption",
    "register_ask_user_question",
    "should_register_ask_user_question",
]
