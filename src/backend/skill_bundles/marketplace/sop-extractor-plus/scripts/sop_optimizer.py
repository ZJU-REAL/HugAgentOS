#!/usr/bin/env python3
"""
SOP 标准化流程提取器 — SOP优化分析器
对已提取的 SOP 进行质量评估、缺口分析和改进建议。
"""

import json
import sys
import os
import re
from collections import Counter


# ============================================================
# 质量评估维度
# ============================================================

QUALITY_DIMENSIONS = [
    'completeness',       # 完整性：是否有明确的开始/结束
    'clarity',            # 清晰度：步骤描述是否清楚
    'actionability',      # 可操作性：是否有具体动作
    'safety',             # 安全性：是否有注意事项
    'measurability',      # 可度量性：是否有检查点/验收标准
    'efficiency',         # 效率：是否有冗余或可并行步骤
    'maintainability',    # 可维护性：结构是否清晰
    'scalability',        # 可扩展性：是否易于更新
]


# ============================================================
# 缺口分析模式
# ============================================================

MISSING_PATTERNS = {
    'no_start_condition': {
        'check': lambda sop: not any(
            s.get('type') in ('preparation',) or
            re.search(r'(准备|前提|前置|条件|需要.*先|在此之前)', s.get('description', ''))
            for s in sop.get('steps', [])
        ),
        'message': '缺少前置条件和准备工作说明',
        'severity': 'medium',
        'suggestion': '建议增加"前置条件"步骤，列出开始前的准备工作、所需权限、系统状态等。',
    },
    'no_end_condition': {
        'check': lambda sop: not any(
            re.search(r'(完成|结束|收尾|最终|交付|归档)', s.get('description', ''))
            for s in sop.get('steps', [])[-3:]
        ),
        'message': '缺少明确的结束条件和收尾步骤',
        'severity': 'medium',
        'suggestion': '建议增加"收尾与交付"步骤，明确流程完成的标志和交付物。',
    },
    'no_checkpoints': {
        'check': lambda sop: sop.get('summary', {}).get('total_checkpoints', 0) == 0,
        'message': '流程中没有任何检查点（验证步骤）',
        'severity': 'high',
        'suggestion': '建议在关键步骤后增加检查点，如"确认数据已导入"、"验证结果正确性"。',
    },
    'no_cautions': {
        'check': lambda sop: sop.get('summary', {}).get('total_cautions', 0) == 0,
        'message': '流程中没有注意事项标记',
        'severity': 'medium',
        'suggestion': '建议标注容易出错的操作，添加注意事项和常见错误提醒。',
    },
    'no_decision_logic': {
        'check': lambda sop: not any(
            s.get('type') == 'decision' for s in sop.get('steps', [])
        ),
        'message': '流程中缺少决策/分支逻辑',
        'severity': 'low',
        'suggestion': '如果流程中存在条件判断，建议标注决策点，如"如果A则执行B，否则执行C"。',
    },
    'no_time_estimate': {
        'check': lambda sop: not any(
            s.get('time') for s in sop.get('steps', [])
        ),
        'message': '所有步骤都没有时间估算',
        'severity': 'low',
        'suggestion': '为每个步骤估算耗时，便于资源规划和SLA设定。',
    },
    'no_roles': {
        'check': lambda sop: not any(
            s.get('roles') for s in sop.get('steps', [])
        ),
        'message': '未明确各步骤的执行角色',
        'severity': 'low',
        'suggestion': '为每个步骤标注责任人/角色，如"客服执行"、"经理审批"。',
    },
    'too_many_high_complexity': {
        'check': lambda sop: sop.get('summary', {}).get('complexity_distribution', {}).get('high', 0) > len(sop.get('steps', [])) * 0.5,
        'message': '高复杂度步骤占比过高',
        'severity': 'medium',
        'suggestion': '考虑将高复杂度步骤拆分为更细粒度的子步骤，降低执行难度。',
    },
}


# ============================================================
# 冗余检测
# ============================================================

