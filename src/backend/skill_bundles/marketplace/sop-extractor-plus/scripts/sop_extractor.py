#!/usr/bin/env python3
"""
SOP 标准化流程提取器 — 核心提取引擎
将口述/转录的非结构化文本自动解析为结构化 SOP 文档。
"""

import re
import json
import sys
import os
from collections import OrderedDict
from datetime import datetime


# ============================================================
# 常量定义
# ============================================================

# 步骤分界标记 — 中文语境下的分段信号
STEP_BOUNDARY_PATTERNS = [
    # 序号型
    (re.compile(r'(?:第\s*)([一二三四五六七八九十百千万\d]+)(?:\s*[步歩]|[\.、．])'), True),
    (re.compile(r'(?:步骤\s*)([一二三四五六七八九十百千万\d]+)'), True),
    (re.compile(r'^(\d+)[\.、．)\s]'), True),
    (re.compile(r'^([一二三四五六七八九十]+)[、．\.\s]'), True),
    # 序号+描述型: "1. 打开浏览器"
    (re.compile(r'(\d+)[\.、．]\s*\S'), True),
    # 连接词型 (作为新步骤信号)
    (re.compile(r'(然后|接着|接下来|之后|再|下一步|下[一１1]步|然后呢|再来)'), False),
    # 阶段标记型
    (re.compile(r'(首先|第一步|第[一１1]步|开始|先)'), False),
    (re.compile(r'(最后|最终|结束|完成|收尾|最后一步)'), False),
]

# 动作动词 — 用于识别操作步骤
ACTION_VERBS = [
    '打开', '点击', '输入', '选择', '设置', '配置', '创建', '删除', '添加',
    '修改', '更新', '上传', '下载', '发送', '接收', '复制', '粘贴', '保存',
    '提交', '确认', '取消', '登录', '注册', '搜索', '筛选', '导出', '导入',
    '安装', '卸载', '启动', '关闭', '重启', '运行', '执行', '调用', '编写',
    '检查', '查看', '预览', '测试', '调试', '部署', '发布', '推送', '拉取',
    '合并', '拆分', '格式化', '转换', '压缩', '解压', '加密', '解密',
    '填写', '录入', '拍照', '扫描', '打印', '复印', '传真', '邮寄',
    '联系', '通知', '汇报', '审批', '签字', '盖章', '归档', '备份',
    '清理', '整理', '分类', '标记', '记录', '备注', '统计', '分析',
    '测量', '称重', '计数', '盘点', '核对', '比对', '校验', '复审',
]

# 工具/软件关键词
TOOL_KEYWORDS = [
    'excel', 'word', 'ppt', 'pdf', 'photoshop', 'ps', 'chrome', '浏览器',
    '微信', '钉钉', '飞书', '企业微信', '邮件', 'outlook', '邮箱',
    'vscode', 'pycharm', 'idea', '编辑器', '终端', '命令行',
    '数据库', 'mysql', 'redis', 'mongodb', '服务器', '云平台',
    'github', 'gitlab', 'jira', 'confluence', 'notion', '飞书文档',
    '手机', '电脑', '打印机', '扫描仪', '相机', '录音笔',
    '计算器', 'excel表格', 'word文档', 'ppt演示',
    'ERP', 'CRM', 'OA', 'WMS', 'TMS', '财务系统', '人事系统',
    'API', '接口', 'SDK', '插件', '扩展', '脚本',
]

# 检查/验证关键词
CHECKPOINT_KEYWORDS = [
    '检查', '确认', '验证', '确保', '核对', '校验', '复查',
    '测试通过', '验收', '审核', '审批通过', '签字确认',
]

# 注意事项关键词
CAUTION_KEYWORDS = [
    '注意', '小心', '不要', '避免', '禁止', '切勿', '务必',
    '重要', '关键', '切记', '别忘了', '必须', '一定要',
    '容易出错', '常见错误', '踩坑', '坑', '陷阱',
]

# 时间指示词
TIME_PATTERNS = [
    (re.compile(r'(?:大约|大概|约|需要|耗时|等待|预计)\s*(\d+\.?\d*)\s*(分钟|小时|秒|天|工作日|min|hour|h|minute|m)'), False),
    (re.compile(r'(\d+\.?\d*)\s*(分钟|小时|秒|天|工作日)'), True),
    (re.compile(r'等待\s*(\d+\.?\d*)'), False),
]

# 决策/分支关键词
DECISION_KEYWORDS = [
    '如果', '假如', '假设', '当', '判断', '选择', '决定',
    '根据', '取决于', '分情况', '要么', '或者',
    '条件', '分支', '否则', '要不然',
]

