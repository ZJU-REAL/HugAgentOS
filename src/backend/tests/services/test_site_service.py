"""Site hosting SiteService unit tests (publish / fetch file / slug / quota / delete)."""

import pytest

from core.db.models import UserShadow
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.services import site_service as ss
from core.services.site_service import SiteService, normalize_rel_path, guess_site_mime


@pytest.fixture()
def user(db_session):
    u = UserShadow(user_id="site_tester", username="site_tester")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def svc(db_session, tmp_path, monkeypatch):
    # Store into tmp_path to avoid polluting the real STORAGE_PATH
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    # LocalStorageBackend needs resetting if it is a module-level singleton cache; if get_storage builds a fresh one each time, no effect
    return SiteService(db_session)


BASIC_FILES = [
    ("index.html", b"<h1>hi</h1>"),
    ("css/style.css", b"h1{}"),
]


def test_publish_and_resolve(svc, user):
    site = svc.publish(user_id=user.user_id, files=BASIC_FILES, title="T1")
    assert site.slug.startswith("s-")
    assert site.current_version == 1
    assert site.file_count == 2

    got = svc.resolve_site_file(site, "")
    assert got is not None
    content, mime = got
    assert content == b"<h1>hi</h1>"
    assert mime.startswith("text/html")

    got = svc.resolve_site_file(site, "css/style.css")
    assert got[0] == b"h1{}"
    assert got[1].startswith("text/css")

    # SPA fallback: an extensionless path falls back to the entry file
    got = svc.resolve_site_file(site, "some/route")
    assert got[0] == b"<h1>hi</h1>"

    # A non-existent path with an extension → None
    assert svc.resolve_site_file(site, "nope.png") is None


def test_publish_new_version_keeps_url(svc, user):
    site = svc.publish(user_id=user.user_id, files=BASIC_FILES, title="T2", slug="my-site")
    s2 = svc.publish(
        user_id=user.user_id,
        files=[("index.html", b"v2")],
        title="T2v2",
        site_id=site.site_id,
    )
    assert s2.slug == "my-site"
    assert s2.current_version == 2
    assert svc.resolve_site_file(s2, "")[0] == b"v2"
    versions = (s2.extra_data or {}).get("versions")
    assert [v["version"] for v in versions] == [1, 2]


def test_slug_rules(svc, user):
    with pytest.raises(BadRequestError):
        svc.publish(user_id=user.user_id, files=BASIC_FILES, title="x", slug="AB")
    with pytest.raises(BadRequestError):
        svc.publish(user_id=user.user_id, files=BASIC_FILES, title="x", slug="api")
    svc.publish(user_id=user.user_id, files=BASIC_FILES, title="x", slug="taken-slug")
    with pytest.raises(BadRequestError):
        svc.publish(user_id=user.user_id, files=BASIC_FILES, title="x", slug="taken-slug")


def test_entry_file_required(svc, user):
    with pytest.raises(BadRequestError):
        svc.publish(user_id=user.user_id, files=[("a.css", b"x")], title="x")
    # A sole html at the root can serve as the entry
    site = svc.publish(user_id=user.user_id, files=[("main.html", b"<p>m</p>")], title="x")
    assert site.entry_file == "main.html"
    assert svc.resolve_site_file(site, "")[0] == b"<p>m</p>"


def test_path_safety():
    assert normalize_rel_path("../etc/passwd") is None
    # After stripping the leading slash it is treated as an in-site path (request paths naturally have no leading /)
    assert normalize_rel_path("/abs") == "abs"
    assert normalize_rel_path("a/../../b") is None
    assert normalize_rel_path("a\\b") is None
    assert normalize_rel_path("a/./b") == "a/b"


def test_limits(svc, user, monkeypatch):
    monkeypatch.setattr(ss, "MAX_SITE_FILES", 2)
    with pytest.raises(BadRequestError):
        svc.publish(
            user_id=user.user_id,
            files=[("index.html", b"x"), ("a.js", b"x"), ("b.js", b"x")],
            title="x",
        )


def test_delete_frees_slug_and_permission(svc, user, db_session):
    site = svc.publish(user_id=user.user_id, files=BASIC_FILES, title="del", slug="del-me")
    with pytest.raises(ResourceNotFoundError):
        svc.delete_site(site.site_id, "another_user")
    svc.delete_site(site.site_id, user.user_id)
    assert svc.repo.get_by_slug("del-me") is None
    # slug is freed and can be claimed again
    again = svc.publish(user_id=user.user_id, files=BASIC_FILES, title="del2", slug="del-me")
    assert again.slug == "del-me"


def test_mime_guess():
    assert guess_site_mime("a.js").startswith("text/javascript")
    assert guess_site_mime("a.svg").startswith("image/svg+xml")
    assert guess_site_mime("a.woff2") == "font/woff2"
    assert guess_site_mime("a.bin") == "application/octet-stream"


