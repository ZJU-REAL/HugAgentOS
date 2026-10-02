"""Plugin system tests: import Claude Code / Codex / native plugin packages → persist to DB → uninstall.

Covers the three-tier portability matrix: direct skill import (including
references + path-variable rewriting), direct remote MCP import, stdio MCP
disabled on install, and drop warnings for hooks/commands/agents.
See internal design docs §10.
"""

import json

from pathlib import Path


OWNER = "test_user_123"


def _make_cc_plugin(root: Path) -> Path:
    pdir = root / "hello-toolkit"
    (pdir / ".claude-plugin").mkdir(parents=True)
    (pdir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "hello-toolkit",
                "version": "2.1.0",
                "description": "A Claude Code demo plugin",
                "userConfig": {
                    "api_token": {
                        "type": "string",
                        "title": "API Token",
                        "sensitive": True,
                        "required": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # Skill 1: with references + scripts + path variables
    sk = pdir / "skills" / "hello-greeter"
    (sk / "references").mkdir(parents=True)
    (sk / "scripts").mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: hello-greeter\ndescription: Greets the user warmly when they say hello\n---\n\n"
        "Run ${CLAUDE_PLUGIN_ROOT}/scripts/greet.py to greet.\n"
        "See references/style.md for tone.\n",
        encoding="utf-8",
    )
    (sk / "references" / "style.md").write_text(
        "Be warm. Path: ${CLAUDE_PLUGIN_ROOT}/data\n", encoding="utf-8"
    )
    (sk / "scripts" / "greet.py").write_text("print('hello')\n", encoding="utf-8")

    # Skill 2: plain text
    sk2 = pdir / "skills" / "farewell"
    sk2.mkdir(parents=True)
    (sk2 / "SKILL.md").write_text(
        "---\nname: farewell\ndescription: Says goodbye when the conversation ends\n---\n\nSay goodbye.\n",
        encoding="utf-8",
    )

    # MCP: one remote http + one stdio
    (pdir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "weather-remote": {
                        "url": "https://mcp.example.com/mcp",
                        "headers": {"X-Key": "${WEATHER_KEY}"},
                    },
                    "local-fs": {
                        "command": "npx",
                        "args": ["-y", "@x/fs-mcp", "${CLAUDE_PLUGIN_ROOT}/data"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    # Tier3: components that should be dropped
    (pdir / "hooks").mkdir()
    (pdir / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8"
    )
    (pdir / "commands").mkdir()
    (pdir / "commands" / "deploy.md").write_text(
        "---\ndescription: deploy\n---\nDeploy $ARGUMENTS\n", encoding="utf-8"
    )
    (pdir / "agents").mkdir()
    (pdir / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: x\n---\nReview.\n", encoding="utf-8"
    )
    return pdir


def _zip_cc_plugin(tmp_path) -> bytes:
    import io
    import zipfile

    pdir = _make_cc_plugin(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(pdir.rglob("*")):
            if p.is_file():
                zf.write(
                    p, p.relative_to(pdir.parent).as_posix()
                )  # extra top-level directory wrapper, tests _locate_plugin_root
    return buf.getvalue()


def _make_standard_plugin(root: Path) -> Path:
    """Build an Agent Plugins standard package: closed-schema plugin.json +
    extensions["org.hugagent"] + standalone mcp.json with type discriminators."""
    pdir = root / "std-toolkit"
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "std-toolkit",
                "version": "1.2.3",
                "description": "An Agent Plugins standard demo package",
                "author": {"name": "Acme", "url": "https://acme.example"},
                "keywords": ["demo"],
                "extensions": {
                    "org.hugagent": {
                        "connection": "lark",
                        "required_secrets": [{"key": "api_key", "label": "API Key"}],
                        "admin_config": {
                            "mode": "any",
                            "fields": [{"key": "std.url", "label": "URL", "secret": False}],
                        },
                        "mcp": {
                            "std-remote": {
                                "display_name": "标准远程",
                                "description": "标准 streamable-http server",
                                "tools": [{"name": "ping", "description": "ping"}],
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    sk = pdir / "skills" / "std-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: std-skill\ndescription: Standard demo skill\n---\n\n"
        "Data lives in ${PLUGIN_ROOT}/data, cache in ${PLUGIN_DATA}.\n",
        encoding="utf-8",
    )
    (pdir / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "std-remote": {
                        "type": "streamable-http",
                        "url": "https://mcp.example.com/mcp",
                    },
                    "std-local": {
                        "type": "stdio",
                        "command": "node",
                        "args": ["server.js"],
                        "cwd": "${PLUGIN_ROOT}/srv",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    # A client-specific reverse-domain namespace dir must be ignored, not imported
    other = pdir / "com.example.client" / "hooks"
    other.mkdir(parents=True)
    (other / "hooks.json").write_text("{}", encoding="utf-8")
    return pdir