# 角色标识
ROLE_KEYWORDS = [
    '开发', '测试', '运维', '设计', '产品', '运营', '客服', '销售',
    '财务', '人事', '行政', '经理', '主管', '负责人', '专员',
    '前端', '后端', '全栈', 'DBA', 'PM', 'QA',
    '甲方', '乙方', '客户', '供应商', '外包',
]


# ============================================================
# 中文数字转换
# ============================================================

CN_NUM_MAP = {
    '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
    '零': 0, '百': 100, '千': 1000, '万': 10000,
}


def cn_to_int(s: str) -> int:
    """将中文数字字符串转为整数"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    # 处理 "十二" "二十五" 等
    if '十' in s:
        parts = s.split('十')
        if parts[0] == '':
            tens = 10
        else:
            tens = CN_NUM_MAP.get(parts[0], 1) * 10
        if len(parts) > 1 and parts[1]:
            return tens + CN_NUM_MAP.get(parts[1], 0)
        return tens
    # 单字
    if s in CN_NUM_MAP:
        return CN_NUM_MAP[s]
    return 0


# ============================================================
# 文本预处理
# ============================================================

def preprocess_text(text: str) -> str:
    """清洗原始文本：去除多余空白、修正标点"""
    text = text.strip()
    # 合并多个换行为双换行
    text = re.sub(r'\n{3,}', '\n\n', text)
    # 合并多个空格
    text = re.sub(r' {2,}', ' ', text)
    # 中文冒号/分号统一
    text = text.replace('：', ':').replace('；', ';')
    return text


def split_into_segments(text: str) -> list:
    """
    按自然段和步骤标记将文本切分为多个片段。
    返回: [(segment_text, segment_index), ...]
    """
    # 先按段落分
    paragraphs = [p.strip() for p in text.split('\n') if p.strip()]

    # 合并过短的段落到相邻段落
    merged = []
    buffer = []
    for p in paragraphs:
        if len(p) < 15 and buffer:
            buffer.append(p)
        elif len(p) < 15:
            buffer.append(p)
        else:
            if buffer:
                merged.append(' '.join(buffer))
                buffer = []
            merged.append(p)
    if buffer:
        merged.append(' '.join(buffer))

    return merged


# ============================================================
# 步骤提取核心逻辑
# ============================================================

def detect_step_boundaries(paragraph: str) -> list:
    """
    在一个段落内检测是否有步骤分割点。
    返回: [(start_position, label), ...] 表示在哪些位置切分
    """
    boundaries = []
    for pattern, is_numbered in STEP_BOUNDARY_PATTERNS:
        for m in pattern.finditer(paragraph):
            pos = m.start()
            # 避免重复标记
            if any(abs(pos - b[0]) < 5 for b in boundaries):
                continue
            boundaries.append((pos, m.group(0).strip(), is_numbered))
    return sorted(boundaries, key=lambda x: x[0])


def classify_step_type(text: str) -> str:
    """
    对步骤文本进行分类。
    返回: 'action' | 'decision' | 'checkpoint' | 'wait' | 'preparation' | 'handover'
    """
    text_lower = text.lower()

    # 决策类
    for kw in DECISION_KEYWORDS:
        if kw in text:
            return 'decision'

    # 检查类
    for kw in CHECKPOINT_KEYWORDS:
        if kw in text:
            return 'checkpoint'

    # 等待类
    if re.search(r'(等待|稍等|等.*分钟|等.*小时|pending|hold)', text_lower):
        return 'wait'

    # 准备类
    if re.search(r'(准备|预备|前置|前提|需要先|在此之前)', text):
        return 'preparation'

    # 交接类
    if re.search(r'(交接|转交|通知.*人|汇报给|发送给|提交.*审核)', text):
        return 'handover'

    return 'action'


def extract_tools(text: str) -> list:
    """提取步骤中涉及的工具/软件"""
    tools = []
    text_lower = text.lower()
    for kw in TOOL_KEYWORDS:
        if kw.lower() in text_lower:
            tools.append(kw)
    return list(set(tools))  # 去重


def extract_time(text: str) -> dict:
    """提取步骤预估时间"""
    for pattern, _ in TIME_PATTERNS:
        m = pattern.search(text)
        if m:
            value = float(m.group(1))
            unit = m.group(2) if pattern.groups > 1 else '分钟'
            # 统一转为分钟
            unit_lower = unit.lower()
            if unit_lower in ('秒', 's'):
                value = value / 60
                unit = '分钟'
            elif unit_lower in ('小时', 'h', 'hour'):
                value = value * 60
                unit = '分钟'
            elif unit_lower in ('天', '工作日'):
                value = value * 480  # 8小时
                unit = '分钟'
            return {'value': int(value), 'unit': '分钟', 'raw': f'{m.group(0).strip()}'}
    return None


def extract_cautions(text: str) -> list:
    """提取注意事项"""
    cautions = []
    for kw in CAUTION_KEYWORDS:
        if kw in text:
            # 提取包含关键词的完整句子片段
            idx = text.find(kw)
            # 向前找句首
            start = max(0, idx - 5)
            for sep in ['。', '！', '？', '.', '!', '?', '；', ';', '\n']:
                sep_pos = text.rfind(sep, 0, idx)
                if sep_pos > start:
                    start = sep_pos + 1
                    break
            # 向后找句尾
            end = min(len(text), idx + 60)
            for sep in ['。', '！', '？', '.', '!', '?', '\n']:
                sep_pos = text.find(sep, idx)
                if sep_pos != -1 and sep_pos < end:
                    end = sep_pos + 1
                    break
            snippet = text[start:end].strip()
            if snippet and snippet not in cautions:
                cautions.append(snippet)
    return cautions


def extract_checkpoints(text: str) -> list:
    """提取检查点"""
    checkpoints = []
    for kw in CHECKPOINT_KEYWORDS:
        if kw in text:
            idx = text.find(kw)
            # 提取检查描述
            snippet = text[idx:idx + 80].strip()
            # 截断到第一个句号
            for sep in ['。', '！', '？', '.', '!', '?']:
                sp = snippet.find(sep)
                if sp > 0:
                    snippet = snippet[:sp]
                    break
            if snippet and len(snippet) > 3:
                checkpoints.append(snippet)
    return checkpoints


def extract_roles(text: str) -> list:
    """提取涉及的角色/人员"""
    roles = []
    for kw in ROLE_KEYWORDS:
        if kw in text:
            roles.append(kw)
    return list(set(roles))


def estimate_complexity(text: str) -> str:
    """
    估算步骤复杂度。
    返回: 'low' | 'medium' | 'high'
    """
    score = 0
    # 有决策分支 → +2
    if classify_step_type(text) == 'decision':
        score += 2
    # 有注意事项 → +1
    if extract_cautions(text):
        score += 1
    # 有检查点 → +1
    if extract_checkpoints(text):
        score += 1
    # 工具多 → +1
    if len(extract_tools(text)) >= 3:
        score += 1
    # 文字长 → +1
    if len(text) > 100:
        score += 1

    if score >= 4:
        return 'high'
    elif score >= 2:
        return 'medium'
    return 'low'


# ============================================================
# 主提取函数
# ============================================================

def extract_sop(raw_text: str, title: str = None) -> dict:
    """
    从原始文本中提取 SOP 结构。

    参数:
        raw_text: 口述转录文本或文件路径
        title: SOP 标题 (可选，自动生成)

    返回:
        {
            "meta": {...},
            "steps": [...],
            "summary": {...}
        }
    """
    # 如果 raw_text 是文件路径，尝试读取
    if os.path.exists(raw_text) and len(raw_text) < 500:
        with open(raw_text, 'r', encoding='utf-8') as f:
            raw_text = f.read()

    text = preprocess_text(raw_text)

    # --- 尝试提取标题 ---
    if not title:
        lines = text.split('\n')
        first_non_empty = ''
        for line in lines:
            if line.strip():
                first_non_empty = line.strip()
                break
        # 如果第一行较短，可能是标题
        if len(first_non_empty) <= 50 and not re.match(r'^(首先|第一步|第[一二三]|然后|接着)', first_non_empty):
            title = first_non_empty
        else:
            title = '未命名SOP流程'

    # --- 分段 ---
    segments = split_into_segments(text)

    # --- 提取步骤 ---
    steps = []
    step_counter = 0
    current_step_text = []

    for seg in segments:
        boundaries = detect_step_boundaries(seg)

        if not boundaries:
            # 纯叙述段落，可能是引言或背景
            if step_counter == 0 and len(steps) == 0:
                # 第一段作为概述
                steps.append({
                    'index': 0,
                    'type': 'overview',
                    'title': '流程概述',
                    'description': seg,
                    'tools': extract_tools(seg),
                    'time': extract_time(seg),
                    'cautions': extract_cautions(seg),
                    'checkpoints': extract_checkpoints(seg),
                    'roles': extract_roles(seg),
                    'complexity': 'low',
                    'dependencies': [],
                    'output': None,
                })
            continue

        # 有分割点，按分割点拆分
        if len(boundaries) == 1:
            step_counter += 1
            # 含分割点的整段作为一个步骤
            step_text = seg
            steps.append(_make_step(step_counter, step_text))
        else:
            # 多个分割点，拆分
            for i, (pos, label, numbered) in enumerate(boundaries):
                if i < len(boundaries) - 1:
                    end_pos = boundaries[i + 1][0]
                else:
                    end_pos = len(seg)
                step_text = seg[pos:end_pos].strip()
                if step_text:
                    step_counter += 1
                    steps.append(_make_step(step_counter, step_text))

    # --- 如果没检测到任何步骤，尝试按句子分割 ---
    if step_counter == 0:
        # 跳过 overview
        non_overview = [s for s in segments if len(s) > 10]
        for seg in non_overview:
            # 按句子分割
            sentences = re.split(r'[。！？\n]', seg)
            for sent in sentences:
                sent = sent.strip()
                if len(sent) > 8:
                    step_counter += 1
                    steps.append(_make_step(step_counter, sent))

    # --- 过滤 overview（如果后面有实质步骤则保留开头描述作为preparation） ---
    if steps and steps[0].get('type') == 'overview':
        steps[0]['type'] = 'preparation'
        steps[0]['title'] = '前置准备与说明'

    # --- 构建依赖关系 ---
    _build_dependencies(steps)

    # --- 计算汇总 ---
    total_time = sum(
        (s.get('time') or {}).get('value', 5) for s in steps
        if s.get('time')
    )
    if total_time == 0:
        total_time = len(steps) * 5  # 默认每步5分钟

    step_types = {}
    for s in steps:
        t = s.get('type', 'action')
        step_types[t] = step_types.get(t, 0) + 1

    # 复杂度分布
    complexity_dist = {'low': 0, 'medium': 0, 'high': 0}
    for s in steps:
        c = s.get('complexity', 'low')
        complexity_dist[c] = complexity_dist.get(c, 0) + 1

    # 总复杂度
    complexity_scores = {'low': 1, 'medium': 2, 'high': 3}
    comp_score = sum(complexity_scores.get(s.get('complexity', 'low'), 1) for s in steps)
    max_score = len(steps) * 3
    if max_score == 0:
        overall_complexity = 'low'
    else:
        ratio = comp_score / max_score
        if ratio > 0.6:
            overall_complexity = 'high'
        elif ratio > 0.3:
            overall_complexity = 'medium'
        else:
            overall_complexity = 'low'

    sop = {
        'meta': {
            'title': title,
            'created_at': datetime.now().isoformat(),
            'version': '1.0',
            'author': 'AI Extracted',
            'source_type': 'dictation',
            'overall_complexity': overall_complexity,
            'total_steps': len([s for s in steps if s.get('type') != 'preparation']),
            'estimated_total_time_minutes': total_time,
            'estimated_total_time_display': _format_duration(total_time),
        },
        'steps': steps,
        'summary': {
            'step_type_distribution': step_types,
            'complexity_distribution': complexity_dist,
            'total_tools': len(set(t for s in steps for t in s.get('tools', []))),
            'total_checkpoints': sum(len(s.get('checkpoints', [])) for s in steps),
            'total_cautions': sum(len(s.get('cautions', [])) for s in steps),
        },
    }

    return sop


def _make_step(index: int, text: str) -> dict:
    """根据文本创建一个步骤字典"""
    # 尝试提取步骤标题（取前20字）
    title = text[:40].strip()
    # 去掉序号前缀
    title = re.sub(r'^(第[一二三四五六七八九十\d]+[步歩]|[一二三四五六七八九十\d]+[\.、．)\s])', '', title).strip()
    if len(title) > 30:
        title = title[:27] + '...'

    step_type = classify_step_type(text)
    return {
        'index': index,
        'type': step_type,
        'title': title,
        'description': text,
        'tools': extract_tools(text),
        'time': extract_time(text),
        'cautions': extract_cautions(text),
        'checkpoints': extract_checkpoints(text),
        'roles': extract_roles(text),
        'complexity': estimate_complexity(text),
        'dependencies': [],
        'output': _infer_output(text),
    }


def _infer_output(text: str) -> str:
    """推断步骤产出物"""
    output_patterns = [
        (r'(?:生成|创建|新建|产出|输出|得到|获得|形成|编写|撰写)了?[一]?(?:个|份|张|条|篇)?\s*(\S{2,20})', '产出'),
        (r'(?:保存|导出|另存为|输出为)\s*(\S{2,20})', '文件'),
        (r'(?:发送|通知)了?\s*(\S{2,20})', '通知'),
    ]
    for pattern, label in output_patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def _build_dependencies(steps: list):
    """构建步骤间的依赖关系"""
    for i, step in enumerate(steps):
        if i == 0:
            continue
        # 检查是否有"先...再..."、"在...基础上"等依赖信号
        text = step.get('description', '')
        # 默认：每个步骤依赖前一个步骤（线性流程）
        if re.search(r'(然后|接着|之后|完成.*后|在前.*基础)', text):
            step['dependencies'] = [steps[i - 1]['index']]
        elif step.get('type') == 'checkpoint':
            # 检查点通常依赖最近的操作步骤
            step['dependencies'] = [steps[i - 1]['index']]
        elif step.get('type') == 'handover':
            step['dependencies'] = [steps[i - 1]['index']]
        else:
            step['dependencies'] = [steps[i - 1]['index']]


def _format_duration(minutes: int) -> str:
    """将分钟格式化为可读时间"""
    if minutes < 60:
        return f'{minutes}分钟'
    elif minutes < 480:
        hours = minutes // 60
        mins = minutes % 60
        if mins == 0:
            return f'{hours}小时'
        return f'{hours}小时{mins}分钟'
    else:
        days = minutes // 480
        remaining = minutes % 480
        hours = remaining // 60
        if hours == 0:
            return f'{days}个工作日'
        return f'{days}个工作日{hours}小时'


# ============================================================
# 命令行接口
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='SOP 标准化流程提取器 — 从口述/转录文本中提取结构化 SOP',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python sop_extractor.py -t "首先打开Excel，然后导入数据表..."
  python sop_extractor.py -f transcript.txt -o sop_output.json
  python sop_extractor.py -f transcript.txt --output-format markdown
  echo "第一步：打开浏览器..." | python sop_extractor.py --stdin
        """
    )
    parser.add_argument('-t', '--text', type=str, help='直接输入文本')
    parser.add_argument('-f', '--file', type=str, help='从文件读取文本')
    parser.add_argument('--stdin', action='store_true', help='从标准输入读取')
    parser.add_argument('--title', type=str, help='SOP 标题')
    parser.add_argument('-o', '--output', type=str, default='sop_output.json',
                        help='输出文件路径（默认 sop_output.json）')
    parser.add_argument('--output-format', type=str, choices=['json', 'md', 'markdown'],
                        default='json', help='输出格式')

    args = parser.parse_args()

    # 获取输入
    if args.text:
        raw = args.text
    elif args.file:
        raw = open(args.file, 'r', encoding='utf-8').read()
    elif args.stdin:
        raw = sys.stdin.read()
    else:
        parser.print_help()
        sys.exit(1)

    # 提取
    sop = extract_sop(raw, title=args.title)

    # 输出
    out_path = args.output
    fmt = args.output_format

    if fmt == 'json':
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(sop, f, ensure_ascii=False, indent=2)
        print(f'[SOP Extractor] SOP 已提取并保存至: {out_path}')
        print(f'  - 标题: {sop["meta"]["title"]}')
        print(f'  - 步骤数: {sop["meta"]["total_steps"]}')
        print(f'  - 预估时间: {sop["meta"]["estimated_total_time_display"]}')
        print(f'  - 复杂度: {sop["meta"]["overall_complexity"]}')
        print(f'  - 检查点: {sop["summary"]["total_checkpoints"]}')
    elif fmt in ('md', 'markdown'):
        # 生成简单的 Markdown
        md_lines = [f'# {sop["meta"]["title"]}', '', f'**版本**: {sop["meta"]["version"]}']
        md_lines.append(f'**总步骤**: {sop["meta"]["total_steps"]}')
        md_lines.append(f'**预估耗时**: {sop["meta"]["estimated_total_time_display"]}')
        md_lines.append('')
        for step in sop['steps']:
            md_lines.append(f'## 步骤 {step["index"]}: {step["title"]}')
            md_lines.append(f'**类型**: {step["type"]}  |  **复杂度**: {step["complexity"]}')
            md_lines.append(f'**描述**: {step["description"]}')
            if step.get('time'):
                td = step['time']
                md_lines.append(f'**耗时**: {td["raw"]}')
            if step.get('tools'):
                md_lines.append(f'**工具**: {", ".join(step["tools"])}')
            if step.get('cautions'):
                md_lines.append('**注意事项**:')
                for c in step['cautions']:
                    md_lines.append(f'  - {c}')
            if step.get('checkpoints'):
                md_lines.append('**检查点**:')
                for c in step['checkpoints']:
                    md_lines.append(f'  - {c}')
            md_lines.append('')

        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(md_lines))
        print(f'[SOP Extractor] Markdown SOP 已保存至: {out_path}')


if __name__ == '__main__':
    main()
