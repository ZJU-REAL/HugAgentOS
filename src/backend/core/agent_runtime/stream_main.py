"""Translate main-agent model and tool output into chat events."""

from __future__ import annotations
from core.llm.citations import allocator

import json
import logging

import core.agent_runtime.events as events
import core.agent_runtime.integrations.ontology_evidence as ontology_evidence
import core.agent_runtime.skills as skills
import core.chat.plan_progress as plan_progress
import core.chat.tool_payloads as tool_payloads
import core.config.display_names as display_names
import core.llm.tools.plan_update as plan_update_tool
from core.agent_runtime.main_state import MainTurn

logger = logging.getLogger(__name__)


async def stream_main(turn: MainTurn):
    try:
        # Project initialization is a server-expanded command. The DB row
        # deliberately remains /init; only the live model input is expanded.
        stream_options = (
            {"effective_user_message": turn.user_message}
            if turn.context.get("project_init")
            else {}
        )
        turn.main_stream = turn.streaming_agent.stream(
            turn.session_messages, turn.context, **stream_options
        )
        async for event_type, payload in turn.main_stream:
            state_runtime = getattr(turn.streaming_agent.agent.state, "ontology_runtime", None)
            if isinstance(state_runtime, dict):
                turn.ontology_runtime = state_runtime
                turn.context["ontology_runtime"] = state_runtime
            pending_ontology_events = turn.ontology_runtime.get("runtime_events", [])[
                turn.ontology_event_cursor :
            ]
            for ontology_event in pending_ontology_events:
                yield dict(ontology_event)
            turn.ontology_event_cursor += len(pending_ontology_events)
            if event_type == "text_delta":
                turn.full_response += payload
                yield {"type": "content", "event": "ai_message", "delta": payload}

            elif event_type == "vision_progress":
                # 识图是模型开口前的一段纯网络等待；透传给前端，让轮级状态
                # 从「深度拥抱中」换成「图像理解中」，而不是干等一个笼统的计时器。
                yield {"type": "vision_progress", **(payload or {})}

            elif event_type == "reasoning_protocol":
                yield {"type": "thinking", **payload}

            elif event_type == "steer_applied":
                yield {"type": "steer_applied", **(payload or {})}

            elif event_type == "thinking_delta":
                yield {"type": "thinking", "delta": payload}

            elif event_type == "context_usage":
                yield {"type": "context_usage", **(payload or {})}

            elif event_type == "model_step":
                yield {"type": "model_step", "step": payload}

            elif event_type == "model_call_start":
                yield {"type": "model_dispatch"}

            elif event_type == "tool_call_start":
                tool_name = payload.get("name", "unknown")
                if tool_name != "update_plan":
                    yield {
                        "type": "tool_call_start",
                        "tool_name": tool_name,
                        "tool_display_name": display_names.TOOL_DISPLAY_NAMES.get(
                            tool_name, tool_name
                        ),
                        "tool_id": payload.get("id", ""),
                    }

            elif event_type == "tool_call_delta":
                tool_name = payload.get("name", "unknown")
                if tool_name != "update_plan" and payload.get("delta"):
                    yield {
                        "type": "tool_call_delta",
                        "tool_name": tool_name,
                        "tool_id": payload.get("id", ""),
                        "arguments_delta": payload.get("delta", ""),
                    }

            elif event_type == "tool_call":
                tool_name = payload.get("name", "unknown")
                tool_id = payload.get("id", "")
                tool_args = payload.get("args", {})

                # Stage the full plan until its real result is saved.
                # A tool request alone must not announce completed work.
                if tool_name == "update_plan":
                    _pu = plan_update_tool.parse_plan_update_args(tool_args)
                    if _pu:
                        turn.pending_plan_updates[tool_id] = _pu
                    continue

                # In streaming mode, the first chunk for a tool_call may
                # arrive with empty args.  For view_text_file we need args
                # to decide if this is a skill load, so skip empty-arg
                # duplicates until we get the complete args.
                _is_fast_emit = tool_name in tool_payloads._FAST_EMIT_TOOLS
                if tool_id and tool_id in turn.displayed_tools:
                    # Fast-emit tools: re-emit when args arrive so the
                    # frontend can update input display immediately.
                    if _is_fast_emit and tool_args:
                        pass  # fall through to emit update
                    else:
                        continue
                if not tool_payloads._tool_args_ready(tool_name, tool_args):
                    continue
                if tool_id:
                    turn.displayed_tools.add(tool_id)

                # Detect skill loading: view_text_file reading a SKILL.md
                is_skill_load = (
                    tool_name == "view_text_file"
                    and isinstance(tool_args, dict)
                    and "SKILL.md" in str(tool_args.get("file_path", ""))
                )
                if is_skill_load and tool_id:
                    turn.skill_load_ids.add(tool_id)
                    _sid = skills._extract_skill_id_from_path(str(tool_args.get("file_path", "")))
                    if _sid:
                        turn.skill_id_by_tool_id[tool_id] = _sid
                if (
                    tool_name in ("view_text_file", "Read")
                    and not is_skill_load
                    and tool_id
                    and isinstance(tool_args, dict)
                ):
                    turn.view_text_file_args[tool_id] = tool_args
                emit_name = "load_skill" if is_skill_load else tool_name
                display_name = (
                    "加载技能"
                    if is_skill_load
                    else display_names.TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                )
                safe_args = tool_args if isinstance(tool_args, dict) else {}
                turn.ontology_trace.append(
                    {
                        "type": "tool_call",
                        "tool_id": tool_id,
                        "tool_name": tool_name,
                        "input": ontology_evidence._bounded_trace_value(safe_args),
                    }
                )

                # Resolve sub-agent name for call_subagent tool card
                _tc_sa_name = ""
                if tool_name == "call_subagent" and turn.visible_subagents:
                    _tc_sa_id = safe_args.get("agent_id", "") if safe_args else ""
                    if _tc_sa_id:
                        for _sa in turn.visible_subagents:
                            if _sa.get("agent_id") == _tc_sa_id:
                                _tc_sa_name = _sa.get("name", "")
                                break
                    if not _tc_sa_name and turn.mentioned_ids:
                        for _sa in turn.visible_subagents:
                            if _sa.get("agent_id") in turn.mentioned_ids:
                                _tc_sa_name = _sa.get("name", "")
                                break

                yield {
                    "type": "tool_call",
                    "tool_name": emit_name,
                    "tool_display_name": display_name,
                    "tool_args": safe_args,
                    "input": safe_args,
                    "tool_id": tool_id,
                    **({"subagent_name": _tc_sa_name} if _tc_sa_name else {}),
                }

            elif event_type == "tool_progress":
                yield {"type": "tool_progress", **payload}

            elif event_type == "tool_result":
                tool_name = payload.get("name", "unknown")
                tool_id = payload.get("id", "")
                # The canonical model_step was already forwarded and
                # persisted independently. Keep update_plan in the plan
                # bar; only its redundant display card is suppressed.
                if tool_name == "update_plan":
                    _pu = turn.pending_plan_updates.pop(tool_id, None)
                    if _pu and payload.get("status") == "success":
                        turn.last_plan = _pu
                        plan_progress.save_plan_progress(
                            str(turn.context.get("chat_id") or ""), _pu
                        )
                        yield {"type": "plan_update", **_pu}
                    continue
                # Also override tool_name for skill load results
                is_skill_result = (tool_id and tool_id in turn.skill_load_ids) or (
                    tool_name == "view_text_file" and "SKILL.md" in str(payload.get("content", ""))
                )
                if is_skill_result:
                    tool_name = "load_skill"

                _missing_call = events._synthesize_missing_tool_call(
                    tool_id, tool_name, turn.displayed_tools
                )
                if _missing_call is not None:
                    turn.ontology_trace.append(
                        {
                            "type": "tool_call",
                            "tool_id": tool_id,
                            "tool_name": tool_name,
                            "input": {},
                        }
                    )
                    yield _missing_call

                tool_content = payload.get("content", "")

                # Parse tool result
                try:
                    tool_result_json = json.loads(tool_content) if tool_content else {}
                except json.JSONDecodeError:
                    tool_result_json = {"result": tool_content}

                # Skill load: replace the full SKILL.md text with the same curated detail used by the capability center
                if is_skill_result:
                    _sid = turn.skill_id_by_tool_id.get(
                        tool_id, ""
                    ) or skills._extract_skill_id_from_path(str(tool_content))
                    tool_result_json = tool_payloads._build_skill_load_payload(_sid)
                elif tool_name == "view_text_file":
                    # Plain file read (AgentScope built-in view_text_file): replace with file metadata + short preview
                    tool_result_json = tool_payloads._build_view_text_file_payload(
                        turn.view_text_file_args.get(tool_id, {}), tool_content
                    )
                elif tool_name == "Read":
                    # Claude-Code-style Read tool: JSON payload, content holds the whole file
                    tool_result_json = tool_payloads._build_read_tool_payload(
                        turn.view_text_file_args.get(tool_id, {}), tool_result_json
                    )
                elif tool_name == "read_artifact":
                    tool_result_json = tool_payloads._build_read_artifact_payload(tool_result_json)

                # Extract query if present
                extracted_query = ""
                if isinstance(tool_result_json, dict) and "result" in tool_result_json:
                    result_data = tool_result_json["result"]
                    if isinstance(result_data, dict):
                        extracted_query = result_data.get("query", result_data.get("question", ""))

                # Citations
                cit_dicts = allocator.collect_citation_dicts(tool_id, turn.anchor_allocator)
                turn.all_citations.extend(cit_dicts)
                turn.ontology_trace.append(
                    {
                        "type": "tool_result",
                        "tool_id": tool_id,
                        "tool_name": tool_name,
                        "result": ontology_evidence._bounded_trace_value(tool_result_json),
                        "citations": cit_dicts,
                    }
                )

                # Resolve sub-agent name from call_subagent result text
                _tr_sa_name = ""
                if tool_name == "call_subagent":
                    _res_str = (
                        str(tool_result_json.get("result", ""))
                        if isinstance(tool_result_json, dict)
                        else str(tool_result_json)
                    )
                    if "【" in _res_str and "】" in _res_str:
                        _tr_sa_name = _res_str.split("【", 1)[1].split("】", 1)[0]

                yield {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "tool_args": {"query": extracted_query} if extracted_query else {},
                    "result": tool_result_json,
                    "tool_id": tool_id,
                    "citations": cit_dicts,
                    "status": payload.get("status", "success"),
                    "model_step": payload.get("model_step"),
                    **({"subagent_name": _tr_sa_name} if _tr_sa_name else {}),
                }

            elif event_type in ("heartbeat", "model_progress"):
                # heartbeat = transport keep-alive; model_progress = the
                # model is still streaming while a small argument batch or
                # another suppressed event has nothing renderable yet —
                # forwarded so the run watchdog counts activity (it
                # excludes only heartbeat).
                yield {"type": event_type}

            elif event_type == "tool_pending":
                yield {"type": "tool_pending", **(payload or {})}

            elif event_type == "subagent_event":
                # Bypass channel for the sub-agent's internal
                # thinking/tool_call/tool_result/content — attached under
                # the call_subagent tool card that launched it (linked via
                # parent_tool_id).
                if (payload or {}).get("sub_type") == "progress":
                    # Sub-agent liveness (long tool-call-arg generation with
                    # nothing renderable yet) — feed the run inactivity
                    # watchdog without writing a sub-step to the stream or
                    # the persisted tool log.
                    yield {"type": "model_progress"}
                else:
                    nested_citations = ontology_evidence._capture_nested_ontology_evidence(
                        payload or {},
                        turn.ontology_trace,
                        turn.all_citations,
                        turn.anchor_allocator,
                    )
                    yield {
                        "type": "subagent_event",
                        **(payload or {}),
                        **({"citations": nested_citations} if nested_citations else {}),
                    }

            elif event_type in (
                "file_confirm",
                "design_pick",
                "user_question",
                "user_question_resolved",
            ):
                # Human-interaction events (§13 MySpace write confirm /
                # site-design pick-one-of-three / ask_user_question): a tool
                # coroutine has suspended waiting for the user's out-of-band
                # action. Pass through to the frontend to show the card; the
                # agent task stays blocked in that tool and this SSE stream
                # does not end — after the out-of-band answer POST the tool
                # resumes in place.
                yield {"type": event_type, **(payload or {})}

            elif event_type == "error":
                # payload may be a real exception object (kind=="err") or a
                # dict (e.g. ExceedMaxIters mapped to {"kind":..,"name":..}).
                # Raising the latter directly gives a TypeError, masking
                # the real situation — wrap it in a RuntimeError.
                if isinstance(payload, BaseException):
                    raise payload
                raise RuntimeError(str(payload))

    finally:
        if turn.main_stream is not None:
            await turn.main_stream.aclose()
