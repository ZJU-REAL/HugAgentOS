#!/usr/bin/env python3
"""
run_all.py — SOP 提取器一键运行脚本

串联全部步骤（提取 → 优化 → 格式化 → 报告），一条命令生成完整交付物。
出错时自动输出「错误原因 + 修复建议」，无需手动排查。

用法:
    python run_all.py -f input.txt -o output_dir/
    python run_all.py -t "我的流程描述..." -o output_dir/ --format all
    python run_all.py -f input.txt -o output_dir/ --no-report

参数说明:
    -f / --file     输入文本文件路径
    -t / --text     直接传入流程文本（与 -f 二选一）
    -o / --output   输出目录（不存在会自动创建）
    --format        输出格式: html | markdown | checklist | mermaid | training | all（默认 all）
    --no-report     跳过生成综合分析报告
    --theme         HTML 主题: dark | light（默认 dark）
    --quiet         不输出详细日志，只显示最终结果
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
SKILL_DIR = SCRIPTS_DIR.parent

# 每个步骤失败时的诊断提示
STEP_DIAGNOSTICS = {
    "extract": [
        "💡 常见原因及修复方式：",
        "   · 输入文本无具体操作动词 → 补充描述，或加 --min-steps 2 参数",
        "   · 文件路径含中文/空格 → 改用纯英文绝对路径，如 C:/sop/input.txt",
        "   · 文件编码非UTF-8 → 用记事本另存为UTF-8格式再重试",
        "   · 内容过短（< 50字）→ 补充更多操作细节后重试",
    ],
    "optimize": [
        "💡 优化步骤失败，已自动降级为原始提取结果继续：",
        "   · 如需完整优化分析，请检查 sop_output.json 是否生成正常",
        "   · 手动重试：python sop_optimizer.py -i sop_output.json -o sop_optimized.json --report opt.json",
    ],
    "format": [
        "💡 格式化输出失败，常见原因：",
        "   · 输入JSON文件损坏 → 检查提取步骤是否成功生成了合法JSON",
        "   · 输出目录无写权限 → 更换输出目录，或以管理员身份运行",
        "   · HTML主题参数无效 → 仅支持 dark / light",
    ],
    "report": [
        "💡 报告生成失败，不影响主要SOP文档的生成：",
        "   · 手动重试：python report_generator.py -s sop_output.json -o sop_report.html",
        "   · 或添加 --no-report 参数跳过报告生成",
    ],
}


def run_step(cmd: list, desc: str, quiet: bool = False) -> tuple:
    """运行一个子步骤，返回 (成功与否, 错误信息)"""
    if not quiet:
        print(f"\n⏳ {desc}...")
    try:
        result = subprocess.run(
            [sys.executable] + cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=SCRIPTS_DIR
        )
        if result.returncode == 0:
            if not quiet:
                print(f"   ✅ 完成")
            return True, ""
        else:
            err = result.stderr.strip() or result.stdout.strip()
            return False, err
    except Exception as e:
        return False, str(e)


def print_diagnostic(step_key: str, raw_err: str, quiet: bool = False):
    """打印详细诊断信息"""
    if quiet:
        return
    if raw_err:
        # 提取关键错误行（避免刷屏）
        lines = raw_err.splitlines()
        key_lines = [l for l in lines if any(
            kw in l for kw in ["Error", "error", "错误", "FileNotFound", "JSONDecodeError", "No steps"]
        )]
        if key_lines:
            print(f"   📋 错误详情：{key_lines[-1].strip()}")
        elif lines:
            print(f"   📋 错误详情：{lines[-1].strip()}")
    hints = STEP_DIAGNOSTICS.get(step_key, [])
    for h in hints:
        print(h)


def parse_args():
    parser = argparse.ArgumentParser(
        description="SOP 提取器 — 一键运行脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("-f", "--file", help="输入文本文件路径")
    input_group.add_argument("-t", "--text", help="直接传入流程文本")

    parser.add_argument("-o", "--output", required=True, help="输出目录路径")
    parser.add_argument("--format", default="all",
                        choices=["html", "markdown", "checklist", "mermaid", "training", "json", "all"],
                        help="输出格式（默认 all）")
    parser.add_argument("--no-report", action="store_true", help="跳过生成综合分析报告")
    parser.add_argument("--theme", default="dark", choices=["dark", "light"], help="HTML 主题")
    parser.add_argument("--quiet", action="store_true", help="静默模式")
    return parser.parse_args()


def main():
    args = parse_args()

    # 准备输出目录
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 准备输入文件
    tmp_input = None
    if args.text:
        tmp_input = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                                delete=False, encoding="utf-8")
        tmp_input.write(args.text)
        tmp_input.close()
        input_path = tmp_input.name
    else:
        input_path = args.file
        if not Path(input_path).exists():
            print(f"❌ 错误：输入文件不存在：{input_path}")
            print("💡 请检查文件路径是否正确，建议使用绝对路径（如 C:/sop/input.txt）")
            sys.exit(1)

    sop_json = str(output_dir / "sop_output.json")
    optimized_json = str(output_dir / "sop_optimized.json")
    opt_report_json = str(output_dir / "optimization_report.json")

    errors = []
    success_count = 0

    if not args.quiet:
        print("=" * 60)
        print("  SOP 标准化流程提取器 — 一键运行")
        print("  出错时将自动输出修复建议，请按提示操作")
        print("=" * 60)

    # ── 步骤 1：提取 ────────────────────────────────────────
    ok, err = run_step(
        ["sop_extractor.py", "-f", input_path, "-o", sop_json],
        "步骤 1/4：提取结构化 SOP",
        args.quiet
    )
    if not ok:
        print(f"\n❌ 步骤1（提取）失败")
        print_diagnostic("extract", err, args.quiet)
        print("\n📖 更多排错详情：参阅 references/faq.md Q8-Q10")
        _cleanup(tmp_input)
        sys.exit(1)
    success_count += 1

    # ── 步骤 2：优化分析 ────────────────────────────────────
    ok, err = run_step(
        ["sop_optimizer.py", "-i", sop_json, "-o", optimized_json, "--report", opt_report_json],
        "步骤 2/4：质量评估与优化分析",
        args.quiet
    )
    if not ok:
        if not args.quiet:
            print_diagnostic("optimize", err, args.quiet)
        optimized_json = sop_json   # 自动降级：使用原始JSON继续
        errors.append(f"优化分析：{err[:120]}")
    else:
        success_count += 1

    # ── 步骤 3：多格式输出 ──────────────────────────────────
    fmt_args = ["sop_formatter.py", "-i", optimized_json,
                "-f", args.format, "-o", str(output_dir / "sop")]
    if args.format in ("html", "all"):
        fmt_args += ["--theme", args.theme]

    ok, err = run_step(fmt_args, "步骤 3/4：生成多格式文档", args.quiet)
    if not ok:
        if not args.quiet:
            print_diagnostic("format", err, args.quiet)
        errors.append(f"格式化输出：{err[:120]}")
    else:
        success_count += 1

    # ── 步骤 4：综合报告 ────────────────────────────────────
    if not args.no_report:
        report_cmd = ["report_generator.py", "-s", sop_json,
                      "-o", str(output_dir / "sop_report.html")]
        if Path(opt_report_json).exists():
            report_cmd += ["--optimization", opt_report_json]

        ok, err = run_step(report_cmd, "步骤 4/4：生成综合分析报告", args.quiet)
        if not ok:
            if not args.quiet:
                print_diagnostic("report", err, args.quiet)
            errors.append(f"综合报告：{err[:120]}")
        else:
            success_count += 1

    # ── 收尾：读取质量评分 ──────────────────────────────────
    score_info = ""
    if Path(sop_json).exists():
        try:
            with open(sop_json, encoding="utf-8") as f:
                data = json.load(f)
            steps = len(data.get("steps", []))
            score = data.get("quality_score", {}).get("total", "N/A")
            grade = data.get("quality_score", {}).get("grade", "")
            score_info = f"  步骤数：{steps} | 质量评分：{score}/100（{grade}级）"
        except Exception:
            pass

    # ── 清理临时文件 ────────────────────────────────────────
    _cleanup(tmp_input)

    # ── 输出结果摘要 ────────────────────────────────────────
    print("\n" + "=" * 60)
    if errors:
        print(f"⚠️  完成（{success_count}/4 步成功，{len(errors)} 步有警告）")
    else:
        print(f"✅ 全部完成（{success_count}/4 步）")

    if score_info:
        print(score_info)

    print(f"\n📁 输出目录：{output_dir.resolve()}")

    # 列出生成的文件
    generated = list(output_dir.glob("*"))
    if generated:
        print("\n📦 生成的文件：")
        for f in sorted(generated):
            if f.is_file():
                size_kb = f.stat().st_size / 1024
                print(f"   {f.name:<32} {size_kb:.1f} KB")

    if errors:
        print("\n⚠️  以下步骤有警告（主要SOP文档不受影响）：")
        for e in errors:
            print(f"   · {e}")
        print("📖 详细排错指南：references/faq.md")

    print("=" * 60)
    return 0 if not errors else 1


def _cleanup(tmp_input):
    if tmp_input and Path(tmp_input.name).exists():
        try:
            os.unlink(tmp_input.name)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