# ── Team visibility / rollback / KV / forms (roadmap 1/2/4/5) ────────────────

@pytest.fixture()
def team(db_session, user):
    from core.db.models import Team, TeamMember, UserShadow

    mate = UserShadow(user_id="site_mate", username="site_mate")
    outsider = UserShadow(user_id="site_outsider", username="site_outsider")
    t = Team(team_id="team_site_test", name="站点测试团队")
    db_session.add_all([mate, outsider, t])
    db_session.flush()
    db_session.add_all([
        TeamMember(team_id=t.team_id, user_id=user.user_id, role="owner"),
        TeamMember(team_id=t.team_id, user_id="site_mate", role="member"),
    ])
    db_session.commit()
    return t


def test_team_visibility_authorize(svc, user, team):
    site = svc.publish(
        user_id=user.user_id, files=BASIC_FILES, title="team site", visibility="team",
    )
    assert site.team_id == team.team_id  # when no team_id is passed, take the first team
    assert svc.authorize_view(site, user.user_id) is True       # site owner
    assert svc.authorize_view(site, "site_mate") is True        # team member
    assert svc.authorize_view(site, "site_outsider") is False   # non-member
    assert svc.authorize_view(site, None) is False              # anonymous


def test_team_visibility_requires_membership(svc, user, team):
    with pytest.raises(BadRequestError):
        svc.publish(
            user_id="site_outsider", files=BASIC_FILES, title="x",
            visibility="team", team_id=team.team_id,
        )


def test_rollback(svc, user):
    site = svc.publish(user_id=user.user_id, files=[("index.html", b"v1")], title="rb")
    svc.publish(user_id=user.user_id, files=[("index.html", b"v2")], title="rb", site_id=site.site_id)
    s3 = svc.publish(user_id=user.user_id, files=[("index.html", b"v3")], title="rb", site_id=site.site_id)
    assert s3.current_version == 3

    rolled = svc.rollback(site.site_id, user.user_id, 2)
    assert rolled.current_version == 2
    assert svc.resolve_site_file(rolled, "")[0] == b"v2"
    # Publishing again after rollback → version number continues from the historical max (4, not 3)
    s4 = svc.publish(user_id=user.user_id, files=[("index.html", b"v4")], title="rb", site_id=site.site_id)
    assert s4.current_version == 4
    with pytest.raises(BadRequestError):
        svc.rollback(site.site_id, user.user_id, 99)


def test_kv(svc, user):
    site = svc.publish(user_id=user.user_id, files=BASIC_FILES, title="kv")
    assert svc.kv_get(site, "score") is None
    svc.kv_set(site, "score", "42")
    assert svc.kv_get(site, "score") == "42"
    svc.kv_set(site, "score", "43")
    assert svc.kv_get(site, "score") == "43"
    assert svc.kv_delete(site, "score") is True
    assert svc.kv_get(site, "score") is None
    with pytest.raises(BadRequestError):
        svc.kv_set(site, "bad key!", "x")
    with pytest.raises(BadRequestError):
        svc.kv_set(site, "big", "x" * 5000)


def test_form_submissions_and_export(svc, user, db_session):
    site = svc.publish(user_id=user.user_id, files=BASIC_FILES, title="表单站")
    svc.submit_form(site, "contact", {"name": "张三", "msg": "你好"}, client_ip="1.2.3.4")
    svc.submit_form(site, "contact", {"name": "李四", "phone": "138"})
    items, total = svc.repo.submission_list(site.site_id)
    assert total == 2
    with pytest.raises(BadRequestError):
        svc.submit_form(site, "bad key!", {"a": 1})
    with pytest.raises(BadRequestError):
        svc.submit_form(site, "contact", {})

    result = svc.export_submissions_to_artifact(site.site_id, user.user_id)
    assert result["rows"] == 2
    assert result["filename"].endswith(".csv")
    from core.db.models import Artifact

    row = db_session.query(Artifact).filter(
        Artifact.artifact_id == result["artifact_id"]
    ).first()
    assert row is not None and row.user_id == user.user_id


def test_reserved_api_prefix(svc, user):
    with pytest.raises(BadRequestError):
        svc.publish(
            user_id=user.user_id,
            files=[("index.html", b"x"), ("__api/kv.js", b"x")],
            title="x",
        )


def test_view_count_increment(svc, user, db_session):
    site = svc.publish(user_id=user.user_id, files=BASIC_FILES, title="pv")
    svc.repo.increment_view(site.site_id)
    svc.repo.increment_view(site.site_id)
    db_session.expire_all()
    assert svc.repo.get_by_id(site.site_id).view_count == 2
