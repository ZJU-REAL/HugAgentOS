"""只读作用域（团队项目）下的写类操作必须**在动手之前**拒绝。

回归的是这条真实故障：``sync_upsert`` 遇到只读作用域返回 ``None``，而 ``None`` 在它的
契约里表示"同步失败"、约定"同步失败不要拦住写入本身"。两件事撞在一个返回值上，于是
Write 照样把字节写进了用户的镜像目录、还回 ``ok: True`` —— 模型以为团队文件存好了，
团队里没有、个人空间里也看不见，磁盘上多一个孤儿文件。
"""

from core.llm.tools._common import myspace_mutation_refusal
from core.services.project_scope import ProjectScope


def _team_scope():
    return ProjectScope(
        project_id="proj_x",
        kind="team",
        root_folder_id="tfld_x",
        folder_name="团队资料",
        team_id="team_x",
    )


def _personal_scope():
    return ProjectScope(
        project_id="proj_y",
        kind="personal",
        root_folder_id="ufld_y",
        folder_name="我的项目",
    )


def test_team_scope_refuses_write_with_a_clear_message():
    refusal = myspace_mutation_refusal(_team_scope(), "/myspace/团队资料/x.txt", "写入文件")
    assert refusal is not None
    assert "写入文件" in refusal["error"]
    assert "/myspace/团队资料/x.txt" in refusal["error"]


def test_team_scope_refuses_edit():
    assert myspace_mutation_refusal(_team_scope(), "/myspace/团队资料/x.txt", "编辑文件") is not None


def test_personal_project_scope_is_not_refused():
    """个人项目照常可写——只读限制只针对团队等组织范围。"""
    assert myspace_mutation_refusal(_personal_scope(), "/myspace/我的项目/x.txt", "写入文件") is None


def test_no_scope_is_not_refused():
    assert myspace_mutation_refusal(None, "/myspace/x.txt", "写入文件") is None


def test_write_tool_checks_before_writing_any_bytes():
    """拒绝必须早于落盘：晚一步就会在用户镜像目录里留下孤儿文件。"""
    import inspect

    from core.llm.tools import write_tool

    src = inspect.getsource(write_tool)
    guard = src.index("myspace_mutation_refusal(scope, file_path")
    write = src.index("provider.put_file(_sess, physical, new_bytes")
    assert guard < write, "只读作用域的拒绝必须发生在写入之前"
