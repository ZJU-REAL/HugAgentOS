#!/usr/bin/env python3
"""
SOP 标准化流程提取器 — 多格式输出器
将结构化 SOP 数据渲染为多种可用格式。
"""

import json
import sys
import os
import re
from datetime import datetime


# ============================================================
# Markdown 格式
# ============================================================

STEP_TYPE_ICONS = {
    'action': '🔧',
    'decision': '🔀',
    'checkpoint': '✅',
    'wait': '⏳',
    'preparation': '📋',
    'handover': '📤',
    'overview': '📖',
}

STEP_TYPE_LABELS = {
    'action': '操作',
    'decision': '决策',
    'checkpoint': '检查点',
    'wait': '等待',
    'preparation': '准备',
    'handover': '交接',
    'overview': '概述',
}

COMPLEXITY_COLORS = {
    'low': '🟢',
    'medium': '🟡',
    'high': '🔴',
}


def format_markdown(sop: dict, include_toc: bool = True) -> str:
    """将 SOP 渲染为完整的 Markdown 文档"""
    meta = sop.get('meta', {})
    steps = sop.get('steps', [])
    summary = sop.get('summary', {})

    lines = []

    # 标题
    lines.append(f'# {meta.get("title", "SOP 文档")}')
    lines.append('')

    # 元信息
    lines.append('## 文档信息')
    lines.append('')
    lines.append('| 属性 | 值 |')
    lines.append('|------|-----|')
    lines.append(f'| 版本 | {meta.get("version", "-")} |')
    lines.append(f'| 创建日期 | {meta.get("created_at", "-")[:10]} |')
    lines.append(f'| 总步骤数 | {meta.get("total_steps", len(steps))} |')
    lines.append(f'| 预估总耗时 | {meta.get("estimated_total_time_display", "-")} |')
    lines.append(f'| 整体复杂度 | {meta.get("overall_complexity", "-")} |')
    lines.append('')

    # 目录
    if include_toc:
        lines.append('## 目录')
        lines.append('')
        for s in steps:
            icon = STEP_TYPE_ICONS.get(s.get('type', 'action'), '')
            lines.append(f'- [{icon} 步骤 {s["index"]}: {s["title"]}](#步骤-{s["index"]})')
        lines.append('')

    # 流程概览
    lines.append('## 流程概览')
    lines.append('')
    type_dist = summary.get('step_type_distribution', {})
    if type_dist:
        lines.append('### 步骤类型分布')
        lines.append('')
        for t, count in sorted(type_dist.items()):
            label = STEP_TYPE_LABELS.get(t, t)
            bar = '█' * count
            lines.append(f'- {label}: {bar} ({count})')
    lines.append('')

    # 步骤详情
    lines.append('## 详细步骤')
    lines.append('')

    for s in steps:
        icon = STEP_TYPE_ICONS.get(s.get('type', 'action'), '')
        comp = COMPLEXITY_COLORS.get(s.get('complexity', 'low'), '')
        lines.append(f'### {icon} 步骤 {s["index"]}: {s["title"]}')
        lines.append('')
        lines.append(f'**类型**: {STEP_TYPE_LABELS.get(s["type"], s["type"])}  |  '
                     f'**复杂度**: {comp} {s.get("complexity", "low")}')
        lines.append('')

        # 描述
        lines.append(f'> {s.get("description", "")}')
        lines.append('')

        # 时间
        if s.get('time'):
            lines.append(f'⏱️ **预估耗时**: {s["time"]["raw"]}')
            lines.append('')

        # 工具
        if s.get('tools'):
            lines.append(f'🛠️ **所需工具**: {", ".join(s["tools"])}')
            lines.append('')

        # 角色
        if s.get('roles'):
            lines.append(f'👤 **执行角色**: {", ".join(s["roles"])}')
            lines.append('')

        # 产出
        if s.get('output'):
            lines.append(f'📦 **产出物**: {s["output"]}')
            lines.append('')

        # 依赖
        if s.get('dependencies'):
            deps = ', '.join(f'步骤{d}' for d in s['dependencies'])
            lines.append(f'🔗 **依赖**: {deps}')
            lines.append('')

        # 注意事项
        if s.get('cautions'):
            lines.append('**⚠️ 注意事项**:')
            for c in s['cautions']:
                lines.append(f'- {c}')
            lines.append('')

        # 检查点
        if s.get('checkpoints'):
            lines.append('**✅ 检查点**:')
            for c in s['checkpoints']:
                lines.append(f'- ☐ {c}')
            lines.append('')

        lines.append('---')
        lines.append('')

    # 总结
    lines.append('## 流程总结')
    lines.append('')
    lines.append(f'- 共 {len(steps)} 个步骤')
    lines.append(f'- 预估总耗时: {meta.get("estimated_total_time_display", "-")}')
    checkpoints = summary.get('total_checkpoints', 0)
    cautions = summary.get('total_cautions', 0)
    lines.append(f'- 检查点: {checkpoints} 个')
    lines.append(f'- 注意事项: {cautions} 个')
    lines.append('')

    return '\n'.join(lines)


