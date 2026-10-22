#!/usr/bin/env python3
"""
SOP 标准化流程提取器 — 可视化报告生成器
生成深色主题 HTML 报告，含流程图、雷达图、改进对比等。
"""

import json
import sys
import os
import math
from datetime import datetime


def generate_report(sop: dict, optimization: dict = None, output_path: str = 'sop_report.html') -> str:
    """
    生成完整的 SOP 可视化报告。

    参数:
        sop: extract_sop() 返回的 SOP 字典
        optimization: optimize() 返回的优化分析（可选）
        output_path: 输出文件路径
    """
    meta = sop.get('meta', {})
    steps = sop.get('steps', [])
    summary = sop.get('summary', {})

    # 计算数据
    action_steps = [s for s in steps if s.get('type') not in ('overview',)]
    total_steps = len(action_steps)
    total_time = meta.get('estimated_total_time_minutes', total_steps * 5)

    # 步骤时序数据
    cumulative_time = 0
    timeline_points = []
    for s in action_steps:
        t = (s.get('time') or {}).get('value', 5)
        cumulative_time += t
        timeline_points.append({
            'step': s['index'],
            'title': s.get('title', '')[:15],
            'time': t,
            'cumulative': cumulative_time,
            'type': s.get('type', 'action'),
        })

    max_cumulative = max(p['cumulative'] for p in timeline_points) if timeline_points else 1

    # 复杂度分布
    complexity_dist = summary.get('complexity_distribution', {'low': 0, 'medium': 0, 'high': 0})
    total_complexity = sum(complexity_dist.values()) or 1

    # 质量评分（如果有优化数据）
    quality = None
    if optimization:
        quality = optimization.get('quality_score', {})
        gaps = optimization.get('gaps', [])
        redundancies = optimization.get('redundancies', [])
        bottlenecks = optimization.get('bottlenecks', [])
        improvements = optimization.get('improvements', [])
        best_practices = optimization.get('best_practices', [])
    else:
        gaps = []
        redundancies = []
        bottlenecks = []
        improvements = []
        best_practices = []

    # --- 生成 SVG 元素 ---

    # 雷达图
    radar_svg = _generate_radar_chart(quality) if quality else ''

    # 时间线图
    timeline_svg = _generate_timeline_chart(timeline_points, max_cumulative)

    # 饼图
    pie_svg = _generate_pie_chart(complexity_dist, total_complexity)

    # 质量仪表盘
    gauge_svg = _generate_gauge(quality['total_score'], quality['max_score']) if quality else ''

    # 进度条
    progress_svg = _generate_progress_bars(timeline_points, max_cumulative)

    # 改进统计卡片
    gap_count = len(gaps)
    redundancy_count = len(redundancies)
    bottleneck_count = len(bottlenecks)
    improvement_count = len(improvements)

    gap_high = sum(1 for g in gaps if g.get('severity') == 'high')
    gap_medium = sum(1 for g in gaps if g.get('severity') == 'medium')

    # 缺口列表 HTML
    gaps_html = ''
    for g in gaps:
        sev_color = {'high': '#ff4444', 'medium': '#ff9800', 'low': '#2196f3'}.get(g.get('severity', 'low'), '#888')
        gaps_html += f'''
        <div class="gap-item" style="border-left: 3px solid {sev_color};">
            <div class="gap-header">
                <span class="gap-severity" style="background:{sev_color}22;color:{sev_color};">{g.get("severity", "").upper()}</span>
                <span class="gap-message">{g.get("message", "")}</span>
            </div>
            <div class="gap-suggestion">💡 {g.get("suggestion", "")}</div>
        </div>
        '''

    # 改进建议 HTML
    improvements_html = ''
    type_labels = {
        'merge': '合并', 'parallelize': '并行化', 'automate': '自动化',
        'optimize_wait': '等待优化',
    }
    for imp in improvements:
        itype = imp.get('type', '')
        label = type_labels.get(itype, itype)
        improvements_html += f'''
        <div class="improvement-item">
            <span class="imp-type">[{label}]</span>
            <span class="imp-text">{imp.get("suggestion", "")}</span>
        </div>
        '''

    # 最佳实践 HTML
    practices_html = ''
    for bp in best_practices:
        result = bp.get('result', {})
        score = result.get('score', 0)
        max_s = result.get('max', 5)
        pct = score / max_s * 100 if max_s else 0
        color = '#4caf50' if pct >= 75 else ('#ff9800' if pct >= 50 else '#ff4444')
        details = '<br>'.join(result.get('details', []))
        practices_html += f'''
        <div class="practice-item">
            <div class="practice-header">
                <span class="practice-name">{bp.get("name", "")}</span>
                <span class="practice-score" style="color:{color};">{score}/{max_s}</span>
            </div>
            <div class="practice-bar-bg"><div class="practice-bar" style="width:{pct}%;background:{color};"></div></div>
            <div class="practice-details">{details}</div>
        </div>
        '''

    # 步骤时间线
    step_timeline_html = ''
    for s in action_steps:
        idx = s['index']
        stype = s.get('type', 'action')
        comp = s.get('complexity', 'low')
        type_colors = {
            'action': '#00d4ff', 'decision': '#ff9800', 'checkpoint': '#4caf50',
            'wait': '#9c27b0', 'handover': '#ff5722', 'preparation': '#2196f3',
        }
        color = type_colors.get(stype, '#00d4ff')

        step_timeline_html += f'''
        <div class="timeline-step">
            <div class="timeline-dot" style="background:{color};"></div>
            <div class="timeline-content">
                <span class="timeline-step-num">Step {idx}</span>
                <span class="timeline-step-title">{s.get("title", "")}</span>
                <span class="timeline-step-type" style="color:{color};">{stype}</span>
                {f'<span class="timeline-step-time">⏱ {s["time"]["raw"]}</span>' if s.get("time") else ''}
            </div>
        </div>
        '''

    # ============================================================

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{meta.get("title", "SOP")} — 分析报告</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #0a0a14;
    color: #e0e0e0;
    line-height: 1.6;
    padding: 20px;
}}
.container {{ max-width: 1100px; margin: 0 auto; }}

