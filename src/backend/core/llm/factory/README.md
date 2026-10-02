# Agent factory

Public entry: `from core.llm.factory import create_agent_executor`.
`warmup_mcp_tools` is the startup entry for shared MCP discovery.
The initializer loads these entries lazily so importing selection helpers does
not initialize the model/tool stack. No legacy `agent_factory` module remains.

## Ownership

| Location | Responsibility | Extend here when |
| --- | --- | --- |
| `build.py`, `request.py` | Preserve the caller contract and construct a typed request | An existing caller needs an assembly option |
| `assembly.py` | Order the phases and pass explicit results | The execution lifecycle gains a phase |
| `selection/` | Resolve allowed, required, bound and inherited capabilities | A capability source or selection policy changes |
| `tools/` | Connect MCP clients and register the selected tools and skills | A tool transport or registration path changes |
| `prompts/` | Compose system policy and per-run context | Prompt/context composition changes |
| `runtime/` | Select the model and budgets; attach middleware, manifests and listeners | Agent execution policy changes |
| `models.py`, `defaults.py` | Assembly-owned value types and defaults | Multiple phases share a domain value |

The factory consumes `core.plugins.runtime`; it does not own plugin lifecycle
or manifest ingestion. Plugin behavior belongs in `core/plugins/`.
The orchestration layer calls the public entry; it should not coordinate the
private phases itself. Keep phase inputs explicit rather than introducing a
mutable catch-all context or arbitrary pipeline registry.
