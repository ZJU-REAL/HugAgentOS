#!/usr/bin/env python3
"""
敏感数据脱敏工具 - 自动识别并替换文本中的数字数据。

支持的脱敏策略（优先级从高到低）：
  hash        - 哈希映射：相同数值保持一致替换，支持还原（默认）
  placeholder - 占位符：替换为 [A]、[B] 等标记，完全隐藏数值
  blur        - 模糊化：保留数量级，有效数字随机偏移
  range       - 区间替换：用数量级区间代替（如 [7000~13000]）

使用示例：
  python mask.py --strategy hash < input.txt
  python mask.py --strategy range --input report.txt --output masked.txt
  python mask.py --strategy placeholder --map-file mapping.json < text.txt
  python mask.py --unmask --map-file mapping.json < masked.txt
"""

import argparse
import json
import re
import random
import sys
import hashlib
import os


# ── 命名规范 ─────────────────────────────────────────────────
def generate_default_output(input_path):
    """
    根据输入文件路径自动生成输出路径。
    命名规范：原文件名_mask.原后缀
      例：report.txt → report_mask.txt
    """
    dir_name = os.path.dirname(input_path) or '.'
    base = os.path.splitext(os.path.basename(input_path))[0]
    ext = os.path.splitext(input_path)[1].lower() or '.txt'
    return os.path.join(dir_name, f'{base}_mask{ext}')

# ── 数字识别正则（单次遍历，避免二次匹配） ──

# 中文单位后缀（按长度降序排列）
UNIT_SUFFIXES = sorted(
    ['万元', '亿美元', '亿港元', '亿日元', '亿欧元', '亿英镑',
     '美元', '美金', '港币', '日元', '欧元', '英镑', '韩元', '卢布',
     '万亿', '亿', '万', '兆', '千', '百', '十', '元', '块'],
    key=lambda x: -len(x)
)

_UNIT_LIST = '|'.join(UNIT_SUFFIXES)

# 单次匹配正则：数字+单位 | 数字+% | 纯数字
_COMBINED_PATTERN = (
    r'(?<![a-zA-Z0-9_\[\]~])'                          # 通用前缀检查
    r'('
        # 1. 数字 + 中文单位
        r'(\d+(?:,\d{3})*(?:\.\d+)?)\s*(' + _UNIT_LIST + r')'
        r'(?![a-zA-Z0-9_])'
    r'|'
        # 2. 数字 + 百分比
        r'(\d+(?:,\d{3})*(?:\.\d+)?)\s*(%)'
        r'(?![a-zA-Z0-9_])'
    r'|'
        # 3. 纯数字（支持任意长度，带或不带逗号分隔，支持负数和小数）
        r'(-?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?)'
        r'(?![a-zA-Z0-9_])'
    r')'
)

COMBINED_RE = re.compile(_COMBINED_PATTERN)

# 电话号码格式（可选跳过）
PHONE_RE = re.compile(r'(?<![0-9])(1[3-9]\d)\d{4}\d{4}(?![0-9])')


# ── 脱敏策略实现 ──────────────────────────────────────────────

def _parse_num(num_str):
    """将字符串转为 float，失败返回 None"""
    clean = num_str.replace(',', '')
    try:
        return float(clean)
    except ValueError:
        return None


def _magnitude_range(num):
    """根据数值绝对值返回量级区间 (low, high)"""
    abs_n = abs(num)
    if abs_n == 0:
        return (0, 10)
    elif abs_n < 10:
        return (1, 10)
    elif abs_n < 100:
        return (10, 100)
    elif abs_n < 1000:
        return (100, 1000)
    elif abs_n < 10000:
        return (1000, 10000)
    elif abs_n < 100000:
        return (10000, 100000)
    elif abs_n < 1000000:
        return (100000, 1000000)
    elif abs_n < 10000000:
        return (1000000, 10000000)
    elif abs_n < 100000000:
        return (10000000, 100000000)
    else:
        return (100000000, 1000000000)


def _format_num(value, decimal_places=None):
    """格式化数字输出：整数不显示小数点，小数保留适当精度"""
    if value == int(value):
        return str(int(value))
    if decimal_places is not None:
        return f'{value:.{decimal_places}f}'
    # 自动判断：小数位不超过 6 位
    abs_v = abs(value)
    if abs_v >= 1000:
        return str(int(round(value)))
    elif abs_v >= 1:
        return f'{value:.1f}'
    else:
        return f'{value:.3f}'


