#!/usr/bin/env python3
"""
contract-guardian 合同文件解析模块

支持 TXT、MD、PDF、DOCX 格式的合同文件解析。
免费版仅支持 TXT/MD，付费版支持全部格式。
"""

import argparse
import os
import sys

# 将 scripts 目录加入路径以便导入 utils
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import (
    check_subscription,
    mask_sensitive_info,
    output_error,
    output_success,
    read_text_file,
)


def parse_txt(file_path: str) -> str:
    """解析 TXT 文件。

    Args:
        file_path: 文件路径。

    Returns:
        文件文本内容。
    """
    return read_text_file(file_path)


def parse_md(file_path: str) -> str:
    """解析 Markdown 文件。

    Args:
        file_path: 文件路径。

    Returns:
        文件文本内容。
    """
    return read_text_file(file_path)


def parse_pdf(file_path: str) -> str:
    """解析 PDF 文件。

    优先使用 pdfplumber 库，若不可用则尝试基础文本提取。

    Args:
        file_path: 文件路径。

    Returns:
        提取的文本内容。

    Raises:
        ImportError: 当 PDF 解析库不可用时抛出。
    """
    try:
        import pdfplumber

        text_parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        if text_parts:
            return "\n\n".join(text_parts)
        return ""
    except ImportError:
        pass

    # 基础 fallback：尝试读取 PDF 中的文本流
    try:
        with open(file_path, "rb") as f:
            content = f.read()
        # 尝试提取 PDF 文本流中的内容
        import re

        text_parts = []
        # 匹配 PDF 文本对象中的字符串
        for match in re.finditer(rb"\(([^)]+)\)", content):
            try:
                decoded = match.group(1).decode("utf-8", errors="ignore")
                if len(decoded.strip()) > 1:
                    text_parts.append(decoded.strip())
            except Exception:
                continue

        if text_parts:
            return "\n".join(text_parts)

        raise ImportError(
            "无法解析 PDF 文件。请安装 pdfplumber: pip install pdfplumber"
        )
    except ImportError:
        raise
    except Exception:
        raise ImportError(
            "无法解析 PDF 文件。请安装 pdfplumber: pip install pdfplumber"
        )


def parse_docx(file_path: str) -> str:
    """解析 DOCX 文件。

    使用 python-docx 库解析 Word 文档。

    Args:
        file_path: 文件路径。

    Returns:
        提取的文本内容。

    Raises:
        ImportError: 当 python-docx 库不可用时抛出。
    """
    try:
        from docx import Document

        doc = Document(file_path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    except ImportError:
        raise ImportError(
            "无法解析 DOCX 文件。请安装 python-docx: pip install python-docx"
        )


def detect_format(file_path: str) -> str:
    """根据文件扩展名检测格式。

    Args:
        file_path: 文件路径。

    Returns:
        格式字符串: txt, md, pdf, docx。
    """
    ext = os.path.splitext(file_path)[1].lower()
    format_map = {
        ".txt": "txt",
        ".text": "txt",
        ".md": "md",
        ".markdown": "md",
        ".pdf": "pdf",
        ".docx": "docx",
        ".doc": "docx",
    }
    return format_map.get(ext, "txt")


def parse_contract(file_path: str, file_format: str = None) -> dict:
    """解析合同文件并返回结构化结果。

    Args:
        file_path: 合同文件路径。
        file_format: 文件格式，若为 None 则自动检测。

    Returns:
        包含解析结果的字典:
        {
            "file_path": str,
            "format": str,
            "text": str,
            "char_count": int,
            "line_count": int
        }
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"合同文件不存在: {file_path}")

    if file_format is None:
        file_format = detect_format(file_path)

    file_format = file_format.lower()

    # 订阅校验
    sub = check_subscription()
    supported = sub.get("supported_formats", ["txt", "md"])
    if file_format not in supported:
        raise ValueError(
            f"当前订阅等级不支持 {file_format.upper()} 格式。"
            f"免费版仅支持 TXT/MD，付费版支持 TXT/MD/PDF/DOCX。"
            f"如需升级至付费版（¥129/月），请联系管理员。"
        )

    # 解析文件
    parsers = {
        "txt": parse_txt,
        "md": parse_md,
        "pdf": parse_pdf,
        "docx": parse_docx,
    }

    parser_func = parsers.get(file_format)
    if parser_func is None:
        raise ValueError(f"不支持的文件格式: {file_format}")

    text = parser_func(file_path)

    return {
        "file_path": os.path.abspath(file_path),
        "format": file_format,
        "text": text,
        "char_count": len(text),
        "line_count": len(text.splitlines()),
    }


def extract_text(file_path: str, file_format: str = None) -> str:
    """从合同文件中提取纯文本内容。

    Args:
        file_path: 合同文件路径。
        file_format: 文件格式，若为 None 则自动检测。

    Returns:
        提取的纯文本内容。
    """
    result = parse_contract(file_path, file_format)
    return result["text"]


def main():
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="合同文件解析工具 — 支持 TXT/MD/PDF/DOCX 格式",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["parse", "extract-text"],
        help="操作类型: parse（完整解析）, extract-text（提取纯文本）",
    )
    parser.add_argument(
        "--file",
        required=True,
        help="合同文件路径",
    )
    parser.add_argument(
        "--format",
        default=None,
        choices=["txt", "md", "pdf", "docx"],
        help="文件格式（可选，默认自动检测）",
    )

    args = parser.parse_args()

    try:
        if args.action == "parse":
            result = parse_contract(args.file, args.format)
            # 脱敏处理后输出
            result["text"] = mask_sensitive_info(result["text"])
            output_success(result)

        elif args.action == "extract-text":
            text = extract_text(args.file, args.format)
            text = mask_sensitive_info(text)
            output_success({"text": text})

    except FileNotFoundError as e:
        output_error(str(e), "FILE_NOT_FOUND")
    except ImportError as e:
        output_error(str(e), "DEPENDENCY_MISSING")
    except ValueError as e:
        output_error(str(e), "VALIDATION_ERROR")
    except Exception as e:
        output_error(f"解析失败: {e}", "PARSE_ERROR")


if __name__ == "__main__":
    main()
