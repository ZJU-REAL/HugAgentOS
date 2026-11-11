"""Memory panel routes: pages, the write log with revert, snapshots with restore, lint."""
import pytest

import personal_agent.workspace.files as _pa_files
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit
from personal_agent.memory import layout, seed, snapshots, state, store
from personal_agent.models import PersonalAgent
from tests.test_personal_agent_routing import sessions, personal_agent_on  # noqa: F401

BASE = "/v1/personal-agent/memory"


def good_page(name="person-zhang-san", src="conv-x", extra="", status="active"):
    return ("---\nname: " + name + "\ndescription: 用户的直属上级；审批前读\naliases: [\"张三\"]\n"
            "type: " + name.split("-")[0] + "\nstatus: " + status + "\nupdated: 2026-11-09\n---\n"
            "## 角色\n- 直属上级 [src: " + src + " | as-of: 2026-10-02]\n" + extra)


@pytest.fixture
def agent(sessions, monkeypatch, tmp_path):
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    with sessions() as db:
        view = AgentContext(db, "owner").identity.ensure()
        message = admit(AgentContext(db, "owner"), "张三是我直属领导", "memory-src")
        # The panel never seeds the persona files; this keeps ensure_seeded out of the way.
        row = db.get(PersonalAgent, view["id"])
        row.config = {**row.config, "workspace_seeded": True}
        db.commit()
    _pa_files.local_workspace(view["id"]).mkdir(parents=True, exist_ok=True)
    return view["id"], "conv-" + message["message_id"]


@pytest.fixture
def client(sessions, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.middleware.error_handler import setup_error_handlers
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db
    from personal_agent.api.agent import router

    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(router)

    def database():
        with sessions() as db: yield db
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="owner", user_center_id="owner", username="owner")
    with TestClient(app) as test_client:
        test_client.app_ref = app
        yield test_client


def as_user(client, uid):
    from core.auth.backend import UserContext, get_current_user
    client.app_ref.dependency_overrides[get_current_user] = lambda: UserContext(user_id=uid, user_center_id=uid, username=uid)


def write(sessions, ident, rel, text, writer=layout.ONLINE, src=None):
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        saved = store.write(db, row, rel, text, None, writer=writer, source_id=src)
        db.commit()
        return saved["version"].id


def test_new_user_sees_seeded_empty_wiki(agent, client):
    data = client.get(BASE + "/pages").json()["data"]
    assert data["available"] and data["pages"] == [] and data["where_to_start"] == ""
    assert [t["label"] for t in data["types"]] == ["人", "组织", "项目", "主题与决定", "做法"]
    ident, _ = agent
    assert store.read(ident, layout.SCHEMA) == seed.schema_text()
    # Only the seeding baselines: the files injected every turn get a version to check against.
    items = client.get(BASE + "/versions").json()["data"]["items"]
    assert {item["writer"] for item in items} <= {layout.SYSTEM} and not any(item["revertable"] for item in items)
    assert client.get(BASE + "/snapshots").json()["data"]["items"] == []


def test_pages_describe_type_status_sources_and_pending(agent, client, sessions):
    ident, src = agent
    client.get(BASE + "/pages")
    write(sessions, ident, "memory/person-zhang-san.md", good_page(src=src), src=src)
    write(sessions, ident, "memory/project-q4-budget.md", good_page("project-q4-budget", src, status="archived"), layout.USER_EDIT)
    index = store.read(ident, layout.INDEX)
    with sessions() as db:
        store.write(db, db.get(PersonalAgent, ident), layout.INDEX,
                    index.replace("## Where to start", "## Where to start\n- 先看 [[person-zhang-san]]"), None, writer=layout.SYSTEM)
        db.commit()
    data = client.get(BASE + "/pages").json()["data"]
    pages = {p["name"]: p for p in data["pages"]}
    assert pages["person-zhang-san"]["type"] == "person" and pages["person-zhang-san"]["sources"] == 1
    assert pages["person-zhang-san"]["pending"] and pages["person-zhang-san"]["aliases"] == ["张三"]
    assert pages["person-zhang-san"]["path"] == "/workspace/memory/person-zhang-san.md"
    assert pages["project-q4-budget"]["status"] == "archived"
    assert data["where_to_start"] == "- 先看 [[person-zhang-san]]"
    assert "## 待整理（2 页）" in data["index_head"]