def detect_redundancy(sop: dict) -> list:
    """
    检测 SOP 中的冗余步骤。
    返回: [{'step_a': int, 'step_b': int, 'similarity': float, 'reason': str}, ...]
    """
    redundancies = []
    steps = sop.get('steps', [])

    for i, s1 in enumerate(steps):
        for j, s2 in enumerate(steps):
            if j <= i:
                continue
            # 简单相似度：标题或描述的关键词重合度
            words1 = set(re.findall(r'[\u4e00-\u9fff\w]+', s1.get('description', '')))
            words2 = set(re.findall(r'[\u4e00-\u9fff\w]+', s2.get('description', '')))

            if not words1 or not words2:
                continue

            intersection = words1 & words2
            union = words1 | words2
            jaccard = len(intersection) / len(union) if union else 0

            if jaccard > 0.5:
                redundancies.append({
                    'step_a': s1['index'],
                    'step_b': s2['index'],
                    'similarity': round(jaccard, 2),
                    'reason': f'步骤{s1["index"]}与步骤{s2["index"]}描述高度相似（重合度 {jaccard:.0%}），可能存在冗余。',
                })

    return redundancies


# ============================================================
# 瓶颈识别
# ============================================================

def detect_bottlenecks(sop: dict) -> list:
    """
    识别流程中的瓶颈步骤。
    返回: [{'step': int, 'reason': str}, ...]
    """
    bottlenecks = []
    steps = sop.get('steps', [])

    for s in steps:
        reasons = []

        # 耗时过长
        time_val = (s.get('time') or {}).get('value', 0)
        if time_val > 60:
            reasons.append(f'耗时 {time_val} 分钟，超过1小时')

        # 被多次依赖
        dep_count = sum(1 for other in steps if s['index'] in other.get('dependencies', []))
        if dep_count >= 3:
            reasons.append(f'被 {dep_count} 个后续步骤依赖，是关键路径节点')

        # 高复杂度
        if s.get('complexity') == 'high':
            reasons.append('复杂度为"高"，可能需要拆分')

        # 多个工具
        if len(s.get('tools', [])) >= 4:
            reasons.append(f'需要 {len(s["tools"])} 个工具/系统')

        if reasons:
            bottlenecks.append({
                'step': s['index'],
                'title': s.get('title', ''),
                'reasons': reasons,
            })

    return bottlenecks


# ============================================================
# 效率改进建议
# ============================================================

def suggest_improvements(sop: dict) -> list:
    """
    生成效率改进建议。
    """
    suggestions = []
    steps = sop.get('steps', [])

    # 规则1: 连续相同角色的步骤可以合并
    for i in range(len(steps) - 1):
        s1 = steps[i]
        s2 = steps[i + 1]
        s1_roles = set(s1.get('roles', []))
        s2_roles = set(s2.get('roles', []))
        if s1_roles and s2_roles and s1_roles == s2_roles:
            if s1.get('type') == 'action' and s2.get('type') == 'action':
                suggestions.append({
                    'type': 'merge',
                    'target': [s1['index'], s2['index']],
                    'suggestion': f'步骤{s1["index"]}和步骤{s2["index"]}由同一角色连续执行，可考虑合并为原子操作。',
                })

    # 规则2: 独立步骤可以并行
    for i in range(len(steps)):
        for j in range(i + 1, len(steps)):
            s1 = steps[i]
            s2 = steps[j]
            # 如果两个步骤都不互相依赖，且角色不同
            if (s2['index'] not in s1.get('dependencies', []) and
                s1['index'] not in s2.get('dependencies', []) and
                s1.get('roles') and s2.get('roles') and
                set(s1.get('roles', [])) != set(s2.get('roles', []))):
                suggestions.append({
                    'type': 'parallelize',
                    'target': [s1['index'], s2['index']],
                    'suggestion': f'步骤{s1["index"]}（{s1.get("title","")}）和步骤{s2["index"]}（{s2.get("title","")}）可并行执行。',
                })
                break  # 每个步骤只给一个并行建议

    # 规则3: 添加模板化/自动化建议
    auto_keywords = ['复制', '粘贴', '填写', '手动', '录入', '逐个', '重复']
    for s in steps:
        desc = s.get('description', '')
        if any(kw in desc for kw in auto_keywords):
            suggestions.append({
                'type': 'automate',
                'target': [s['index']],
                'suggestion': f'步骤{s["index"]}包含重复性操作，可考虑通过脚本/模板自动化。',
            })

    # 规则4: 等待步骤优化
    for s in steps:
        if s.get('type') == 'wait':
            suggestions.append({
                'type': 'optimize_wait',
                'target': [s['index']],
                'suggestion': f'步骤{s["index"]}是等待步骤，可考虑异步通知或并行处理减少等待时间。',
            })

    return suggestions


