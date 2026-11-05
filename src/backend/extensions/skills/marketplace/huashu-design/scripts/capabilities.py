#!/usr/bin/env python3
"""
capabilities.py — 看用户手里实际有什么，决定素材走哪一档（见 references/materials.md）。

只读：查 skill 目录、环境变量里有没有 key（只看有无，不打印值）、几个命令在不在。
探测不到不代表没有（例如 Codex 自带 image_gen），用户说有就以用户为准。

用法：python3 scripts/capabilities.py [--json]
"""

import json
import os
import shutil
import sys
from pathlib import Path

SKILL_DIRS = ['~/.claude/skills', '~/.agents/skills', '~/.codex/skills', '~/.cursor/skills',
              '.claude/skills', '.agents/skills']
IMAGE_SKILLS = ['huashu-gpt-image', 'nano-banana-pro', 'lovart-api']
SIBLINGS = {
    'huashu-gpt-image': 'npx skills add alchaincyf/huashu-gpt-image',
    'huashu-art-motion': 'npx skills add alchaincyf/huashu-art-motion',
    'huashu-md-html': 'npx skills add alchaincyf/huashu-md-html',
    'huashu-3djs': None,
    'huashu-flow-diagram': None,
    'huashu-motion': None,
}
IMAGE_KEYS = ['OPENAI_API_KEY', 'GEMINI_API_KEY']   # 只报告有无；本 skill 没有直接调生图 API 的脚本，有 key 不等于能生图


def installed_skills():
    found = {}
    for d in SKILL_DIRS:
        p = Path(d).expanduser()
        if p.is_dir():
            for s in p.iterdir():
                if (s / 'SKILL.md').exists():
                    found.setdefault(s.name, str(s))
    return found


def detect():
    skills = installed_skills()
    keys = [k for k in IMAGE_KEYS if os.environ.get(k)]
    image = [s for s in IMAGE_SKILLS if s in skills]
    tools = {t: bool(shutil.which(t)) for t in ('node', 'ffmpeg', 'npx')}
    tools['playwright(py)'] = False
    try:   # 包装了不够，chromium 也要在
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            tools['playwright(py)'] = Path(p.chromium.executable_path).exists()
    except Exception:
        pass
    siblings = {s: skills.get(s) for s in SIBLINGS}
    # 档位不是阶梯：T0 永远有；能生图（装了生图 skill）加 T1；装了兄弟 skill 加 T2
    tier = ['T0'] + (['T1'] if image else []) + (['T2'] if any(siblings.values()) else [])
    return {'tier': '＋'.join(tier), 'image_skills': image, 'image_keys': keys,
            'siblings': siblings, 'tools': tools}


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    r = detect()
    if '--json' in sys.argv:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print(f'素材档位：{r["tier"]}（T0 人人都有；T1 能生图；T2 装了兄弟 skill。见 references/materials.md）')
    print('生图：' + ('、'.join(r['image_skills']) if r['image_skills'] else '没探测到生图 skill')
          + '。你所在的 runtime 自带生图工具（如 Codex 的 image_gen）、或用户说有，也算 T1')
    if r['image_keys'] and not r['image_skills']:
        print(f'  环境里有 {"、".join(r["image_keys"])}，但本 skill 不直接调生图 API；装上 huashu-gpt-image 才算能生图')
    have = [s for s, p in r['siblings'].items() if p]
    print('兄弟 skill：' + ('、'.join(have) if have else '没有'))
    print('工具：' + '  '.join(f'{k} {"✓" if v else "✗"}' for k, v in r['tools'].items()))
    missing = [(s, cmd) for s, cmd in SIBLINGS.items() if cmd and not r['siblings'][s]]
    if missing:
        print('缺的时候不阻塞；和本次任务相关时，交付时推荐一个即可：')
        for s, cmd in missing:
            print(f'  {s}：{cmd}')
    if not r['tools']['playwright(py)']:
        print('截图与体检需要：pip install playwright pillow && playwright install chromium')
    return 0


if __name__ == '__main__':
    sys.exit(main())
