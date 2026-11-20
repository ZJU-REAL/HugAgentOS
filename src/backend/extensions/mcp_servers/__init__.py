"""MCP servers package.

Bundled Model Context Protocol servers. ``_launcher`` starts each one as its own
long-running streamable-http process (ports in ``registry``). They are kept
separate from the FastAPI app so importing/starting HugAgentOS won't implicitly
start or require MCP dependencies.
"""