# ============================================================
# 交互式 HTML 格式
# ============================================================

def format_html(sop: dict, theme: str = 'dark') -> str:
    """将 SOP 渲染为交互式 HTML 页面"""
    meta = sop.get('meta', {})
    steps = sop.get('steps', [])
    summary = sop.get('summary', {})

    # 步骤卡片 HTML
    step_cards = []
    for s in steps:
        icon = STEP_TYPE_ICONS.get(s.get('type', 'action'), '')
        comp = COMPLEXITY_COLORS.get(s.get('complexity', 'low'), '')

        tools_html = ''
        if s.get('tools'):
            tools_html = '<div class="tag-group">' + ''.join(
                f'<span class="tag tag-tool">{t}</span>' for t in s['tools']
            ) + '</div>'

        roles_html = ''
        if s.get('roles'):
            roles_html = '<div class="tag-group">' + ''.join(
                f'<span class="tag tag-role">{r}</span>' for r in s['roles']
            ) + '</div>'

        cautions_html = ''
        if s.get('cautions'):
            items = ''.join(f'<li>{c}</li>' for c in s['cautions'])
            cautions_html = f'<div class="caution-box"><h4>⚠️ 注意事项</h4><ul>{items}</ul></div>'

        checkpoints_html = ''
        if s.get('checkpoints'):
            items = ''.join(f'<li><input type="checkbox" disabled> {c}</li>' for c in s['checkpoints'])
            checkpoints_html = f'<div class="checkpoint-box"><h4>✅ 检查点</h4><ul class="checklist">{items}</ul></div>'

        time_html = ''
        if s.get('time'):
            time_html = f'<span class="time-badge">⏱ {s["time"]["raw"]}</span>'

        output_html = ''
        if s.get('output'):
            output_html = f'<span class="output-badge">📦 {s["output"]}</span>'

        type_class = f'step-type-{s.get("type", "action")}'

        step_cards.append(f'''
        <div class="step-card {type_class}" id="step-{s['index']}">
            <div class="step-header">
                <span class="step-number">{icon} 步骤 {s['index']}</span>
                <span class="step-type-badge">{STEP_TYPE_LABELS.get(s["type"], s["type"])}</span>
                <span class="step-complexity">{comp} {s.get("complexity", "")}</span>
                {time_html}
                {output_html}
            </div>
            <div class="step-body">
                <p class="step-description">{s.get("description", "")}</p>
                {tools_html}
                {roles_html}
                {cautions_html}
                {checkpoints_html}
            </div>
        </div>
        ''')

    # 摘要统计
    type_dist = summary.get('step_type_distribution', {})
    type_bars = ''
    for t, count in sorted(type_dist.items()):
        pct = count / max(len(steps), 1) * 100
        type_bars += f'''
        <div class="stat-row">
            <span class="stat-label">{STEP_TYPE_LABELS.get(t, t)}</span>
            <div class="stat-bar-container">
                <div class="stat-bar" style="width:{pct}%"></div>
            </div>
            <span class="stat-value">{count}</span>
        </div>
        '''

    total_time = meta.get('estimated_total_time_display', '-')
    total_steps = meta.get('total_steps', len(steps))
    complexity = meta.get('overall_complexity', '-')
    total_checkpoints = summary.get('total_checkpoints', 0)
    total_cautions = summary.get('total_cautions', 0)

    # 主题选择
    if theme == 'dark':
        bg = '#0f0f1a'
        card_bg = '#1a1a2e'
        text = '#e0e0e0'
        accent = '#00d4ff'
        accent2 = '#ff6b6b'
        border = '#2a2a4a'
    else:
        bg = '#f8f9fa'
        card_bg = '#ffffff'
        text = '#333333'
        accent = '#0066cc'
        accent2 = '#cc3300'
        border = '#dee2e6'

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{meta.get("title", "SOP文档")}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: {bg};
    color: {text};
    line-height: 1.6;
    padding: 20px;
}}
.container {{ max-width: 900px; margin: 0 auto; }}

