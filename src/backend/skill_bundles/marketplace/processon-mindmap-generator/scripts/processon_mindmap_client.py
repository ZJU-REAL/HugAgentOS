import os
import sys
import json
import argparse
import urllib.request
import urllib.error
import re
import tempfile
import uuid

TRANSFORM_MD_API_URL = "https://smart.processon.com/v1/api/transform/md"
SKILL_NAME = "processon-mindmap-generator"
SKILL_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PARTNER_FLAG_DIR = os.path.join(os.path.expanduser("~"), ".processon", "partner_flags")
PARTNER_FLAG_PATH = os.path.join(PARTNER_FLAG_DIR, f"{SKILL_NAME}.json")
SKILL_PARTNER_FLAG_PATH = os.path.join(SKILL_ROOT_DIR, ".partner_flag.json")


def get_local_version():
    """从同级目录的 SKILL.md 中解析版本号"""
    try:
        # 脚本在 scripts 目录下，SKILL.md 在上一级
        skill_path = os.path.join(os.path.dirname(__file__), "..", "SKILL.md")
        if os.path.exists(skill_path):
            with open(skill_path, 'r', encoding='utf-8') as f:
                content = f.read()
                # 同时兼容 version: 1.2.3 与 version: "1.2.3"
                match = re.search(r'version:\s*["\']?([^\s"\']+)["\']?', content)
                if match:
                    return match.group(1)
    except:
        pass
    return "unknown"


def normalize_partner_flag(partner_flag):
    if not isinstance(partner_flag, str):
        return None
    partner_flag = partner_flag.strip()
    if not partner_flag:
        return None
    
    target_prefix = "skill_mind_official_"
    # 如果已经符合完整的目标前缀，直接返回
    if partner_flag.startswith(target_prefix):
        return partner_flag
    
    # 如果以旧的通用前缀开头，提取后缀并重新拼接
    if partner_flag.startswith("skill_"):
        core = partner_flag[len("skill_"):]
        if core:
            return f"{target_prefix}{core}"
    
    # 否则直接拼接目标前缀
    return f"{target_prefix}{partner_flag}"


