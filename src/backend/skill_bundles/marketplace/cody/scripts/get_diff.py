#!/usr/bin/env python3
"""
get_diff.py - 获取 Git diff 内容

用法:
  python get_diff.py                    # 最近一次提交的变更
  python get_diff.py --staged           # 已暂存（staged）的变更
  python get_diff.py --branch main      # 当前分支与 main 的差异
  python get_diff.py --commits HEAD~3   # 最近 3 次提交的变更
  python get_diff.py --file src/app.py  # 指定文件的变更
"""

import subprocess
import sys
import argparse
import os


def run_git(args, cwd=None):
    """执行 git 命令，返回输出"""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            cwd=cwd or os.getcwd()
        )
        if result.returncode != 0:
            print(f"[错误] git 命令失败: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        return result.stdout
    except FileNotFoundError:
        print("[错误] 未找到 git 命令，请确认已安装 Git", file=sys.stderr)
        sys.exit(1)


def get_repo_info(cwd=None):
    """获取仓库基本信息"""
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()
    commit = run_git(["log", "-1", "--format=%h %s", "--"], cwd).strip()
    return branch, commit


def main():
    parser = argparse.ArgumentParser(description="获取 Git diff 用于代码 Review")
    parser.add_argument("--staged", action="store_true", help="获取已暂存的变更")
    parser.add_argument("--branch", type=str, help="与指定分支对比（如 main）")
    parser.add_argument("--commits", type=str, help="最近 N 次提交（如 HEAD~3）")
    parser.add_argument("--file", type=str, help="只获取指定文件的变更")
    parser.add_argument("--path", type=str, help="Git 仓库路径（默认当前目录）")
    parser.add_argument("--stat", action="store_true", help="只显示变更统计，不显示详细 diff")
    args = parser.parse_args()

    cwd = args.path or os.getcwd()

    # 检查是否在 git 仓库中
    run_git(["rev-parse", "--git-dir"], cwd)

    branch, last_commit = get_repo_info(cwd)
    print(f"📁 仓库路径: {cwd}")
    print(f"🌿 当前分支: {branch}")
    print(f"📝 最新提交: {last_commit}")
    print("-" * 60)

    # 构建 git diff 命令
    git_args = ["diff"]

    if args.stat:
        git_args.append("--stat")
    else:
        git_args.extend(["--unified=3"])

    if args.staged:
        git_args.append("--cached")
        print("📋 模式: 已暂存的变更（staged）")
    elif args.branch:
        git_args.append(f"{args.branch}...HEAD")
        print(f"📋 模式: 与 {args.branch} 分支的差异")
    elif args.commits:
        git_args.extend([args.commits, "HEAD"])
        print(f"📋 模式: {args.commits} 到 HEAD 的变更")
    else:
        git_args.extend(["HEAD~1", "HEAD"])
        print("📋 模式: 最近一次提交的变更")

    if args.file:
        git_args.extend(["--", args.file])
        print(f"📋 文件过滤: {args.file}")

    print("-" * 60)

    diff_output = run_git(git_args, cwd)

    if not diff_output.strip():
        print("✅ 没有发现变更")
        return

    # 统计信息
    lines = diff_output.split("\n")
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    files = [l[6:] for l in lines if l.startswith("+++ b/")]

    print(f"📊 变更统计: {len(files)} 个文件，+{added} 行，-{removed} 行")
    if files:
        print("📄 变更文件:")
        for f in files:
            print(f"   - {f}")
    print("-" * 60)
    print(diff_output)


if __name__ == "__main__":
    main()