def test_versions_filter_detail_and_revert(agent, client, sessions):
    ident, src = agent
    client.get(BASE + "/pages")
    rel = "memory/person-zhang-san.md"
    write(sessions, ident, rel, good_page(src=src), src=src)
    changed = write(sessions, ident, rel, good_page(src=src, extra="- 爱喝茶 [src: " + src + " | as-of: 2026-10-03]\n"), layout.USER_EDIT)

    listed = client.get(BASE + "/versions", params={"path": "/workspace/" + rel}).json()["data"]
    assert [v["writer"] for v in listed["items"]] == ["user", "online"] and not listed["has_more"]
    assert listed["items"][0]["revertable"] and not listed["items"][1]["revertable"]
    assert client.get(BASE + "/versions", params={"source_id": src}).json()["data"]["items"][0]["source_id"] == src
    paged = client.get(BASE + "/versions", params={"page_size": 1}).json()["data"]
    assert len(paged["items"]) == 1 and paged["has_more"]
    assert client.get(BASE + "/versions", params={"writer": "nobody"}).status_code == 400

    detail = client.get(BASE + "/versions/" + changed).json()["data"]
    assert "爱喝茶" in detail["after"] and "爱喝茶" not in detail["before"]

    assert client.post(BASE + "/versions/" + changed + "/revert").status_code == 200
    assert store.read(ident, rel) == good_page(src=src)
    again = client.post(BASE + "/versions/" + changed + "/revert")
    assert again.status_code == 400 and "撤销过" in again.json()["message"]
    first = listed["items"][1]["id"]
    write(sessions, ident, rel, good_page(src=src, extra="- 新事实 [src: " + src + " | as-of: 2026-10-04]\n"), layout.USER_EDIT)
    stale = client.post(BASE + "/versions/" + first + "/revert")
    assert stale.status_code == 400 and "手动修改" in stale.json()["message"]
    assert client.post(BASE + "/versions/pamv_missing/revert").status_code == 404


def test_snapshot_restore_refused_while_dreaming_then_restores(agent, client, sessions):
    ident, src = agent
    client.get(BASE + "/pages")
    write(sessions, ident, "memory/person-zhang-san.md", good_page(src=src), src=src)
    snapshots.take(ident, "before")
    write(sessions, ident, "memory/project-q4-budget.md", good_page("project-q4-budget", src), src=src)
    items = client.get(BASE + "/snapshots").json()["data"]["items"]
    assert [s["id"] for s in items] == ["before"] and items[0]["files"] == 3

    with sessions() as db:
        row = db.get(PersonalAgent, ident); state.hold(row, "run-1"); db.commit()
    busy = client.post(BASE + "/snapshots/before/restore")
    assert busy.status_code == 409 and "Dreaming" in busy.json()["message"]
    with sessions() as db:
        row = db.get(PersonalAgent, ident); state.release(row, "run-1"); db.commit()

    done = client.post(BASE + "/snapshots/before/restore").json()["data"]
    assert "memory/project-q4-budget.md" in done["changed"]
    assert store.read(ident, "memory/project-q4-budget.md") is None
    writers = {v["writer"] for v in client.get(BASE + "/versions").json()["data"]["items"][:2]}
    assert writers == {"system"}
    assert client.post(BASE + "/snapshots/missing/restore").status_code == 404
    assert client.post(BASE + "/snapshots/bad%20id/restore").status_code == 400


def test_lint_reports_findings(agent, client, sessions):
    ident, src = agent
    client.get(BASE + "/pages")
    write(sessions, ident, "memory/person-zhang-san.md", good_page(src=src, extra="见 [[org-nowhere]]\n"), src=src)
    data = client.get(BASE + "/lint").json()["data"]
    rules = {f["rule"] for f in data["findings"]}
    assert "broken-link" in rules and data["warnings"] >= 1


def test_routes_are_owner_scoped(agent, client, sessions):
    ident, src = agent
    client.get(BASE + "/pages")
    version = write(sessions, ident, "memory/person-zhang-san.md", good_page(src=src), src=src)
    as_user(client, "other")
    assert client.get(BASE + "/versions/" + version).status_code == 404
    assert client.post(BASE + "/versions/" + version + "/revert").status_code == 404
    assert client.get(BASE + "/versions").json()["data"]["items"] == []
