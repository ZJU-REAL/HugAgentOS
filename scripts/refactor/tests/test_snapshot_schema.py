from sqlalchemy import Column, Integer, MetaData, Table, UniqueConstraint, CheckConstraint, ForeignKeyConstraint
from scripts.refactor.snapshot_schema import snapshot


def test_snapshot_detects_removed_composite_unique_constraint():
    metadata = MetaData()
    table = Table("example", metadata, Column("owner", Integer), Column("name", Integer), UniqueConstraint("owner", "name"))
    before = snapshot(metadata)
    table.constraints.remove(next(c for c in table.constraints if isinstance(c, UniqueConstraint)))
    assert snapshot(metadata) != before


def test_snapshot_detects_check_foreign_key_and_default_changes():
    metadata = MetaData()
    Table("parent", metadata, Column("a", Integer, primary_key=True), Column("b", Integer, primary_key=True))
    table = Table("child", metadata, Column("a", Integer, server_default="1"), Column("b", Integer), CheckConstraint("a > 0"), ForeignKeyConstraint(["a", "b"], ["parent.a", "parent.b"], ondelete="CASCADE"))
    before = snapshot(metadata)
    table.c.a.server_default = None
    assert snapshot(metadata) != before
    foreign = next(c for c in before["child"]["constraints"] if c["type"] == "ForeignKeyConstraint")
    assert foreign["columns"] == ["a", "b"]
    assert [item["target"] for item in foreign["references"]] == ["parent.a", "parent.b"]
    assert all(item["ondelete"] == "CASCADE" for item in foreign["references"])
    assert any(c.get("sqltext") == "a > 0" for c in before["child"]["constraints"])


def test_computed_defaults_are_stable_across_metadata_instances():
    from sqlalchemy import Computed
    def schema():
        metadata = MetaData()
        Table("example", metadata, Column("a", Integer), Column("b", Integer, Computed("a + 1", persisted=True)))
        return snapshot(metadata)
    assert schema() == schema()
