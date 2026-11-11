"""The one step vocabulary of pa_computer_act, and its translation for older computers.

A computer that reports the ``read`` and ``steps`` capabilities takes a whole batch as one
command; an older one gets the batch as the single actions it already knows, one at a time,
and the answers are put back into the batch shape here. The model sees the same result
either way. The step rules themselves live in the driver (contract A.2), which the cloud,
the screenshot driver and the macOS accessibility host all check against.
"""
from personal_agent.device_link import driver
from personal_agent.device_link.driver import KEYS, NEEDS_ELEMENTS, SKILLS

PLATFORMS = {"darwin": "macos", "macos": "macos", "windows": "windows", "linux": "linux"}
POINTED = {"click", "double_click", "right_click", "move"}
# A batch on a pixels screen runs blind after its first step: keep it short.
MAX_PIXEL_STEPS = 5
# The capabilities that let a computer take read and steps as one command each.
BATCH = frozenset({"read", "steps"})


class NeedsElements(ValueError):
    """A pixels-mode screen cannot take element tokens, set_value or menu."""


def validate_step(step):
    problem = driver.step_problem(step, elements=True)
    if problem: raise ValueError(problem)
    return step


def validate_steps(steps, mode):
    """Check a batch against A.2 for the screen it was read from. Raises NeedsElements for pixels misuse."""
    elements = mode == "elements"
    refusal = driver.steps_refusal(steps, elements)
    if refusal and refusal["error"] == NEEDS_ELEMENTS: raise NeedsElements(NEEDS_ELEMENTS)
    if refusal: raise ValueError(refusal["error"].removeprefix("invalid_steps: "))
    if not elements and len(steps) > MAX_PIXEL_STEPS:
        raise ValueError("A screen read as pixels takes at most %d steps per batch: check `after`, then send the rest" % MAX_PIXEL_STEPS)
    return steps


def supports_batch(capabilities):
    return BATCH <= set(capabilities or ())


# -- older computers ---------------------------------------------------------------

def legacy_action(step):
    """The single old action for one pixels step, or None when old computers cannot do it."""
    kind = step["type"]
    if kind in POINTED: return {"type": kind, "x": step["x"], "y": step["y"]}
    if kind == "type": return {"type": "type", "text": step["text"]}
    if kind == "key": return {"type": "key", "key": step["key"]}
    if kind == "open_browser": return {"type": "open_browser", "url": step["url"]}
    if kind == "hotkey":
        combo = "+".join(step["keys"]).lower()
        return {"type": "key", "key": combo} if combo in KEYS else None
    if kind == "scroll":
        direction, amount = step.get("direction"), step.get("amount", 5)
        if direction in {"left", "right"}: return None
        if direction: amount = abs(amount) if direction == "up" else -abs(amount)
        return {"type": "scroll", "amount": amount, **{key: step[key] for key in ("x", "y") if key in step}}
    return None


def step_from_legacy(action):
    """An old approved computer.action as one step of the new batch."""
    return {key: value for key, value in action.items() if key in driver.STEP_FIELDS}


def normalize_read(result):
    """Any read answer in the full read shape: an old observe answer becomes a pixels read."""
    if isinstance(result, dict) and not result.get("mode") and result.get("observation_id"):
        observation = result["observation_id"]
        result = {key: value for key, value in result.items() if key not in {"observation_id", "status"}}
        result.update(mode="pixels", window="screen", snapshot_id=observation)
    return finish_read(result)


def finish_read(result):
    """Fill the fields every read answer has, so the model can always rely on them."""
    if not isinstance(result, dict) or not result.get("snapshot_id"): return result
    mode = result.get("mode") or "pixels"
    result["mode"] = mode
    result["platform"] = PLATFORMS.get(str(result.get("platform") or "").lower(), result.get("platform"))
    result.setdefault("skill", SKILLS.get(mode, SKILLS["pixels"]))
    result.setdefault("window", "screen")
    if result.get("image_base64"): result.setdefault("media_type", "image/png")
    return result


