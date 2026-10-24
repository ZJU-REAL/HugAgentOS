"""Run-bound MCP interactions. Remote services never receive the main conversation.

A server request (sampling, elicitation) is answered only on behalf of the outer
tool call that caused it. On 2026-07-28 connections the SDK runs those callbacks
inside the calling task, so `CURRENT_CALL` names the call exactly; handshake-era
servers deliver them from the session task, where only a sole in-flight call can
be credited.
"""
import asyncio
import json
from contextlib import aclosing, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit
import jsonschema
from agentscope.message import Msg, TextBlock
from mcp import types
from core.llm.tools import user_questions

CURRENT_CALL: ContextVar["CallScope | None"] = ContextVar("mcp_current_call", default=None)


def route(session):
    call = CURRENT_CALL.get()
    if call is not None and call.session is session:
        return call
    return next(iter(session.in_flight)) if len(session.in_flight) == 1 else None


@dataclass
class Interactions:
    """What one run allows a server to ask of it; shared by that run's calls."""
    request: object
    server: str
    interactive: bool
    agent: object = None
    samples: int = 0

    def open_call(self, session=None):
        return CallScope(self, session)


class CallScope:
    """One outer tool call; server requests made on its behalf end with it."""

    def __init__(self, run, session=None):
        self.run, self.session = run, session
        self.active = True
        self.pending = set()
        self.budget = None

    async def dispatch(self, method, context, params):
        task = asyncio.create_task(getattr(self, method)(context, params))
        self.pending.add(task)
        try:
            return await task
        finally:
            self.pending.discard(task)

    async def finish(self):
        self.active = False
        tasks = list(self.pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def validate(self):
        if not self.active or self.run.agent is None:
            raise RuntimeError("MCP interaction is outside an active run")
        prepared = getattr(self.run.agent, "_jx_prepared_capabilities", None)
        if prepared is not None:
            await asyncio.to_thread(prepared.validate, user_id=str(self.run.request.current_user_id or ""))
            if not await asyncio.to_thread(prepared.validate_tool_call, self.run.server):
                raise PermissionError("MCP capability was revoked")
        guard = getattr(getattr(self.run.agent, "toolkit", None), "capability_guard", None)
        if guard is not None and not await asyncio.to_thread(guard, self.run.server):
            raise PermissionError("MCP capability was revoked")

    async def sampling(self, _context, params):
        if not self.active or self.run.agent is None or self.run.samples >= 4:
            return types.ErrorData(code=-32600, message="Sampling unavailable or budget exhausted")
        if params.tools or params.include_context not in (None, "none"):
            return types.ErrorData(code=-32602, message="Sampling accepts isolated text without tools or shared context")
        messages = []
        for message in params.messages:
            content = message.content if isinstance(message.content, list) else [message.content]
            if any(not isinstance(item, types.TextContent) for item in content):
                return types.ErrorData(code=-32602, message="Only text sampling is enabled")
            messages.append(Msg(name=self.run.server, role=message.role,
                                content=[TextBlock(text=item.text) for item in content]))
        if params.system_prompt:
            messages.insert(0, Msg(name=self.run.server, role="system", content=params.system_prompt))
        if len(params.model_dump_json()) > 32000 or not 0 < params.max_tokens <= 4096:
            return types.ErrorData(code=-32602, message="Sampling request exceeds the run budget")
        self.run.samples += 1
        await self.validate()
        from core.models.usage import model_usage_scope
        from core.harness.ledger import HarnessUsageLedger
        with model_usage_scope(self.run.request.run_id or "", HarnessUsageLedger()):
            async with asyncio.timeout(60):
                response = await self.run.agent.model(messages, tools=None, max_tokens=params.max_tokens)
                response_value = None
                if hasattr(response, "__aiter__"):
                    async with aclosing(response):
                        async for chunk in response:
                            response_value = chunk
                else:
                    response_value = response
        if response_value is None:
            return types.ErrorData(code=-32603, message="Sampling model returned no response")
        if not self.active:
            return types.ErrorData(code=-32600, message="Outer MCP invocation ended")
        await self.validate()
        text = "".join(block.text for block in response_value.content if isinstance(block, TextBlock))
        return types.CreateMessageResult(role="assistant", content=types.TextContent(text=text),
                                         model=str(self.run.agent.model.model), stop_reason="endTurn")

    async def elicitation(self, _context, params):
        if not self.active or not self.run.interactive:
            return types.ElicitResult(action="decline")
        await self.validate()
        if params.mode == "url":
            parsed = urlsplit(params.url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                return types.ElicitResult(action="decline")
            questions = [{"id": "external", "question": f"{self.run.server}: {params.message}\n{params.url}",
                          "options": [{"label": "已在外部页面完成"}, {"label": "拒绝"}]}]
        else:
            schema = params.requested_schema
            props = schema.get("properties", {})
            if not props or len(props) > user_questions.MAX_QUESTIONS:
                return types.ElicitResult(action="decline")
            questions = []
            for key, spec in props.items():
                if spec.get("format") in {"password", "secret"} or spec.get("writeOnly"):
                    return types.ElicitResult(action="decline")
                values = spec.get("enum", [True, False] if spec.get("type") == "boolean" else [])
                questions.append({"id": key, "question": f"{self.run.server}: {params.message} — {spec.get('title', key)}",
                                  "options": [{"label": json.dumps(v, ensure_ascii=False)} for v in values]})
        # Only the human wait is excluded: validation, sampling and server
        # work before/after this block still consume the original budget.
        with self.budget.waiting_for_user() if self.budget is not None else nullcontext():
            outcome = await user_questions.ask(chat_id=self.run.request.chat_id, questions=questions, interactive=True)
        if not self.active or outcome["status"] != "answered":
            return types.ElicitResult(action="cancel")
        await self.validate()
        answers = outcome["answers"]
        if params.mode == "url":
            accepted = answers[0].get("selected_labels") == ["已在外部页面完成"]
            return types.ElicitResult(action="accept" if accepted else "decline")
        content = {}
        try:
            for answer in answers:
                if answer.get("skipped"):
                    continue
                key = answer["id"]
                selected = answer.get("selected_labels") or []
                value = answer.get("custom")
                if value is None and selected:
                    value = json.loads(selected[0])
                elif props[key].get("type") != "string":
                    value = json.loads(value)
                content[key] = value
            jsonschema.validate(content, schema)
        except (ValueError, TypeError, jsonschema.ValidationError, jsonschema.SchemaError):
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content=content)
