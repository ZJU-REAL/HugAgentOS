#!/usr/bin/env python3
"""
结构化数据脱敏模块 - 专门处理 Excel/CSV/DataFrame 表格数据。
与 mask.py 配合，提供列级控制、批量脱敏、映射管理等能力。

使用示例:
  # 命令行: 对 Excel 脱敏
  python dataframe_mask.py --input data.xlsx --output masked.xlsx --columns 指标,金额

  # 命令行: 对 CSV 脱敏
  python dataframe_mask.py --input data.csv --output masked.csv --strategy blur

  # 编程: 直接操作 DataFrame
  from dataframe_mask import mask_dataframe
  df = pd.read_excel('data.xlsx')
  masked_df, mapping = mask_dataframe(df, columns=['销售额', '利润'], strategy='hash')
  masked_df.to_excel('masked.xlsx', index=False)
"""

import argparse
import json
import os
import sys

# 混合同目录下的 mask.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mask import mask_text, unmask_text


# ── 命名规范 ─────────────────────────────────────────────────
def generate_default_output(input_path, fmt='xlsx'):
    """
    根据输入文件路径自动生成输出路径。
    命名规范：原文件名_mask.原后缀
      例：data.xlsx → data_mask.xlsx
          data.csv  → data_mask.csv
    """
    dir_name = os.path.dirname(input_path) or '.'
    base = os.path.splitext(os.path.basename(input_path))[0]
    ext = os.path.splitext(input_path)[1].lower()
    return os.path.join(dir_name, f'{base}_mask{ext}')


def _infer_numeric_columns(df):
    """自动推断 DataFrame 中的数值列"""
    try:
        import numpy as np
        import pandas as pd
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        # 排除明显不是敏感数据的列（如纯 ID、年份）
        result = []
        for col in numeric_cols:
            sample = df[col].dropna()
            if len(sample) == 0:
                continue
            # 跳过全为整数的年份列（1900-2099）
            if sample.dtype.kind == 'i' and sample.between(1900, 2099).all():
                continue
            result.append(col)
        return result
    except ImportError:
        return df.columns.tolist()


def mask_dataframe(df, columns=None, strategy='hash', seed=42,
                   skip_phone=False, auto_detect_numeric=True):
    """
    对 DataFrame 的指定列进行脱敏。

    参数:
        df: pandas DataFrame
        columns: 需要脱敏的列名列表，None 则自动检测数值列
        strategy: 脱敏策略 (hash|placeholder|blur|range)
        seed: 随机种子
        skip_phone: 是否跳过电话号码
        auto_detect_numeric: 未指定 columns 时是否自动检测数值列

    返回:
        (脱敏后 DataFrame, 全局映射字典 {列名: {原始值: 脱敏值}})
    """
    import pandas as pd

    result_df = df.copy()
    all_mappings = {}

    # 确定需要脱敏的列
    if columns is None and auto_detect_numeric:
        columns = _infer_numeric_columns(df)
        if not columns:
            print('警告：未检测到数值列，将对所有列进行文本脱敏', file=sys.stderr)
            columns = df.columns.tolist()
    elif columns is None:
        columns = df.columns.tolist()

    for col in columns:
        if col not in df.columns:
            print(f'警告：列 "{col}" 不存在，已跳过', file=sys.stderr)
            continue

        col_mapping = {}
        placeholder_counter = [0]

        masked_values = []
        for idx, val in df[col].items():
            if pd.isna(val):
                masked_values.append(val)
                continue

            # 将单元格值转为文本处理
            text = str(val)
            masked_text, _ = mask_text(
                text, strategy=strategy,
                mapping=col_mapping,
                placeholder_counter=placeholder_counter if strategy == 'placeholder' else [0],
                seed=seed, skip_phone=skip_phone
            )
            masked_values.append(masked_text)

        result_df[col] = masked_values
        all_mappings[col] = col_mapping

    return result_df, all_mappings


