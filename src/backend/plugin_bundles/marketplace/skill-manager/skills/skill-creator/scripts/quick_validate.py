#!/usr/bin/env python3
"""快速校验一个技能目录的 SKILL.md 是否合法（落库前自检）。

规则对齐后端技能引擎 core/agent_skills/registry.py（**不是** Anthropic 原版规则）：
- 必须有 `---` 包裹的 YAML frontmatter；
- frontmatter 必须有 `name` 与 `description`；
- name 必须匹配 ^[a-z0-9_-]{1,63}$（小写字母/数字/下划线/连字符，1–63 字符）；
- description 非空；
- 正文（frontmatter 之后）建议非空（为空只告警，不判错——register 只强制 description）。

纯标准库，可直接在沙箱里跑：
    python3 quick_validate.py <技能目录>        # 目录里应含 SKILL.md
    python3 quick_validate.py <路径/SKILL.md>   # 也接受直接给 SKILL.md
退出码 0=通过，1=不通过。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ID_RE = re.compile(r"^[a-z0-9_-]{1,63}$")


def _split_frontmatter(raw: str):
    """返回 (frontmatter 原文, 正文)。要求以 `---\\n` 开头且有闭合 `---`。"""
    if not raw.startswith("---\n") and not raw.startswith("---\r\n"):
        raise ValueError("SKILL.md 必须以 YAML frontmatter 开头（第一行是 ---）")
    rest = raw.split("\n", 1)[1] if "\n" in raw else ""
    end = re.search(r"\n---\s*(\n|$)", rest)
    if not end:
        raise ValueError("frontmatter 缺少闭合的 --- 行")
    return rest[: end.start()], rest[end.end():]


def _parse_scalar(fm: str, key: str):
    """从 frontmatter 里取一个标量字段，支持 `key: 值` 和块标量 `key: |` / `key: >`。"""
    lines = fm.split("\n")
    for i, line in enumerate(lines):
        m = re.match(rf"^{re.escape(key)}\s*:\s*(.*)$", line)
        if not m:
            continue
        val = m.group(1).strip()
        if val in ("|", ">", "|-", ">-", "|+", ">+"):
            # 块标量：收集后续缩进行
            block = []
            for nxt in lines[i + 1:]:
                if nxt.strip() == "":
                    block.append("")
                    continue
                if re.match(r"^\s+\S", nxt):
                    block.append(nxt.strip())
                else:
                    break
            sep = "\n" if val.startswith("|") else " "
            return sep.join(block).strip()
        # 去掉可能的引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        return val.strip()
    return None


def validate_skill(path: str):
    p = Path(path)
    skill_md = p / "SKILL.md" if p.is_dir() else p
    if not skill_md.is_file():
        return False, f"找不到 SKILL.md：{skill_md}"

    raw = skill_md.read_text(encoding="utf-8")
    try:
        fm, body = _split_frontmatter(raw)
    except ValueError as e:
        return False, str(e)

    name = _parse_scalar(fm, "name")
    if not name:
        return False, "frontmatter 缺少 `name`"
    if not _ID_RE.match(name):
        return False, (
            f"name「{name}」不合法：只能是小写字母/数字/下划线/连字符，长度 1–63。"
            "（含空格/中文/大写等会在落库时被规范化，但建议直接写规范）"
        )

    description = _parse_scalar(fm, "description")
    if not description or not description.strip():
        return False, "frontmatter 缺少非空的 `description`（这是技能被唤起的主要依据，务必写清"
    if len(description) > 1024:
        return False, f"description 过长（{len(description)} 字符），建议 ≤1024"

    warnings = []
    if not body.strip():
        warnings.append("正文为空——建议写清‘怎么做’的主干（register 只强制 description，但空正文的技能几乎没用）")

    msg = "✅ SKILL.md 校验通过，可以落库（register_skill）。"
    if warnings:
        msg += " 提醒：" + "；".join(warnings)
    return True, msg


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法：python3 quick_validate.py <技能目录 或 SKILL.md 路径>")
        sys.exit(2)
    ok, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if ok else 1)
