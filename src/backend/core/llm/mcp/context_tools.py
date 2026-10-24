"""Expose advertised MCP resources and prompts as permission-governed tools."""
import hashlib
import json
import jsonschema
from mcp.shared.uri_template import UriTemplate
from agentscope.message import TextBlock
from agentscope.tool import ToolChunk
from agentscope.permission import PermissionBehavior, PermissionDecision
from core.llm.mcp.results import content_blocks, envelope
from core.llm.mcp.tools import SessionTool


class ContextTool(SessionTool):
    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, resource, kind, descriptor, timeout):
        super().__init__(resource, timeout)
        server = resource.name
        self.kind, self.descriptor = kind, descriptor
        identity = (str(descriptor.uri) if kind == "resource" else descriptor.uri_template
                    if kind == "resource_template" else descriptor.name)
        self.name = "mcp_" + kind + "_" + hashlib.sha256(f"{server}:{identity}".encode()).hexdigest()[:16]
        self.description = f"{kind} from {server}: {descriptor.description or identity}"
        self.input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
        if kind == "resource_template":
            self.template = UriTemplate.parse(descriptor.uri_template)
            self.input_schema["properties"] = {v.name: ({"type": "array", "items": {"type": "string"}}
                if v.explode else {"type": "string"}) for v in self.template.variables}
            self.input_schema["required"] = [v for v in self.template.variable_names
                                             if v not in self.template.query_variable_names]
        if kind == "prompt":
            self.input_schema["properties"] = {a.name: {"type": "string", "description": a.description or a.name}
                                               for a in descriptor.arguments or []}
            self.input_schema["required"] = [a.name for a in descriptor.arguments or [] if a.required]

    async def check_permissions(self, *_args, **_kwargs):
        return PermissionDecision(behavior=PermissionBehavior.ASK,
                                  message="Read remote MCP context from the selected server")

    async def call(self, **kwargs):
        return await self._invoke(lambda: self._read(**kwargs), exclusive=False)

    async def _read(self, **kwargs):
        jsonschema.validate(kwargs, self.input_schema)
        client = self.resource.session.client
        if self.kind in {"resource", "resource_template"}:
            uri = str(self.descriptor.uri) if self.kind == "resource" else self.template.expand(kwargs)
            # An explicit read wants current content; only listings are cached.
            result = await client.read_resource(uri, cache_mode="bypass")
            blocks = [TextBlock(text=item.model_dump_json(by_alias=True, exclude_none=True)) for item in result.contents]
        else:
            result = await client.get_prompt(self.descriptor.name, arguments=kwargs)
            blocks = []
            for message in result.messages:
                blocks.append(TextBlock(text=json.dumps({"role": message.role}, ensure_ascii=False)))
                blocks.extend(content_blocks([message.content]))
        return ToolChunk(content=blocks, metadata={"mcp": envelope(result)})


def context_tools(resource, timeout):
    tools, names = [], set()
    for kind, descriptor in resource.session.context:
        tool = ContextTool(resource, kind, descriptor, timeout)
        if tool.name in names:
            raise ValueError("Duplicate MCP context identity")
        names.add(tool.name)
        tools.append(tool)
    return tools
