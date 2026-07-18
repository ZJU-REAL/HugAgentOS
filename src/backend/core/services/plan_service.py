"""Plan mode business logic — CRUD for plans and steps."""

from typing import Optional, List, Dict, Any
from datetime import datetime
import uuid

from sqlalchemy.orm import Session

from core.db.models import Plan, PlanStep


class PlanService:
    """Service for plan-mode operations."""

    def __init__(self, db: Session):
        self.db = db

    # ── Plan CRUD ──────────────────────────────────────────────────

    def create_plan(
        self,
        *,
        user_id: str,
        title: str,
        description: str = "",
        task_input: str,
        steps: List[Dict[str, Any]],
    ) -> Plan:
        """Create a plan with its steps."""
        plan_id = f"plan_{uuid.uuid4().hex[:16]}"
        plan = Plan(
            plan_id=plan_id,
            user_id=user_id,
            title=title,
            description=description,
            task_input=task_input,
            status="draft",
            total_steps=len(steps),
            completed_steps=0,
        )
        self.db.add(plan)

        for i, s in enumerate(steps):
            step = PlanStep(
                step_id=s.get("step_id") or f"step_{uuid.uuid4().hex[:16]}",
                plan_id=plan_id,
                step_order=i + 1,
                title=s["title"],
                description=s.get("description", ""),
                expected_tools=s.get("expected_tools", []),
                expected_skills=s.get("expected_skills", []),
                expected_agents=s.get("expected_agents", []),
                status="pending",
            )
            self.db.add(step)

        self.db.commit()
        self.db.refresh(plan)
        return plan

    def get_plan(self, plan_id: str, user_id: str) -> Optional[Plan]:
        """Get a plan by id, with ownership check."""
        plan = self.db.query(Plan).filter(Plan.plan_id == plan_id).first()
        if plan and plan.user_id != user_id:
            return None
        return plan

    def list_plans(self, user_id: str, limit: int = 20, offset: int = 0) -> List[Plan]:
        """List plans for a user, newest first."""
        return (
            self.db.query(Plan)
            .filter(Plan.user_id == user_id)
            .order_by(Plan.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def update_plan(self, plan_id: str, **kwargs: Any) -> Optional[Plan]:
        """Update plan fields."""
        plan = self.db.query(Plan).filter(Plan.plan_id == plan_id).first()
        if not plan:
            return None
        for k, v in kwargs.items():
            if hasattr(plan, k):
                setattr(plan, k, v)
        plan.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def update_step(self, step_id: str, **kwargs: Any) -> Optional[PlanStep]:
        """Update step fields."""
        step = self.db.query(PlanStep).filter(PlanStep.step_id == step_id).first()
        if not step:
            return None
        for k, v in kwargs.items():
            if hasattr(step, k):
                setattr(step, k, v)
        self.db.commit()
        self.db.refresh(step)
        return step

    def delete_plan(self, plan_id: str, user_id: str) -> bool:
        """Delete a plan (cascades to steps)."""
        plan = self.db.query(Plan).filter(
            Plan.plan_id == plan_id, Plan.user_id == user_id
        ).first()
        if not plan:
            return False
        self.db.delete(plan)
        self.db.commit()
        return True

    def replace_steps(self, plan_id: str, steps: List[Dict[str, Any]]) -> Optional[Plan]:
        """Replace all steps of a plan (for reordering/editing)."""
        plan = self.db.query(Plan).filter(Plan.plan_id == plan_id).first()
        if not plan:
            return None

        # Delete existing steps
        self.db.query(PlanStep).filter(PlanStep.plan_id == plan_id).delete()

        # Create new steps
        for i, s in enumerate(steps):
            step = PlanStep(
                step_id=s.get("step_id") or f"step_{uuid.uuid4().hex[:16]}",
                plan_id=plan_id,
                step_order=i + 1,
                title=s["title"],
                description=s.get("description", ""),
                expected_tools=s.get("expected_tools", []),
                expected_skills=s.get("expected_skills", []),
                expected_agents=s.get("expected_agents", []),
                status="pending",
            )
            self.db.add(step)

        plan.total_steps = len(steps)
        plan.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(plan)
        return plan

    # ── Serialization helpers ──────────────────────────────────────

    @staticmethod
    def plan_to_dict(plan: Plan) -> Dict[str, Any]:
        """Serialize a Plan + steps to dict."""
        extra = plan.extra_data or {}
        result: Dict[str, Any] = {
            "plan_id": plan.plan_id,
            "title": plan.title,
            "description": plan.description,
            "task_input": plan.task_input,
            "status": plan.status,
            "total_steps": plan.total_steps,
            "completed_steps": plan.completed_steps,
            "result_summary": plan.result_summary,
            "steps": [PlanService.step_to_dict(s) for s in plan.steps],
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
            "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        }
        # Include agent name map if stored in plan metadata
        if isinstance(extra, dict) and extra.get("agent_name_map"):
            result["agent_name_map"] = extra["agent_name_map"]
        return result

    @staticmethod
    def step_to_dict(step: PlanStep) -> Dict[str, Any]:
        """Serialize a PlanStep to dict."""
        return {
            "step_id": step.step_id,
            "step_order": step.step_order,
            "title": step.title,
            "description": step.description,
            "expected_tools": step.expected_tools or [],
            "expected_skills": step.expected_skills or [],
            "expected_agents": step.expected_agents or [],
            "status": step.status,
            "result_summary": step.result_summary,
            "ai_output": step.ai_output,
            "error_message": step.error_message,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
        }

    @staticmethod
    def build_execution_snapshot(
        plan: Plan,
        *,
        completed_steps: int,
        total_steps: int,
        result_text: str,
    ) -> Dict[str, Any]:
        """Snapshot of plan execution for persistence in assistant message.

        Shared by chat-triggered plan execution (api/routes/v1/plans.py) and
        automation-triggered plan execution (routing/automation_scheduler.py);
        drives the frontend PlanCard(mode="complete") rendering.
        """
        return {
            "mode": "complete",
            "title": plan.title,
            "description": plan.description,
            "steps": [
                {
                    "step_order": s.step_order,
                    "title": s.title,
                    "description": s.description,
                    "expected_tools": s.expected_tools or [],
                    "expected_skills": s.expected_skills or [],
                    "status": s.status,
                    "summary": s.result_summary,
                    "ai_output": s.ai_output,
                    "tool_calls_log": s.tool_calls_log or [],
                }
                for s in plan.steps
            ],
            "completed_steps": completed_steps,
            "total_steps": total_steps,
            "result_text": result_text,
        }
