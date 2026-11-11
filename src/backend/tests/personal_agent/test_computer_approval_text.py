"""What an approval card and an IM notice say about a computer action, and the computer skills every task gets."""
from personal_agent.channels import im_actions
from personal_agent.identity.capabilities import COMPUTER_SKILL_IDS, capability_ids
from personal_agent.models import PersonalDevice
from tests.test_personal_agent import personal_agent_on, sessions  # noqa: F401
from tests.test_personal_agent_im_actions import im  # noqa: F401

LONG = "这是一段很长的文字。" * 12  # 120 characters


class Agent:
    user_id = "owner"


def steps_action(target="device:mac1", **extra):
    return {"kind": "computer.steps", "target": target, "generation": 3, "app": "TextEdit", "window": "123:456",
            "snapshot_id": "s0000002a", "steps": [
                {"type": "click", "element": "s0000002a:3"},
                {"type": "type", "element": "s0000002a:5", "text": LONG},
                {"type": "hotkey", "keys": ["cmd", "s"]},
                {"type": "menu", "path": ["文件", "导出…"]},
                {"type": "scroll", "element": "s0000002a:9", "direction": "down", "amount": 3},
                {"type": "open_browser", "url": "https://example.com/a"},
                {"type": "key", "key": "enter"},
                {"type": "click", "x": 40, "y": 80}], **extra}


def add_device(db, user_id="owner", name="办公室 Mac"):
    db.add(PersonalDevice(id="mac1", user_id=user_id, name=name, platform="macos", secret_hash="0"))
    db.commit()


def test_old_single_action_still_reads_as_before():
    old = {"kind": "computer.action", "target": "cloud", "action": {"type": "click", "x": 1, "y": 2, "observation_id": "o1"}}
    assert im_actions.describe(old) == "在你的云电脑上点击 坐标 (1, 2)"
    typed = {"kind": "computer.action", "target": "device:mac1", "action": {"type": "type", "text": LONG}}
    # Without a database the paired computer has no name yet, and long text is cut to 80 characters.
    assert im_actions.describe(typed) == "在你的电脑上输入文字「" + LONG[:80] + "…」"
    lines, body = im_actions.explain(None, None, typed)
    assert lines[:2] == ["操作：输入文字「" + LONG[:80] + "…」", "设备：你的电脑"] and body == LONG


def test_steps_name_the_device_window_and_every_step(sessions):
    with sessions() as db:
        add_device(db)
        line = im_actions.describe(steps_action(), db, Agent())
        lines, body = im_actions.explain(db, Agent(), steps_action(title="未命名"))
    assert line.startswith("在你的电脑「办公室 Mac」的 TextEdit 中执行 8 步操作：1. 点击 元素 #3；2. 输入文字")
    assert lines[:5] == ["操作：电脑操作（共 8 步）", "设备：你的电脑「办公室 Mac」", "应用：TextEdit", "窗口：未命名",
                         "第 1 步：点击 元素 #3"]
    assert lines[5:12] == ["第 2 步：输入文字 元素 #5「" + LONG[:80] + "…」", "第 3 步：按组合键 cmd+s",
                           "第 4 步：选择菜单「文件 > 导出…」", "第 5 步：向下滚动 3 格 元素 #9",
                           "第 6 步：打开网页 https://example.com/a", "第 7 步：按键 enter", "第 8 步：点击 坐标 (40, 80)"]
    # The cut text is shown whole in the body, so the owner reads all of it before approving.
    assert body == "第 2 步输入的全文：\n" + LONG


def test_another_owners_device_is_not_named(sessions):
    with sessions() as db:
        add_device(db, user_id="other", name="别人的电脑")
        assert im_actions.describe(steps_action(), db, Agent()).startswith("在你的电脑的 TextEdit 中")


def test_cloud_pixels_steps_and_screenshot_reference():
    action = {"kind": "computer.steps", "target": "cloud", "window": "screen", "snapshot_id": "o1", "resource_ref": "screen_" + "a" * 30,
              "steps": [{"type": "scroll", "x": 5, "y": 6, "amount": -4}]}
    assert im_actions.describe(action) == "在你的云电脑上向下滚动 4 格 坐标 (5, 6)"
    lines, body = im_actions.explain(None, None, action)
    # No screenshot is promised: the approval card has no picture to show.
    assert lines == ["操作：向下滚动 4 格 坐标 (5, 6)", "设备：你的云电脑", "窗口：整个屏幕"] and body == ""


