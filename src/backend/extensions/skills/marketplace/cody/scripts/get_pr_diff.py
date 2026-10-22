#!/usr/bin/env python3
"""
get_pr_diff.py - 从 GitHub / GitLab 获取 PR diff

用法:
  python get_pr_diff.py --provider github --pr 123
  python get_pr_diff.py --provider gitlab --pr 45 --project "group/project"
  python get_pr_diff.py --url "https://github.com/owner/repo/pull/123"

环境变量:
  GITHUB_TOKEN - GitHub Personal Access Token
  GITLAB_TOKEN - GitLab Personal Access Token
"""

import subprocess
import sys
import argparse
import os
import re
import json
import urllib.request
import urllib.error


def run_git(args, cwd=None):
    """执行 git 命令"""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            cwd=cwd or os.getcwd()
        )
        return result.stdout.strip()
    except FileNotFoundError:
        return None


def get_github_diff(owner, repo, pr_number, token=None):
    """从 GitHub API 获取 PR diff"""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    
    headers = {"Accept": "application/vnd.github.v3.diff"}
    if token:
        headers["Authorization"] = f"token {token}"
    
    req = urllib.request.Request(url, headers=headers)
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[错误] PR #{pr_number} 不存在或无权限访问", file=sys.stderr)
        elif e.code == 401:
            print(f"[错误] GitHub Token 无效或未设置", file=sys.stderr)
        else:
            print(f"[错误] HTTP {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[错误] 网络错误: {e.reason}", file=sys.stderr)
        sys.exit(1)


def get_gitlab_diff(project, pr_number, token=None, base_url="https://gitlab.com"):
    """从 GitLab API 获取 MR diff"""
    # URL encode project path
    project_encoded = project.replace("/", "%2F")
    url = f"{base_url}/api/v4/projects/{project_encoded}/merge_requests/{pr_number}/diffs"
    
    headers = {}
    if token:
        headers["PRIVATE-TOKEN"] = token
    
    req = urllib.request.Request(url, headers=headers)
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            diffs = json.loads(response.read().decode("utf-8"))
            # 合并所有 diff
            return "\n".join(d.get("diff", "") for d in diffs)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[错误] MR !{pr_number} 不存在或无权限访问", file=sys.stderr)
        elif e.code == 401:
            print(f"[错误] GitLab Token 无效或未设置", file=sys.stderr)
        else:
            print(f"[错误] HTTP {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(1)


def parse_github_url(url):
    """解析 GitHub PR URL"""
    # https://github.com/owner/repo/pull/123
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if match:
        return match.group(1), match.group(2), int(match.group(3))
    return None


def parse_gitlab_url(url):
    """解析 GitLab MR URL"""
    # https://gitlab.com/group/project/-/merge_requests/123
    match = re.match(r"https://([^/]+)/([^/]+)/([^/]+)/-/merge_requests/(\d+)", url)
    if match:
        base_url = f"https://{match.group(1)}"
        project = f"{match.group(2)}/{match.group(3)}"
        return base_url, project, int(match.group(4))
    return None


def get_repo_info():
    """从本地 git 仓库获取 remote 信息"""
    remote_url = run_git(["remote", "get-url", "origin"])
    if not remote_url:
        return None
    
    # GitHub: git@github.com:owner/repo.git 或 https://github.com/owner/repo.git
    github_match = re.match(r"(?:git@github\.com:|https://github\.com/)([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if github_match:
        return {
            "provider": "github",
            "owner": github_match.group(1),
            "repo": github_match.group(2)
        }
    
    # GitLab: git@gitlab.com:group/project.git
    gitlab_match = re.match(r"(?:git@([^:]+):|https://([^/]+)/)([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if gitlab_match:
        host = gitlab_match.group(1) or gitlab_match.group(2)
        return {
            "provider": "gitlab",
            "base_url": f"https://{host}",
            "project": f"{gitlab_match.group(3)}/{gitlab_match.group(4)}"
        }
    
    return None


def main():
    parser = argparse.ArgumentParser(description="从 GitHub/GitLab 获取 PR/MR diff")
    parser.add_argument("--provider", choices=["github", "gitlab"], help="平台 (github/gitlab)")
    parser.add_argument("--pr", type=int, help="PR/MR 编号")
    parser.add_argument("--project", type=str, help="GitLab 项目路径 (group/project)")
    parser.add_argument("--url", type=str, help="完整的 PR/MR URL")
    parser.add_argument("--token", type=str, help="API Token (或使用环境变量)")
    parser.add_argument("--base-url", type=str, default="https://gitlab.com", help="GitLab 实例 URL")
    args = parser.parse_args()
    
    # 从 URL 解析
    if args.url:
        github_info = parse_github_url(args.url)
        if github_info:
            owner, repo, pr_number = github_info
            token = args.token or os.environ.get("GITHUB_TOKEN")
            print(f"📦 GitHub PR: {owner}/{repo}#{pr_number}")
            print("-" * 60)
            diff = get_github_diff(owner, repo, pr_number, token)
            print(diff)
            return
        
        gitlab_info = parse_gitlab_url(args.url)
        if gitlab_info:
            base_url, project, pr_number = gitlab_info
            token = args.token or os.environ.get("GITLAB_TOKEN")
            print(f"📦 GitLab MR: {project}!{pr_number}")
            print("-" * 60)
            diff = get_gitlab_diff(project, pr_number, token, base_url)
            print(diff)
            return
        
        print("[错误] 无法解析 URL，请使用完整的 GitHub/GitLab PR/MR URL", file=sys.stderr)
        sys.exit(1)
    
    # 从参数获取
    if args.provider and args.pr:
        if args.provider == "github":
            repo_info = get_repo_info()
            if not repo_info or repo_info["provider"] != "github":
                print("[错误] 无法从当前目录获取 GitHub 仓库信息，请使用 --url 参数", file=sys.stderr)
                sys.exit(1)
            
            owner = repo_info["owner"]
            repo = repo_info["repo"]
            token = args.token or os.environ.get("GITHUB_TOKEN")
            print(f"📦 GitHub PR: {owner}/{repo}#{args.pr}")
            print("-" * 60)
            diff = get_github_diff(owner, repo, args.pr, token)
            print(diff)
            return
        
        elif args.provider == "gitlab":
            repo_info = get_repo_info()
            if not repo_info or repo_info["provider"] != "gitlab":
                if not args.project:
                    print("[错误] 请指定 --project 参数 (group/project)", file=sys.stderr)
                    sys.exit(1)
                project = args.project
                base_url = args.base_url
            else:
                project = repo_info["project"]
                base_url = repo_info.get("base_url", args.base_url)
            
            token = args.token or os.environ.get("GITLAB_TOKEN")
            print(f"📦 GitLab MR: {project}!{args.pr}")
            print("-" * 60)
            diff = get_gitlab_diff(project, args.pr, token, base_url)
            print(diff)
            return
    
    print("[错误] 请指定 --url 或 (--provider + --pr)", file=sys.stderr)
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()