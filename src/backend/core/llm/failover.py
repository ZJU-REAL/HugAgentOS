"""Cross-provider failover for chat models.

A role maps to a single provider, so an upstream that stops serving — an
exhausted balance, a revoked key, a retired model name — ends every run that
picks it. ``_stream_with_bounded_retry`` already re-issues transient failures
against the *same* endpoint; this module answers the other half: when the
failure says the provider itself is unusable, move to the next one.

Candidate order is ``model_providers.weight`` (higher first) — the "gateway
weight" already editable in the model console, where it means exactly what
failover needs it to mean: the bigger the number, the more this endpoint
should be used. Nothing new is configured, and every active chat provider is
a candidate, so a deployment that never touches the field still falls back.

Switching is only legal before the first stream event — the boundary
``_stream_with_bounded_retry`` establishes, since a completion stays idempotent
until a chunk has been consumed and swapping models mid-answer would
contradict what the reader already has on screen.

A request carrying media narrows the chain further: a text-only endpoint
answers it with a 400 that ``provider_is_unusable`` reads — correctly — as
"the request is wrong", ending the run instead of walking on. So candidates
that cannot read media are dropped before they are tried rather than after.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import Any, AsyncGenerator, Callable, Optional

from agentscope.message import Msg
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.tool._types import ToolChoice
from core.llm.providers._image_tokens import ImageTokenCountingMixin
from core.llm.tool_call_identity import ToolCallIdentityMixin

logger = logging.getLogger(__name__)

# How long a provider stays deprioritised after reporting itself unusable.
# Short enough that a top-up or key rotation recovers on its own, long enough
# that a dead upstream is not re-probed by every concurrent run.
PROVIDER_COOLDOWN_SECONDS = 120.0

_cooldown: dict[str, float] = {}

# Which candidate answered the call in flight. The model instance is cached and
# shared by every concurrent run, so "who is answering" cannot live on it: one
# run falling back would otherwise rewrite the context window and the
# reasoning-channel flag that another run's SSE parsing reads.
_active_var: ContextVar[Optional["ChatModelBase"]] = ContextVar("jx_failover_active", default=None)


def mark_provider_down(provider_id: str) -> None:
    if provider_id:
        _cooldown[provider_id] = time.monotonic() + PROVIDER_COOLDOWN_SECONDS


def provider_is_cooling(provider_id: str) -> bool:
    until = _cooldown.get(provider_id)
    if until is None:
        return False
    if time.monotonic() >= until:
        _cooldown.pop(provider_id, None)
        return False
    return True


def clear_cooldowns() -> None:
    _cooldown.clear()


def provider_is_unusable(exc: BaseException) -> bool:
    """Whether *exc* blames the provider rather than the request we sent.

    Classified by exception type, never by message text, matching
    ``_is_retryable_stream_start_error``. 400 and 422 describe the request
    itself — every other endpoint would reject it identically, so they end the
    run instead of walking the chain. Everything else an endpoint can answer
    with (402 out of credit, 401 revoked key, 404 retired model, 429 saturated,
    5xx, transport failure) is a property of that endpoint alone.
    """
    import openai

    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        return False
    if isinstance(exc, (openai.APIStatusError, openai.APIConnectionError, openai.APITimeoutError)):
        return True
    return type(exc) is openai.APIError


def _block_type(block: Any) -> str:
    """The ``type`` of one content block, whichever shape it arrives in.

    Blocks reach this layer as pydantic models or as the plain dicts their
    TypedDict form produces; both are read the same way.
    """
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return str(getattr(block, "type", "") or "")


def _tool_result_holds_data(block: Any) -> bool:
    output = block.get("output") if isinstance(block, dict) else getattr(block, "output", None)
    if not isinstance(output, list):
        return False
    return any(_block_type(item) == "data" for item in output)


def carries_media(messages: list[Msg]) -> bool:
    """Whether *messages* hold media a candidate has to be able to read.

    Media rides in ``DataBlock``s — either directly on a message (an upload the
    middleware passed through) or folded into a ``ToolResultBlock``'s output
    (see ``core.llm.tool_result_media``). Both places are checked because both
    reach the wire.
    """
    for msg in messages or []:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            kind = _block_type(block)
            if kind == "data":
                return True
            if kind == "tool_result" and _tool_result_holds_data(block):
                return True
    return False


class FailoverChatModel(ToolCallIdentityMixin, ImageTokenCountingMixin, ChatModelBase):
    """Presents a candidate chain as one model, switching on an unusable provider.

    Subclasses ``ChatModelBase`` and overrides ``_call_api`` rather than
    wrapping ``__call__``: usage instrumentation installs itself on
    ``_call_api``, so one logical model call stays one recorded attempt however
    many endpoints it took to answer.

    ``ToolCallIdentityMixin`` has to be worn here as well: candidates are
    entered through ``candidate._call_api``, which walks past their own
    ``__call__`` and therefore past the mixin they carry. Whichever endpoint of
    the chain answers, its tool-call ids are repaired on the way out.

    ``ImageTokenCountingMixin`` for the same reason, and ``__getattr__`` cannot
    stand in for it: ``count_tokens`` resolves on ``ChatModelBase``, so without
    the mixin the facade silently bills image payloads as text.

    Fallback candidates are built on first use. A healthy primary — the normal
    case — never constructs the rest of the chain.
    """

    # ``ChatModelBase.__init__`` only assigns the six attributes below; this
    # facade derives them from whichever candidate is answering instead, so it
    # sets what it owns and leaves the rest to the properties.
    def __init__(
        self,
        primary: ChatModelBase,
        fallback_specs: list[Any],
        builder: Callable[[Any], ChatModelBase],
    ) -> None:
        self._primary = primary
        self._fallback_specs = list(fallback_specs)
        self._builder = builder
        self._built: dict[int, ChatModelBase] = {}
        self._listener: Any = None
        self.max_retries = 0

    @property
    def _active(self) -> ChatModelBase:
        return _active_var.get() or self._primary

    def _adopt(self, model: ChatModelBase) -> None:
        _active_var.set(model)

    # What downstream reads off a model is per-endpoint: the SSE layer branches
    # on structured_reasoning, replay keys off wire_protocol, and AS2 derives
    # its compaction threshold from context_size. Reading them off the active
    # candidate keeps the facade honest about who actually answered, without
    # one run's fallback leaking into another's.
    #
    # These deliberately narrow ``ChatModelBase``'s writeable attributes to
    # read-only: the facade owns no endpoint of its own, so assigning one would
    # silently disagree with whoever is answering. Nothing assigns them —
    # ``__init__`` above skips the base initialiser precisely for this — and a
    # future writer gets an AttributeError rather than a stale value.
    @property
    def model(self) -> str:  # type: ignore[override]
        return self._active.model

    @property
    def stream(self) -> bool:  # type: ignore[override]
        return self._active.stream

    @property
    def context_size(self) -> int:  # type: ignore[override]
        return self._active.context_size

    @property
    def credential(self) -> Any:  # type: ignore[override]
        return self._active.credential

    @property
    def parameters(self) -> Any:  # type: ignore[override]
        return self._active.parameters

    def __getattr__(self, name: str) -> Any:
        # Only consulted for attributes this facade does not define itself
        # (formatter, provider_id, wire_protocol, structured_reasoning, ...).
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._active, name)

    def set_context_rewrite_listener(self, listener: Any) -> None:
        self._listener = listener
        self._install_listener(self._primary)
        for model in self._built.values():
            self._install_listener(model)

    def _install_listener(self, model: ChatModelBase) -> None:
        setter = getattr(model, "set_context_rewrite_listener", None)
        if callable(setter) and self._listener is not None:
            setter(self._listener)

    def _candidate(self, index: int) -> Optional[ChatModelBase]:
        if index == 0:
            return self._primary
        if index in self._built:
            return self._built[index]
        try:
            model = self._builder(self._fallback_specs[index - 1])
        except Exception as exc:  # noqa: BLE001 - a candidate that cannot be built is skipped
            logger.warning("[failover] candidate %d could not be built: %s", index, exc)
            return None
        self._install_listener(model)
        self._built[index] = model
        return model

    def _order(self) -> list[int]:
        """Candidate indices, cooling providers moved to the back rather than dropped.

        A chain where every provider is cooling still has to attempt
        something: the cooldown is an optimisation, never a reason to fail a
        run an already-recovered endpoint could have answered.
        """
        indices = list(range(len(self._fallback_specs) + 1))
        live = [i for i in indices if not provider_is_cooling(self._pid_at(i))]
        return live + [i for i in indices if i not in live]

    def _pid_at(self, index: int) -> str:
        if index == 0:
            return str(getattr(self._primary, "provider_id", "") or "")
        return str(getattr(self._fallback_specs[index - 1], "provider_id", "") or "")

    def _reads_media(self, index: int) -> bool:
        """Whether candidate *index* may be sent a request carrying media.

        The primary needs no test: the turn's vision mode is resolved against
        it (``core.vision.resolve_vision_mode``), so a tool only ever returns
        pixels — and an upload only ever passes through untranscribed — when
        the primary reads them natively. Fallbacks were chosen by weight and
        context window, neither of which says anything about media, so each is
        asked the one question that decides it.
        """
        if index == 0:
            return True
        from core.vision import model_supports_vision

        return model_supports_vision(self._fallback_specs[index - 1])

    def _media_readers(self, order: list[int]) -> list[int]:
        """*order* without the candidates that cannot read media.

        Never empties the chain: the primary always reads media when media is
        present, so it survives the filter and stays the endpoint the run fails
        against — reporting why *it* could not answer rather than a text-only
        stand-in's complaint about a picture it was never meant to receive.
        """
        readers = [index for index in order if self._reads_media(index)]
        if len(readers) < len(order):
            logger.info(
                "[failover] request carries media; skipping %d text-only candidate(s)",
                len(order) - len(readers),
            )
        return readers

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        order = self._order()
        if carries_media(messages):
            order = self._media_readers(order)
        last_exc: Optional[BaseException] = None

        for position, index in enumerate(order):
            candidate = self._candidate(index)
            if candidate is None:
                continue
            is_last = position == len(order) - 1

            try:
                result = await candidate._call_api(
                    candidate.model, messages, tools, tool_choice, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 - classified below
                last_exc = exc
                if is_last or not provider_is_unusable(exc):
                    raise
                self._note_switch(candidate, exc)
                continue

            if not isinstance(result, AsyncGenerator):
                self._adopt(candidate)
                return result

            # A stream only proves the endpoint answered once a chunk arrives,
            # so the first one is pulled here — inside the window where
            # re-issuing against another provider is still sound.
            try:
                first = await result.__anext__()
            except StopAsyncIteration:
                self._adopt(candidate)
                return _replay([], result)
            except Exception as exc:  # noqa: BLE001 - classified below
                last_exc = exc
                if is_last or not provider_is_unusable(exc):
                    raise
                self._note_switch(candidate, exc)
                continue

            self._adopt(candidate)
            return _replay([first], result)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("failover chain produced no usable candidate")

    def _note_switch(self, failed: ChatModelBase, exc: BaseException) -> None:
        pid = str(getattr(failed, "provider_id", "") or "")
        mark_provider_down(pid)
        logger.warning(
            "[failover] provider %s (%s) unusable (%s); trying next candidate",
            pid,
            failed.model,
            type(exc).__name__,
        )


async def _replay(
    head: list[ChatResponse], rest: AsyncGenerator[ChatResponse, None]
) -> AsyncGenerator[ChatResponse, None]:
    for item in head:
        yield item
    async for item in rest:
        yield item


def with_failover(
    primary: ChatModelBase,
    resolved: Any,
    *,
    mode: Optional[str] = None,
    parameter_overrides: Optional[dict[str, Any]] = None,
):
    """Wrap *primary* in its failover chain, or return it unchanged when alone.

    ``resolved`` is the ``ResolvedModelConfig`` *primary* was built from; it is
    excluded from its own fallback list.
    """
    from core.llm.chat_models import build_model_for_mode
    from core.services.model_config import ModelConfigService

    try:
        chain = ModelConfigService.get_instance().resolve_failover_chain(resolved)
    except Exception as exc:  # noqa: BLE001 - failover is best-effort, never fatal
        logger.warning("[failover] chain resolve failed: %s", exc)
        return primary

    fallbacks = [c for c in chain if not resolved or c.provider_id != resolved.provider_id]
    if parameter_overrides:
        from dataclasses import replace

        fallbacks = [replace(cfg, **parameter_overrides) for cfg in fallbacks]
    if not fallbacks:
        return primary
    return FailoverChatModel(
        primary,
        fallbacks,
        lambda cfg: build_model_for_mode(cfg, mode=mode, stream=primary.stream),
    )
