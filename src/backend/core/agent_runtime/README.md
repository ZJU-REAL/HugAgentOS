# Normal agent execution

[中文](README.zh-CN.md)

The public entry is runner.astream_chat_workflow. It selects the main or direct
agent route and closes the full generator chain when a consumer exits.

- runner / direct: lifecycle, output review, final metadata and post-turn work.
- prepare_main / prepare_direct: build the agent and assemble its input.
- stream_main / stream_direct: adapt model/tool events into the existing event contract.
- main_state / direct_state: typed state owned by one turn, never shared across runs.
- targets, skills, budget, events: routing inputs and focused runtime helpers.
- integrations: memory, compaction, ontology evidence/repair and evolution integration.
- complete: the existing non-streaming entry and result shape.

Durable ownership and database transactions are outside these phases.
Chat workers in core.chat.runtime consume events and persist them under core.runs
lease fencing. Plan and autonomous Loop composition belong to orchestration.
