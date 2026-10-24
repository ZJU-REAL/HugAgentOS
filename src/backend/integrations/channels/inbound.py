"""Channel inbound responsibilities."""
from __future__ import annotations
from typing import Optional
import core.chat.runtime.admission as admission
from integrations.channels.protocol import InboundMsg
from integrations.channels.registry import get_adapter
from core.db.engine import SessionLocal
from integrations.channels.models import ChannelConnection
from integrations.channels.repository import ChannelConnectionRepository
import logging
from core.db.threads import run_db, run_in_session
logger = logging.getLogger(__name__)
from integrations.channels import inbound_attachments
from integrations.channels import inbound_delivery
from integrations.channels import inbound_history
from integrations.channels import inbound_observation
from integrations.channels import inbound_sessions
from integrations.channels import inbound_state

def module_route(db, conn: ChannelConnection):
    """The admit hook of the first enabled module that claims this bot, if any."""
    from core.harness.registry import channel_routes

    for handles, admit in channel_routes():
        if handles(db, conn):
            return admit
    return None


async def handle_inbound(msg: InboundMsg) -> None:
    """Main entry point for inbound messages. Scheduled on the main event loop (long-connection threads submit via run_coroutine_threadsafe).

    All exceptions are swallowed and logged — one message's failure must not take down the long connection / process.
    """
    from integrations.channels.relay_dispatch import dispatch_to_desktop
    if await dispatch_to_desktop(msg, SessionLocal):
        return
    if inbound_sessions.already_handled(f"{msg.channel_id}:{msg.message_id}" if msg.message_id else ""):
        return
    if not (msg.text or "").strip() and not msg.attachments:
        return
    # #1 Per-conversation serialization: until an in-progress run finishes, later messages in the same conversation queue up, avoiding history races/corruption.
    async with inbound_state.conv_locks[inbound_sessions.conv_key(msg)]:
        await process_inbound(msg)


def _inbound_target(db, channel_id: str) -> Optional[ChannelConnection]:
    conn = ChannelConnectionRepository(db).get_by_id(channel_id)
    if conn is None or not conn.enabled:
        return None
    _ = conn.config  # force-load the credential column so it remains usable after detaching
    db.expunge(conn)
    return conn


def _observe(db, channel_id: str, msg: InboundMsg) -> None:
    repo = ChannelConnectionRepository(db)
    conn = repo.get_by_id(channel_id)
    inbound_observation.observe_message(db, conn, msg)
    repo.touch_event(conn.channel_id)  # bystander traffic still proves the connection is alive


def _reset(db, channel_id: str, msg: InboundMsg) -> None:
    conn = ChannelConnectionRepository(db).get_by_id(channel_id)
    inbound_sessions.reset_session(db, conn, msg)


def _session_for(db, channel_id: str, msg: InboundMsg) -> str:
    conn = ChannelConnectionRepository(db).get_by_id(channel_id)
    return inbound_sessions.find_or_create_session(db, conn, msg).chat_id


def _drain_observed(db, chat_id: str):
    from core.db.models import ChatSession

    return inbound_observation.drain_observed(db, db.get(ChatSession, chat_id))


