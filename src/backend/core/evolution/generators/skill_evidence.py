"""The evidence pack a generated skill may be written from."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

@dataclass
class EvidencePack:
    """Everything stage B is allowed to see, and nothing else."""

    skill_id: str
    objectives: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    successful_sequences: List[List[str]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    ordering_constraints: List[Dict[str, Any]] = field(default_factory=list)
    procedures: List[Dict[str, str]] = field(default_factory=list)
    task_types: List[str] = field(default_factory=list)
    excluded_task_types: List[str] = field(default_factory=list)
    support: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "objectives": self.objectives[:10],
            "tools": list(self.tools),
            "successful_sequences": self.successful_sequences[:6],
            "failures": self.failures[:6],
            "ordering_constraints": self.ordering_constraints,
            "procedures": self.procedures[:10],
            "task_types": self.task_types,
            "excluded_task_types": self.excluded_task_types,
            "support": self.support,
        }


@dataclass
class DraftResult:
    ok: bool
    body: str = ""
    display_name: str = ""
    description: str = ""
    violations: List[str] = field(default_factory=list)
    attempts: int = 0
    # ``True`` when no draft could be produced because the model was unreachable
    # — an operational state, not a rejection, and the two must not look alike
    # in the console.
    generator_unavailable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "display_name": self.display_name,
            "description": self.description,
            "violations": list(self.violations),
            "attempts": self.attempts,
            "generator_unavailable": self.generator_unavailable,
            "body_chars": len(self.body),
        }


def build_evidence_pack(
    *,
    skill_id: str,
    episodes: Sequence[Dict[str, Any]],
    ordering_constraints: Sequence[Dict[str, Any]] = (),
    procedures: Sequence[Dict[str, str]] = (),
) -> EvidencePack:
    """Assemble what a skill may be written from.

    Failures are included deliberately. A document distilled only from successes
    describes a happy path and says nothing about the step where things actually
    go wrong, which is the part a reader needs most.
    """
    objectives: List[str] = []
    tools: List[str] = []
    sequences: List[List[str]] = []
    failures: List[Dict[str, Any]] = []
    task_types: List[str] = []

    for episode in episodes:
        objective = str(episode.get("objective") or "").strip()
        if objective and objective not in objectives:
            objectives.append(objective)
        task_type = str(episode.get("task_type") or "").strip()
        if task_type and task_type not in task_types:
            task_types.append(task_type)

        sequence = [str(t) for t in (episode.get("tool_sequence") or [])]
        for tool in sequence:
            if tool not in tools:
                tools.append(tool)
        if episode.get("verdict") == "success":
            if sequence and sequence not in sequences:
                sequences.append(sequence)
        elif episode.get("verdict") == "failed":
            failures.append(
                {
                    "objective": objective[:160],
                    "tool_sequence": sequence[:12],
                    "failed_at": str(episode.get("failed_tool") or ""),
                    "error": str(episode.get("error") or "")[:200],
                }
            )

    excluded = sorted(
        {
            str(name)
            for rule in ordering_constraints
            for name in (rule.get("contradicted_in") or [])
            if str(name)
        }
    )
    return EvidencePack(
        skill_id=skill_id,
        objectives=objectives,
        tools=tools,
        successful_sequences=sequences,
        failures=failures,
        ordering_constraints=list(ordering_constraints),
        procedures=[dict(p) for p in procedures],
        task_types=[t for t in task_types if t not in excluded],
        excluded_task_types=excluded,
        support=len(episodes),
    )