# ============================================================
# 最佳实践对齐
# ============================================================

BEST_PRACTICES = [
    {
        'id': 'smart_sop',
        'name': 'SMART原则',
        'check': lambda sop: _check_smart(sop),
        'description': 'SOP应符合SMART原则：Specific（具体）、Measurable（可度量）、Achievable（可实现）、Relevant（相关）、Time-bound（有时限）',
    },
    {
        'id': '5w1h',
        'name': '5W1H完整性',
        'check': lambda sop: _check_5w1h(sop),
        'description': '每个步骤应尽可能回答 Who（谁做）、What（做什么）、When（何时做）、Where（在哪做）、Why（为什么）、How（怎么做）',
    },
    {
        'id': 'pdca',
        'name': 'PDCA循环',
        'check': lambda sop: _check_pdca(sop),
        'description': 'SOP应包含 Plan（计划）、Do（执行）、Check（检查）、Act（改进）的完整循环',
    },
]


def _check_smart(sop: dict) -> dict:
    """检查是否符合 SMART 原则"""
    score = 0
    details = []

    # Specific: 有明确步骤描述
    steps_with_desc = sum(1 for s in sop['steps'] if len(s.get('description', '')) > 15)
    if steps_with_desc >= len(sop['steps']) * 0.7:
        score += 1
        details.append('Specific — 步骤描述具体')
    else:
        details.append('Specific — 部分步骤描述过于简短，建议补充细节')

    # Measurable: 有检查点
    if sop['summary'].get('total_checkpoints', 0) > 0:
        score += 1
        details.append('Measurable — 包含检查点/验收标准')
    else:
        details.append('Measurable — 缺少检查点，建议增加验证步骤')

    # Achievable: 复杂度分布合理
    c_dist = sop['summary'].get('complexity_distribution', {})
    if c_dist.get('high', 0) <= len(sop['steps']) * 0.3:
        score += 1
        details.append('Achievable — 复杂度分布合理')
    else:
        details.append('Achievable — 高复杂度步骤偏多，建议拆分')

    # Time-bound: 有时间估算
    if any(s.get('time') for s in sop['steps']):
        score += 1
        details.append('Time-bound — 包含时间估算')
    else:
        details.append('Time-bound — 缺少时间估算')

    return {
        'score': score,
        'max': 4,
        'details': details,
    }


def _check_5w1h(sop: dict) -> dict:
    """检查 5W1H 覆盖度"""
    steps = sop['steps']
    total = len(steps)
    if total == 0:
        return {'score': 0, 'max': 6, 'details': ['无步骤数据']}

    who_count = sum(1 for s in steps if s.get('roles'))
    what_count = sum(1 for s in steps if len(s.get('description', '')) > 10)
    when_count = sum(1 for s in steps if s.get('time'))
    where_count = sum(1 for s in steps if s.get('tools'))
    why_count = sum(1 for s in steps if re.search(r'(为了|目的|原因|因为|确保|防止|避免)', s.get('description', '')))
    how_count = sum(1 for s in steps if s.get('type') != 'overview')

    scores = [
        ('Who（谁做）', who_count / total),
        ('What（做什么）', what_count / total),
        ('When（何时）', when_count / total),
        ('Where（用什么）', where_count / total),
        ('Why（为什么）', why_count / total),
        ('How（怎么做）', how_count / total),
    ]

    total_score = sum(1 for _, r in scores if r > 0.3)
    details = [f'{name}: {"已覆盖" if ratio > 0.3 else "待完善"} ({ratio:.0%})' for name, ratio in scores]

    return {'score': total_score, 'max': 6, 'details': details}


