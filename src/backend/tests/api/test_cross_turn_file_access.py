"""Integration-style tests for the cross-turn file access feature.

Exercises the three building blocks that together let the agent read files
from previous turns (both user-uploaded and AI-generated):

- `_collect_historical_attachments` in api/routes/v1/chats.py: queries the
  Artifact table by chat_id — covers user uploads AND AI-generated files.
- `_backfill_artifact_cache`: writes frontend-parsed `content` into
  Artifact.parsed_text + .summary so future turns skip re-parsing.
- `_build_historical_files_context` in core/llm/hooks.py: renders the list
  into a prompt block labeled by provenance.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace


# ── helpers ────────────────────────────────────────────────────────────────

def _make_msg(role, extra_data, created_at):
    return SimpleNamespace(
        role=role,
        extra_data=extra_data or {},
        created_at=created_at,
    )


def _make_art(artifact_id, user_id="user_1", summary="a summary",
              filename="file.txt", mime_type="text/plain", deleted=False,
              source="user_upload", parsed_text=None, created_at=None,
              chat_id="chat_x"):
    return SimpleNamespace(
        artifact_id=artifact_id,
        user_id=user_id,
        chat_id=chat_id,
        filename=filename,
        title=filename,
        mime_type=mime_type,
        summary=summary,
        parsed_text=parsed_text,
        parsed_at=None,
        parse_error=None,
        deleted_at=datetime.utcnow() if deleted else None,
        extra_data={"source": source},
        created_at=created_at or datetime.utcnow(),
    )


class _MockQuery:
    """Mock SQLAlchemy query. Assumes tests provide rows in ascending order;
    treats order_by() as a descending sort (matches production's .desc() call
    pattern), so .limit(N) returns the most recent N in desc order."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return _MockQuery(list(reversed(self._rows)))

    def limit(self, n):
        return _MockQuery(self._rows[:n])

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _MockSession:
    """Route queries by model: ChatMessage → messages, Artifact → artifacts."""

    def __init__(self, artifacts=None, messages=None):
        self._artifacts = artifacts or []
        self._messages = messages or []
        self.committed = False

    def query(self, model):
        try:
            from core.db.models import ChatMessage, Artifact as ArtifactModel
        except Exception:
            return _MockQuery(self._artifacts)
        if model is ChatMessage:
            return _MockQuery(self._messages)
        if model is ArtifactModel:
            return _MockQuery(self._artifacts)
        return _MockQuery([])

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── _collect_historical_attachments ────────────────────────────────────────

def test_collect_historical_includes_user_uploaded(monkeypatch):
    from api.routes.v1 import chats as chats_mod

    t0 = datetime.utcnow()
    messages = [
        _make_msg("user", {"attachments": [{"file_id": "ua_1"}]}, t0),
        _make_msg("user", {"attachments": [{"file_id": "ua_2"}]}, t0 + timedelta(seconds=5)),
    ]
    artifacts = [
        _make_art("ua_1", summary="PDF summary A", filename="report.pdf",
                  source="user_upload"),
        _make_art("ua_2", summary="XLSX summary B", filename="table.xlsx",
                  mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                  source="user_upload"),
    ]

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal",
                        lambda: _MockSession(artifacts=artifacts, messages=messages))

    result = chats_mod._collect_historical_attachments("chat_x", "user_1", set())
    assert [r["file_id"] for r in result] == ["ua_1", "ua_2"]
    assert result[0]["summary"] == "PDF summary A"
    assert result[0]["source"] == "user_upload"


def test_collect_historical_includes_ai_generated_from_assistant_messages(monkeypatch):
    """AI-generated artifacts live in assistant messages' extra_data["artifacts"]
    (not in user messages). Must be scanned too."""
    from api.routes.v1 import chats as chats_mod

    t0 = datetime.utcnow()
    messages = [
        _make_msg("user", {"attachments": [{"file_id": "ua_1"}]}, t0),
        _make_msg("assistant",
                  {"artifacts": [
                      {"file_id": "ai_rpt1", "name": "report.docx"},
                      {"file_id": "ai_chart1", "name": "chart.png"},
                  ]},
                  t0 + timedelta(seconds=5)),
    ]
    artifacts = [
        _make_art("ua_1", filename="user.pdf", source="user_upload"),
        _make_art("ai_rpt1", filename="annual_report.docx",
                  mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  summary="Q4 financial review", source="ai_generated"),
        _make_art("ai_chart1", filename="sales_chart.png",
                  mime_type="image/png", source="ai_generated"),
    ]

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal",
                        lambda: _MockSession(artifacts=artifacts, messages=messages))

    result = chats_mod._collect_historical_attachments("chat_x", "user_1", set())
    ids = [r["file_id"] for r in result]
    assert "ua_1" in ids and "ai_rpt1" in ids and "ai_chart1" in ids
    sources = {r["file_id"]: r["source"] for r in result}
    assert sources["ua_1"] == "user_upload"
    assert sources["ai_rpt1"] == "ai_generated"


