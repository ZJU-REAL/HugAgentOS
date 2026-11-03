#!/usr/bin/env python3
"""
learn.py — 让 huashu-design 每做一个项目就更懂这个用户一点。

经验分两层：
  随 skill 分发  references/experience.md —— 通用发现（两个项目复现过或测量过），对所有人成立；
                 在仓库里手工编辑，本命令不写它。
  本机私有      ~/.huashu-design/experience.md（HUASHU_DESIGN_HOME 可改位置）—— 这台机器的用户：
                 他的原话、他的品味、只在一个项目里见过的观察。永不分发。

用法：
  python3 scripts/learn.py [主题…]            开工前：读这个用户的偏好 + 与主题相关的经验
  python3 scripts/learn.py add "…" --kind preference|observed|verified|superseded \
          --topics deck,color --evidence "原话/项目/文件/测量" [--do "下次怎么做"] [--project 名]
  python3 scripts/learn.py harvest <项目目录>   收工后：列出项目里能当证据的东西，以及该不该记的判据
  python3 scripts/learn.py status
  python3 scripts/learn.py -- status            主题词恰好叫 add/harvest/status 时，用 -- 隔开

移植自 huashu-motion 的 `om learn`（同一作者，MIT），去掉了对 studio 评论的依赖。
"""

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

KINDS = {
    'preference': '用户亲口说的——必须引用原话；对这个用户一直成立，直到他改口',
    'observed': '只在一个项目里见过——是线索，不是规则',
    'verified': '两个以上项目复现，或测量过——通用的那些最终进 references/experience.md',
    'superseded': '已经不成立——保留条目，写清被什么取代',
}

SKILL_ROOT = Path(__file__).resolve().parent.parent
SHIPPED = SKILL_ROOT / 'references' / 'experience.md'

HEAD = """# 本机经验（huashu-design）

这台机器上的项目教会了什么。由 `learn.py add` 写入，也可以手改：条目错了就原地改，
不再成立就把 kind 改成 superseded 并写明原因。不随 skill 分发。
"""


def home() -> Path:
    return Path(os.environ.get('HUASHU_DESIGN_HOME') or Path.home() / '.huashu-design')


def local_file() -> Path:
    return home() / 'experience.md'


class LearnError(Exception):
    pass


def entries(path: Path, layer: str):
    """markdown 文件里每个「## …」标题加它的正文是一条。"""
    if not path.exists():
        return []
    out = []
    for block in re.split(r'\n(?=## )', path.read_text(encoding='utf-8')):
        if not block.startswith('## '):
            continue
        head = block.split('\n', 1)[0][3:].strip()
        m = re.search(r'·\s*(preference|observed|verified|superseded)\b', head)   # 手改时漏了结尾的「·」也认
        kind = m.group(1) if m else ('shipped' if layer == 'shipped' else 'observed')
        t = re.search(r'topics?:\s*([^\n·]+)', head, re.I)
        tags = [x for x in re.split(r'[,，\s]+', t.group(1) if t else '') if x]
        idm = re.match(r'(L\d+)', head)
        out.append({'layer': layer, 'head': head, 'kind': kind, 'tags': tags,
                    'body': block.strip(), 'id': idm.group(1) if idm else None})
    return out


def recall(topics):
    want = [t.lower() for t in topics]

    def match(e):
        return not want or any(t in [x.lower() for x in e['tags']] or t in e['body'].lower() for t in want)

    local = entries(local_file(), 'local')
    shipped = entries(SHIPPED, 'shipped')
    prefs = [e for e in local if e['kind'] == 'preference']
    rest = [e for e in local if e['kind'] not in ('preference', 'superseded') and match(e)] + \
           [e for e in shipped if match(e)]
    on = f'（{"、".join(want)}）' if want else ''
    parts = []
    if prefs:
        parts.append(f'## 这个用户的偏好（{len(prefs)} 条）——他的原话，直到他改口都成立。'
                     f'注意：偏好有适用范围，写明只用于某类物料的，别外溢成所有设计的默认\n\n'
                     + '\n\n'.join(e['body'] for e in prefs))
    else:
        parts.append(f'## 这个用户的偏好：还没有记录（{local_file()}）')
    if rest:
        parts.append(f'## 过去项目的经验{on}（{len(rest)} 条）——眼前的项目和条目冲突时以项目为准，然后去改条目\n\n'
                     + '\n\n'.join(('[本机] ' if e['layer'] == 'local' else '') + e['body'] for e in rest))
    else:
        parts.append(f'## 过去项目的经验{on}：没有匹配的')
    return '\n\n'.join(parts)


