"""Ontology-assisted validation for skills, tools, and sub-agent definitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from core.config.display_names import MCP_SERVER_DISPLAY_NAMES, TOOL_DISPLAY_NAMES
from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin
from core.infra.exceptions import BadRequestError
from core.ontology.schemas import OntologyPackDocument
from core.ontology.validator import activate_runtime_for_asset
from sqlalchemy.orm import Session


@dataclass(slots=True)
class BuildIssue:
    severity: str
    code: str
    message: str
    workflow_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BuildValidationReport:
    valid: bool
    matched_workflows: list[str] = field(default_factory=list)
    resolved_tools: list[str] = field(default_factory=list)
    resolved_tool_details: list[dict[str, str]] = field(default_factory=list)
    errors: list[BuildIssue] = field(default_factory=list)
    warnings: list[BuildIssue] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "matched_workflows": self.matched_workflows,
            "resolved_tools": self.resolved_tools,
            "resolved_tool_details": self.resolved_tool_details,
            "errors": [asdict(item) for item in self.errors],
            "warnings": [asdict(item) for item in self.warnings],
            "suggestions": self.suggestions,
        }


class OntologyBuildValidator:
    """Validate an agent asset against active default Domain Packs."""

    def __init__(self, db: Session):
        self.db = db

    def validate(
        self,
        *,
        asset_type: Literal["skill", "tool", "subagent"],
        name: str,
        description: str = "",
        instructions: str = "",
        tool_names: list[str] | None = None,
        mcp_server_ids: list[str] | None = None,
        skill_ids: list[str] | None = None,
        plugin_ids: list[str] | None = None,
        ontology_tags: list[str] | None = None,
        output_schema: dict[str, Any] | None = None,
        tool_schemas: dict[str, dict[str, Any]] | None = None,
    ) -> BuildValidationReport:
        from core.services.ontology_service import OntologyService

        task = "\n".join(item for item in (name, description, instructions[:4000]) if item)
        runtime = OntologyService(self.db).build_runtime(task=task)
        resolved_tools = set(tool_names or [])
        resolved_mcp_ids = set(mcp_server_ids or [])
        resolved_skill_ids = set(skill_ids or [])
        if plugin_ids:
            plugin_rows = (
                self.db.query(InstalledPlugin)
                .filter(InstalledPlugin.install_id.in_(plugin_ids))
                .all()
            )
            for plugin in plugin_rows:
                components = plugin.component_ids or {}
                resolved_mcp_ids.update(components.get("mcp") or [])
                resolved_skill_ids.update(components.get("skills") or [])
        if resolved_skill_ids:
            skill_rows = (
                self.db.query(AdminSkill).filter(AdminSkill.skill_id.in_(resolved_skill_ids)).all()
            )
            for row in skill_rows:
                resolved_tools.update(str(item) for item in row.allowed_tools or [] if item)
        if resolved_mcp_ids:
            rows = (
                self.db.query(AdminMcpServer)
                .filter(AdminMcpServer.server_id.in_(resolved_mcp_ids))
                .all()
            )
            for row in rows:
                resolved_tools.update(
                    str(item["name"])
                    for item in row.tools_json or []
                    if isinstance(item, dict) and item.get("name")
                )

        errors: list[BuildIssue] = []
        warnings: list[BuildIssue] = []
        matched: list[str] = []
        suggestions: list[str] = []
        declared_ontology_tags = {
            tag.split(":", 1)[1].strip()
            for tag in (ontology_tags or [])
            if isinstance(tag, str) and tag.startswith("ontology:") and tag.split(":", 1)[1].strip()
        }
        if declared_ontology_tags:
            controlled_ids: set[str] = set()
            for version in OntologyService(self.db).repo.get_active_versions():
                try:
                    document = OntologyPackDocument.model_validate(version.content)
                except Exception:
                    continue
                controlled_ids.update(concept.id for concept in document.concepts)
            unknown_tags = sorted(declared_ontology_tags - controlled_ids)
            if controlled_ids and unknown_tags:
                errors.append(
                    BuildIssue(
                        "error",
                        "unknown_ontology_tags",
                        "资产声明了不在激活领域词表中的本体标签: " + ", ".join(unknown_tags),
                    )
                )
                suggestions.append(
                    "改用受控本体标签: "
                    + ", ".join(f"ontology:{item}" for item in sorted(controlled_ids)[:20])
                )
        if asset_type == "tool":
            for tool_name in resolved_tools:
                activate_runtime_for_asset(
                    runtime,
                    kind="tool",
                    asset_id=tool_name,
                    tags=ontology_tags or [],
                )
        else:
            activate_runtime_for_asset(
                runtime,
                kind=asset_type,
                asset_id=name,
                tags=ontology_tags or [],
            )
        for pack in runtime.get("packs", []):
            for workflow in pack.get("workflows", []):
                workflow_id = workflow.get("id")
                if workflow_id:
                    matched.append(f"{pack.get('pack_id')}:{workflow_id}")
                missing = sorted(set(workflow.get("required_tools", [])) - resolved_tools)
                forbidden = sorted(set(workflow.get("forbidden_tools", [])) & resolved_tools)
                # A skill/subagent must carry the complete workflow.  A tool
                # asset is only one building block, so requiring every sibling
                # tool on the same MCP server would reject valid modular tools.
                if missing and asset_type != "tool":
                    guidance = self._mcp_binding_guidance(missing)
                    missing_names = "、".join(
                        item["display_name"] for item in guidance["missing_tools"]
                    )
                    recommended_names = "、".join(
                        f"“{item['display_name']}”" for item in guidance["recommended_mcp_servers"]
                    )
                    asset_label = {"skill": "技能", "subagent": "子智能体"}[asset_type]
                    message = f"{asset_label}缺少领域工作流要求的能力：{missing_names}。"
                    if recommended_names:
                        message += f"请在“绑定工具 (MCP)”中选择 {recommended_names}。"
                    else:
                        message += "当前未找到可绑定的 MCP，请联系管理员配置对应能力。"
                    errors.append(
                        BuildIssue(
                            "error",
                            "missing_required_tools",
                            message,
                            workflow_id,
                            guidance,
                        )
                    )
                    for server in guidance["recommended_mcp_servers"]:
                        provided = "、".join(
                            item["display_name"] for item in server["provided_tools"]
                        )
                        suggestions.append(
                            f"绑定 MCP“{server['display_name']}”（提供：{provided}）"
                        )
                    if guidance["unmapped_tools"]:
                        suggestions.append(
                            "请联系管理员配置可提供以下能力的 MCP："
                            + "、".join(item["display_name"] for item in guidance["unmapped_tools"])
                        )
                if forbidden:
                    errors.append(
                        BuildIssue(
                            "error",
                            "forbidden_tools_bound",
                            f"{asset_type} 绑定了领域工作流禁止的工具: {', '.join(forbidden)}",
                            workflow_id,
                        )
                    )
                    suggestions.append("移除禁止工具: " + ", ".join(forbidden))

            output_rules = [
                rule
                for rule in pack.get("constraints", [])
                if (rule.get("target") or {}).get("kind") == "output"
            ]
            if output_rules and output_schema is None:
                warnings.append(
                    BuildIssue(
                        "warning",
                        "missing_output_contract",
                        "资产命中输出约束，但未声明结构化 output_schema；运行时仍会校验文本输出。",
                    )
                )

        if asset_type == "tool":
            self._validate_tool_contracts(
                resolved_tools=resolved_tools,
                tool_schemas=tool_schemas or {},
                errors=errors,
                warnings=warnings,
                matched=matched,
                suggestions=suggestions,
            )

        return BuildValidationReport(
            valid=not errors,
            matched_workflows=matched,
            resolved_tools=sorted(resolved_tools),
            resolved_tool_details=[
                self._tool_detail(tool_name) for tool_name in sorted(resolved_tools)
            ],
            errors=errors,
            warnings=warnings,
            suggestions=sorted(set(suggestions)),
        )

    @staticmethod
    def _tool_detail(tool_name: str) -> dict[str, str]:
        return {
            "name": tool_name,
            "display_name": TOOL_DISPLAY_NAMES.get(tool_name, tool_name),
        }

    @staticmethod
    def _mcp_display_name(server: AdminMcpServer) -> str:
        configured = str(server.display_name or "").strip()
        fallback = MCP_SERVER_DISPLAY_NAMES.get(server.server_id, server.server_id)
        return configured if configured and configured != server.server_id else fallback

    def _mcp_binding_guidance(self, missing_tools: list[str]) -> dict[str, Any]:
        """Map low-level required tools to bindable global MCP services.

        Suggestions intentionally exclude private and plugin-owned MCP rows: those are not
        universally visible in the direct MCP selector, and exposing them could leak another
        user's private capability names.
        """
        missing = set(missing_tools)
        candidates: dict[str, dict[str, Any]] = {}
        tool_servers: dict[str, list[dict[str, str]]] = {name: [] for name in missing}
        rows = (
            self.db.query(AdminMcpServer)
            .filter(
                AdminMcpServer.owner_user_id.is_(None),
                AdminMcpServer.source_plugin.is_(None),
                AdminMcpServer.is_enabled.is_(True),
            )
            .all()
        )
        for server in rows:
            provided = {
                str(item.get("name") or "").strip()
                for item in (server.tools_json or [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            }
            covered = provided & missing
            if not covered:
                continue
            server_info = {
                "server_id": server.server_id,
                "display_name": self._mcp_display_name(server),
            }
            for tool_name in covered:
                tool_servers[tool_name].append(server_info)
            candidates[server.server_id] = {
                **server_info,
                "tool_names": covered,
            }

        # Recommend the smallest practical set of MCPs using a deterministic greedy cover.
        uncovered = set(missing)
        recommended: list[dict[str, Any]] = []
        remaining = dict(candidates)
        while uncovered and remaining:
            best = max(
                remaining.values(),
                key=lambda item: (
                    len(item["tool_names"] & uncovered),
                    item["display_name"],
                    item["server_id"],
                ),
            )
            covered = best["tool_names"] & uncovered
            if not covered:
                break
            recommended.append(
                {
                    "server_id": best["server_id"],
                    "display_name": best["display_name"],
                    "provided_tools": [
                        self._tool_detail(tool_name) for tool_name in sorted(covered)
                    ],
                }
            )
            uncovered -= covered
            remaining.pop(best["server_id"], None)

        missing_details = []
        for tool_name in sorted(missing):
            detail: dict[str, Any] = self._tool_detail(tool_name)
            detail["mcp_servers"] = sorted(
                tool_servers[tool_name],
                key=lambda item: (item["display_name"], item["server_id"]),
            )
            missing_details.append(detail)
        return {
            "missing_tools": missing_details,
            "recommended_mcp_servers": recommended,
            "unmapped_tools": [self._tool_detail(tool_name) for tool_name in sorted(uncovered)],
        }

    def _validate_tool_contracts(
        self,
        *,
        resolved_tools: set[str],
        tool_schemas: dict[str, dict[str, Any]],
        errors: list[BuildIssue],
        warnings: list[BuildIssue],
        matched: list[str],
        suggestions: list[str],
    ) -> None:
        """Check discovered tool inputs directly against active pack rules."""
        from core.services.ontology_service import OntologyService

        service = OntologyService(self.db)
        for version in service.repo.get_active_versions():
            try:
                document = OntologyPackDocument.model_validate(version.content)
            except Exception:
                continue
            for workflow in document.workflows:
                if resolved_tools & set(workflow.required_tools + workflow.forbidden_tools):
                    key = f"{document.pack_id}:{workflow.id}"
                    if key not in matched:
                        matched.append(key)
            for rule in document.constraints:
                target = rule.target
                tool_name = target.tool
                if not rule.enabled or not tool_name or tool_name not in resolved_tools:
                    continue
                schema = tool_schemas.get(tool_name) or {}
                properties = self._schema_properties(schema)
                if not schema:
                    warnings.append(
                        BuildIssue(
                            "warning",
                            "missing_tool_input_schema",
                            f"工具 {tool_name} 命中本体规则 {rule.id}，但没有可校验的 inputSchema。",
                            rule.id,
                        )
                    )
                    continue
                required_parameters: set[str] = set()
                if target.kind == "tool_parameter" and target.parameter:
                    required_parameters.add(target.parameter)
                elif target.kind == "tool":
                    required_parameters.update(
                        str(item) for item in rule.schema_.get("required", [])
                    )
                    required_parameters.update(
                        str(item) for item in rule.schema_.get("properties", {}).keys()
                    )
                missing_parameters = sorted(required_parameters - set(properties))
                if missing_parameters:
                    errors.append(
                        BuildIssue(
                            "error",
                            "missing_ontology_parameters",
                            f"工具 {tool_name} 的 inputSchema 缺少本体规则要求的参数: "
                            + ", ".join(missing_parameters),
                            rule.id,
                        )
                    )
                    suggestions.append(f"为 {tool_name} 补充参数: " + ", ".join(missing_parameters))

    @staticmethod
    def _schema_properties(schema: dict[str, Any]) -> dict[str, Any]:
        function = schema.get("function") if isinstance(schema, dict) else None
        if isinstance(function, dict):
            schema = function.get("parameters") or function.get("inputSchema") or {}
        else:
            schema = schema.get("inputSchema") or schema.get("parameters") or schema
        properties = schema.get("properties") if isinstance(schema, dict) else None
        return properties if isinstance(properties, dict) else {}


def ensure_ontology_build_valid(
    db: Session,
    *,
    asset_type: Literal["skill", "tool", "subagent"],
    name: str,
    description: str = "",
    instructions: str = "",
    tool_names: list[str] | None = None,
    mcp_server_ids: list[str] | None = None,
    skill_ids: list[str] | None = None,
    plugin_ids: list[str] | None = None,
    ontology_tags: list[str] | None = None,
    output_schema: dict[str, Any] | None = None,
    tool_schemas: dict[str, dict[str, Any]] | None = None,
    message: str | None = None,
) -> BuildValidationReport:
    """Run the common reconstruction-layer gate and reject invalid assets."""
    report = OntologyBuildValidator(db).validate(
        asset_type=asset_type,
        name=name,
        description=description,
        instructions=instructions,
        tool_names=tool_names,
        mcp_server_ids=mcp_server_ids,
        skill_ids=skill_ids,
        plugin_ids=plugin_ids,
        ontology_tags=ontology_tags,
        output_schema=output_schema,
        tool_schemas=tool_schemas,
    )
    if not report.valid:
        labels = {"skill": "技能", "tool": "工具", "subagent": "子智能体"}
        raise BadRequestError(
            message=message or f"{labels[asset_type]}未通过本体构建校验",
            data=report.as_dict(),
        )
    return report