def test_collect_historical_finds_imported_artifact_from_other_chat(monkeypatch):
    """Regression: when a user imports a file from 'My Space', the artifact's
    `chat_id` still points to the original chat. The scan-messages approach
    must still find it because the current chat's user message references its
    file_id."""
    from api.routes.v1 import chats as chats_mod

    t0 = datetime.utcnow()
    messages = [
        _make_msg("user",
                  {"attachments": [{"file_id": "ai_imported_rpt"}]},
                  t0),
    ]
    # Artifact's chat_id belongs to a DIFFERENT chat (where it was originally generated).
    artifacts = [
        _make_art("ai_imported_rpt", filename="report.docx",
                  summary="第四季度销售同比增长 15%",
                  source="ai_generated",
                  chat_id="some_OTHER_chat_xyz"),
    ]

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal",
                        lambda: _MockSession(artifacts=artifacts, messages=messages))

    result = chats_mod._collect_historical_attachments("chat_x", "user_1", set())
    assert [r["file_id"] for r in result] == ["ai_imported_rpt"]
    assert result[0]["summary"] == "第四季度销售同比增长 15%"


def test_collect_historical_infers_source_from_id_prefix(monkeypatch):
    from api.routes.v1 import chats as chats_mod

    t0 = datetime.utcnow()
    messages = [
        _make_msg("user", {"attachments": [{"file_id": "ua_abc"}]}, t0),
        _make_msg("assistant", {"artifacts": [{"file_id": "chart_xyz"}]}, t0 + timedelta(seconds=5)),
    ]
    art_user = _make_art("ua_abc", filename="x.pdf")
    art_user.extra_data = {}
    art_ai = _make_art("chart_xyz", filename="c.png")
    art_ai.extra_data = {}

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal",
                        lambda: _MockSession(artifacts=[art_user, art_ai], messages=messages))

    result = chats_mod._collect_historical_attachments("chat_x", "user_1", set())
    by_id = {r["file_id"]: r["source"] for r in result}
    assert by_id["ua_abc"] == "user_upload"
    assert by_id["chart_xyz"] == "ai_generated"


def test_collect_historical_excludes_current_turn_file_ids(monkeypatch):
    from api.routes.v1 import chats as chats_mod
    t0 = datetime.utcnow()
    messages = [
        _make_msg("user",
                  {"attachments": [{"file_id": "ua_1"}, {"file_id": "ua_2"}]},
                  t0),
    ]
    artifacts = [_make_art("ua_1"), _make_art("ua_2")]

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal",
                        lambda: _MockSession(artifacts=artifacts, messages=messages))

    result = chats_mod._collect_historical_attachments(
        "chat_x", "user_1", exclude_file_ids={"ua_1"}
    )
    assert [r["file_id"] for r in result] == ["ua_2"]


def test_collect_historical_dedupes_across_messages(monkeypatch):
    """Same file_id referenced in multiple messages → returned once."""
    from api.routes.v1 import chats as chats_mod
    t0 = datetime.utcnow()
    messages = [
        _make_msg("user", {"attachments": [{"file_id": "ua_1"}]}, t0),
        _make_msg("user", {"attachments": [{"file_id": "ua_1"}, {"file_id": "ua_2"}]},
                  t0 + timedelta(seconds=5)),
    ]
    artifacts = [_make_art("ua_1"), _make_art("ua_2")]

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal",
                        lambda: _MockSession(artifacts=artifacts, messages=messages))

    result = chats_mod._collect_historical_attachments("chat_x", "user_1", set())
    assert [r["file_id"] for r in result] == ["ua_1", "ua_2"]


def test_collect_historical_marks_missing_artifact_as_deleted(monkeypatch):
    """Message references a file_id but no Artifact row exists → deleted."""
    from api.routes.v1 import chats as chats_mod
    t0 = datetime.utcnow()
    messages = [_make_msg("user", {"attachments": [{"file_id": "ua_missing"}]}, t0)]

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal",
                        lambda: _MockSession(artifacts=[], messages=messages))

    result = chats_mod._collect_historical_attachments("chat_x", "user_1", set())
    assert result[0]["deleted"] is True


def test_collect_historical_hides_foreign_user_artifact(monkeypatch):
    """An artifact owned by a different user → returned as deleted (no content)."""
    from api.routes.v1 import chats as chats_mod
    t0 = datetime.utcnow()
    messages = [_make_msg("user", {"attachments": [{"file_id": "ua_foreign"}]}, t0)]
    artifacts = [_make_art("ua_foreign", user_id="other_user", summary="secret")]

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal",
                        lambda: _MockSession(artifacts=artifacts, messages=messages))

    result = chats_mod._collect_historical_attachments("chat_x", "user_1", set())
    assert result[0]["deleted"] is True
    assert "summary" not in result[0] or not result[0].get("summary")


def test_collect_historical_empty_inputs_returns_empty():
    from api.routes.v1 import chats as chats_mod
    assert chats_mod._collect_historical_attachments(None, "user_1", set()) == []
    assert chats_mod._collect_historical_attachments("", "user_1", set()) == []
    assert chats_mod._collect_historical_attachments("c", "", set()) == []