def _check_pdca(sop: dict) -> dict:
    """检查 PDCA 循环完整性"""
    score = 0
    details = []
    steps = sop['steps']
    descs = ' '.join(s.get('description', '') for s in steps)

    # Plan: 有计划准备步骤
    if any(s.get('type') == 'preparation' for s in steps):
        score += 1
        details.append('Plan（计划）— 有准备步骤')
    else:
        details.append('Plan（计划）— 缺少计划/准备步骤')

    # Do: 有执行步骤（必有）
    if any(s.get('type') == 'action' for s in steps):
        score += 1
        details.append('Do（执行）— 有操作步骤')
    else:
        details.append('Do（执行）— 缺少操作步骤')

    # Check: 有检查步骤
    if any(s.get('type') == 'checkpoint' for s in steps):
        score += 1
        details.append('Check（检查）— 有验证步骤')
    else:
        details.append('Check（检查）— 缺少验证步骤')

    # Act: 有改进/收尾
    if re.search(r'(改进|优化|复盘|总结|归档|记录)', descs):
        score += 1
        details.append('Act（改进）— 有收尾/改进步骤')
    else:
        details.append('Act（改进）— 建议增加复盘/总结步骤')

    return {'score': score, 'max': 4, 'details': details}


# ============================================================
# 质量评分
# ============================================================

def score_quality(sop: dict) -> dict:
    """
    对 SOP 进行多维质量评分（百分制）。
    """
    scores = {}
    details = {}
    steps = sop.get('steps', [])
    total = max(len(steps), 1)
    summary = sop.get('summary', {})

    # 1. 完整性 (25分)
    has_start = any(s.get('type') == 'preparation' for s in steps)
    has_end = any(re.search(r'(完成|结束|收尾|交付)', s.get('description', '')) for s in steps[-3:])
    completeness = 0
    if has_start:
        completeness += 8
    if has_end:
        completeness += 7
    if total >= 3:
        completeness += 5
    # 步骤连续性
    has_gaps = any(abs(steps[i]['index'] - steps[i - 1]['index']) > 1 for i in range(1, len(steps)))
    if not has_gaps:
        completeness += 5
    scores['completeness'] = min(completeness, 25)
    details['completeness'] = {
        'start_defined': has_start,
        'end_defined': has_end,
        'minimal_steps': total >= 3,
        'no_gaps': not has_gaps,
    }

    # 2. 清晰度 (15分)
    avg_desc_len = sum(len(s.get('description', '')) for s in steps) / total
    clarity = 0
    if avg_desc_len > 20:
        clarity += 5
    elif avg_desc_len > 10:
        clarity += 3
    if total <= 15:
        clarity += 5  # 步骤数合理
    if all(not re.search(r'[?？]$', s.get('title', '')) for s in steps):
        clarity += 5  # 标题陈述清晰
    scores['clarity'] = min(clarity, 15)
    details['clarity'] = {
        'avg_description_length': round(avg_desc_len, 1),
        'reasonable_step_count': total <= 15,
    }

    # 3. 可操作性 (20分)
    action_verbs_in_steps = sum(
        1 for s in steps
        for v in ['打开', '点击', '输入', '选择', '设置', '创建', '保存', '发送', '填写', '导入']
        if v in s.get('description', '')
    )
    actionability = 0
    if action_verbs_in_steps >= total * 0.5:
        actionability += 10
    elif action_verbs_in_steps > 0:
        actionability += 5
    if summary.get('total_tools', 0) > 0:
        actionability += 5
    if any(s.get('output') for s in steps):
        actionability += 5
    scores['actionability'] = min(actionability, 20)
    details['actionability'] = {
        'has_action_verbs': action_verbs_in_steps > 0,
        'tools_specified': summary.get('total_tools', 0) > 0,
        'outputs_defined': any(s.get('output') for s in steps),
    }

    # 4. 安全性 (15分)
    safety = 0
    if summary.get('total_cautions', 0) >= total * 0.3:
        safety += 10
    elif summary.get('total_cautions', 0) > 0:
        safety += 5
    if summary.get('total_checkpoints', 0) > 0:
        safety += 5
    scores['safety'] = min(safety, 15)
    details['safety'] = {
        'cautions_count': summary.get('total_cautions', 0),
        'checkpoints_count': summary.get('total_checkpoints', 0),
    }

    # 5. 可度量性 (10分)
    measurability = 0
    if summary.get('total_checkpoints', 0) >= 1:
        measurability += 5
    if any(s.get('time') for s in steps):
        measurability += 5
    scores['measurability'] = min(measurability, 10)
    details['measurability'] = {
        'has_checkpoints': summary.get('total_checkpoints', 0) > 0,
        'has_time_estimates': any(s.get('time') for s in steps),
    }

    # 6. 效率 (10分)
    redundancy = detect_redundancy(sop)
    bottlenecks = detect_bottlenecks(sop)
    efficiency = 10
    if redundancy:
        efficiency -= min(len(redundancy) * 3, 5)
    if bottlenecks:
        efficiency -= min(len(bottlenecks) * 2, 3)
    scores['efficiency'] = max(efficiency, 0)
    details['efficiency'] = {
        'redundancy_count': len(redundancy),
        'bottleneck_count': len(bottlenecks),
    }

    # 7. 可维护性 (5分)
    maintainability = 0
    if all(s.get('type') for s in steps):
        maintainability += 2
    if total <= 20:
        maintainability += 3
    scores['maintainability'] = min(maintainability, 5)

    total_score = sum(scores.values())
    # 评级
    if total_score >= 85:
        grade = 'A'
        grade_label = '优秀'
    elif total_score >= 70:
        grade = 'B'
        grade_label = '良好'
    elif total_score >= 55:
        grade = 'C'
        grade_label = '合格'
    else:
        grade = 'D'
        grade_label = '需改进'

    return {
        'total_score': total_score,
        'max_score': 100,
        'grade': grade,
        'grade_label': grade_label,
        'dimensions': scores,
        'details': details,
    }


