#!/usr/bin/env python3
"""
contract-guardian 合同对比模块

逐条对比两份合同的差异，高亮变更内容，分析风险增减。
此功能仅限付费版用户使用。
"""

import argparse
import difflib
import os
import re
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import (
    check_subscription,
    is_paid,
    output_error,
    output_success,
    read_text_file,
)


def split_clauses(text: str) -> List[Dict[str, str]]:
    """将合同文本按条款拆分。

    识别常见的条款编号格式（第X条、X.X、一/二/三等）。

    Args:
        text: 合同文本。

    Returns:
        条款列表，每项包含 title 和 content。
    """
    # 匹配条款标题模式
    clause_pattern = re.compile(
        r"^(?:"
        r"第[一二三四五六七八九十百零\d]+[条章节]"
        r"|[一二三四五六七八九十]+[、.．]"
        r"|\d+[、.．]\d*"
        r"|[（(]\d+[)）]"
        r")",
        re.MULTILINE,
    )

    matches = list(clause_pattern.finditer(text))

    if not matches:
        # 无法按条款分割，按段落分割
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        return [
            {"title": f"段落 {i + 1}", "content": p}
            for i, p in enumerate(paragraphs)
        ]

    clauses = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        clause_text = text[start:end].strip()

        # 提取标题（第一行）
        lines = clause_text.split("\n", 1)
        title = lines[0].strip()
        content = lines[1].strip() if len(lines) > 1 else ""

        clauses.append({
            "title": title,
            "content": content,
        })

    return clauses


def compare_texts(text1: str, text2: str) -> Dict[str, Any]:
    """对比两段文本的差异。

    Args:
        text1: 第一份合同文本。
        text2: 第二份合同文本。

    Returns:
        差异分析结果。
    """
    lines1 = text1.splitlines(keepends=True)
    lines2 = text2.splitlines(keepends=True)

    differ = difflib.unified_diff(
        lines1,
        lines2,
        fromfile="合同A",
        tofile="合同B",
        lineterm="",
    )
    diff_lines = list(differ)

    # 统计变更
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))

    # 计算相似度
    matcher = difflib.SequenceMatcher(None, text1, text2)
    similarity = round(matcher.ratio() * 100, 1)

    return {
        "similarity": similarity,
        "lines_added": added,
        "lines_removed": removed,
        "diff": "".join(diff_lines) if diff_lines else "两份合同内容完全相同",
    }


