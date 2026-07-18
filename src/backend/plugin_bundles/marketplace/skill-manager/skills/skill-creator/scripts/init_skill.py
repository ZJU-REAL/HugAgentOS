#!/usr/bin/env python3
"""在沙箱里脚手架一个新技能目录（含规范的 SKILL.md 模板 + 可选 references/scripts）。

    python3 init_skill.py <技能名> [目标父目录]   # 默认建在 /workspace 下

例：python3 init_skill.py weather-brief
  → /workspace/weather-brief/SKILL.md（模板已填好 frontmatter 骨架）

技能名须为小写字母/数字/下划线/连字符。生成后按注释填正文，再用 quick_validate.py 自检，
最后打包 → sandbox_get_artifact → register_skill 落库。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ID_RE = re.compile(r"^[a-z0-9_-]{1,63}$")

_TEMPLATE = """\
---
name: {name}
description: 一句话说清"做什么 + 什么时候该触发"。把典型触发语、同义说法、相关场景都写进来，
  哪怕用户没明说技能名也能命中（当前模型偏向少触发，描述要"主动"一点）。所有"何时使用"放这里。
---

# {title}

<!-- 用祈使句写"怎么做"的主干。解释每一步的原因，别堆 ALWAYS/NEVER。控制在 500 行内。 -->

## 步骤

1. …
2. …

## 输出格式

<!-- 需要固定格式时，直接给模板。 -->

## 示例

<!-- 贴 1–3 个"输入→输出"的小例子，比抽象描述有效。 -->

<!--
可选目录（按需建，别为空而建）：
  references/   放需要时才读的长文档（正文里清楚指路"需要 X 时读 references/x.md"）
  scripts/      放确定性/重复逻辑的脚本，让技能调用而不是每次重写
  assets/       放产出里用到的模板/图标/字体
-->
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python3 init_skill.py <技能名> [目标父目录=/workspace]")
        return 2
    name = sys.argv[1].strip()
    if not _ID_RE.match(name):
        print(f"❌ 技能名「{name}」不合法：只能是小写字母/数字/下划线/连字符，长度 1–63。")
        return 1
    parent = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/workspace")
    skill_dir = parent / name
    if (skill_dir / "SKILL.md").exists():
        print(f"❌ 已存在：{skill_dir / 'SKILL.md'}（不覆盖）")
        return 1
    skill_dir.mkdir(parents=True, exist_ok=True)
    title = name.replace("-", " ").replace("_", " ").title()
    (skill_dir / "SKILL.md").write_text(
        _TEMPLATE.format(name=name, title=title), encoding="utf-8"
    )
    print(f"✅ 已创建 {skill_dir / 'SKILL.md'}")
    print("下一步：填正文 → python3 quick_validate.py "
          f"{skill_dir} → 打包 tar → sandbox_get_artifact → register_skill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
