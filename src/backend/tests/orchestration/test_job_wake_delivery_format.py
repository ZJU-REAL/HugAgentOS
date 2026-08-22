"""终态唤醒的交付段必须把「按用户指定的格式产出」写成硬要求。

背景：工作流模式（``run_job``）把 N 个工作项丢后台跑完之后，交付轮是由
``job_wakeup._wake_prompt`` 拼出来的一条系统口吻消息唤醒的。这条消息过去只说
「产出用户要的文件，并如实报告覆盖率」——对**产物形态**一个字没提。

后果是可预期的：那一轮模型满脑子都是台账 JSONL 和 python 脚本，最省事的路径永远是
``openpyxl`` 裸拼一个文件、或者干脆把 markdown 表格贴回复里，而用户在会话最开头指定的
「我要一份 .xlsx，列是 A/B/C」早被挤到上下文很远的地方。历史其实就在同一个会话里，
缺的只是一句"回去看一眼用户原话"。

这里把交付段的三件事钉住：
1. 明确要求回看用户原始要求、按他指定的格式交付；
2. 明确禁止用更省事的形式兜底（csv 顶替 xlsx、表格贴回复、停在中间产物）；
3. 交付前回读自查。

措辞可以改，但凡是这三层意思被整段删掉，这个测试就该红——那是把交付质量重新交回给
模型自由裁量。
"""

from core.db.models import Job as JobModel
from orchestration.job_wakeup import _wake_prompt

_SETTLED = {"total": 265, "done": 261, "not_found": 4, "failed": 0, "pending": 0, "remaining": 0}


def _prompt(stats=None):
    job = JobModel(job_id="job_1", name="补全展品信息", status="completed")
    return _wake_prompt(job, stats or _SETTLED, None)


def test_delivery_section_defers_to_the_user_requested_format():
    prompt = _prompt()
    assert "最终交付" in prompt
    assert "用户原话" in prompt, "交付段没要求回看用户原始要求，格式全凭模型自由发挥"
    assert ".xlsx" in prompt, "没有列举具体产物格式，「按用户指定格式」落不到实处"


def test_delivery_section_forbids_cheaper_substitutes():
    prompt = _prompt()
    assert ".csv" in prompt, "没堵住「要 xlsx 却给 csv」这条最省事的兜底路径"
    assert "pin_to_workspace" in prompt, "没说文件要真的交到用户手上"


def test_delivery_section_requires_a_readback_before_handing_over():
    assert "回读" in _prompt(), "交付前不回读自查，格式/字段对不对没人验"


def test_unsettled_jobs_still_get_the_resume_branch_not_the_delivery_one():
    """还有未结算项时是续跑分支，不该冒出交付要求——否则模型会拿半份数据去交差。"""
    prompt = _prompt({"total": 265, "done": 200, "remaining": 65})
    assert "resume" in prompt
    assert "最终交付" not in prompt