/* 头部 */
.header {{
    background: linear-gradient(135deg, {accent}22, {accent2}22);
    border: 1px solid {border};
    border-radius: 12px;
    padding: 30px;
    margin-bottom: 24px;
}}
.header h1 {{ font-size: 28px; color: {accent}; margin-bottom: 12px; }}
.header-meta {{
    display: flex; flex-wrap: wrap; gap: 12px;
    font-size: 14px; color: {text}; opacity: 0.8;
}}
.header-meta span {{ background: {card_bg}; padding: 4px 12px; border-radius: 20px; border: 1px solid {border}; }}

/* 摘要面板 */
.summary-panel {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
}}
.summary-card {{
    background: {card_bg};
    border: 1px solid {border};
    border-radius: 10px;
    padding: 16px;
    text-align: center;
}}
.summary-card .value {{ font-size: 28px; font-weight: 700; color: {accent}; }}
.summary-card .label {{ font-size: 13px; color: {text}; opacity: 0.7; margin-top: 4px; }}

/* 步骤类型分布 */
.stat-row {{
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 8px; font-size: 14px;
}}
.stat-label {{ width: 50px; text-align: right; }}
.stat-bar-container {{
    flex: 1; height: 18px; background: {border}; border-radius: 9px; overflow: hidden;
}}
.stat-bar {{
    height: 100%; background: linear-gradient(90deg, {accent}, {accent2});
    border-radius: 9px; transition: width 0.5s ease;
}}
.stat-value {{ width: 30px; }}