def _admit_run(db, msg: InboundMsg, chat_id: str, user_text: str, uploaded_files):
    """Build the run context and accept the run; returns everything the launch needs, detached."""
    from core.chat.context import build_runtime_context, collect_historical_attachments
    from core.chat.sequencer import ChatSequencer
    from core.ontology.runtime_policy import build_user_ontology_runtime

    repo = ChannelConnectionRepository(db)
    conn = repo.get_by_id(msg.channel_id)
    owner_id = conn.owner_user_id
    enabled = inbound_sessions.resolve_enabled(db, conn, owner_id)
    # Enable thinking: same as the web client's default in non-fast mode. With thinking off,
    # models (Qwen family especially) tend to emit shallow filler like "I'll get right on X"
    # without actually landing on tool calls, idling repeatedly across turns. Thinking never
    # leaks into the channel reply: structured-reasoning models emit separate thinking events
    # (ignored by _collect_reply, which only accumulates content/meta), and inline-thinking
    # models' <think>…</think> spans inside content are stripped by _collect_reply.
    context = build_runtime_context(
        model_name=None, user_id=owner_id, chat_id=chat_id, enable_thinking=True,
        uploaded_files=uploaded_files,
        enabled_skills=enabled["enabled_skills"], enabled_agents=enabled["enabled_agents"],
        enabled_mcps=enabled["enabled_mcps"], enabled_kbs=enabled["enabled_kbs"],
    )
    ontology_enabled, ontology_runtime = build_user_ontology_runtime(
        user_id=owner_id,
        task=user_text,
        db=db,
    )
    context["ontology_enabled"] = ontology_enabled
    context["ontology_runtime"] = ontology_runtime
    # Cross-turn file readability: inject files uploaded/generated earlier in this session
    # (including last turn's Feishu attachments) as a summary block so the model gets real
    # file_ids to call read_artifact with. Exclude this turn's attachments (already injected
    # in full via uploaded_files) to avoid duplication. build_runtime_context doesn't include
    # this field, so we add it here (aligned with the web client's historical_files injection
    # in chats.py).
    current_file_ids = {f.get("file_id") for f in uploaded_files if f.get("file_id")}
    context["historical_files"] = collect_historical_attachments(
        chat_id, owner_id, exclude_file_ids=current_file_ids,
    )
    # Bind to a specific sub-agent: pin the whole run to that sub-agent (running with its own
    # prompt/tools/model/knowledge bases), via the workflow's direct sub-agent mode. NULL →
    # unset, run with the owner's default capabilities (main agent).
    # Note: the sub-agent carries its own capability bindings, which override the whitelist
    # narrowing from _resolve_enabled above — this is intended behavior.
    direct_agent_id = conn.agent_id or (conn.config or {}).get("desktop_relay_agent_id")
    if direct_agent_id:
        context["agent_id"] = direct_agent_id
    # #7 Trigger A: let the agent self-create scheduled delivery tasks within this conversation
    context["channel_origin"] = {
        "channel_id": conn.channel_id,
        "conversation_id": msg.external_conversation_id,
        "chat_type": msg.chat_type,
        # channel_type lets the agent be told *which* platform it is in, so it can reach
        # for that platform's CLI to pull group history the bot never received (anything
        # from before it joined). Delivery targets never needed it; the prompt does.
        "channel_type": conn.channel_type,
    }
    accepted = ChatSequencer(db).accept_main_run(
        chat_id=chat_id,
        user_id=owner_id,
        user_content=user_text,
        user_extra_data={
            "channel_sender_open_id": msg.sender_id,
            "channel_sender_name": msg.sender_name,
            "channel_message_id": msg.message_id,
            "channel_attachments": [f.get("file_id") for f in uploaded_files],
            "attachments": [
                {"file_id": f.get("file_id"), "name": f.get("name")} for f in uploaded_files
            ],
        },
        request_payload={
            "channel_id": msg.channel_id,
            "source": "desktop_channel" if (conn.config or {}).get("desktop_relay") else "channel",
            "kind": "chat",
        },
    )
    run_id = accepted.run.run_id
    try:
        session_messages = inbound_sessions.load_history(db, chat_id, owner_id)
        repo.touch_event(conn.channel_id)
        # The commits above expire the rows' columns; refresh before detaching so the
        # launch reads them without a session.
        db.refresh(accepted.run)
        db.expunge(accepted.run)
    except Exception:
        db.rollback()
        ChatSequencer(db).abandon_pending_run(run_id, reason="channel preparation failed")
        raise
    return accepted.run, context, session_messages


def _abandon(db, run_id: str, reason: str) -> None:
    from core.chat.sequencer import ChatSequencer

    ChatSequencer(db).abandon_pending_run(run_id, reason=reason)


