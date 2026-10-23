"""Desktop MCP connector for an account's mounted cloud directories."""

import base64
import json
from typing import Literal

from core.capabilities.manifest_protocol import canonical_hash
from core.files.spaces.access import resolve_grants
from core.files.spaces.files import SpaceFiles
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

SERVER_ID = "hugagent-space-files"


class ReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["roots", "list", "read"]
    space: str = "personal"
    path: str = ""
    cursor: str | None = None


class WriteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["write", "mkdir", "move", "delete"]
    space: str = "personal"
    path: str
    destination: str = ""
    content_base64: str = Field(default="", max_length=7_000_000)
    directory: bool = False


READ_TOOL = "space_files_read"
WRITE_TOOL = "space_files_write"
ARGUMENTS = {READ_TOOL: ReadArguments, WRITE_TOOL: WriteArguments}


def tools():
    return [
        {
            "name": READ_TOOL,
            "description": "Browse and read authorized cloud files. List roots first. Paths are relative to a root. File contents use base64; maximum 5 MiB.",
            "inputSchema": ReadArguments.model_json_schema(),
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": WRITE_TOOL,
            "description": "Create, overwrite, move or delete authorized cloud files after approval. Paths are relative to a root. Writes use base64; maximum 5 MiB. Delete directories only when empty.",
            "inputSchema": WriteArguments.model_json_schema(),
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
    ]


def manifest():
    schemas = tools()
    return {
        "server_id": SERVER_ID,
        "display_name": "我的空间",
        "description": "访问当前账号有权查看的云端文件目录",
        "origin": "cloud",
        "execution_scope": "cloud",
        "source_plugin": None,
        "created_at": None,
        "enabled": True,
        "icon": "",
        "tools": schemas,
        "schema_hash": canonical_hash(schemas),
    }


def resolve(user, name, schema_hash):
    from core.capabilities.manifest_protocol import CapabilityManifestStaleError

    if not user or str(user).startswith("api_ak_") or name not in ARGUMENTS:
        return None
    if schema_hash != manifest()["schema_hash"]:
        raise CapabilityManifestStaleError("capability manifest changed")
    return {
        "user_id": user,
        "server_id": SERVER_ID,
        "target": {"embedded_mcp": SERVER_ID},
        "tool": next(tool for tool in tools() if tool["name"] == name),
    }


def execute(user, tool_name, arguments):
    from core.db.engine import SessionLocal
    from core.files.storage import get_storage

    try:
        args = ARGUMENTS[tool_name].model_validate(arguments)
    except (KeyError, ValidationError) as exc:
        raise HTTPException(400, "Invalid file operation arguments") from exc
    with SessionLocal() as db:
        files = SpaceFiles(db, user, get_storage())
        if args.operation == "roots":
            result = {"items": [grant.public() for grant in resolve_grants(db, user)]}
        elif args.operation == "list":
            result = files.list(args.space, args.path, cursor=args.cursor)
        elif args.operation == "read":
            data, info = files.read(args.space, args.path)
            if len(data) > 5 * 1024 * 1024:
                raise HTTPException(413, "Use the web file manager to download files over 5 MiB")
            result = {
                "path": args.path,
                "content_base64": base64.b64encode(data).decode(),
                "size": info.size,
            }
        elif args.operation == "write":
            try:
                data = base64.b64decode(args.content_base64, validate=True)
            except ValueError as exc:
                raise HTTPException(400, "Invalid base64 content") from exc
            if len(data) > 5 * 1024 * 1024:
                raise HTTPException(413, "File exceeds 5 MiB")
            result = files.write(args.space, args.path, data)
        elif args.operation == "mkdir":
            result = files.mkdir(args.space, args.path)
        elif args.operation == "move":
            result = files.move(args.space, args.path, args.destination)
        else:
            files.delete(args.space, args.path, directory=args.directory)
            result = {"path": args.path}
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
        "metadata": {"origin": "cloud", "mcp_server_id": SERVER_ID},
    }
