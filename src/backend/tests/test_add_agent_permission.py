"""Unit tests for the sub-agent self-service capability bit ``can_add_agent``.

Covers: capability bit registration + default off, three-tier resolution
(personal / team default), and the two route-side ``_require_can_add_agent``
gates (self-built agents.py + marketplace agent_marketplace.py).
"""

import pytest

from core.auth.capabilities import BOOL_CAPABILITY_DEFAULTS, resolve_capabilities
from core.db.models import UserShadow
from core.infra.exceptions import AccessDeniedError


# ── Capability bit ──────────────────────────────────────────────────────
def test_capability_bit_registered_default_false():
    assert "can_add_agent" in BOOL_CAPABILITY_DEFAULTS
    assert BOOL_CAPABILITY_DEFAULTS["can_add_agent"] is False


def test_capability_three_tier_resolution():
    # System default off
    assert resolve_capabilities({}, {})["can_add_agent"] is False
    # Personal explicit takes precedence
    assert resolve_capabilities({"can_add_agent": True}, {})["can_add_agent"] is True
    # Team default (personal not explicit)
    assert resolve_capabilities({}, {"can_add_agent": True})["can_add_agent"] is True
    # Personal explicit off overrides team default on
    assert resolve_capabilities({"can_add_agent": False}, {"can_add_agent": True})["can_add_agent"] is False


# ── Route gate helper ───────────────────────────────────────────────────
def _mk_user(db, uid, meta=None):
    db.add(UserShadow(user_id=uid, username=uid, extra_data=meta or {}))
    db.commit()


def test_agents_create_gate(db_session):
    from api.routes.v1.agents import _require_can_add_agent

    _mk_user(db_session, "u_off", meta={})
    with pytest.raises(AccessDeniedError):
        _require_can_add_agent("u_off", db_session)

    _mk_user(db_session, "u_on", meta={"can_add_agent": True})
    _require_can_add_agent("u_on", db_session)  # passes if it does not raise


def test_marketplace_install_gate(db_session):
    from api.routes.v1.agent_marketplace import _require_can_add_agent

    _mk_user(db_session, "m_off", meta={})
    with pytest.raises(AccessDeniedError):
        _require_can_add_agent("m_off", db_session)

    _mk_user(db_session, "m_on", meta={"can_add_agent": True})
    _require_can_add_agent("m_on", db_session)
