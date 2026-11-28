"""已下线的模型角色（如 ``code_exec``）不再出现在角色分配里，残留行也不能卡住供应商。

代码执行改由智能体自己绑定模型后，「代码执行推理」角色从 ROLE_DEFINITIONS 删除。
老库里可能还留着 ``code_exec`` 的指派行：控制台看不到也取消不了它，所以删除供应商
或修改供应商类型时不能再把它算作"仍被引用"。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.models import ModelProvider, ModelRoleAssignment
from core.models.repository import ROLE_DEFINITIONS, list_role_assignments, provider_is_referenced


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    ModelProvider.__table__.create(engine)
    ModelRoleAssignment.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _seed(db):
    db.add(ModelProvider(
        provider_id="p1", provider_type="chat", display_name="m",
        base_url="http://x", api_key="k", model_name="m", extra_config={},
    ))
    db.flush()
    db.add(ModelRoleAssignment(role_key="code_exec", provider_id="p1"))
    db.add(ModelRoleAssignment(role_key="summarizer", provider_id="p1"))
    db.commit()


def test_code_exec_role_is_retired():
    assert "code_exec" not in ROLE_DEFINITIONS


def test_stale_retired_assignment_is_hidden_and_not_a_reference(db):
    _seed(db)
    assert "code_exec" not in {r["role_key"] for r in list_role_assignments(db)}
    assert provider_is_referenced(db, "p1") == ["summarizer"]
