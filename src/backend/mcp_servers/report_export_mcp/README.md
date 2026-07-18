# report_export_mcp

stdio MCP server exposing `export_report_to_docx`.

Run:

```bash
python -m mcp_servers.report_export_mcp.server
```

Selftest:

```bash
python -m mcp_servers.report_export_mcp._selftest
```

Notes:
- Requires `python-docx` for real docx generation.
- If dependency is missing, tool returns `{"ok": false, "error": "..."}`.

