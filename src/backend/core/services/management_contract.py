"""Versioned plugin-owned management contract; presentation varies by execution plane."""
import copy

META_KEY = "org.hugagent/executor"
VERSION = 1
MANAGERS = {"skill-manager": "skill", "plugin-manager": "plugin"}


def metadata(manager):
    return {META_KEY: {"id": manager, "version": VERSION}}


def declared(raw, source_plugin):
    contract = (raw.get("_meta") or raw.get("meta") or {}).get(META_KEY)
    if contract is None:
        return None
    if not isinstance(contract, dict) or contract.get("id") not in MANAGERS:
        raise ValueError("unsupported plugin executor; upgrade required")
    if contract.get("version") != VERSION or source_plugin != contract["id"]:
        raise ValueError("plugin executor contract mismatch; upgrade required")
    return contract["id"]


def tools(manager, plane):
    kind = MANAGERS[manager]
    label = "技能" if kind == "skill" else "插件"
    local = plane == "local"
    source = ({"kind": {"const": "local_path"}, "path": {"type": "string", "description": "本机绝对目录或 ZIP/TGZ 路径"}}
              if local else {"kind": {"const": "artifact"}, "artifact_id": {"type": "string", "description": "当前云端账号拥有的技能或插件包产物 ID"}})
    source_schema = {"type": "object", "properties": source, "required": list(source), "additionalProperties": False}
    identity = {"install_id": {"type": "string", "description": "list/get 返回的精确安装 ID；不能传名称"}}
    revision = {"expected_revision": {"type": "string", "description": "get 返回的当前 revision，用于冲突检查"}}
    operations = [
        ("install_" + kind, {"source": source_schema}, ["source"],
         f"安装{label}到当前用户的{'本机能力目录' if local else '云端私有库'}。接收{'本机目录或压缩包，不需要 artifact_id，不上传云端' if local else '当前云端包产物 ID，不接收本机路径'}。同名内容不同时返回冲突，请显式更新。成功后返回安装 ID、版本及可用状态。"),
        ("list_" + kind + "s", {}, [], f"列出当前用户{'本机' if local else '云端'}已安装的{label}，返回精确安装 ID 和 revision。"),
        ("get_" + kind, identity, ["install_id"], f"读取一个已安装{label}的版本、内容位置与组件信息。"),
        ("update_" + kind, {**identity, **revision, "source": source_schema}, ["install_id", "expected_revision", "source"],
         f"用完整包更新已有{label}，先读取当前 revision。版本变化则拒绝覆盖。插件及子组件一起更新；插件内技能必须通过插件管理。"),
        ("uninstall_" + kind, {**identity, **revision}, ["install_id", "expected_revision"],
         f"撤销当前用户的{label}安装登记。插件及子组件一起卸载，保留导入源文件和历史版本，不删除云端副本。"),
    ]
    if local and kind == "skill":
        operations.append(("upload_skill_to_cloud", {**identity, **revision, "expected_cloud_revision": {"type": ["string", "null"]}},
                           ["install_id", "expected_revision"],
                           "把本机独立技能上传并注册为当前账号的云端私有技能，保留本机安装。需要当前云端登录；不申请市场发布。首次上传不覆盖已有云端技能；再次上传需提供已关联云端版本 expected_cloud_revision。"))
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": copy.deepcopy(props), "required": required, "additionalProperties": False},
             "annotations": {"readOnlyHint": name.startswith(("list_", "get_"))}, "_meta": metadata(manager)}
            for name, props, required, desc in operations]


def localize(raw, source_plugin, *, cloud_available):
    manager = declared(raw, source_plugin)
    if manager is None:
        return raw
    candidates = {x["name"]: x for x in tools(manager, "local")}
    tool = candidates.get(raw["name"])
    if tool is None:
        raise ValueError("unsupported management operation; upgrade required")
    if raw["name"] == "upload_skill_to_cloud" and not cloud_available:
        return None
    return tool
