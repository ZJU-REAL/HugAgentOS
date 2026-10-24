"""Community-edition site visibility ORM fields."""

from sqlalchemy import CheckConstraint, Column, String
from sqlalchemy.orm import declared_attr


class SiteScopeMixin:
    @declared_attr
    def visibility(cls):
        return Column(String(16), nullable=False, default="public")


def site_scope_table_args() -> tuple:
    return (
        CheckConstraint(
            "visibility IN ('public', 'private')",
            name="sites_visibility_check",
        ),
    )