# ============================================================
# 主优化函数
# ============================================================

def optimize(sop: dict) -> dict:
    """
    对 SOP 进行全方位分析和优化建议。
    输入: extract_sop() 返回的 SOP 字典
    输出: 优化分析报告
    """
    # 质量评分
    quality = score_quality(sop)

    # 缺口分析
    gaps = []
    for gap_id, gap_info in MISSING_PATTERNS.items():
        if gap_info['check'](sop):
            gaps.append({
                'id': gap_id,
                'message': gap_info['message'],
                'severity': gap_info['severity'],
                'suggestion': gap_info['suggestion'],
            })

    # 冗余检测
    redundancies = detect_redundancy(sop)

    # 瓶颈识别
    bottlenecks = detect_bottlenecks(sop)

    # 改进建议
    improvements = suggest_improvements(sop)

    # 最佳实践对齐
    practices = []
    for bp in BEST_PRACTICES:
        result = bp['check'](sop)
        practices.append({
            'id': bp['id'],
            'name': bp['name'],
            'description': bp['description'],
            'result': result,
        })

    # 版本建议
    version_advice = _version_advice(quality['total_score'])

    return {
        'analysis_timestamp': sop['meta']['created_at'],
        'sop_title': sop['meta']['title'],
        'original_version': sop['meta']['version'],
        'quality_score': quality,
        'gaps': gaps,
        'redundancies': redundancies,
        'bottlenecks': bottlenecks,
        'improvements': improvements,
        'best_practices': practices,
        'version_advice': version_advice,
        'optimized_version': _generate_optimized_version(sop, gaps, improvements),
    }


def _version_advice(score: int) -> str:
    if score >= 85:
        return 'v1.0 — 质量优秀，可直接投入使用。建议在未来实践中迭代优化。'
    elif score >= 70:
        return 'v0.9 — 质量良好，建议根据缺口分析补充后正式发布为 v1.0。'
    elif score >= 55:
        return 'v0.5 — 基本合格，需要重点补充检查点和注意事项后再发布。'
    else:
        return 'v0.1 — 初始草稿，建议大幅补充完善后再投入使用。'