def add_entry(kind, topics, text, evidence, todo=None, project=None):
    if kind not in KINDS:
        raise LearnError('learn.py add 需要 --kind ' + '|'.join(KINDS) + '\n' +
                         '\n'.join(f'  {k:<11} {v}' for k, v in KINDS.items()))
    tags = [x for x in re.split(r'[,，\s]+', topics or '') if x]
    if not tags:
        raise LearnError('learn.py add 需要 --topics（如 deck,color,direction），不然下次找不回来')
    if not (text or '').strip():
        raise LearnError('learn.py add 需要把发现写成文字：learn.py add "…" --kind … --topics … --evidence …')
    if not (evidence or '').strip():
        raise LearnError('learn.py add 需要 --evidence：原话、项目和文件、测量结果——没人能核对的条目只是传言')
    if kind == 'preference' and not re.search(r'[「“"]', text):
        raise LearnError('preference 必须引用用户原话：用「…」或 "…" 括起来')
    f = local_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    prev = f.read_text(encoding='utf-8') if f.exists() else HEAD
    n = 1 + max([0] + [int(e['id'][1:]) for e in entries(f, 'local') if e['id']])
    block = [f'## L{n} · {date.today().isoformat()} · {kind} · topics: {", ".join(tags)}', '',
             f'- {"原话" if kind == "preference" else "发现"}：{text.strip()}']
    if todo:
        block.append(f'- 下次：{todo.strip()}')
    block.append(f'- 证据：{evidence.strip()}' + (f'（{project}）' if project else ''))
    f.write_text(prev.rstrip() + '\n\n' + '\n'.join(block) + '\n', encoding='utf-8')
    return f'已记下  L{n} → {f}'


def harvest(project: Path):
    if not project.is_dir():
        raise LearnError(f'不是目录：{project}')
    lines = [f'收割  {project}']
    approved = project / 'direction-approved.md'
    if approved.exists():
        lines += ['', 'direction-approved.md（三方向里他选了什么、怎么说的——最直接的品味证据）：',
                  approved.read_text(encoding='utf-8').strip()[:3000]]
    directions = project / 'directions.md'
    if directions.exists():
        lines += ['', f'directions.md 在：{directions}（方向卡，对照他的选择看他偏向哪套逻辑、哪种力度，而不只是哪种颜色）']
    demos = sorted((project / 'design-demos').glob('dir-*.html')) if (project / 'design-demos').is_dir() else []
    if demos:
        lines.append(f'design-demos：{len(demos)} 个完整方向' + ('（另有缩略图 thumbs.html）' if (project / 'design-demos' / 'thumbs.html').exists() else ''))
    ledger = home() / 'ledger.jsonl'
    if ledger.exists():
        mine = [l for l in ledger.read_text(encoding='utf-8').splitlines()
                if l.strip() and json.loads(l).get('project_dir') == str(project.resolve())]
        lines.append(f'外观账本：本项目 {len(mine)} 条' + ('' if mine else '——交付了却没记账？跑 design_lint.py ledger add'))
    lines += ['', '再把对话里他说过的话找出来——逐字引用。', '',
              '只有三条同时成立才记：不显而易见；下个项目能省时间或少走弯路；这个项目产生了证据。然后按顺序：',
              '  1. 有条目说的相反吗？先改那条（learn.py 会列出来；直接编辑文件）。',
              '  2. 工具能抓住它吗（design_lint.py 的一项检查）？改工具和它的测试，而不是写下来。',
              '  3. 否则：learn.py add "…" --kind preference|observed --topics … --evidence "…" [--do "…"]。',
              '     他的原话是 preference，并写明适用范围（「只用于他自己的封面」）；你在一个项目里看到的是 observed。',
              '  4. 两个项目都见过、或测量过、且对任何人成立（不是这个用户的口味）？在开发机上',
              '     写进随 skill 分发的 references/experience.md，稳定的部分并进它所属的 reference。',
              f'本机经验：{local_file()}']
    return '\n'.join(lines)


def status():
    f = local_file()
    local = entries(f, 'local')
    shipped = entries(SHIPPED, 'shipped')
    by = {k: sum(1 for e in local if e['kind'] == k) for k in KINDS}
    s = (f'本机 {f}' + (f'（{len(local)} 条：偏好 {by["preference"]}、观察 {by["observed"]}、'
                        f'已验证 {by["verified"]}、已取代 {by["superseded"]}）' if f.exists() else '（还没有）'))
    return s + f'\n随 skill 分发 {len(shipped)} 条，在 references/experience.md'


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):   # Windows 控制台默认不是 UTF-8
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == '--':
            print(recall(argv[1:]))
        elif argv and argv[0] == 'add':
            p = argparse.ArgumentParser(prog='learn.py add')
            p.add_argument('text')
            p.add_argument('--kind', required=True)
            p.add_argument('--topics', required=True)
            p.add_argument('--evidence', default='')
            p.add_argument('--do', dest='todo')
            p.add_argument('--project')
            a = p.parse_args(argv[1:])
            print(add_entry(a.kind, a.topics, a.text, a.evidence, a.todo, a.project))
        elif argv and argv[0] == 'harvest':
            if len(argv) < 2:
                raise LearnError('用法：learn.py harvest <项目目录>')
            print(harvest(Path(argv[1])))
        elif argv and argv[0] == 'status':
            print(status())
        else:
            print(recall(argv))
    except LearnError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