def blur_value(num):
    """
    模糊化：保留数量级和正负号，有效数字随机偏移。
    偏移量与数值的量级相关，确保模糊后的值仍在合理范围内。
    """
    if num == 0:
        return 0

    abs_num = abs(num)

    # 根据数量级确定偏移范围（约为该量级宽度的 5%-15%）
    if abs_num < 10:
        delta_range = abs_num * 0.5 + 0.1
    elif abs_num < 100:
        delta_range = abs_num * 0.15
    elif abs_num < 1000:
        delta_range = abs_num * 0.1
    elif abs_num < 10000:
        delta_range = abs_num * 0.08
    elif abs_num < 100000:
        delta_range = abs_num * 0.06
    elif abs_num < 1000000:
        delta_range = abs_num * 0.05
    elif abs_num < 10000000:
        delta_range = abs_num * 0.04
    elif abs_num < 100000000:
        delta_range = abs_num * 0.03
    else:
        delta_range = abs_num * 0.02

    delta = random.uniform(-delta_range, delta_range)
    new_num = num + delta

    # 保护：钳制到原值的 ±50% 范围内，不改变正负号
    if num > 0:
        new_num = max(num * 0.5, min(num * 1.5, new_num))
    elif num < 0:
        new_num = min(num * 0.5, max(num * 1.5, new_num))

    return new_num


def range_value(num):
    """
    区间替换：根据数值大小生成不同宽度的区间。
    - 大数用 ±30% 区间
    - 中数用 ±25% 区间
    - 小数用 ±50% 区间
    - 保留正负号
    """
    abs_n = abs(num)
    sign = -1 if num < 0 else 1

    if abs_n == 0:
        return '[0~1]'

    if abs_n >= 10000:
        low, high = int(abs_n * 0.7), int(abs_n * 1.3)
    elif abs_n >= 1000:
        low, high = int(abs_n * 0.75), int(abs_n * 1.25)
    elif abs_n >= 100:
        low, high = int(abs_n * 0.75), int(abs_n * 1.25)
    elif abs_n >= 10:
        low, high = int(abs_n * 0.5), int(abs_n * 1.5)
    elif abs_n >= 1:
        low, high = round(abs_n * 0.5, 1), round(abs_n * 1.5, 1)
    else:
        low, high = round(max(0, abs_n * 0.1), 3), round(abs_n * 2.0, 3)

    if sign < 0:
        return f'[-{high}~-{low}]'
    return f'[{low}~{high}]'


def _find_keys_by_value(mapping, target_value):
    """返回映射表中值为 target_value 的所有 key（用于冲突检测）。"""
    return [k for k, v in mapping.items() if v == target_value]


def hash_value(num, key, suffix, mapping, seed=42):
    """
    哈希映射：相同数值保持一致替换，支持还原。

    改进点：
    1. 保留原值的正负号
    2. 在同一量级区间内保持相对位置（hash 值映射到区间内的相对位置）
    3. 处理零值
    4. 映射表存储完整脱敏值（含单位），确保 unmask 可精准还原
    """
    if key in mapping:
        return mapping[key]

    sign = -1 if num < 0 else 1

    if num == 0:
        result_with_suffix = '0' + suffix
        mapping[key] = result_with_suffix
        return result_with_suffix

    abs_num = abs(num)
    h = hashlib.md5((key + str(seed)).encode()).hexdigest()[:8]
    hash_int = int(h, 16)

    low, high = _magnitude_range(num)
    range_width = high - low

    # hash 值映射到区间内的相对位置（保留内部排序关系）
    relative_pos = (hash_int % 10000) / 10000.0  # 0.0 ~ 1.0
    new_abs = low + int(range_width * relative_pos)

    # 修正小数精度
    if abs_num < 1:
        new_abs = round(low + range_width * relative_pos, 4)

    new_val = sign * new_abs
    result = _format_num(new_val)
    result_with_suffix = str(result) + suffix

    # 检测值冲突：如果此脱敏值已被另一个不同原文使用，则递增避免冲突
    existing_keys = _find_keys_by_value(mapping, result_with_suffix)
    retry = 0
    while existing_keys and key not in existing_keys:
        retry += 1
        new_val = sign * (new_abs + retry)
        result = _format_num(new_val)
        result_with_suffix = str(result) + suffix
        existing_keys = _find_keys_by_value(mapping, result_with_suffix)

    mapping[key] = result_with_suffix
    return result_with_suffix


def placeholder_value(key, mapping, counter):
    """占位符替换：按出现顺序分配 [A], [B], ..., [Z], [V1], [V2], ..."""
    if key not in mapping:
        if counter[0] < 26:
            label = chr(65 + counter[0])
        else:
            label = f'V{counter[0] - 25}'
        mapping[key] = f'[{label}]'
        counter[0] += 1
    return mapping[key]


# ── 主处理逻辑 ────────────────────────────────────────────────