def mask_excel(input_path, output_path, columns=None, strategy='hash',
               seed=42, sheet_name=0, map_path=None, skip_phone=False,
               auto_detect_numeric=True):
    """
    对 Excel 文件进行脱敏。

    参数:
        input_path: 输入 Excel 路径
        output_path: 输出 Excel 路径
        columns: 需要脱敏的列名列表
        strategy: 脱敏策略
        seed: 随机种子
        sheet_name: 工作表名或索引
        map_path: 映射表输出路径（JSON）
        skip_phone: 是否跳过电话号码
        auto_detect_numeric: 是否自动检测数值列

    返回:
        (脱敏后 DataFrame, 映射字典)
    """
    if not os.path.exists(input_path):
        print(
            f'错误：输入文件不存在 —— "{input_path}"\n'
            f'  请确认文件路径是否正确。',
            file=sys.stderr
        )
        sys.exit(1)

    try:
        import pandas as pd
    except ImportError:
        print(
            '错误：处理 Excel 需要安装 pandas 和 openpyxl 库。\n'
            '  请执行：pip install pandas openpyxl',
            file=sys.stderr
        )
        sys.exit(1)

    try:
        df = pd.read_excel(input_path, sheet_name=sheet_name)
    except ValueError as e:
        # 可能是 sheet 名称不存在
        print(
            f'错误：读取 Excel 失败 —— {e}\n'
            f'  可能原因：工作表名 "{sheet_name}" 不存在。\n'
            f'  请确认工作表名称正确。不指定 --sheet-name 时默认读取第一个工作表。',
            file=sys.stderr
        )
        sys.exit(1)
    except Exception as e:
        print(
            f'错误：无法读取 Excel 文件 —— {e}\n'
            f'  请确认：\n'
            f'  1. 文件是有效的 .xlsx 格式（不是旧版 .xls）\n'
            f'  2. 文件未损坏\n'
            f'  3. 文件未被其他程序占用',
            file=sys.stderr
        )
        sys.exit(1)

    masked_df, mappings = mask_dataframe(
        df, columns=columns, strategy=strategy, seed=seed,
        skip_phone=skip_phone, auto_detect_numeric=auto_detect_numeric
    )

    # 确保输出目录存在
    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    try:
        masked_df.to_excel(output_path, index=False)
    except PermissionError:
        print(
            f'错误：无法写入输出文件 "{output_path}" —— 权限不足。\n'
            f'  请确认输出目录有写入权限，且同名文件未被其他程序（如 Excel）打开。',
            file=sys.stderr
        )
        sys.exit(1)

    if map_path:
        with open(map_path, 'w', encoding='utf-8') as f:
            json.dump(mappings, f, ensure_ascii=False, indent=2)

    return masked_df, mappings


def mask_csv(input_path, output_path, columns=None, strategy='hash',
             seed=42, map_path=None, skip_phone=False,
             auto_detect_numeric=True, encoding='utf-8'):
    """
    对 CSV 文件进行脱敏。

    参数:
        input_path: 输入 CSV 路径
        output_path: 输出 CSV 路径
        columns: 需要脱敏的列名列表
        strategy: 脱敏策略
        seed: 随机种子
        map_path: 映射表输出路径（JSON）
        skip_phone: 是否跳过电话号码
        auto_detect_numeric: 是否自动检测数值列
        encoding: 文件编码（默认 utf-8）

    返回:
        (脱敏后 DataFrame, 映射字典)
    """
    if not os.path.exists(input_path):
        print(
            f'错误：输入文件不存在 —— "{input_path}"',
            file=sys.stderr
        )
        sys.exit(1)

    try:
        import pandas as pd
    except ImportError:
        print(
            '错误：处理 CSV 需要安装 pandas 库。\n'
            '  请执行：pip install pandas',
            file=sys.stderr
        )
        sys.exit(1)

    # 尝试多种编码读取 CSV
    encodings_to_try = [encoding] if encoding != 'utf-8' else ['utf-8', 'utf-8-sig', 'gbk', 'gb2312']
    df = None
    used_enc = None
    for enc in encodings_to_try:
        try:
            df = pd.read_csv(input_path, encoding=enc)
            used_enc = enc
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception as e:
            print(
                f'错误：读取 CSV 文件失败 ({enc} 编码) —— {e}',
                file=sys.stderr
            )
            continue

    if df is None:
        print(
            f'错误：无法读取 CSV 文件 "{input_path}"。\n'
            f'  尝试过的编码: {", ".join(encodings_to_try)}\n'
            f'  请尝试用 --encoding 参数指定正确的编码，如 --encoding gbk',
            file=sys.stderr
        )
        sys.exit(1)

    if used_enc and used_enc != encoding:
        print(f'提示: 自动检测到文件编码为 {used_enc}（指定编码 {encoding} 读取失败）', file=sys.stderr)

    masked_df, mappings = mask_dataframe(
        df, columns=columns, strategy=strategy, seed=seed,
        skip_phone=skip_phone, auto_detect_numeric=auto_detect_numeric
    )

    # 确保输出目录存在
    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    try:
        masked_df.to_csv(output_path, index=False, encoding=used_enc or encoding)
    except PermissionError:
        print(
            f'错误：无法写入 "{output_path}" —— 权限不足。',
            file=sys.stderr
        )
        sys.exit(1)

    if map_path:
        with open(map_path, 'w', encoding='utf-8') as f:
            json.dump(mappings, f, ensure_ascii=False, indent=2)

    return masked_df, mappings