def test_collect_historical_no_messages_returns_empty(monkeypatch):
    from api.routes.v1 import chats as chats_mod
    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal",
                        lambda: _MockSession(artifacts=[], messages=[]))
    assert chats_mod._collect_historical_attachments("chat_x", "user_1", set()) == []


def test_collect_historical_honors_soft_cap(monkeypatch):
    from api.routes.v1 import chats as chats_mod
    t0 = datetime.utcnow()
    messages = [
        _make_msg("user",
                  {"attachments": [{"file_id": f"ua_{i:02d}"}]},
                  t0 + timedelta(seconds=i))
        for i in range(50)
    ]
    artifacts = [
        _make_art(f"ua_{i:02d}", summary="x" * 200, filename=f"f{i}.txt")
        for i in range(50)
    ]

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal",
                        lambda: _MockSession(artifacts=artifacts, messages=messages))

    result = chats_mod._collect_historical_attachments("chat_x", "user_1", set())
    assert 0 < len(result) < 50
    # Most recent preserved
    assert result[-1]["file_id"] == "ua_49"


def test_extract_message_file_ids_reads_both_keys():
    """Unit test for the low-level extractor: user.attachments + assistant.artifacts."""
    from api.routes.v1.chats import _extract_message_file_ids
    user_msg = _make_msg("user",
                         {"attachments": [{"file_id": "ua_1"}, {"file_id": "ua_2"}]},
                         datetime.utcnow())
    asst_msg = _make_msg("assistant",
                         {"artifacts": [{"file_id": "ai_1"}]},
                         datetime.utcnow())
    assert _extract_message_file_ids(user_msg) == ["ua_1", "ua_2"]
    assert _extract_message_file_ids(asst_msg) == ["ai_1"]
    assert _extract_message_file_ids(_make_msg("user", None, datetime.utcnow())) == []


# ── _backfill_artifact_cache ────────────────────────────────────────────────

def test_backfill_writes_parsed_text_and_summary(monkeypatch):
    from api.routes.v1 import chats as chats_mod

    art = _make_art("ua_new", parsed_text=None, summary=None, filename="doc.txt")
    sess = _MockSession([art])
    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)

    chats_mod._backfill_artifact_cache(
        [{"file_id": "ua_new", "content": "hello world from frontend parse"}],
        user_id="user_1",
    )
    assert art.parsed_text == "hello world from frontend parse"
    assert art.summary  # non-empty after build_summary_from_text
    assert sess.committed


def test_backfill_skips_when_cache_already_populated(monkeypatch):
    from api.routes.v1 import chats as chats_mod
    art = _make_art("ua_existing",
                    parsed_text="already cached",
                    summary="already summarized",
                    filename="x.txt")
    sess = _MockSession([art])
    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", lambda: sess)

    chats_mod._backfill_artifact_cache(
        [{"file_id": "ua_existing", "content": "new content ignored"}],
        user_id="user_1",
    )
    assert art.parsed_text == "already cached"  # not overwritten
    assert art.summary == "already summarized"


def test_backfill_no_attachments_is_noop(monkeypatch):
    from api.routes.v1 import chats as chats_mod
    called = {"n": 0}

    def _fake():
        called["n"] += 1
        return _MockSession([])

    import core.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "SessionLocal", _fake)

    chats_mod._backfill_artifact_cache([], "user_1")
    chats_mod._backfill_artifact_cache([{"file_id": "", "content": ""}], "user_1")
    chats_mod._backfill_artifact_cache([{"file_id": "ua_x", "content": ""}], "user_1")
    assert called["n"] == 0  # no DB touch


# ── _build_historical_files_context — source labels ───────────────────────

def test_build_historical_context_labels_source():
    from core.llm.hooks import _build_historical_files_context
    files = [
        {"file_id": "ua_1", "name": "user.pdf", "source": "user_upload", "summary": "S1"},
        {"file_id": "ai_1", "name": "ai.docx", "source": "ai_generated", "summary": "S2"},
    ]
    out = _build_historical_files_context(files)
    assert "用户上传" in out
    assert "AI 生成" in out
    assert "user.pdf" in out and "ai.docx" in out


def test_build_historical_context_includes_read_artifact_hint():
    from core.llm.hooks import _build_historical_files_context
    files = [{"file_id": "ua_1", "name": "report.pdf",
              "source": "user_upload", "summary": "annual"}]
    out = _build_historical_files_context(files)
    assert "read_artifact" in out
    assert "ua_1" in out


def test_build_historical_context_marks_deleted():
    from core.llm.hooks import _build_historical_files_context
    files = [{"file_id": "ua_1", "name": "gone.pdf", "deleted": True}]
    out = _build_historical_files_context(files)
    assert "已删除" in out or "无法读取" in out


def test_build_historical_context_empty_list_returns_empty():
    from core.llm.hooks import _build_historical_files_context
    assert _build_historical_files_context([]) == ""