def mask_text(text, strategy='hash', mapping=None, placeholder_counter=None,
              seed=42, skip_phone=False):
    """
    对文本中的数字进行脱敏处理。
    单次正则遍历，避免已脱敏内容被二次匹配。

    参数:
        text: 输入文本
        strategy: 脱敏策略 (hash|placeholder|blur|range)
        mapping: 已有的映射字典（用于增量脱敏）
        placeholder_counter: 占位符计数器 [int]
        seed: 随机种子
        skip_phone: 是否跳过电话号码

    返回:
        (脱敏后文本, 映射字典)
    """
    if mapping is None:
        mapping = {}
    if placeholder_counter is None:
        placeholder_counter = [0]

    random.seed(seed)

    def replace_callback(m):
        full_match = m.group(0)

        # 跳过电话号码
        if skip_phone and PHONE_RE.fullmatch(full_match):
            return full_match

        # 判断匹配类型
        # group(2): 数字+单位中的数字部分
        # group(3): 数字+单位中的单位部分
        # group(4): 百分比中的数字部分
        # group(5): 百分比中的 % 部分
        # group(6): 纯数字

        if m.group(2) is not None and m.group(3) is not None:
            # 数字 + 单位
            num_str = m.group(2)
            suffix = m.group(3)
        elif m.group(4) is not None and m.group(5) is not None:
            # 数字 + 百分比
            num_str = m.group(4)
            suffix = m.group(5)
        elif m.group(6) is not None:
            # 纯数字
            num_str = m.group(6)
            suffix = ''
        else:
            return full_match

        num = _parse_num(num_str)
        if num is None:
            return full_match

        if strategy == 'placeholder':
            return placeholder_value(full_match, mapping, placeholder_counter)
        elif strategy == 'hash':
            return hash_value(num, full_match, suffix, mapping, seed)
        elif strategy == 'range':
            # 记录到 mapping 用于统计（不可逆还原）
            if full_match not in mapping:
                mapping[full_match] = 'RANGE'
            return range_value(num) + suffix
        else:  # blur
            new_num = blur_value(num)
            # 记录到 mapping 用于统计（不可逆还原）
            if full_match not in mapping:
                mapping[full_match] = 'BLUR'
            return _format_num(new_num) + suffix

    result = COMBINED_RE.sub(replace_callback, text)
    return result, mapping


def _read_text_file(filepath):
    """读取文本文件，自动尝试多种编码。返回 (text, encoding_used)。"""
    for encoding in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
        try:
            with open(filepath, 'r', encoding=encoding) as f:
                return f.read(), encoding
        except (UnicodeDecodeError, UnicodeError):
            continue
    # 最后尝试 utf-8 with errors='replace'
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        return f.read(), 'utf-8 (部分字符已替换)'


def mask_file(input_path, output_path, strategy='hash',
              map_path=None, seed=42, skip_phone=False):
    """对文件内容进行脱敏"""
    if not os.path.exists(input_path):
        print(
            f'错误：输入文件不存在 —— "{input_path}"\n'
            f'  请确认文件路径是否正确。',
            file=sys.stderr
        )
        sys.exit(1)

    mapping = {}
    placeholder_counter = [0]

    text, encoding_used = _read_text_file(input_path)
    if encoding_used != 'utf-8':
        print(f'提示: 文件编码为 {encoding_used}，已自动适配读取', file=sys.stderr)

    masked, mapping = mask_text(
        text, strategy=strategy, mapping=mapping,
        placeholder_counter=placeholder_counter,
        seed=seed, skip_phone=skip_phone
    )

    # 确保输出目录存在
    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(masked)

    if map_path:
        with open(map_path, 'w', encoding='utf-8') as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)

    return masked, mapping


def unmask_text(text, mapping):
    """根据映射表还原脱敏后的文本

    使用两步法避免级联替换：
    1. 先将脱敏值替换为唯一占位符（纯字母，不含数字以免被短数值误匹配）
    2. 再将占位符替换为原始值
    """
    import random
    import string

    # 按脱敏值长度降序，确保 "8034万" 先于 "834万" 被处理
    sorted_pairs = sorted(mapping.items(), key=lambda kv: len(kv[1]), reverse=True)

    # 生成唯一会话令牌（纯字母，确保不会和任何脱敏值冲突）
    session = ''.join(random.choices(string.ascii_lowercase, k=8))

    # 第一遍：脱敏值 → 占位符
    placeholders = {}
    for i, (original, masked) in enumerate(sorted_pairs):
        placeholder = f'\x01{session}_{_encode_index(i)}\x01'
        placeholders[placeholder] = original
        text = text.replace(masked, placeholder)

    # 第二遍：占位符 → 原始值
    for placeholder, original in placeholders.items():
        text = text.replace(placeholder, original)

    return text


def _encode_index(i):
    """将索引转为纯字母编码，确保不含数字"""
    chars = 'abcdefghijklmnopqrstuvwxyz'
    if i == 0:
        return chars[0]
    result = []
    while i > 0:
        result.append(chars[i % 26])
        i //= 26
    return ''.join(reversed(result))