def _outcome(index, step, answer):
    if answer.get("status") == "input_executed":
        return {"index": index, "type": step["type"], "ok": True, "effect": "unverifiable"}
    if answer.get("status") == "stale_observation":
        return {"index": index, "type": step["type"], "ok": False, "effect": "refused",
                "message": answer.get("message") or "Screen changed", "hint": "Read again, then act on the new snapshot"}
    return {"index": index, "type": step["type"], "ok": False, "effect": "refused",
            "message": answer.get("message") or answer.get("error") or "The step did not run"}


async def run_legacy_steps(send, command):
    """Run a pixels batch as old single actions through ``send(action) -> answer``; stop at the first failure.

    Each old action checks the screen against the observation it names, so every step names the screen the
    previous one left behind; only the first carries the signature the batch was approved against."""
    outcomes, last, observation = [], None, command.get("snapshot_id")
    signature = command.get("expected_signature")
    for index, step in enumerate(command["steps"]):
        action = legacy_action(step)
        if action is None:
            outcomes.append({"index": index, "type": step["type"], "ok": False, "effect": "refused",
                             "message": "This computer's HugAgentOS version cannot do this step; update it"})
            break
        action["observation_id"] = observation
        if signature: action["expected_signature"] = signature
        answer = await send(action)
        if answer.get("waiting") or answer.get("outcome_unknown") or answer.get("not_executed"):
            # Nothing more runs; the caller pauses for the user exactly as for one old action.
            flags = {key: answer[key] for key in ("waiting", "outcome_unknown", "not_executed", "message") if key in answer}
            if index == 0: return {**flags, "ok": False, "executed": 0, "failed_step": 0, "steps": []}
            flags.pop("not_executed", None)
            return {**flags, "ok": False, "executed": index, "failed_step": index, "steps": outcomes,
                    **({"after": normalize_read(last)} if last else {})}
        outcome = _outcome(index, step, answer)
        outcomes.append(outcome)
        if answer.get("observation_id"):
            last, observation, signature = answer, answer["observation_id"], answer.get("signature")
        if not outcome["ok"]: break
    executed = sum(1 for item in outcomes if item["ok"])
    failed = None if executed == len(command["steps"]) else executed
    result = {"ok": failed is None, "executed": executed, "failed_step": failed, "steps": outcomes}
    if last is None:
        # Nothing ran, so the screen is as it was: read it now so the model still gets an after.
        last = await send({"type": "observe"})
    if last and (last.get("observation_id") or last.get("mode")): result["after"] = normalize_read(last)
    return result


# -- element names for approvals ------------------------------------------------------

ROLE_NAMES = {"AXButton": "按钮", "AXTextArea": "文本区", "AXTextField": "输入框", "AXSearchField": "搜索框",
              "AXSecureTextField": "密码框", "AXCheckBox": "复选框", "AXRadioButton": "单选项",
              "AXPopUpButton": "下拉框", "AXComboBox": "组合框", "AXMenuItem": "菜单项", "AXMenuBarItem": "菜单",
              "AXMenuButton": "菜单按钮", "AXLink": "链接", "AXTab": "标签页", "AXRow": "行", "AXCell": "单元格",
              "AXStaticText": "文字", "AXImage": "图片", "AXSlider": "滑块", "AXWindow": "窗口", "AXSheet": "对话框"}
MAX_LABELS = 400


def element_labels(markdown):
    """``{row index: "按钮「发送」"}`` from a read's element rows, for naming approved targets:
    the element's title, else its stable accessibility id, never its current value."""
    labels = {}
    for index, (role, title, ax_id) in list(driver.element_rows(markdown).items())[:MAX_LABELS]:
        name = title or ax_id
        labels[index] = ROLE_NAMES.get(role, role.removeprefix("AX")) + ("「" + name[:60] + "」" if name else "")
    return labels


def targets_of(steps, labels):
    """``{element token: name}`` for the steps' elements that the read named."""
    out = {}
    for step in steps:
        token = step.get("element") if isinstance(step, dict) else None
        name = (labels or {}).get(str(token).rsplit(":", 1)[-1]) if token else None
        if name: out[token] = name
    return out