async def process_inbound(msg: InboundMsg) -> None:
    placeholder_id: Optional[str] = None
    try:
        conn = await run_in_session(_inbound_target, msg.channel_id)
        if conn is None:
            logger.warning("[channels] inbound 无对应启用连接 channel_id=%s", msg.channel_id)
            return

        owner_id = conn.owner_user_id
        adapter = get_adapter(conn.channel_type)

        # Group listening gate. Only reached at all when the platform actually delivers non-@
        # group messages (needs the channel app's "read all group messages" permission);
        # otherwise every inbound group message is an @ and this is a no-op.
        if msg.chat_type == "group" and not await inbound_observation.resolve_addressed(adapter, conn, msg):
            if conn.group_listen_mode == "observe_all":
                await run_in_session(_observe, conn.channel_id, msg)
                logger.info(
                    "[channels] 旁听群消息 channel_id=%s type=%s conv=%s",
                    conn.channel_id, conn.channel_type, msg.external_conversation_id,
                )
            else:
                # mention_only → drop entirely, exactly as before this feature existed.
                # Logged because reaching this line at all is the *only* evidence that the
                # platform is delivering non-@ group messages (i.e. that the "read all group
                # messages" permission is actually granted). Without it, "the bot ignores the
                # group" and "the platform never sends the group" look identical from outside.
                # DEBUG, not INFO: an active group would otherwise flood the log.
                logger.debug(
                    "[channels] 丢弃未 @ 的群消息（旁听未开启）channel_id=%s type=%s",
                    conn.channel_id, conn.channel_type,
                )
            # Either way: no reset-command handling, no run, no placeholder, no reply.
            return

        admit = await run_in_session(module_route, conn)
        if admit is not None:
            await admit(conn, msg, adapter)
            return

        # Channel-side "new conversation / clear context": soft-deleting the current session is
        # enough (the next message automatically creates an empty session); no agent run. This is
        # where /new and clear-context actually take effect in Feishu and other clients —
        # otherwise the command would be fed to the agent as plain text and the history would keep
        # being reused, impossible to clear.
        if inbound_sessions.is_reset_command(msg.text):
            await run_in_session(_reset, conn.channel_id, msg)
            try:
                await adapter.send_text(conn, msg, "✅ 已开启新对话，之前的上下文已清除。")
            except Exception:  # noqa: BLE001
                logger.debug("[channels] 清空回执发送失败", exc_info=True)
            return

        chat_id = await run_in_session(_session_for, conn.channel_id, msg)

        # #4 Placeholder first. Everything below this line does real network work —
        # downloading attachments, pulling group history — and the placeholder exists
        # precisely so the user is not staring at nothing while that happens. It used to be
        # sent after the whole prep stage, which put ~1s of history pull ahead of any visible
        # feedback and read as the bot being slow to react.
        # Prefer the channel's send_placeholder (DingTalk: robot API, recallable) — only that
        # path yields a message_id supporting the later edit/recall-replace; otherwise plain
        # send_text.
        try:
            send_ph = getattr(adapter, "send_placeholder", None) or adapter.send_text
            ph = await send_ph(conn, msg, "🤔 正在处理，请稍候…")
            if ph.success:
                placeholder_id = ph.message_id
        except Exception:  # noqa: BLE001
            logger.debug("[channels] 占位消息发送失败", exc_info=True)

        # Inbound attachments: download → store as Artifact → uploaded_files
        uploaded_files = await inbound_attachments.ingest_attachments(adapter, conn, owner_id, chat_id, msg)
        # File-only message with no text → synthesize a one-line user prompt
        if (msg.text or "").strip():
            user_text = msg.text
        elif uploaded_files:
            user_text = "[收到文件] " + "、".join(f.get("name", "") for f in uploaded_files)
        else:
            return
        # #5 Label the speaker in group scenarios so the agent can tell multi-person conversations apart
        if msg.chat_type == "group":
            user_text = f"{inbound_observation.speaker_label(msg)}：{user_text}"
            # Deterministic incremental pull, before draining: whatever the platform pushed
            # (possibly nothing) plus whatever we can pull ends up in the same buffer, and the
            # drain below sees both. Failure is absorbed inside — it must not block the reply.
            await inbound_history.pull_history(adapter, conn, msg, chat_id)
            # observe_all: prepend everything the group said since the last @ as background.
            # Prepending into user_text (rather than a separate context field) means it is
            # persisted with the turn, so later turns still see the group log through normal
            # history loading and compaction — no second retention mechanism to maintain.
            observed = await run_in_session(_drain_observed, chat_id)
            if observed:
                user_text = inbound_observation.render_observed_block(observed) + user_text

        accepted_run, context, session_messages = await run_in_session(
            _admit_run, msg, chat_id, user_text, uploaded_files
        )
    except Exception:
        logger.exception("[channels] inbound 准备阶段失败 channel_id=%s", msg.channel_id)
        from integrations.channels.registry import current_relay_adapter
        if current_relay_adapter():
            current_relay_adapter().error = "本机机器人准备失败"
        return

    # (The placeholder is sent inside the prep stage above, before attachment download and
    # history pull — those are the slow parts it needs to cover.)

    # §13 Channel adaptation: IM clients like Feishu have no approval UI for MySpace write
    # operations. Without pre-authorization, the gate would suspend on a MySpace write waiting
    # for out-of-band user confirmation (_collect_reply only recognizes content/meta;
    # file_confirm is dropped) → stuck until the 2h timeout with the placeholder never updated.
    # The bot runs as the owner, and in private chat the message sender is the owner themselves,
    # so pre-mark this session as allowed — equivalent to the owner approving their own write.
    # (Sub-agents remain non-interactive; their /myspace writes are still rejected.)
    try:
        from core.llm.tools import confirmation as _mc
        if not (conn.config or {}).get("desktop_relay"):
            _mc.allow_session(chat_id)
    except Exception:  # noqa: BLE001 — a pre-authorization failure must not take down the whole run
        logger.debug("[channels] myspace 写预授权失败 chat_id=%s", chat_id, exc_info=True)

    try:
        run = await admission.start_run(
            accepted_run=accepted_run,
            chat_id=chat_id,
            user_id=owner_id,
            session_messages=session_messages,
            effective_user_message=user_text,
            raw_user_message=user_text,
            context=context,
            model_name=None,
        )
    except Exception as exc:
        await run_in_session(_abandon, accepted_run.run_id, str(exc))
        logger.exception("[channels] inbound run 启动失败 channel_id=%s", msg.channel_id)
        from integrations.channels.registry import current_relay_adapter
        if current_relay_adapter():
            current_relay_adapter().error = "本机机器人启动失败"
        try:
            await inbound_delivery.replace_placeholder(
                adapter, conn, msg, placeholder_id, "⚠️ 处理出错了，请稍后重试。"
            )
        except Exception:  # noqa: BLE001
            logger.debug("[channels] 失败消息回推异常", exc_info=True)
        return

    try:
        from integrations.channels.registry import current_relay_adapter
        relay_adapter = current_relay_adapter()
        if relay_adapter:
            relay_adapter.record_run(run.run_id, owner_id)
        reply, gen_artifacts = await inbound_delivery.collect_reply(run.run_id)
    except Exception as exc:
        logger.exception("[channels] inbound run 失败 channel_id=%s", msg.channel_id)
        if relay_adapter:
            relay_adapter.error = "本机机器人执行失败"
        notice = (
            str(exc).strip()
            if isinstance(exc, inbound_delivery.ChannelRunError) and str(exc).strip()
            else "处理出错了，请稍后重试。"
        )
        try:
            await inbound_delivery.replace_placeholder(adapter, conn, msg, placeholder_id, f"⚠️ {notice}")
        except Exception:  # noqa: BLE001
            logger.debug("[channels] 失败消息回推异常", exc_info=True)
        return

    # Note: the assistant reply is **not persisted here**. start_run's background executor
    # (chat_run_executor) already persists the assistant message together with
    # usage/model/tool_calls/artifacts when the stream ends. Saving another one here would create
    # a **duplicate assistant message**, and one without usage (tokens recorded as 0) — exactly
    # the root cause of "input/output tokens are both 0" in channel sessions. We only use the
    # reply text to push back to the channel, without persisting again.

    # Push back: text (placeholder edit + follow-up chunks) + generated files
    try:
        if reply:
            await inbound_delivery.deliver_reply(adapter, conn, msg, reply, placeholder_id)
        else:
            note = "已为你生成文件。" if gen_artifacts else "（本次无文本回复）"
            await inbound_delivery.replace_placeholder(adapter, conn, msg, placeholder_id, note)
        if getattr(adapter, "push_file", None):
            for content, name, mime in await run_db(
                inbound_attachments.load_generated_files, gen_artifacts
            ):
                fr = await adapter.push_file(conn, msg, content, name, mime)
                if not fr.success:
                    logger.warning("[channels] 文件回传失败 name=%s kind=%s", name, fr.error_kind)
    except Exception:
        logger.exception("[channels] inbound 回推阶段失败 channel_id=%s", msg.channel_id)
        if relay_adapter:
            relay_adapter.error = "本机机器人回复回传失败"
