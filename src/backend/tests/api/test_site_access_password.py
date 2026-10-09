"""站点访问密码：凭据签发/校验 + 托管路由上的密码闸。"""

import time

import pytest
from api.routes import sites_serve
from core.services.site_rate_limit import UNLOCK_LIMIT
from core.db.engine import get_db
from core.db.models import UserShadow
from core.services import site_password
from core.services.site_service import SiteService
from fastapi import FastAPI
from fastapi.testclient import TestClient

FILES = [("index.html", b"<h1>secret</h1>"), ("app.js", b"console.log(1)")]


@pytest.fixture()
def owner(db_session):
    user = UserShadow(user_id="pw_owner", username="pw_owner")
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture()
def svc(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    return SiteService(db_session)


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setattr("core.services.site_rate_limit.redis_configured", lambda: False)
    from types import SimpleNamespace
    monkeypatch.setattr("core.services.site_rate_limit.settings", SimpleNamespace(deploy=SimpleNamespace(is_local=True)))
    app = FastAPI()
    app.include_router(sites_serve.router)
    app.dependency_overrides[get_db] = lambda: db_session
    from core.services import site_rate_limit
    site_rate_limit._rate_buckets.clear()
    site_rate_limit._unlock_buckets.clear()
    return TestClient(app)


def _site(svc, owner, **kwargs):
    return svc.publish(user_id=owner.user_id, files=FILES, title="Secret", **kwargs)


# ── 密码存储 ────────────────────────────────────────────────────

def test_password_is_stored_hashed_and_can_be_cleared(svc, owner):
    site = _site(svc, owner)
    assert site.access_password_hash is None

    site = svc.set_access_password(site.site_id, owner.user_id, "open-sesame")
    assert site.access_password_hash
    assert "open-sesame" not in site.access_password_hash
    assert site_password.check_access_password(site, "open-sesame")
    assert not site_password.check_access_password(site, "wrong")

    site = svc.clear_access_password(site.site_id, owner.user_id)
    assert site.access_password_hash is None


def test_short_password_is_rejected(svc, owner):
    from core.infra.exceptions import BadRequestError

    site = _site(svc, owner)
    with pytest.raises(BadRequestError):
        svc.set_access_password(site.site_id, owner.user_id, "ab")


def test_non_owner_cannot_touch_the_password(svc, owner, db_session):
    db_session.add(UserShadow(user_id="stranger", username="stranger"))
    db_session.commit()
    site = _site(svc, owner)
    from core.infra.exceptions import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        svc.set_access_password(site.site_id, "stranger", "open-sesame")


# ── 访问凭据 ────────────────────────────────────────────────────

def test_token_round_trip_and_rejections(svc, owner):
    site = svc.set_access_password(
        _site(svc, owner).site_id, owner.user_id, "open-sesame"
    )
    token = site_password.issue_access_token(site)
    assert site_password.verify_access_token(site, token)
    assert not site_password.verify_access_token(site, "garbage")
    assert not site_password.verify_access_token(site, token + "x")
    assert not site_password.verify_access_token(site, None)

    expired = f"{int(time.time()) - 1}.{token.split('.')[1]}"
    assert not site_password.verify_access_token(site, expired)


def test_changing_the_password_invalidates_issued_tokens(svc, owner):
    site = svc.set_access_password(
        _site(svc, owner).site_id, owner.user_id, "open-sesame"
    )
    token = site_password.issue_access_token(site)
    rotated = svc.set_access_password(site.site_id, owner.user_id, "another-one")
    assert not site_password.verify_access_token(rotated, token)


def test_token_is_bound_to_its_own_site(svc, owner):
    first = svc.set_access_password(_site(svc, owner).site_id, owner.user_id, "same-pass")
    second = _site(svc, owner)
    # 两个站点密码相同也不能互相解锁：签名把 site_id 一起算进去了。
    second.access_password_hash = first.access_password_hash
    assert not site_password.verify_access_token(
        second, site_password.issue_access_token(first)
    )


# ── 托管路由上的闸门 ────────────────────────────────────────────

def test_visitor_gets_the_gate_page_then_the_site(svc, owner, client):
    site = svc.set_access_password(
        _site(svc, owner).site_id, owner.user_id, "open-sesame"
    )

    locked = client.get(f"/site/{site.slug}/", headers={"accept-language": "zh-CN,zh;q=0.9"})
    assert locked.status_code == 401
    assert "需要访问密码" in locked.text
    assert f"/site/{site.slug}/__api/access" in locked.text
    assert b"secret" not in locked.content
    # 英文访客拿到同一个页面的英文版
    assert "Password required" in client.get(
        f"/site/{site.slug}/", headers={"accept-language": "en-US,en;q=0.9"}
    ).text
    # 验证页自己不能被 sandbox，否则拿不到解锁 cookie
    assert "Content-Security-Policy" not in locked.headers

    assert client.post(f"/site/{site.slug}/__api/access", json={"password": "nope"}).status_code == 401
    unlocked = client.post(f"/site/{site.slug}/__api/access", json={"password": "open-sesame"})
    assert unlocked.status_code == 200
    assert site_password.ACCESS_COOKIE_NAME in unlocked.cookies

    opened = client.get(f"/site/{site.slug}/")
    assert opened.status_code == 200
    assert "secret" in opened.text
    # 资源文件同样跟着解锁
    assert client.get(f"/site/{site.slug}/app.js").status_code == 200


def test_subresources_get_an_empty_401_not_the_whole_page(svc, owner, client):
    """图片/脚本拿到一整页 HTML 只会被丢掉，回空 401 就够。"""
    site = svc.set_access_password(
        _site(svc, owner).site_id, owner.user_id, "open-sesame"
    )
    asset = client.get(f"/site/{site.slug}/app.js", headers={"sec-fetch-dest": "script"})
    assert asset.status_code == 401
    assert asset.content == b""
    document = client.get(f"/site/{site.slug}/", headers={"sec-fetch-dest": "document"})
    assert document.status_code == 401
    assert "Password required" in document.text


def test_unlock_attempts_are_rate_limited(svc, owner, client):
    site = svc.set_access_password(
        _site(svc, owner).site_id, owner.user_id, "open-sesame"
    )
    codes = [
        client.post(f"/site/{site.slug}/__api/access", json={"password": "nope"}).status_code
        for _ in range(UNLOCK_LIMIT + 2)
    ]
    assert codes[: UNLOCK_LIMIT] == [401] * UNLOCK_LIMIT
    assert codes[-1] == 429


def test_manager_is_handed_a_credential_so_assets_skip_the_lookup(
    svc, owner, client, monkeypatch
):
    """管理者免密进入后要拿到凭据，否则同一页的每个资源都要重查一次会话 + 权限。"""
    site = svc.set_access_password(
        _site(svc, owner).site_id, owner.user_id, "open-sesame"
    )

    async def _as_owner(_request):
        return owner.user_id

    monkeypatch.setattr("api.deps._resolve_session_user_id", _as_owner)
    opened = client.get(f"/site/{site.slug}/")
    assert opened.status_code == 200
    assert "secret" in opened.text
    assert site_password.ACCESS_COOKIE_NAME in opened.cookies


def test_manager_skips_the_gate_but_visitors_do_not(svc, owner, db_session):
    db_session.add(UserShadow(user_id="pw_stranger", username="pw_stranger"))
    db_session.commit()
    site = svc.set_access_password(
        _site(svc, owner).site_id, owner.user_id, "open-sesame"
    )
    assert svc.authorize_access(site, owner.user_id, None)
    assert not svc.authorize_access(site, "pw_stranger", None)
    assert not svc.authorize_access(site, None, None)
    assert svc.authorize_access(site, None, site_password.issue_access_token(site))


def test_management_endpoints_set_and_clear_the_password(svc, owner, db_session):
    """站点管理面板走的就是这两个接口——顺带确认 DELETE 没被 /{site_id} 抢走路由。"""
    from api.routes.v1 import sites as sites_routes
    from core.auth.backend import UserContext, get_current_user

    app = FastAPI()
    app.include_router(sites_routes.router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=owner.user_id, user_center_id=owner.user_id, username=owner.user_id
    )
    api = TestClient(app)
    site = _site(svc, owner)

    assert api.get(f"/v1/sites/{site.site_id}").json()["data"]["has_password"] is False

    created = api.put(f"/v1/sites/{site.site_id}/password", json={"password": "open-sesame"})
    assert created.status_code == 200
    assert created.json()["data"]["has_password"] is True
    assert "open-sesame" not in created.text

    cleared = api.delete(f"/v1/sites/{site.site_id}/password")
    assert cleared.status_code == 200
    assert cleared.json()["data"]["has_password"] is False
    # 站点本身还在，没被当成删除站点
    assert api.get(f"/v1/sites/{site.site_id}").status_code == 200


def test_site_without_password_is_untouched(svc, owner, client):
    site = _site(svc, owner)
    response = client.get(f"/site/{site.slug}/")
    assert response.status_code == 200
    assert "secret" in response.text
    assert "sandbox" in response.headers.get("Content-Security-Policy", "")


def test_unlocked_site_drops_the_sandbox_so_assets_keep_the_cookie(svc, owner, client):
    """沙箱把文档放到不透明源上，浏览器就不再给它的子资源带 SameSite cookie——
    密码站点若还套 sandbox，解锁后脚本样式会全部 401、页面白屏。
    """
    site = svc.set_access_password(
        _site(svc, owner).site_id, owner.user_id, "open-sesame"
    )
    unlocked = client.post(
        f"/site/{site.slug}/__api/access", json={"password": "open-sesame"}
    )
    assert unlocked.status_code == 200

    page = client.get(f"/site/{site.slug}/")
    assert page.status_code == 200
    assert "Content-Security-Policy" not in page.headers
    asset = client.get(f"/site/{site.slug}/app.js", headers={"sec-fetch-dest": "script"})
    assert asset.status_code == 200
    assert "Content-Security-Policy" not in asset.headers

    svc.clear_access_password(site.site_id, owner.user_id)
    assert "sandbox" in client.get(f"/site/{site.slug}/").headers.get(
        "Content-Security-Policy", ""
    )