/* 步骤卡片 */
.step-card {{
    background: {card_bg};
    border: 1px solid {border};
    border-radius: 10px;
    margin-bottom: 16px;
    overflow: hidden;
    transition: box-shadow 0.3s;
}}
.step-card:hover {{ box-shadow: 0 4px 20px rgba(0,0,0,0.2); }}
.step-card.step-type-checkpoint {{ border-left: 3px solid #4caf50; }}
.step-card.step-type-decision {{ border-left: 3px solid #ff9800; }}
.step-card.step-type-preparation {{ border-left: 3px solid #2196f3; }}
.step-card.step-type-wait {{ border-left: 3px solid #9c27b0; }}
.step-card.step-type-handover {{ border-left: 3px solid #ff5722; }}

.step-header {{
    padding: 14px 18px;
    display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
    border-bottom: 1px solid {border};
    background: {bg};
}}
.step-number {{ font-weight: 700; font-size: 16px; color: {accent}; }}
.step-type-badge {{
    font-size: 12px; padding: 2px 8px; border-radius: 12px;
    background: {accent}22; color: {accent};
}}
.step-complexity {{ font-size: 12px; }}
.time-badge, .output-badge {{
    font-size: 12px; padding: 2px 8px; border-radius: 12px;
    background: {border};
}}
.step-body {{ padding: 16px 18px; }}
.step-description {{ margin-bottom: 12px; font-size: 15px; }}

.tag-group {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
.tag {{
    font-size: 12px; padding: 3px 10px; border-radius: 14px;
    border: 1px solid;
}}
.tag-tool {{ border-color: {accent}; color: {accent}; }}
.tag-role {{ border-color: #ff9800; color: #ff9800; }}

.caution-box {{
    background: #ff6b6b11;
    border: 1px solid #ff6b6b33;
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 10px;
}}
.caution-box h4 {{ color: #ff6b6b; font-size: 14px; margin-bottom: 6px; }}
.caution-box ul {{ padding-left: 20px; font-size: 14px; }}
.caution-box li {{ margin-bottom: 4px; }}

.checkpoint-box {{
    background: #4caf5011;
    border: 1px solid #4caf5033;
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 10px;
}}
.checkpoint-box h4 {{ color: #4caf50; font-size: 14px; margin-bottom: 6px; }}
.checklist {{ list-style: none; padding: 0; font-size: 14px; }}
.checklist li {{ margin-bottom: 4px; }}
.checklist input {{ margin-right: 8px; }}

/* 响应式 */
@media (max-width: 600px) {{
    .header {{ padding: 20px; }}
    .header h1 {{ font-size: 22px; }}
    .summary-panel {{ grid-template-columns: repeat(2, 1fr); }}
}}
</style>
</head>
<body>
<div class="container">

    <!-- 头部 -->
    <div class="header">
        <h1>{meta.get("title", "SOP文档")}</h1>
        <div class="header-meta">
            <span>📌 v{meta.get("version", "1.0")}</span>
            <span>📅 {meta.get("created_at", "-")[:10]}</span>
            <span>⏱ {total_time}</span>
            <span>📊 {complexity}</span>
            <span>✅ {total_checkpoints} 检查点</span>
            <span>⚠️ {total_cautions} 注意事项</span>
        </div>
    </div>

    <!-- 摘要面板 -->
    <div class="summary-panel">
        <div class="summary-card">
            <div class="value">{total_steps}</div>
            <div class="label">总步骤数</div>
        </div>
        <div class="summary-card">
            <div class="value">{total_time}</div>
            <div class="label">预估总耗时</div>
        </div>
        <div class="summary-card">
            <div class="value">{total_checkpoints}</div>
            <div class="label">检查点</div>
        </div>
        <div class="summary-card">
            <div class="value">{total_cautions}</div>
            <div class="label">注意事项</div>
        </div>
    </div>

    <!-- 步骤详情 -->
    <h2 style="margin-bottom:16px; color:{accent};">📋 详细步骤</h2>
    {''.join(step_cards)}

</div>
</body>
</html>'''

    return html


# ============================================================
# Mermaid 流程图
# ============================================================

def format_mermaid(sop: dict) -> str:
    """将 SOP 渲染为 Mermaid 流程图"""
    meta = sop.get('meta', {})
    steps = sop.get('steps', [])

    lines = ['```mermaid', 'flowchart TD']

    # 样式定义
    lines.append('    classDef action fill:#1a1a2e,stroke:#00d4ff,color:#e0e0e0')
    lines.append('    classDef decision fill:#1a1a2e,stroke:#ff9800,color:#e0e0e0')
    lines.append('    classDef checkpoint fill:#1a1a2e,stroke:#4caf50,color:#e0e0e0')
    lines.append('    classDef preparation fill:#1a1a2e,stroke:#2196f3,color:#e0e0e0')
    lines.append('    classDef wait fill:#1a1a2e,stroke:#9c27b0,color:#e0e0e0')

    lines.append('')
    lines.append(f'    Title["{meta.get("title", "SOP流程")}"]')

    # 步骤节点
    for s in steps:
        idx = s['index']
        title = s.get('title', '')[:20]
        stype = s.get('type', 'action')

        if stype == 'decision':
            lines.append(f'    S{idx}{{"{title}"}}')
        elif stype == 'checkpoint':
            lines.append(f'    S{idx}[("{title}")]')
        elif stype == 'preparation':
            lines.append(f'    S{idx}["{title}"]')
        elif stype == 'wait':
            lines.append(f'    S{idx}(("{title}"))')
        else:
            lines.append(f'    S{idx}["{title}"]')

    lines.append('')

    # 连线
    non_overview = [s for s in steps if s.get('type') != 'overview']
    for i, s in enumerate(non_overview):
        if i == 0:
            lines.append(f'    Title --> S{s["index"]}')

        if i < len(non_overview) - 1:
            next_s = non_overview[i + 1]
            stype = s.get('type', 'action')
            if stype == 'decision':
                lines.append(f'    S{s["index"]} -->|是| S{next_s["index"]}')
                lines.append(f'    S{s["index"]} -.->|否| S{next_s["index"]}')
            else:
                lines.append(f'    S{s["index"]} --> S{next_s["index"]}')

    # 样式应用
    for s in steps:
        stype = s.get('type', 'action')
        class_map = {
            'action': 'action',
            'decision': 'decision',
            'checkpoint': 'checkpoint',
            'preparation': 'preparation',
            'wait': 'wait',
            'handover': 'action',
        }
        cls = class_map.get(stype, 'action')
        lines.append(f'    class S{s["index"]} {cls}')

    lines.append('```')
    return '\n'.join(lines)


# ============================================================
# 检查清单格式
# ============================================================

def format_checklist(sop: dict) -> str:
    """将 SOP 渲染为可打印的检查清单"""
    meta = sop.get('meta', {})
    steps = sop.get('steps', [])

    lines = []
    lines.append(f'# 📋 {meta.get("title", "SOP")} — 执行检查清单')
    lines.append(f'**版本**: {meta.get("version", "1.0")}  |  '
                 f'**预估耗时**: {meta.get("estimated_total_time_display", "-")}')
    lines.append('')
    lines.append('---')
    lines.append('')

    for s in steps:
        if s.get('type') in ('overview', 'preparation'):
            continue

        icon = STEP_TYPE_ICONS.get(s.get('type', 'action'), '')
        lines.append(f'## {icon} [{s["complexity"].upper()}] {s["title"]}')
        lines.append('')
        lines.append(f'- [ ] {s.get("description", "")[:80]}')
        if s.get('time'):
            lines.append(f'      ⏱ {s["time"]["raw"]}')
        if s.get('cautions'):
            lines.append(f'      ⚠️ 注意: {"; ".join(s["cautions"][:2])}')
        lines.append('')

    lines.append('---')
    lines.append('')
    lines.append('✅ 完成确认: ____/____/____  签字: __________')
    lines.append('')

    return '\n'.join(lines)


# ============================================================
# 培训卡片格式
# ============================================================

def format_training_card(sop: dict) -> str:
    """将 SOP 渲染为培训/新人上手卡片"""
    meta = sop.get('meta', {})
    steps = sop.get('steps', [])
    summary = sop.get('summary', {})

    lines = []
    lines.append(f'# 🎓 培训卡片: {meta.get("title", "SOP")}')
    lines.append('')

    # 概览区
    lines.append('## 📊 流程概览')
    lines.append('')
    lines.append(f'| 项目 | 内容 |')
    lines.append(f'|------|------|')
    lines.append(f'| 流程名称 | {meta.get("title", "-")} |')
    lines.append(f'| 总步骤数 | {meta.get("total_steps", 0)} |')
    lines.append(f'| 预估耗时 | {meta.get("estimated_total_time_display", "-")} |')
    lines.append(f'| 复杂度 | {meta.get("overall_complexity", "-")} |')
    lines.append(f'| 检查点数 | {summary.get("total_checkpoints", 0)} |')
    lines.append(f'| 涉及工具 | {summary.get("total_tools", 0)} 种 |')
    lines.append('')

    # 关键步骤速查
    lines.append('## 🔑 关键步骤速查')
    lines.append('')
    for s in steps:
        if s.get('type') in ('overview',):
            continue
        lines.append(f'**步骤 {s["index"]}**: {s["title"]}')
        lines.append(f'> {s.get("description", "")[:100]}')
        if s.get('cautions'):
            lines.append(f'> ⚠️ {s["cautions"][0][:80]}')
        lines.append('')

    # 常见错误
    lines.append('## ⚠️ 新手常见易错点')
    lines.append('')
    all_cautions = []
    for s in steps:
        for c in s.get('cautions', []):
            all_cautions.append(c)
    for i, c in enumerate(all_cautions[:10], 1):
        lines.append(f'{i}. {c}')
    if not all_cautions:
        lines.append('_（暂无标注的易错点）_')
    lines.append('')

    # 验收标准
    lines.append('## ✅ 验收标准')
    lines.append('')
    all_checkpoints = []
    for s in steps:
        for c in s.get('checkpoints', []):
            all_checkpoints.append(c)
    for i, c in enumerate(all_checkpoints, 1):
        lines.append(f'- [ ] {c}')
    if not all_checkpoints:
        lines.append('- [ ] 所有步骤按顺序执行完毕')
        lines.append('- [ ] 最终产出物已确认')
    lines.append('')

    return '\n'.join(lines)


# ============================================================
# JSON 格式（保持原样）
# ============================================================

def format_json(sop: dict) -> str:
    """输出格式化的 JSON"""
    return json.dumps(sop, ensure_ascii=False, indent=2)


# ============================================================
# 格式映射
# ============================================================

FORMATTERS = {
    'markdown': format_markdown,
    'md': format_markdown,
    'html': format_html,
    'mermaid': format_mermaid,
    'flowchart': format_mermaid,
    'checklist': format_checklist,
    'training': format_training_card,
    'training_card': format_training_card,
    'json': format_json,
}

THEME_OPTIONS = ['dark', 'light']


# ============================================================
# 命令行接口
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='SOP 多格式输出器 — 将 SOP JSON 渲染为多种格式',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python sop_formatter.py -i sop_output.json -f markdown -o sop.md
  python sop_formatter.py -i sop_output.json -f html -o sop.html --theme dark
  python sop_formatter.py -i sop_output.json -f checklist -o checklist.md
  python sop_formatter.py -i sop_output.json -f mermaid -o flowchart.md
  python sop_formatter.py -i sop_output.json -f training -o training.md
  python sop_formatter.py -i sop_output.json -f all -o sop_docs/
        """
    )
    parser.add_argument('-i', '--input', type=str, required=True,
                        help='sop_extractor.py 或 sop_optimizer.py 输出的 JSON 文件')
    parser.add_argument('-f', '--format', type=str, default='markdown',
                        choices=list(FORMATTERS.keys()) + ['all'],
                        help='输出格式')
    parser.add_argument('-o', '--output', type=str, default='sop_output',
                        help='输出文件路径或目录（-f all 时为目录）')
    parser.add_argument('--theme', type=str, default='dark',
                        choices=THEME_OPTIONS, help='HTML 主题')

    args = parser.parse_args()

    # 读取 SOP
    with open(args.input, 'r', encoding='utf-8') as f:
        sop = json.load(f)

    # 生成输出
    if args.format == 'all':
        # 输出所有格式到指定目录
        out_dir = args.output.rstrip('/').rstrip('\\')
        os.makedirs(out_dir, exist_ok=True)

        formats_to_generate = [
            ('sop.md', 'markdown'),
            ('sop.html', 'html'),
            ('sop_checklist.md', 'checklist'),
            ('sop_training.md', 'training'),
            ('sop_flowchart.md', 'mermaid'),
            ('sop.json', 'json'),
        ]

        generated = []
        for filename, fmt in formats_to_generate:
            out_path = os.path.join(out_dir, filename)
            if fmt == 'html':
                content = FORMATTERS[fmt](sop, theme=args.theme)
            else:
                content = FORMATTERS[fmt](sop)
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(content)
            generated.append(out_path)

        print(f'[SOP Formatter] 已生成 {len(generated)} 种格式到 {out_dir}/')
        for path in generated:
            size = os.path.getsize(path)
            print(f'  - {os.path.basename(path)} ({size:,} bytes)')

    else:
        # 单格式输出
        out_path = args.output

        # 自动补充扩展名
        ext_map = {
            'markdown': '.md', 'md': '.md',
            'html': '.html',
            'mermaid': '.md', 'flowchart': '.md',
            'checklist': '.md',
            'training': '.md', 'training_card': '.md',
            'json': '.json',
        }
        if not any(out_path.endswith(ext) for ext in ['.md', '.html', '.json', '.txt']):
            out_path += ext_map.get(args.format, '.md')

        if args.format == 'html':
            content = FORMATTERS[args.format](sop, theme=args.theme)
        else:
            content = FORMATTERS[args.format](sop)

        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(content)

        size = os.path.getsize(out_path)
        print(f'[SOP Formatter] {args.format.upper()} 格式已生成: {out_path} ({size:,} bytes)')


if __name__ == '__main__':
    main()