def main():
    parser = argparse.ArgumentParser(
        description='敏感数据脱敏工具 - 自动识别并替换文本中的数字',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 管道输入
  echo "今年营收12345万元" | python mask.py

  # 文件脱敏（自动生成 report_mask.txt）
  python mask.py -i report.txt -s hash

  # 手动指定输出
  python mask.py -i report.txt -o masked.txt -s hash

  # 带映射表（支持还原）
  python mask.py -i report.txt -m mapping.json

  # 还原
  python mask.py --unmask -i masked.txt -o restored.txt -m mapping.json
        """
    )
    parser.add_argument(
        '-s', '--strategy',
        choices=['blur', 'range', 'placeholder', 'hash'],
        default='hash',
        help='脱敏策略 (默认: hash)'
    )
    parser.add_argument('-i', '--input', help='输入文件路径')
    parser.add_argument('-o', '--output', default=None,
                        help='输出文件路径（可选，默认：原文件名_mask.原后缀）')
    parser.add_argument('-m', '--map-file', help='映射表 JSON 文件路径')
    parser.add_argument('--unmask', action='store_true', help='还原模式')
    parser.add_argument('--seed', type=int, default=42, help='随机种子 (默认: 42)')
    parser.add_argument('--skip-phone', action='store_true', help='跳过电话号码')
    parser.add_argument('--columns', help='指定脱敏的列（逗号分隔，仅限结构数据模块使用）')

    args = parser.parse_args()

    # 输入验证
    if args.input:
        if not os.path.exists(args.input):
            print(
                f'错误：输入文件不存在 —— "{args.input}"\n'
                f'  请确认文件路径是否正确。如果是相对路径，请确认当前工作目录。',
                file=sys.stderr
            )
            sys.exit(1)
        if not os.path.isfile(args.input):
            print(
                f'错误：路径不是文件 —— "{args.input}"\n'
                f'  输入的路径是一个目录而非文件。',
                file=sys.stderr
            )
            sys.exit(1)

    # 自动生成输出路径（如未指定且提供了输入文件）
    if args.input and args.output is None:
        args.output = generate_default_output(args.input)
        print(f'输出文件未指定，自动生成: {args.output}', file=sys.stderr)

    # 还原模式
    if args.unmask:
        if not args.map_file:
            print(
                '错误：还原模式需要提供映射表文件。\n'
                '  用法: python mask.py --unmask -i masked.txt -o restored.txt -m mapping.json\n'
                '  映射表文件是之前脱敏时用 -m 参数保存的 JSON 文件。',
                file=sys.stderr
            )
            sys.exit(1)
        if not os.path.exists(args.map_file):
            print(
                f'错误：映射表文件不存在 —— "{args.map_file}"\n'
                f'  请确认映射表文件路径，或重新脱敏并保存映射表。',
                file=sys.stderr
            )
            sys.exit(1)
        with open(args.map_file, 'r', encoding='utf-8') as f:
            try:
                mapping = json.load(f)
            except json.JSONDecodeError as e:
                print(
                    f'错误：映射表文件格式错误 —— {e}\n'
                    f'  请确认该文件是有效的 JSON 格式。',
                    file=sys.stderr
                )
                sys.exit(1)
        if args.input:
            text, _ = _read_text_file(args.input)
        else:
            text = sys.stdin.read()
        result = unmask_text(text, mapping)
    else:
        if args.input:
            text, enc = _read_text_file(args.input)
            if enc != 'utf-8':
                print(f'提示: 文件编码为 {enc}，已自动适配', file=sys.stderr)
        else:
            text = sys.stdin.read()

        mapping = {}
        placeholder_counter = [0]
        result, mapping = mask_text(
            text, strategy=args.strategy,
            mapping=mapping, placeholder_counter=placeholder_counter,
            seed=args.seed, skip_phone=args.skip_phone
        )
        if args.map_file:
            map_dir = os.path.dirname(os.path.abspath(args.map_file))
            if map_dir and not os.path.exists(map_dir):
                os.makedirs(map_dir, exist_ok=True)
            with open(args.map_file, 'w', encoding='utf-8') as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)

    # 输出
    if args.output:
        out_dir = os.path.dirname(os.path.abspath(args.output))
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        try:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(result)
        except PermissionError:
            print(
                f'错误：无法写入输出文件 "{args.output}" —— 权限不足。\n'
                f'  请确认输出目录有写入权限。',
                file=sys.stderr
            )
            sys.exit(1)
        unique_count = len(mapping) if mapping else 0
        if unique_count > 0:
            print(f'已脱敏 {unique_count} 个唯一值 → {args.output}', file=sys.stderr)
        else:
            print(f'已输出到: {args.output}', file=sys.stderr)
    else:
        sys.stdout.write(result)


if __name__ == '__main__':
    main()