def _generate_optimized_version(sop: dict, gaps: list, improvements: list) -> dict:
    """
    基于缺口和改进建议，生成优化后的 SOP。
    """
    optimized = json.loads(json.dumps(sop))  # 深拷贝
    new_steps = list(optimized['steps'])

    # 按建议插入缺失步骤
    step_offset = len(new_steps)

    for gap in gaps:
        if gap['id'] == 'no_start_condition':
            new_steps.insert(0, {
                'index': 0,
                'type': 'preparation',
                'title': '前置准备',
                'description': '【AI建议新增】开始操作前的准备工作：确认权限、检查系统状态、准备所需工具和资料。',
                'tools': [],
                'time': {'value': 5, 'unit': '分钟', 'raw': '5分钟'},
                'cautions': [],
                'checkpoints': ['确认所有前置条件已满足'],
                'roles': [],
                'complexity': 'low',
                'dependencies': [],
                'output': '准备工作清单 ✓',
            })
        elif gap['id'] == 'no_end_condition':
            new_steps.append({
                'index': step_offset + len(gaps),
                'type': 'checkpoint',
                'title': '收尾与确认',
                'description': '【AI建议新增】确认流程完成：检查所有产出物、归档相关文档、通知相关人员。',
                'tools': [],
                'time': {'value': 5, 'unit': '分钟', 'raw': '5分钟'},
                'cautions': ['确保所有检查点已通过'],
                'checkpoints': ['所有步骤已完成', '产出物已归档', '相关人员已通知'],
                'roles': [],
                'complexity': 'low',
                'dependencies': [],
                'output': '完成确认单 ✓',
            })

    # 重新编号
    for i, s in enumerate(new_steps):
        s['index'] = i + 1 if s.get('type') != 'preparation' else 0

    optimized['steps'] = new_steps
    optimized['meta']['version'] = '1.0 (优化版)'
    optimized['meta']['total_steps'] = len([s for s in new_steps if s.get('type') != 'preparation'])

    return optimized


# ============================================================
# 命令行接口
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='SOP 优化分析器 — 评估SOP质量并生成改进建议',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python sop_optimizer.py -i sop_output.json
  python sop_optimizer.py -i sop_output.json -o optimized_sop.json --report optimization_report.json
        """
    )
    parser.add_argument('-i', '--input', type=str, required=True,
                        help='sop_extractor.py 输出的 JSON 文件')
    parser.add_argument('-o', '--output', type=str, default='sop_optimized.json',
                        help='输出优化后的 SOP 文件路径')
    parser.add_argument('--report', type=str, default='optimization_report.json',
                        help='优化分析报告输出路径')

    args = parser.parse_args()

    # 读取 SOP
    with open(args.input, 'r', encoding='utf-8') as f:
        sop = json.load(f)

    # 分析优化
    result = optimize(sop)

    # 保存优化后的 SOP
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(result['optimized_version'], f, ensure_ascii=False, indent=2)

    # 保存分析报告
    report = {
        'quality_score': result['quality_score'],
        'gaps': result['gaps'],
        'redundancies': result['redundancies'],
        'bottlenecks': result['bottlenecks'],
        'improvements': result['improvements'],
        'best_practices': result['best_practices'],
        'version_advice': result['version_advice'],
    }
    with open(args.report, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f'[SOP Optimizer] 优化完成')
    print(f'  质量评分: {result["quality_score"]["total_score"]}/100 ({result["quality_score"]["grade_label"]})')
    print(f'  发现缺口: {len(result["gaps"])} 个')
    print(f'  冗余步骤: {len(result["redundancies"])} 对')
    print(f'  瓶颈步骤: {len(result["bottlenecks"])} 个')
    print(f'  改进建议: {len(result["improvements"])} 条')
    print(f'  版本建议: {result["version_advice"]}')
    print(f'  优化版SOP: {args.output}')
    print(f'  分析报告: {args.report}')


if __name__ == '__main__':
    main()