/* 头部 */
.report-header {{
    background: linear-gradient(135deg, #00d4ff15, #ff6b6b15);
    border: 1px solid #2a2a4a;
    border-radius: 16px;
    padding: 36px;
    margin-bottom: 28px;
    text-align: center;
}}
.report-header h1 {{ font-size: 32px; background: linear-gradient(90deg, #00d4ff, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
.report-header .subtitle {{ color: #888; margin-top: 8px; font-size: 14px; }}

/* 统计面板 */
.stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 16px;
    margin-bottom: 28px;
}}
.stat-card {{
    background: #1a1a2e;
    border: 1px solid #2a2a4a;
    border-radius: 12px;
    padding: 20px;
    text-align: center;
    transition: all 0.3s;
}}
.stat-card:hover {{ border-color: #00d4ff44; box-shadow: 0 4px 24px rgba(0,212,255,0.08); }}
.stat-card .stat-value {{ font-size: 32px; font-weight: 800; color: #00d4ff; }}
.stat-card .stat-label {{ font-size: 13px; color: #888; margin-top: 6px; }}
.stat-card .stat-sub {{ font-size: 12px; color: #666; margin-top: 4px; }}

/* 双列布局 */
.two-col {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 28px;
}}
@media (max-width: 768px) {{ .two-col {{ grid-template-columns: 1fr; }} }}

/* 面板 */
.panel {{
    background: #1a1a2e;
    border: 1px solid #2a2a4a;
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 28px;
}}
.panel h2 {{
    font-size: 20px;
    color: #00d4ff;
    margin-bottom: 20px;
    padding-bottom: 10px;
    border-bottom: 1px solid #2a2a4a;
}}

/* 缺口列表 */
.gap-item {{
    background: #0f0f1a;
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 12px;
}}
.gap-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
.gap-severity {{
    font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 700; flex-shrink: 0;
}}
.gap-message {{ font-size: 15px; font-weight: 600; }}
.gap-suggestion {{ font-size: 13px; color: #aaa; padding-left: 4px; }}

/* 改进项 */
.improvement-item {{
    background: #0f0f1a; border-radius: 8px; padding: 12px 16px;
    margin-bottom: 10px; display: flex; gap: 10px; align-items: flex-start;
}}
.imp-type {{
    color: #00d4ff; font-size: 12px; font-weight: 700; flex-shrink: 0;
    background: #00d4ff15; padding: 2px 8px; border-radius: 6px;
}}
.imp-text {{ font-size: 14px; }}

/* 最佳实践 */
.practice-item {{
    background: #0f0f1a; border-radius: 8px; padding: 16px; margin-bottom: 12px;
}}
.practice-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
.practice-name {{ font-weight: 600; font-size: 15px; }}
.practice-score {{ font-weight: 700; font-size: 18px; }}
.practice-bar-bg {{
    height: 6px; background: #2a2a4a; border-radius: 3px; margin-bottom: 8px; overflow: hidden;
}}
.practice-bar {{ height: 100%; border-radius: 3px; transition: width 0.5s ease; }}
.practice-details {{ font-size: 12px; color: #888; }}

/* 时间线 */
.timeline-step {{
    display: flex; align-items: flex-start; gap: 14px;
    padding: 10px 0; position: relative;
}}
.timeline-step::before {{
    content: '';
    position: absolute; left: 7px; top: 28px; bottom: -10px;
    width: 2px; background: #2a2a4a;
}}
.timeline-step:last-child::before {{ display: none; }}
.timeline-dot {{
    width: 16px; height: 16px; border-radius: 50%; flex-shrink: 0;
    margin-top: 2px; box-shadow: 0 0 10px currentColor;
}}
.timeline-content {{
    flex: 1; display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
}}
.timeline-step-num {{ font-weight: 700; color: #00d4ff; font-size: 14px; }}
.timeline-step-title {{ font-size: 14px; }}
.timeline-step-type {{
    font-size: 11px; padding: 1px 8px; border-radius: 10px;
    background: #ffffff08;
}}
.timeline-step-time {{ font-size: 12px; color: #888; }}

/* 空状态 */
.empty-state {{ text-align: center; padding: 40px; color: #555; }}
.empty-state p {{ font-size: 14px; }}

/* SVG */
.chart-container {{ margin-bottom: 20px; }}
.chart-container svg {{ max-width: 100%; }}

/* 进度条组 */
.progress-bars {{ display: flex; flex-direction: column; gap: 8px; }}
.progress-row {{ display: flex; align-items: center; gap: 10px; }}
.progress-label {{ width: 60px; font-size: 12px; color: #888; text-align: right; flex-shrink: 0; }}
.progress-track {{ flex: 1; height: 20px; background: #2a2a4a; border-radius: 10px; overflow: hidden; }}
.progress-fill {{ height: 100%; border-radius: 10px; transition: width 0.6s ease; }}
.progress-val {{ width: 50px; font-size: 12px; color: #aaa; }}
</style>
</head>
<body>
<div class="container">

    <!-- 报告头 -->
    <div class="report-header">
        <h1>{meta.get("title", "SOP 分析报告")}</h1>
        <p class="subtitle">
            v{meta.get("version", "1.0")} ·
            提取时间: {meta.get("created_at", "-")[:10]} ·
            {total_steps} 步骤 · {meta.get("estimated_total_time_display", "-")}
        </p>
    </div>

    <!-- 统计面板 -->
    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-value">{total_steps}</div>
            <div class="stat-label">总步骤数</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{meta.get("estimated_total_time_display", "-")}</div>
            <div class="stat-label">预估总耗时</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{meta.get("overall_complexity", "-")}</div>
            <div class="stat-label">整体复杂度</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{summary.get("total_checkpoints", 0)}</div>
            <div class="stat-label">检查点</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{summary.get("total_cautions", 0)}</div>
            <div class="stat-label">注意事项</div>
        </div>
    </div>

    <!-- 进度条 (步骤时间分布) -->
    <div class="panel">
        <h2>⏱ 步骤耗时分布</h2>
        {progress_svg}
    </div>

    <!-- 双列: 复杂度饼图 + 时间线图 -->
    <div class="two-col">
        <div class="panel">
            <h2>📊 复杂度分布</h2>
            {pie_svg}
        </div>
        <div class="panel">
            <h2>📈 累计时间曲线</h2>
            {timeline_svg}
        </div>
    </div>

    <!-- 步骤时间线 -->
    <div class="panel">
        <h2>🔗 流程步骤时间线</h2>
        {step_timeline_html}
    </div>

    <!-- 优化分析（如果有） -->
    {f'''
    <!-- 质量评分 -->
    <div class="panel">
        <h2>🎯 SOP 质量评分</h2>
        <div style="display:flex; align-items:center; gap:30px; flex-wrap:wrap;">
            <div style="flex-shrink:0;">{gauge_svg}</div>
            <div style="flex:1; min-width:250px;">
    ''' if quality else ''}

    {_generate_quality_detail_html(quality) if quality else ''}

    {f'''
            </div>
        </div>
    </div>
    ''' if quality else ''}

    <!-- 缺口分析 -->
    {f'''
    <div class="panel">
        <h2>🔍 缺口分析 <span style="font-size:14px;color:#888;">({gap_count} 个缺口)</span></h2>
        {gaps_html if gaps else '<div class="empty-state"><p>✅ 未发现明显缺口，SOP结构完整</p></div>'}
    </div>
    ''' if optimization else ''}

    <!-- 改进建议 -->
    {f'''
    <div class="panel">
        <h2>💡 改进建议 <span style="font-size:14px;color:#888;">({improvement_count} 条)</span></h2>
        {improvements_html if improvements else '<div class="empty-state"><p>暂无改进建议</p></div>'}
    </div>
    ''' if optimization else ''}

    <!-- 最佳实践 -->
    {f'''
    <div class="panel">
        <h2>🏆 最佳实践对齐</h2>
        {practices_html if best_practices else '<div class="empty-state"><p>暂无最佳实践评估数据</p></div>'}
    </div>
    ''' if optimization else ''}

    <!-- 雷达图 -->
    {f'''
    <div class="panel">
        <h2>🎯 多维度评估雷达</h2>
        {radar_svg}
    </div>
    ''' if quality else ''}

</div>
</body>
</html>'''

    # 写入文件
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    return html


# ============================================================
# SVG 图表生成
# ============================================================

def _generate_radar_chart(quality: dict) -> str:
    """生成 SVG 雷达图"""
    if not quality:
        return ''
    dims = quality.get('dimensions', {})
    labels = []
    values = []
    max_vals = []
    for k, v in dims.items():
        labels.append(k)
        values.append(v)
        # 从 details 获取满分
        max_vals.append(25 if k == 'completeness' else (20 if k == 'actionability' else (15 if k in ('clarity', 'safety') else (10 if k in ('measurability', 'efficiency') else 5))))

    n = len(labels)
    if n < 3:
        return ''

    cx, cy, r = 180, 170, 120
    width, height = 380, 360

    svg_parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']

    # 背景网格
    levels = 5
    for level in range(1, levels + 1):
        scale = level / levels
        points = []
        for i in range(n):
            angle = -math.pi / 2 + 2 * math.pi * i / n
            x = cx + r * scale * math.cos(angle)
            y = cy + r * scale * math.sin(angle)
            points.append(f'{x:.1f},{y:.1f}')
        opacity = 0.15 if level == levels else 0.08
        svg_parts.append(f'<polygon points="{" ".join(points)}" fill="none" stroke="#2a2a4a" stroke-width="1" opacity="{opacity}"/>')

    # 轴线
    for i in range(n):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        svg_parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#2a2a4a" stroke-width="1"/>')

    # 数据区域
    data_points = []
    for i in range(n):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        max_v = max_vals[i] if max_vals[i] > 0 else 1
        scale = values[i] / max_v
        x = cx + r * scale * math.cos(angle)
        y = cy + r * scale * math.sin(angle)
        data_points.append(f'{x:.1f},{y:.1f}')

    svg_parts.append(f'<polygon points="{" ".join(data_points)}" fill="#00d4ff22" stroke="#00d4ff" stroke-width="2"/>')

    # 数据点
    for i in range(n):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        max_v = max_vals[i] if max_vals[i] > 0 else 1
        scale = values[i] / max_v
        x = cx + r * scale * math.cos(angle)
        y = cy + r * scale * math.sin(angle)
        svg_parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#00d4ff"/>')

    # 标签
    label_names = {
        'completeness': '完整性', 'clarity': '清晰度', 'actionability': '可操作性',
        'safety': '安全性', 'measurability': '可度量性', 'efficiency': '效率',
        'maintainability': '可维护性',
    }
    for i in range(n):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        x = cx + (r + 30) * math.cos(angle)
        y = cy + (r + 30) * math.sin(angle)
        label = label_names.get(labels[i], labels[i])
        pct = int(values[i] / max_vals[i] * 100) if max_vals[i] else 0
        anchor = 'middle'
        if x < cx - 20:
            anchor = 'end'
        elif x > cx + 20:
            anchor = 'start'
        svg_parts.append(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" fill="#aaa" font-size="11">{label} ({pct}%)</text>')

    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)


def _generate_timeline_chart(points: list, max_cumulative: float) -> str:
    """生成 SVG 累计时间曲线"""
    if not points:
        return ''

    w, h = 340, 200
    margin_l, margin_r, margin_t, margin_b = 45, 15, 15, 35
    plot_w = w - margin_l - margin_r
    plot_h = h - margin_t - margin_b

    svg_parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">']

    # 网格线
    for i in range(5):
        y = margin_t + plot_h * i / 4
        svg_parts.append(f'<line x1="{margin_l}" y1="{y:.1f}" x2="{w-margin_r}" y2="{y:.1f}" stroke="#2a2a4a" stroke-width="1"/>')

    # 数据线
    path_parts = []
    for i, p in enumerate(points):
        x = margin_l + plot_w * i / max(len(points) - 1, 1)
        y = margin_t + plot_h * (1 - p['cumulative'] / max_cumulative)
        path_parts.append(f'{x:.1f},{y:.1f}')

    # 折线
    svg_parts.append(f'<polyline points="{" ".join(path_parts)}" fill="none" stroke="#00d4ff" stroke-width="2"/>')

    # 面积
    area_points = path_parts.copy()
    area_points.insert(0, f'{margin_l:.1f},{margin_t + plot_h:.1f}')
    area_points.append(f'{margin_l + plot_w * (len(points) - 1) / max(len(points) - 1, 1):.1f},{margin_t + plot_h:.1f}')
    svg_parts.append(f'<polygon points="{" ".join(area_points)}" fill="#00d4ff11"/>')

    # 数据点
    for i, p in enumerate(points):
        x = margin_l + plot_w * i / max(len(points) - 1, 1)
        y = margin_t + plot_h * (1 - p['cumulative'] / max_cumulative)
        svg_parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#00d4ff"/>')

    # Y轴标签
    for i in range(5):
        val = max_cumulative * (4 - i) / 4
        y = margin_t + plot_h * i / 4
        label = f'{int(val)}分' if val >= 1 else '0'
        svg_parts.append(f'<text x="{margin_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#666" font-size="10">{label}</text>')

    # X轴
    svg_parts.append(f'<line x1="{margin_l}" y1="{margin_t + plot_h}" x2="{w-margin_r}" y2="{margin_t + plot_h}" stroke="#2a2a4a" stroke-width="1"/>')

    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)


def _generate_pie_chart(dist: dict, total: int) -> str:
    """生成 SVG 饼图"""
    if total == 0:
        return ''

    colors = {'low': '#4caf50', 'medium': '#ff9800', 'high': '#ff4444'}
    labels_cn = {'low': '低', 'medium': '中', 'high': '高'}

    cx, cy, r = 150, 130, 90
    w, h = 340, 220

    svg_parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">']

    start_angle = -math.pi / 2
    for key in ['low', 'medium', 'high']:
        count = dist.get(key, 0)
        if count == 0:
            continue
        angle = 2 * math.pi * count / total
        end_angle = start_angle + angle

        # 弧形路径
        x1 = cx + r * math.cos(start_angle)
        y1 = cy + r * math.sin(start_angle)
        x2 = cx + r * math.cos(end_angle)
        y2 = cy + r * math.sin(end_angle)
        large_arc = 1 if angle > math.pi else 0

        d = f'M {cx},{cy} L {x1:.1f},{y1:.1f} A {r},{r} 0 {large_arc} 1 {x2:.1f},{y2:.1f} Z'
        svg_parts.append(f'<path d="{d}" fill="{colors[key]}" opacity="0.8"/>')

        # 标签
        mid_angle = start_angle + angle / 2
        lx = cx + (r + 30) * math.cos(mid_angle)
        ly = cy + (r + 30) * math.sin(mid_angle)
        svg_parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" fill="#aaa" font-size="11">{labels_cn[key]} ({count})</text>')

        start_angle = end_angle

    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)


def _generate_gauge(score: int, max_score: int) -> str:
    """生成 SVG 仪表盘"""
    pct = score / max_score * 100 if max_score else 0
    color = '#4caf50' if pct >= 85 else ('#ff9800' if pct >= 70 else ('#ff6b6b' if pct >= 55 else '#ff4444'))

    w, h = 200, 140
    cx, cy, r = 100, 110, 70

    svg = f'''<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <linearGradient id="gaugeGrad" x1="0%25" y1="0%25" x2="100%25" y2="0%25">
            <stop offset="0%25" stop-color="#ff4444"/>
            <stop offset="50%25" stop-color="#ff9800"/>
            <stop offset="100%25" stop-color="#4caf50"/>
        </linearGradient>
    </defs>
    <!-- 背景弧 -->
    <path d="M {cx-r*0.8:.0f} {cy+10:.0f} A {r:.0f} {r:.0f} 0 0 1 {cx+r*0.8:.0f} {cy+10:.0f}" fill="none" stroke="#1a1a2e" stroke-width="14" stroke-linecap="round"/>
    <!-- 值弧 -->
    <path d="M {cx-r*0.8:.0f} {cy+10:.0f} A {r:.0f} {r:.0f} 0 0 1 {cx+r*0.8:.0f} {cy+10:.0f}" fill="none" stroke="{color}" stroke-width="14" stroke-linecap="round" stroke-dasharray="{pct*2.2:.0f} 220" stroke-dashoffset="0"/>
    <!-- 中心数字 -->
    <text x="{cx}" y="{cy-10}" text-anchor="middle" fill="{color}" font-size="36" font-weight="700">{score}</text>
    <text x="{cx}" y="{cy+18}" text-anchor="middle" fill="#666" font-size="12">/ {max_score}</text>
</svg>'''
    return svg


def _generate_progress_bars(points: list, max_val: float) -> str:
    """生成步骤耗时进度条"""
    if not points:
        return ''

    colors = {
        'action': 'linear-gradient(90deg, #00d4ff, #0099cc)',
        'decision': 'linear-gradient(90deg, #ff9800, #cc7700)',
        'checkpoint': 'linear-gradient(90deg, #4caf50, #388e3c)',
        'wait': 'linear-gradient(90deg, #9c27b0, #7b1fa2)',
        'handover': 'linear-gradient(90deg, #ff5722, #cc3300)',
        'preparation': 'linear-gradient(90deg, #2196f3, #1565c0)',
    }

    bars = []
    for p in points:
        pct = p['time'] / max_val * 100
        color = colors.get(p['type'], colors['action'])
        bars.append(f'''
        <div class="progress-row">
            <div class="progress-label">Step {p["step"]}</div>
            <div class="progress-track">
                <div class="progress-fill" style="width:{pct:.1f}%;background:{color};"></div>
            </div>
            <div class="progress-val">{p["time"]}分</div>
        </div>''')

    return '<div class="progress-bars">' + '\n'.join(bars) + '</div>'


def _generate_quality_detail_html(quality: dict) -> str:
    """生成质量评分详情 HTML"""
    if not quality:
        return ''
    dims = quality.get('dimensions', {})
    dim_labels = {
        'completeness': '完整性', 'clarity': '清晰度', 'actionability': '可操作性',
        'safety': '安全性', 'measurability': '可度量性', 'efficiency': '效率',
        'maintainability': '可维护性',
    }

    rows = []
    for k, v in dims.items():
        label = dim_labels.get(k, k)
        max_v = 25 if k == 'completeness' else (20 if k == 'actionability' else (15 if k in ('clarity', 'safety') else (10 if k in ('measurability', 'efficiency') else 5)))
        pct = v / max_v * 100
        color = '#4caf50' if pct >= 75 else ('#ff9800' if pct >= 50 else '#ff4444')
        rows.append(f'''
        <div style="margin-bottom:12px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px;font-size:13px;">
                <span>{label}</span>
                <span style="color:{color};">{v}/{max_v}</span>
            </div>
            <div style="height:6px;background:#2a2a4a;border-radius:3px;overflow:hidden;">
                <div style="width:{pct}%;height:100%;background:{color};border-radius:3px;"></div>
            </div>
        </div>''')

    grade = quality.get('grade', '?')
    grade_label = quality.get('grade_label', '?')
    total = quality.get('total_score', 0)

    return f'''
    <div style="text-align:center;margin-bottom:16px;">
        <span style="font-size:48px;font-weight:800;color:#00d4ff;">{grade}</span>
        <span style="font-size:16px;color:#888;margin-left:8px;">{grade_label}</span>
        <div style="font-size:14px;color:#666;margin-top:4px;">总分 {total}/100</div>
    </div>
    {"".join(rows)}
    '''


# ============================================================
# 命令行接口
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='SOP 可视化报告生成器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python report_generator.py -s sop_output.json -o report.html
  python report_generator.py -s sop_output.json --optimization optimization_report.json -o full_report.html
        """
    )
    parser.add_argument('-s', '--sop', type=str, required=True,
                        help='sop_extractor.py 输出的 SOP JSON 文件')
    parser.add_argument('--optimization', type=str, default=None,
                        help='sop_optimizer.py 输出的优化报告 JSON（可选）')
    parser.add_argument('-o', '--output', type=str, default='sop_report.html',
                        help='输出 HTML 文件路径')

    args = parser.parse_args()

    # 读取 SOP
    with open(args.sop, 'r', encoding='utf-8') as f:
        sop = json.load(f)

    # 读取优化报告（可选）
    optimization = None
    if args.optimization:
        with open(args.optimization, 'r', encoding='utf-8') as f:
            optimization = json.load(f)

    # 生成报告
    generate_report(sop, optimization, args.output)

    size = os.path.getsize(args.output)
    print(f'[SOP Report] 可视化报告已生成: {args.output} ({size:,} bytes)')


if __name__ == '__main__':
    main()
