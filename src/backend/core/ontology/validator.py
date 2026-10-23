"""Validate ontology packs before activation."""
from __future__ import annotations
from typing import Any, Iterable
from core.ontology.schemas import OntologyPackDocument
from jsonschema import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError
from core.ontology import validation_models

class DomainPackValidator:
    """Validate structure, JSON Schema, and tool references before activation."""

    def validate(
        self,
        payload: dict[str, Any],
        *,
        tool_schemas: dict[str, dict[str, Any]] | None = None,
        known_tools: Iterable[str] = (),
    ) -> tuple[OntologyPackDocument | None, validation_models.ValidationReport]:
        errors: list[validation_models.ValidationIssue] = []
        warnings: list[validation_models.ValidationIssue] = []
        try:
            document = OntologyPackDocument.model_validate(payload)
        except PydanticValidationError as exc:
            for item in exc.errors(include_url=False):
                errors.append(
                    validation_models.ValidationIssue(
                        "error",
                        ".".join(str(part) for part in item["loc"]),
                        item["msg"],
                    )
                )
            return None, validation_models.ValidationReport(False, errors, warnings)

        schemas = tool_schemas or {}
        known = set(known_tools) | set(schemas)
        for index, rule in enumerate(document.constraints):
            path = f"constraints.{index}"
            if rule.schema_:
                try:
                    Draft202012Validator.check_schema(rule.schema_)
                except Exception as exc:  # SchemaError is version-specific
                    errors.append(validation_models.ValidationIssue("error", f"{path}.schema", str(exc)))
            tool_name = rule.target.tool
            if not tool_name:
                continue
            if tool_name not in known:
                issue = validation_models.ValidationIssue(
                    "warning" if document.config.allow_unresolved_tools else "error",
                    f"{path}.target.tool",
                    f"unknown tool reference: {tool_name}",
                )
                (warnings if document.config.allow_unresolved_tools else errors).append(issue)
                continue
            if rule.target.kind == "tool_parameter" and tool_name in schemas:
                properties = tool_input_schema(schemas[tool_name]).get("properties", {})
                if rule.target.parameter not in properties:
                    errors.append(
                        validation_models.ValidationIssue(
                            "error",
                            f"{path}.target.parameter",
                            f"unknown parameter {rule.target.parameter!r} for tool {tool_name}",
                        )
                    )

        for index, workflow in enumerate(document.workflows):
            for tool_name in set(workflow.required_tools + workflow.forbidden_tools):
                if tool_name not in known:
                    issue = validation_models.ValidationIssue(
                        "warning" if document.config.allow_unresolved_tools else "error",
                        f"workflows.{index}",
                        f"unknown tool reference: {tool_name}",
                    )
                    (warnings if document.config.allow_unresolved_tools else errors).append(issue)
            overlap = set(workflow.required_tools) & set(workflow.forbidden_tools)
            if overlap:
                errors.append(
                    validation_models.ValidationIssue(
                        "error",
                        f"workflows.{index}",
                        f"tools cannot be both required and forbidden: {sorted(overlap)}",
                    )
                )
            for trigger_index, trigger in enumerate(workflow.asset_triggers):
                if trigger.kind != "tool":
                    continue
                for tool_name in trigger.ids:
                    if tool_name in known:
                        continue
                    issue = validation_models.ValidationIssue(
                        "warning" if document.config.allow_unresolved_tools else "error",
                        f"workflows.{index}.asset_triggers.{trigger_index}.ids",
                        f"unknown tool reference: {tool_name}",
                    )
                    (warnings if document.config.allow_unresolved_tools else errors).append(issue)

        return document, validation_models.ValidationReport(not errors, errors, warnings)


def tool_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    function = schema.get("function") if isinstance(schema, dict) else None
    if isinstance(function, dict):
        return function.get("parameters") or function.get("inputSchema") or {}
    return schema.get("inputSchema") or schema.get("parameters") or schema