def _normalize_for_lookup(val):
    """将单元格值标准化为字符串，用于映射查找"""
    if val is None or (isinstance(val, float) and val != val):  # NaN check
        return None
    s = str(val)
    # 去除 Excel 读回时 .0 后缀（整数变浮点）
    if '.' in s:
        try:
            f = float(s)
            if f == int(f):
                return str(int(f))
        except ValueError:
            pass
    return s


def unmask_dataframe(df, mappings, columns=None):
    """
    根据映射表还原已脱敏的 DataFrame。

    参数:
        df: 已脱敏的 DataFrame
        mappings: 映射字典 {列名: {原始值: 脱敏值}}
        columns: 需要还原的列名列表，None 则根据 mappings 的 key

    返回:
        还原后的 DataFrame
    """
    import pandas as pd

    result_df = df.copy()

    if columns is None:
        columns = list(mappings.keys())

    for col in columns:
        if col not in mappings or col not in df.columns:
            continue

        col_mapping = mappings[col]
        # 反转映射：脱敏值 → 原始值
        reverse_map = {v: k for k, v in col_mapping.items()}

        def restore(val):
            if pd.isna(val):
                return val
            key = _normalize_for_lookup(val)
            if key is None:
                return val
            return reverse_map.get(key, val)

        result_df[col] = df[col].apply(restore)

    return result_df


