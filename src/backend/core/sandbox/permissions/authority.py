"""Who answers a run's tool approvals.

The decision is made once per agent construction and travels on
``PermissionRuntime``. A conversation that carries a credential scope never
borrows the owner's live account preset; this module owns the scope keys so the
runtime and the account policy reader cannot disagree about which runs those are.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

CREDENTIAL_SCOPE_KEYS = ("agent_api_scope", "project_api_scope")


class ApprovalAuthority(str, Enum):
    # The signed-in owner: the live account preset is re-read per dispatch and
    # may answer pending prompts.
    ACCOUNT = "account"
    # A preset fixed when the run was built, such as an explicit module profile.
    RUN = "run"
    # An agent or project API key: nobody is present to answer a prompt.
    CREDENTIAL = "credential"
    EVALUATION = "evaluation"

    @property
    def unattended(self) -> bool:
        return self in (ApprovalAuthority.CREDENTIAL, ApprovalAuthority.EVALUATION)


def credential_scoped(record: Mapping[str, Any] | None) -> bool:
    """Whether a chat or run context belongs to an API credential."""
    return isinstance(record, Mapping) and any(record.get(key) for key in CREDENTIAL_SCOPE_KEYS)


def resolve_run_authority(*, evaluation: bool, credential: bool, profile: bool) -> ApprovalAuthority:
    if evaluation:
        return ApprovalAuthority.EVALUATION
    if credential:
        return ApprovalAuthority.CREDENTIAL
    if profile:
        return ApprovalAuthority.RUN
    return ApprovalAuthority.ACCOUNT
