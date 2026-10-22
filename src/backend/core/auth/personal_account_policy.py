"""Single-tenant account policy for the community edition."""

from core.infra.exceptions import AppException


class AccountCapacityExceeded(AppException):
    """Neutral account-admission exception; CE never raises it."""


def account_capacity_block_reason(db) -> None:
    return None


def validate_registration_credential(db, credential: str):
    return True, None, None


def claim_registration_credential(db, credential: str, user_id: str):
    return True, None


def registration_credential_id(validated) -> None:
    return None


def add_invited_account_to_scope(db, validated, user_id: str) -> None:
    return None


def validate_account_scope(db, scope_id) -> bool:
    return scope_id is None


def add_account_to_scope(db, scope_id, user_id: str, role: str) -> None:
    return None


def list_account_scopes(db, user_id: str) -> list:
    return []


__all__ = [
    "AccountCapacityExceeded",
    "account_capacity_block_reason",
    "add_account_to_scope",
    "add_invited_account_to_scope",
    "claim_registration_credential",
    "list_account_scopes",
    "registration_credential_id",
    "validate_account_scope",
    "validate_registration_credential",
]