def main():
    parser = argparse.ArgumentParser(
        description='结构化数据脱敏工具 - 处理 Excel/CSV 表格数据中的敏感数字',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # Excel 脱敏（自动生成 data_mask.xlsx）
  python dataframe_mask.py -i data.xlsx

  # 手动指定输出
  python dataframe_mask.py -i data.xlsx -o masked.xlsx

  # 手动指定列
  python dataframe_mask.py -i data.xlsx -c "销售额,利润"

  # CSV 脱敏（自动生成 data_mask.csv）
  python dataframe_mask.py -i data.csv --encoding gbk

  # 带映射表（自动生成输出 + 映射表）
  python dataframe_mask.py -i data.xlsx -m mapping.json

  # 还原
  python dataframe_mask.py --unmask -i masked.xlsx -o restored.xlsx -m mapping.json
        """
    )
    parser.add_argument('-i', '--input', required=True, help='输入文件路径 (.xlsx/.csv)')
    parser.add_argument('-o', '--output', default=None,
                        help='输出文件路径（可选，默认：原文件名_mask.原后缀）')
    parser.add_argument('-c', '--columns', help='手动指定脱敏列（逗号分隔），默认自动检测数值列')
    parser.add_argument(
        '-s', '--strategy',
        choices=['hash', 'placeholder', 'blur', 'range'],
        default='hash', help='脱敏策略 (默认: hash)'
    )
    parser.add_argument('--seed', type=int, default=42, help='随机种子 (默认: 42)')
    parser.add_argument('-m', '--map-file', help='映射表输出路径（JSON）')
    parser.add_argument('--skip-phone', action='store_true', help='跳过电话号码')
    parser.add_argument('--sheet-name', default=0, help='Excel 工作表名或索引 (默认: 0)')
    parser.add_argument('--encoding', default='utf-8', help='CSV 文件编码 (默认: utf-8)')
    parser.add_argument('--unmask', action='store_true', help='还原模式，需要 --map-file')

    args = parser.parse_args()

    # 输入文件验证
    if not os.path.exists(args.input):
        print(
            f'错误：输入文件不存在 —— "{args.input}"\n'
            f'  请确认文件路径是否正确。',
            file=sys.stderr
        )
        sys.exit(1)
    if not os.path.isfile(args.input):
        print(
            f'错误：路径不是文件 —— "{args.input}"',
            file=sys.stderr
        )
        sys.exit(1)

    # 自动生成输出路径（如未指定）
    ext = os.path.splitext(args.input)[1].lower()
    if args.output is None:
        args.output = generate_default_output(args.input)
        print(f'输出文件未指定，自动生成: {args.output}', file=sys.stderr)

    columns = None
    if args.columns:
        columns = [c.strip() for c in args.columns.split(',') if c.strip()]

    if args.unmask:
        if not args.map_file or not os.path.exists(args.map_file):
            print(
                '错误：还原模式需要配合 --map-file 使用。\n'
                '  脱敏时请加 -m mapping.json 保存映射表，还原时用 --unmask -m mapping.json。\n'
                f'  {"映射表文件不存在" if args.map_file else "未指定映射表文件"}',
                file=sys.stderr
            )
            sys.exit(1)
        with open(args.map_file, 'r', encoding='utf-8') as f:
            try:
                mappings = json.load(f)
            except json.JSONDecodeError as e:
                print(
                    f'错误：映射表文件格式错误 —— {e}\n'
                    f'  请确认该文件是有效的 JSON 格式。',
                    file=sys.stderr
                )
                sys.exit(1)
        ext = os.path.splitext(args.input)[1].lower()
        if ext in ('.xlsx', '.xls'):
            import pandas as pd
            df = pd.read_excel(args.input, sheet_name=args.sheet_name)
            restored_df = unmask_dataframe(df, mappings, columns=columns)
            restored_df.to_excel(args.output, index=False)
        elif ext == '.csv':
            import pandas as pd
            df = pd.read_csv(args.input, encoding=args.encoding)
            restored_df = unmask_dataframe(df, mappings, columns=columns)
            restored_df.to_csv(args.output, index=False, encoding=args.encoding)
        else:
            print(f'错误：不支持的还原格式: {ext}，支持 .xlsx / .xls / .csv', file=sys.stderr)
            sys.exit(1)
        print(f'✅ 已还原并输出到: {args.output}', file=sys.stderr)
        return

    if ext in ('.xlsx', '.xls'):
        masked_df, mappings = mask_excel(
            args.input, args.output, columns=columns,
            strategy=args.strategy, seed=args.seed,
            sheet_name=args.sheet_name, map_path=args.map_file,
            skip_phone=args.skip_phone
        )
    elif ext == '.csv':
        masked_df, mappings = mask_csv(
            args.input, args.output, columns=columns,
            strategy=args.strategy, seed=args.seed,
            map_path=args.map_file, skip_phone=args.skip_phone,
            encoding=args.encoding
        )
    else:
        print(
            f'错误：不支持的文件格式 "{ext}"。\n'
            f'  本工具支持 .xlsx / .xls / .csv 格式的表格文件。\n'
            f'  如果是其他格式的文档，请使用 file_mask.py。',
            file=sys.stderr
        )
        sys.exit(1)

    total_masked = sum(len(m) for m in mappings.values())
    cols_info = ', '.join(mappings.keys())
    print(f'✅ 脱敏完成: {total_masked} 个唯一值 | 列: {cols_info} | 策略: {args.strategy}',
          file=sys.stderr)
    print(f'  输出: {args.output}', file=sys.stderr)


if __name__ == '__main__':
    main()
