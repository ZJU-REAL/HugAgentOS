"""Personal artifact predicates use the installed database edition policy."""
from typing import Any
from core.db.edition import personal_artifact_scope_fields


def personal_artifact_predicates(artifact_model: Any) -> list[Any]:
    return [getattr(artifact_model, name).is_(value)
            for name, value in personal_artifact_scope_fields().items()]


def is_personal_artifact(artifact: Any) -> bool:
    return all(getattr(artifact, name) == value
               for name, value in personal_artifact_scope_fields().items())
