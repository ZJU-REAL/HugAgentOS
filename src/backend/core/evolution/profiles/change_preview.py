"""Show the concrete content of a proposed evolution change."""
from __future__ import annotations
from typing import Any, Dict, List, Optional

def change_preview(candidate) -> Dict[str, Any]:
    """The concrete content of the proposed change.

    A one-line hypothesis explains *why* something is proposed and says nothing
    about *what* it is. Approving a distilled skill without being able to read
    its body is not a decision, it is a guess — the text was already in the IR,
    it was simply never sent to the page that asks the user to accept it.
    """
    ir = candidate.ir or {}
    kind = str(candidate.target_kind or "")
    changes = ir.get("changes") or []
    for change in changes:
        if kind == "skill":
            document = change.get("document")
            if isinstance(document, dict) and document.get("content"):
                return {
                    "type": "skill_document",
                    "display_name": str(document.get("display_name") or ""),
                    "description": str(document.get("description") or ""),
                    "allowed_tools": [str(t) for t in (document.get("allowed_tools") or [])],
                    "content": str(document.get("content") or ""),
                }
        if kind == "memory":
            operations = change.get("operations") or []
            if operations:
                return {
                    "type": "memory_ops",
                    "operations": [
                        {
                            "operation": str(op.get("operation") or ""),
                            "text": str(op.get("text") or op.get("memory_ref") or ""),
                            "reason": str(op.get("reason") or ""),
                            "before": op.get("before"),
                            "after": op.get("after"),
                        }
                        for op in operations
                    ],
                }
    if kind == "skill":
        # Sequence-only candidates are the common output of the personal
        # evolution path.  They do not carry an authored SKILL.md, but approval
        # still materialises a concrete name, description and ordered set of
        # steps.  Surface that exact structure instead of returning {}, which
        # made the frontend silently remove the detail button.
        tools: List[str] = []
        constraints: List[Dict[str, Any]] = []
        for change in changes:
            sequence = change.get("tool_sequence")
            if isinstance(sequence, list) and sequence and not tools:
                tools = [str(tool) for tool in sequence]
            rules = change.get("ordering_constraints")
            if isinstance(rules, list) and rules:
                constraints = [dict(rule) for rule in rules if isinstance(rule, dict)]

        rules_only = not tools and bool(constraints)
        if rules_only:
            from core.evolution.cycle.skill_document import tools_from_constraints as _tools_from_constraints

            tools = _tools_from_constraints(constraints)
        if tools:
            # Reuse the materialiser's wording so the preview and the installed
            # skill never disagree about what the user accepted.
            from core.evolution.cycle.skill_document import skill_description as _skill_description, skill_label as _skill_label

            return {
                "type": "skill_sequence",
                "display_name": _skill_label(tools, rules_only=rules_only),
                "description": _skill_description(tools, constraints, rules_only=rules_only),
                "allowed_tools": tools,
                "steps": tools,
                "ordering_constraints": constraints,
            }
    return {}


def tool_sequence_of_ir(ir: Optional[Dict[str, Any]]) -> List[str]:
    for change in (ir or {}).get("changes") or []:
        sequence = change.get("tool_sequence")
        if isinstance(sequence, list) and sequence:
            return [str(t) for t in sequence]
    return []
