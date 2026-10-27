"""Community-edition startup hooks."""


def seed_default_roles(db) -> None:
    return None


def bootstrap_edition_plugins(db) -> tuple[str, ...]:
    return ()


def reconcile_datasource_catalog() -> None:
    return None


async def recover_datasource_sidecars() -> dict:
    return {}


def create_distillation_scheduler():
    return None


def recover_persona_distill_jobs() -> int:
    return 0


__all__ = [
    "bootstrap_edition_plugins",
    "create_distillation_scheduler",
    "recover_datasource_sidecars",
    "reconcile_datasource_catalog",
    "recover_persona_distill_jobs",
    "seed_default_roles",
]
