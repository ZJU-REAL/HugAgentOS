"""Team sub-agents: visibility + owner/admin management permission tests."""

from __future__ import annotations

import pytest

from core.db.models import Team, TeamMember, UserShadow
from core.services.user_agent_service import UserAgentService


def _seed(db):
    # Users: mgr (team admin), mem (team member), out (non-member)
    for uid in ("mgr", "mem", "out"):
        db.add(UserShadow(user_id=uid, username=uid, extra_data={}))
    db.add(Team(team_id="t1", name="T1"))
    db.add(TeamMember(team_id="t1", user_id="mgr", role="admin"))
    db.add(TeamMember(team_id="t1", user_id="mem", role="member"))
    db.commit()


def test_only_manager_can_create_team_agent(db_session):
    _seed(db_session)
    svc = UserAgentService(db_session)
    # member cannot create a team agent
    with pytest.raises(PermissionError):
        svc.create(user_id="mem", operator_name="mem", owner_type="team",
                   data={"name": "A", "system_prompt": "x"}, team_id="t1")
    # non-member cannot create
    with pytest.raises(PermissionError):
        svc.create(user_id="out", operator_name="out", owner_type="team",
                   data={"name": "A", "system_prompt": "x"}, team_id="t1")
    # owner/admin can
    agent = svc.create(user_id="mgr", operator_name="mgr", owner_type="team",
                       data={"name": "TeamBot", "system_prompt": "x"}, team_id="t1")
    assert agent["owner_type"] == "team"
    assert agent["team_id"] == "t1"
    assert agent["user_id"] is None
    assert agent["created_by"] == "mgr"


def test_team_agent_requires_team_id(db_session):
    _seed(db_session)
    with pytest.raises(ValueError):
        UserAgentService(db_session).create(
            user_id="mgr", operator_name="mgr", owner_type="team",
            data={"name": "A", "system_prompt": "x"}, team_id=None)


def test_team_agent_visible_to_all_members_not_outsiders(db_session):
    _seed(db_session)
    svc = UserAgentService(db_session)
    a = svc.create(user_id="mgr", operator_name="mgr", owner_type="team",
                   data={"name": "TeamBot", "system_prompt": "x"}, team_id="t1")
    aid = a["agent_id"]
    # both member and manager can see it
    assert any(x["agent_id"] == aid for x in svc.list_for_user("mem"))
    assert any(x["agent_id"] == aid for x in svc.list_for_user("mgr"))
    # non-member cannot see it
    assert not any(x["agent_id"] == aid for x in svc.list_for_user("out"))
    # non-member is denied access to details
    with pytest.raises(PermissionError):
        svc.get_by_id(aid, user_id="out")


def test_member_can_use_but_not_edit_team_agent(db_session):
    _seed(db_session)
    svc = UserAgentService(db_session)
    aid = svc.create(user_id="mgr", operator_name="mgr", owner_type="team",
                     data={"name": "TeamBot", "system_prompt": "x"}, team_id="t1")["agent_id"]
    # member can read (use)
    assert svc.get_by_id(aid, user_id="mem")["agent_id"] == aid
    # member cannot edit (owner_type='user' context)
    with pytest.raises(PermissionError):
        svc.update(aid, user_id="mem", operator_name="mem", owner_type="user", data={"name": "X"})
    # manager can edit
    updated = svc.update(aid, user_id="mgr", operator_name="mgr", owner_type="user", data={"name": "X"})
    assert updated["name"] == "X"
    # manager can delete
    assert svc.delete(aid, user_id="mgr", owner_type="user") is True


def test_disabled_team_agent_hidden_from_members_visible_to_manager(db_session):
    _seed(db_session)
    svc = UserAgentService(db_session)
    aid = svc.create(user_id="mgr", operator_name="mgr", owner_type="team",
                     data={"name": "TeamBot", "system_prompt": "x"}, team_id="t1")["agent_id"]
    svc.update(aid, user_id="mgr", operator_name="mgr", owner_type="user", data={"is_enabled": False})
    # member's list does not show disabled team agents
    assert not any(x["agent_id"] == aid for x in svc.list_for_user("mem"))
    # manager can still access it (to re-enable it)
    assert svc.get_by_id(aid, user_id="mgr")["agent_id"] == aid


def test_admin_route_cannot_edit_team_agent(db_session):
    _seed(db_session)
    svc = UserAgentService(db_session)
    aid = svc.create(user_id="mgr", operator_name="mgr", owner_type="team",
                     data={"name": "TeamBot", "system_prompt": "x"}, team_id="t1")["agent_id"]
    # Admin backend route (owner_type='admin') can only edit admin agents, not team agents
    with pytest.raises(PermissionError):
        svc.update(aid, user_id=None, operator_name="管理员", owner_type="admin", data={"name": "X"})