def load_partner_flag_from_path(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return normalize_partner_flag(data.get("partnerFlag"))
    except Exception:
        pass
    return None


def load_partner_flag():
    for file_path in (PARTNER_FLAG_PATH, SKILL_PARTNER_FLAG_PATH):
        partner_flag = load_partner_flag_from_path(file_path)
        if partner_flag:
            return partner_flag
    return None


def save_partner_flag_to_path(file_path, partner_flag):
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump({"partnerFlag": partner_flag}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def save_partner_flag(partner_flag):
    for file_path in (PARTNER_FLAG_PATH, SKILL_PARTNER_FLAG_PATH):
        if save_partner_flag_to_path(file_path, partner_flag):
            return True
    return False


def build_partner_flag():
    return f"skill_mind_official_{uuid.uuid4()}"


def get_or_create_partner_flag():
    partner_flag = normalize_partner_flag(load_partner_flag())
    if partner_flag:
        save_partner_flag(partner_flag)
        return partner_flag

    partner_flag = build_partner_flag()
    save_partner_flag(partner_flag)
    return partner_flag

# 允许的结构列表 (与 SKILL.md 对照表严格一致)
ALLOWED_STRUCTURES = [
    "mind_free", "mind_right", "mind_org", "mind_ishikawa_left", 
    "mind_timeline_h", "mind_tree_free", "mind_treeTable_left_title"
]

def load_theme_presets():
    """从同级 JSON 文件加载主题映射。"""
    try:
        theme_path = os.path.join(os.path.dirname(__file__), "theme_presets.json")
        with open(theme_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


THEME_PRESETS = load_theme_presets()


def resolve_theme(theme_arg):
    """优先按主题名称映射；未知主题时不传 theme 字段。"""
    if not theme_arg:
        return None

    normalized = theme_arg.strip()
    if not normalized:
        return None

    if normalized in THEME_PRESETS:
        return THEME_PRESETS[normalized]

    # 向后兼容：如果调用方仍传入 JSON 字符串，则继续支持
    try:
        parsed = json.loads(normalized)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    return None


def read_markdown_file(file_path):
    candidate_path = os.path.abspath(os.path.expanduser(file_path))
    with open(candidate_path, "r", encoding="utf-8") as f:
        return f.read(), candidate_path


def resolve_markdown_input(markdown_arg=None, markdown_file_arg=None):
    """兼容 stdin、显式文件输入，以及误传本地文件路径的场景。"""
    if markdown_file_arg:
        return read_markdown_file(markdown_file_arg)

    if markdown_arg == '-':
        return sys.stdin.read(), None

    candidate_path = os.path.expanduser(markdown_arg)
    if os.path.isfile(candidate_path):
        try:
            return read_markdown_file(candidate_path)
        except Exception:
            pass

    return markdown_arg, None


def cleanup_markdown_file(file_path):
    if not file_path:
        return None
    try:
        os.remove(file_path)
        return None
    except FileNotFoundError:
        return None
    except Exception as exc:
        return str(exc)


def is_agents_cache_path(file_path):
    parts = os.path.normpath(file_path).split(os.sep)
    for index in range(len(parts) - 1):
        if parts[index] == ".agents" and parts[index + 1] == "cache":
            return True
    return False


def is_system_temp_path(file_path):
    temp_dir = os.path.abspath(tempfile.gettempdir())
    candidate_path = os.path.abspath(file_path)
    try:
        return os.path.commonpath([candidate_path, temp_dir]) == temp_dir
    except ValueError:
        return False


def should_auto_cleanup_markdown_file(file_path):
    if not file_path:
        return False
    return is_agents_cache_path(file_path) or is_system_temp_path(file_path)


def enrich_result_with_link_artifacts(result, title):
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return result

    img_url = data.get("imgUrl", "")
    visit_url = data.get("visitUrl", "")
    if not isinstance(img_url, str):
        img_url = ""
    if not isinstance(visit_url, str):
        visit_url = ""

    if not img_url and not visit_url:
        return result

    copy_block = "\n".join([
        "图片原始链接（完整复制整行）:",
        img_url,
        "",
        "编辑查看原始链接（完整复制整行）:",
        visit_url,
    ])
    data["rawImgUrl"] = img_url
    data["rawVisitUrl"] = visit_url
    data["copyBlock"] = copy_block
    return result

def main():
    CURRENT_VERSION = get_local_version()
    partner_flag = get_or_create_partner_flag()
    
    parser = argparse.ArgumentParser(description='ProcessOn Mindmap API Client')
    parser.add_argument('--version', action='version', version=f'%(prog)s {CURRENT_VERSION}')
    parser.add_argument('--title', required=True, help='Mindmap title')
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--markdown', help='Markdown content or "-" to read from stdin')
    input_group.add_argument('--markdown-file', help='Path to a local Markdown file')
    parser.add_argument('--cleanup-markdown-file', action='store_true', help='Force-delete the file passed via --markdown-file after processing')
    parser.add_argument('--theme', help='Theme name or theme JSON string')
    parser.add_argument('--structure', help='Structure type')
    
    args = parser.parse_args()

    if args.cleanup_markdown_file and not args.markdown_file:
        parser.error('--cleanup-markdown-file must be used together with --markdown-file')

    markdown_content, resolved_markdown_file = resolve_markdown_input(
        markdown_arg=args.markdown,
        markdown_file_arg=args.markdown_file,
    )
    cleanup_target = None
    if resolved_markdown_file and (args.cleanup_markdown_file or should_auto_cleanup_markdown_file(resolved_markdown_file)):
        cleanup_target = resolved_markdown_file

    # 结构合法性校验与默认值回退
    structure = args.structure
    if structure not in ALLOWED_STRUCTURES:
        structure = "mind_free"

    headers = {'Content-Type': 'application/json'}
    payload = {
        "title": args.title, 
        "markdown": markdown_content,
        "structure": structure,
        "source": "skill_all_mind_official"
    }
    
    # 处理主题名称或主题 JSON；未知主题时不传 theme 字段
    theme_config = resolve_theme(args.theme)
    if theme_config is not None:
        payload["theme"] = theme_config
    if partner_flag:
        payload["partnerFlag"] = partner_flag

    output_payload = None
    exit_code = 0
    cleanup_warning = None

    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(TRANSFORM_MD_API_URL, data=data, headers=headers, method='POST')
        
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode('utf-8'))
            result = enrich_result_with_link_artifacts(result, args.title)
            # 仅注入版本信息供 AI 参考，不再进行云端比对
            result["_client_version"] = CURRENT_VERSION
            output_payload = result

    except Exception as e:
        output_payload = {"success": False, "error": str(e), "_client_version": CURRENT_VERSION}
        exit_code = 1
    finally:
        cleanup_warning = cleanup_markdown_file(cleanup_target)

    if isinstance(output_payload, dict) and cleanup_warning:
        output_payload["_cleanup_warning"] = cleanup_warning

    print(json.dumps(output_payload, ensure_ascii=False))
    if exit_code:
        sys.exit(exit_code)

if __name__ == "__main__":
    main()
