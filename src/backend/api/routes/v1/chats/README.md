# Chat routes

`__init__.py` composes the existing public router in its original order: static
collection paths precede session-id paths. `sessions`, `history`, `reruns`,
`run_views`, `sidebar`, `questions`, `confirms` and `feedback` handle HTTP.
`pending_access`, `agent_targets`, `invocation`, `request_context`,
`session_context`, `rerun_metadata` and `models` supply shared route operations.
Long-running execution and SSE lifecycle remain owned by `core/chat/`.
Add routes to the appropriate module and register the router here only when
introducing a new route group. Public HTTP paths and operation IDs stay stable.
