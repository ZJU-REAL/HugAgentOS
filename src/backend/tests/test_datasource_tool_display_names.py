"""Data-source tools (``<prefix>__<suffix>``) get display names from their suffix."""

from core.agent_runtime.events import _synthesize_missing_tool_call
from core.config.display_names import tool_display_name


def test_suffix_resolves_for_any_datasource_prefix():
    assert tool_display_name("sales_db__execute_sql") == "执行 SQL 查询"
    assert tool_display_name("ds_1a2b__list_tables") == "查看库表"
    assert tool_display_name("ds_1a2b__describe_table") == "查看表结构"
    assert tool_display_name("logs__get_business_context") == "读取数据字典"
    assert tool_display_name("logs__search") == "检索 Elasticsearch"


def test_exact_names_win_and_unknown_names_pass_through():
    assert tool_display_name("web_fetch") == "网页抓取"
    assert tool_display_name("sales_db__unknown_suffix") == "sales_db__unknown_suffix"
    # Not the data-source shape (upper-case / over-long prefix): left untouched.
    assert tool_display_name("Mcp__search") == "Mcp__search"
    assert tool_display_name("x" * 41 + "__search") == "x" * 41 + "__search"
    # The old DBHub exact names are gone.
    assert tool_display_name("execute_sql") == "execute_sql"


def test_suffix_name_reaches_streamed_tool_call_events():
    evt = _synthesize_missing_tool_call("call_1", "sales_db__execute_sql", set())
    assert evt["tool_display_name"] == "执行 SQL 查询"