def compare_clauses(clauses1: List[Dict[str, str]], clauses2: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """逐条对比两份合同的条款。

    Args:
        clauses1: 第一份合同的条款列表。
        clauses2: 第二份合同的条款列表。

    Returns:
        条款差异列表。
    """
    differences = []

    # 构建标题到内容的映射
    map1 = {c["title"]: c["content"] for c in clauses1}
    map2 = {c["title"]: c["content"] for c in clauses2}

    all_titles = list(dict.fromkeys(
        [c["title"] for c in clauses1] + [c["title"] for c in clauses2]
    ))

    for title in all_titles:
        content1 = map1.get(title)
        content2 = map2.get(title)

        if content1 is None:
            differences.append({
                "clause": title,
                "change_type": "新增",
                "description": "合同B中新增的条款",
                "content_b": content2[:200] if content2 else "",
            })
        elif content2 is None:
            differences.append({
                "clause": title,
                "change_type": "删除",
                "description": "合同B中删除的条款",
                "content_a": content1[:200] if content1 else "",
            })
        elif content1 != content2:
            # 计算条款内容的相似度
            matcher = difflib.SequenceMatcher(None, content1, content2)
            sim = round(matcher.ratio() * 100, 1)

            differences.append({
                "clause": title,
                "change_type": "修改",
                "similarity": sim,
                "description": f"条款内容有变更（相似度 {sim}%）",
                "content_a_excerpt": content1[:150],
                "content_b_excerpt": content2[:150],
            })

    return differences


def generate_diff_report(file1: str, file2: str) -> Dict[str, Any]:
    """生成完整的合同对比报告。

    Args:
        file1: 第一份合同文件路径。
        file2: 第二份合同文件路径。

    Returns:
        包含对比报告的字典。
    """
    # 付费版校验
    if not is_paid():
        raise ValueError(
            "合同对比功能仅限付费版用户使用。"
            "升级至付费版（¥129/月）即可使用逐条对比功能。"
        )

    text1 = read_text_file(file1)
    text2 = read_text_file(file2)

    # 整体对比
    overall = compare_texts(text1, text2)

    # 条款级对比
    clauses1 = split_clauses(text1)
    clauses2 = split_clauses(text2)
    clause_diffs = compare_clauses(clauses1, clauses2)

    # 生成 Markdown 报告
    report_lines = [
        "# 合同对比报告\n",
        f"- **合同A**: {os.path.basename(file1)}",
        f"- **合同B**: {os.path.basename(file2)}",
        f"- **整体相似度**: {overall['similarity']}%",
        f"- **新增行数**: {overall['lines_added']}",
        f"- **删除行数**: {overall['lines_removed']}",
        "",
        "## 条款差异汇总\n",
        f"共发现 **{len(clause_diffs)}** 处差异：\n",
    ]

    if clause_diffs:
        report_lines.append("| 条款 | 变更类型 | 说明 |")
        report_lines.append("|------|----------|------|")
        for diff in clause_diffs:
            report_lines.append(
                f"| {diff['clause']} | {diff['change_type']} | {diff['description']} |"
            )

        report_lines.append("\n## 详细差异\n")
        for diff in clause_diffs:
            report_lines.append(f"### {diff['clause']}（{diff['change_type']}）\n")
            if diff["change_type"] == "新增":
                report_lines.append(f"> {diff.get('content_b', '')}\n")
            elif diff["change_type"] == "删除":
                report_lines.append(f"> ~~{diff.get('content_a', '')}~~\n")
            elif diff["change_type"] == "修改":
                report_lines.append(f"**合同A**: {diff.get('content_a_excerpt', '')}\n")
                report_lines.append(f"**合同B**: {diff.get('content_b_excerpt', '')}\n")
    else:
        report_lines.append("未发现条款级差异，两份合同结构一致。\n")

    markdown_report = "\n".join(report_lines)

    return {
        "file1": os.path.abspath(file1),
        "file2": os.path.abspath(file2),
        "similarity": overall["similarity"],
        "total_differences": len(clause_diffs),
        "clause_differences": clause_diffs,
        "markdown_report": markdown_report,
    }


def main():
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="合同对比工具 — 逐条对比两份合同的差异（付费功能）",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["compare", "diff-report"],
        help="操作类型: compare（基础对比）, diff-report（完整差异报告）",
    )
    parser.add_argument(
        "--file1",
        required=True,
        help="第一份合同文件路径",
    )
    parser.add_argument(
        "--file2",
        required=True,
        help="第二份合同文件路径",
    )

    args = parser.parse_args()

    try:
        if args.action == "compare":
            if not is_paid():
                raise ValueError(
                    "合同对比功能仅限付费版用户使用。"
                    "升级至付费版（¥129/月）即可使用逐条对比功能。"
                )
            text1 = read_text_file(args.file1)
            text2 = read_text_file(args.file2)
            result = compare_texts(text1, text2)
            output_success(result)

        elif args.action == "diff-report":
            result = generate_diff_report(args.file1, args.file2)
            output_success(result)

    except ValueError as e:
        output_error(str(e), "VALIDATION_ERROR")
    except FileNotFoundError as e:
        output_error(str(e), "FILE_NOT_FOUND")
    except Exception as e:
        output_error(f"对比失败: {e}", "COMPARE_ERROR")


if __name__ == "__main__":
    main()
