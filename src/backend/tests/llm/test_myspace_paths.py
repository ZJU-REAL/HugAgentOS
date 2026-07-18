"""Pure-logic unit tests for the MySpace path mapping layer (no DB touched)."""

from core.llm.tools.myspace_vfs import myspace_rel, split_rel


def test_myspace_rel_logical_root():
    assert myspace_rel("/myspace", "u1") == ""
    assert myspace_rel("/myspace/", "u1") == ""


def test_myspace_rel_logical_nested():
    assert myspace_rel("/myspace/a/b.txt", "u1") == "a/b.txt"
    assert myspace_rel("/myspace/报告/2024/x.docx", "u1") == "报告/2024/x.docx"


def test_myspace_rel_physical():
    assert myspace_rel("/workspace/myspace/u1/x/y.txt", "u1") == "x/y.txt"
    assert myspace_rel("/workspace/myspace/u1", "u1") == ""


def test_myspace_rel_non_myspace_is_none():
    assert myspace_rel("/workspace/scratch/t.py", "u1") is None
    assert myspace_rel("/workspace/other.txt", "u1") is None
    assert myspace_rel("", "u1") is None
    # physical path's uid doesn't match the current user -> doesn't count as that user's myspace
    assert myspace_rel("/workspace/myspace/u2/x.txt", "u1") is None


def test_split_rel():
    assert split_rel("a/b/c.txt") == (["a", "b"], "c.txt")
    assert split_rel("c.txt") == ([], "c.txt")
    assert split_rel("") == ([], None)
    assert split_rel("/") == ([], None)
    assert split_rel("a//b.txt") == (["a"], "b.txt")