def test_short_text_has_no_body_and_unknown_steps_still_read():
    action = {"kind": "computer.steps", "target": "cloud", "steps": [{"type": "set_value", "element": "s00000001:2", "text": "Ada"}, {"type": "zoom"}]}
    lines, body = im_actions.explain(None, None, action)
    assert lines[-2:] == ["第 1 步：填写 元素 #2「Ada」", "第 2 步：执行操作"] and body == ""


def test_computer_skills_are_in_every_task_and_on_disk():
    from core.capabilities.skills.backends import CompositeBackend, FilesystemBackend
    from core.capabilities.skills.loader import MultiSourceSkillLoader
    from extensions.paths import SKILLS_DIR
    assert capability_ids({})[0] == list(COMPUTER_SKILL_IDS)
    assert capability_ids({"skill_ids": ["officecli-docx", "pa-computer-macos"]})[0] == ["officecli-docx", *COMPUTER_SKILL_IDS]
    # The built-in source of the skill loader, which pa_skill_read reads through.
    loader = MultiSourceSkillLoader(CompositeBackend([FilesystemBackend(root_dir=SKILLS_DIR / "default", source_name="built-in", priority=0)]))
    metadata = loader.load_all_metadata()
    from pathlib import Path
    for ident in COMPUTER_SKILL_IDS:
        assert metadata[ident].description
        text = (Path(loader.get_skill_dir(ident)) / "SKILL.md").read_text()
        assert text.startswith("---\nname: " + ident + "\n")
        for tool in ("pa_computer_read", "pa_computer_act", "pa_deliver"): assert tool in text
        assert "pa_computer_observe" not in text and "pa_computer_action" not in text


def test_task_prompt_lists_the_computer_skills(monkeypatch):
    from personal_agent.workspace import prompting
    monkeypatch.delenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", raising=False)
    monkeypatch.delenv("PERSONAL_AGENT_VOLUME_ROOT", raising=False)
    # The run's skill ids come from capability_ids, which always adds the computer skills.
    text = prompting.render(prompting.sections("pa_x", skill_ids=capability_ids({"skill_ids": ["s1"]})[0], seed_missing=False))
    assert "## Available skills\n- `s1`\n- `pa-computer-macos`" in text and "- `pa-computer-pixels`" in text
    assert "pa_computer_read" in text and "pa_computer_observe" not in text


async def test_im_notice_lists_the_steps(im):  # noqa: F811
    from sqlalchemy import select
    from core.db.models import ChatMessage
    from personal_agent.models import PersonalMessageTask
    from tests.test_personal_agent_im_actions import im_task, receive, settle_run
    user = await receive(im, "在文本编辑里写一句话", "m1")
    with im.sessions() as db:
        add_device(db)
    task_id = im_task(im, user, pending_approval={"id": "a1", "action": steps_action()})
    with im.sessions() as db:
        settle_run(db, task_id, "等待批准")
        notice = db.scalar(select(ChatMessage).join(PersonalMessageTask, PersonalMessageTask.message_id == ChatMessage.message_id)
                           .where(PersonalMessageTask.relation == "approval"))
    text = notice.content
    assert text.startswith("「") and "需要你批准一项电脑操作。\n操作：电脑操作（共 8 步）\n设备：你的电脑「办公室 Mac」\n应用：TextEdit\n" in text
    assert "第 4 步：选择菜单「文件 > 导出…」\n" in text and "回复「同意」允许这一次" in text


def test_approved_elements_are_named_as_the_read_named_them():
    from personal_agent.channels.im_actions import describe_computer, explain_computer
    from personal_agent.computer import steps as step_rules
    labels = step_rules.element_labels('- [0] AXWindow "e2e-note.txt"\n'
                                       '  - [1] AXTextArea = "private draft" [id=First Text View]\n'
                                       '  - [4] AXButton "发送" [actions=[press]]')
    # The current value of a field never becomes its name.
    assert labels == {"0": "窗口「e2e-note.txt」", "1": "文本区「First Text View」", "4": "按钮「发送」"}
    steps = [{"type": "type", "element": "s00000004:1", "text": "hi"}, {"type": "click", "element": "s00000004:4"}]
    action = {"kind": "computer.steps", "target": "cloud", "app": "TextEdit", "window": "1:2",
              "snapshot_id": "s00000004", "steps": steps, "targets": step_rules.targets_of(steps, labels)}
    text = describe_computer(action)
    assert "文本区「First Text View」" in text and "按钮「发送」" in text and "元素 #" not in text
    lines, _ = explain_computer(None, None, action)
    assert any("按钮「发送」" in line for line in lines)
    # Without names (an approval stored before this), the row number is shown as before.
    assert "元素 #1" in describe_computer({**action, "targets": None})
