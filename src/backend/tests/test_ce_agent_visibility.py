"""CE sub-agent visibility regression tests."""

from core.db.edition_tables import ce_create_all
from core.db.models import UserAgent, UserShadow
from core.services.user_agent_service import UserAgentService
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker


def test_ce_list_for_user_has_no_organization_repository_contracts():
    """A fresh CE database and repository expose only personal/admin agents."""
    from core.db.repository.agent import UserAgentRepository

    engine = create_engine("sqlite:///:memory:")
    ce_create_all(engine)
    assert inspect(engine).has_table("team_members") is False
    assert not hasattr(UserAgentRepository, "count_team_agents")
    assert not hasattr(UserAgentRepository, "list_for_team")

    session = sessionmaker(bind=engine)()
    try:
        session.add_all(
            [
                UserShadow(user_id="ce-user", username="CE User", extra_data={}),
                UserShadow(user_id="other-user", username="Other User", extra_data={}),
                UserAgent(
                    agent_id="admin-enabled",
                    owner_type="admin",
                    name="Admin Enabled",
                    system_prompt="admin",
                    is_enabled=True,
                ),
                UserAgent(
                    agent_id="admin-disabled",
                    owner_type="admin",
                    name="Admin Disabled",
                    system_prompt="admin",
                    is_enabled=False,
                ),
                UserAgent(
                    agent_id="ce-personal",
                    owner_type="user",
                    user_id="ce-user",
                    name="CE Personal",
                    system_prompt="personal",
                ),
                UserAgent(
                    agent_id="other-personal",
                    owner_type="user",
                    user_id="other-user",
                    name="Other Personal",
                    system_prompt="personal",
                ),
            ]
        )
        session.commit()
        agents = UserAgentService(session).list_for_user("ce-user")

        assert {item["agent_id"] for item in agents} == {"admin-enabled", "ce-personal"}
    finally:
        session.close()
        engine.dispose()
