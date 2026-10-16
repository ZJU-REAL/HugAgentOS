"""Prepare and audit WB-24 evidence. Native behavior requires actual Windows runs."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from workbench_windows_source import source_snapshot

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "17051f568a47e4ac27d4ffb7380a5769ac630014"
SCENARIOS = (
    ("install", "新装完整包；初始化本机/云端/双端；记录健康/就绪与运行时指纹。"),
    ("binding", "同名不同来源安装；打开精确安装；错误安装/旧revision不得回退。"),
    ("navigation", "两种侧栏固定/排序/更多/二级页；刷新/深链/前后导航；100入口未打开无实例。"),
    ("file", "可信宿主选择/保存中文空格文件；取消无副作用；撤销后旧句柄不可读。"),
    ("external", "HTTPS外链系统浏览器打开且宿主保留；危险协议拒绝；嵌入失败替代入口。"),
    ("native-isolation", "第三方尝试Tauri invoke/父窗口/原生哨兵；拒绝且无系统副作用；自报gesture无授权。"),
    ("identity", "账号A打开插件及数据；退出/切B；旧端口/响应/票据失效且B不见A数据。"),
    ("offline", "只阻断测试环境云端网络；本机已装页面可用；云端明确不可用且不静默回退。"),
    ("release", "关页/禁用/卸载/登出回收端口资源及worker；其他应用和用户进程继续。"),
    ("failure", "握手超时/缺图/插件崩溃/断网只影响本应用；恢复重试无重复副作用。"),
    ("upgrade", "旧UI v1/v2兼容；v3升级新revision；新增grant需重新审批；旧会话拒绝。"),
    ("rollback", "隔离数据副本升级失败；恢复源码/运行时/平台及applications数据库/WAL/SHM。"),
    ("shell", "多窗口/标题栏/缩放/托盘/quickask/通知/更新进度/系统凭据库原生回归。"),
    ("regression", "执行WB-23完整既有功能矩阵的本模式适用行；关联逐条报告，不省略失败或缺项。"),
)
STATUSES = {"pass", "fail", "blocked", "not-run"}
IPC_COMMANDS = ("logout_desktop", "open_login", "plugin:window|set_title",
                "plugin:window|close", "plugin:event|emit", "plugin:dialog|open")
IPC_CONTEXTS = ("srcdoc", "external-iframe")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def prepare(output):
    output = output.resolve()
    scratch = (ROOT / ".scratch" / "workbench-24").resolve()
    if not output.is_relative_to(scratch) or output == scratch:
        raise ValueError("Output must be a new batch beneath .scratch/workbench-24")
    # No overwrite, cleanup, build, install, service control or credential reads.
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text())
    package = json.loads((ROOT / "desktop/package.json").read_text())
    if package["version"] != config["version"]:
        raise ValueError("Desktop version mismatch")
    snapshot = source_snapshot(ROOT)
    manifest = {
        "schemaVersion": 1, "issue": 233, "platform": "windows-x86_64",
        "protocol": "1.0.0", "baseline": BASELINE,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
        "version": config["version"], "identifier": config["identifier"],
        **snapshot, "sourceSha256": snapshot["sourceTreeHash"],
        "createdUtc": datetime.now(timezone.utc).isoformat(),
        "acceptance": "not-run",
    }
    write(output / "source.json", manifest)
    cases = [{"id": f"WB24-{mode}-{name}", "mode": mode, "acceptance": "A13",
              "level": "native", "steps": steps, "expected": steps, "actual": "",
              "status": "not-run", "evidence": [], "issue": ""}
             for mode in ("local", "cloud", "dual") for name, steps in SCENARIOS]
    for row in cases:
        if row["id"].endswith("-native-isolation"):
            row["ipcChecks"] = []
            row["sessionSentinels"] = {
                "sessionUnchanged": None, "windowUnchanged": None, "evidence": []}
            row["expected"] += " 包内srcdoc及外部iframe实际IPC拒绝；会话与宿主窗口无变化。"
    write(output / "results.json", {"schemaVersion": 1,
          "sourceReceiptSha256": digest(output / "source.json"), "cases": cases})
    print(f"Prepared {len(cases)} pending native cases: {output}")


def verified_file(batch, reference):
    relative = Path(reference["path"])
    candidate = batch / relative
    if any(p.is_symlink() for p in (candidate, *candidate.parents) if p != batch):
        raise ValueError("Evidence symlinks are not permitted")
    path = candidate.resolve()
    if relative.is_absolute() or not path.is_relative_to(batch) or path.is_symlink():
        raise ValueError("Evidence path escapes batch")
    if not path.is_file() or digest(path) != reference["sha256"]:
        raise ValueError(f"Evidence hash mismatch: {relative}")
    return path


def check(batch):
    batch = batch.resolve()
    result = json.loads((batch / "results.json").read_text(encoding="utf-8-sig"))
    source = json.loads((batch / "source.json").read_text(encoding="utf-8-sig"))
    if source.get("platform") != "windows-x86_64" or source.get("protocol") != "1.0.0":
        raise ValueError("Wrong native source platform/protocol")
    if result["sourceReceiptSha256"] != digest(batch / "source.json"):
        raise ValueError("Source receipt changed")
    expected = {f"WB24-{mode}-{name}" for mode in ("local", "cloud", "dual")
                for name, _ in SCENARIOS}
    rows = result["cases"]
    if len(rows) != len(expected) or {r["id"] for r in rows} != expected:
        raise ValueError("Incomplete or duplicate case matrix")
    if any(r.get("mode") not in {"local", "cloud", "dual"}
           or not r["id"].startswith(f"WB24-{r['mode']}-")
           or r.get("acceptance") != "A13" for r in rows):
        raise ValueError("Case mode/acceptance mismatch")
    if any(r.get("status") not in STATUSES for r in rows):
        raise ValueError("Invalid case status")
    pending = [r["id"] for r in rows if r["status"] != "pass"]
    if pending:
        raise ValueError("Native acceptance incomplete: " + ", ".join(pending))
    required_ipc = {(context, command) for context in IPC_CONTEXTS for command in IPC_COMMANDS}
    for row in rows:
        if not row["id"].endswith("-native-isolation"):
            continue
        checks = row.get("ipcChecks", [])
        pairs = {(c.get("context"), c.get("command")) for c in checks}
        if (len(checks) != len(required_ipc) or pairs != required_ipc
                or any(c.get("status") != "denied" or not c.get("errorCode")
                       or not c.get("evidence") for c in checks)):
            raise ValueError(f"Missing actual IPC denial records: {row['id']}")
        for item in checks:
            for reference in item["evidence"]:
                verified_file(batch, reference)
        sentinel = row.get("sessionSentinels", {})
        if (sentinel.get("sessionUnchanged") is not True
                or sentinel.get("windowUnchanged") is not True or not sentinel.get("evidence")):
            raise ValueError(f"Missing native session/window sentinels: {row['id']}")
        for reference in sentinel["evidence"]:
            verified_file(batch, reference)
    artifact = json.loads((batch / "artifact.json").read_text(encoding="utf-8-sig"))
    if artifact["sourceReceiptSha256"] != digest(batch / "source.json"):
        raise ValueError("Artifact is not bound to this source receipt")
    if not artifact.get("isolatedTestEnvironment") or artifact.get("level") != "native":
        raise ValueError("Missing isolated native installation record")
    if (not re.fullmatch(r"[0-9a-f]{64}", artifact.get("installerSha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", artifact.get("applicationSha256", ""))
            or artifact.get("installerBytes", 0) <= 0
            or not artifact.get("applicationVersion", "").startswith(source["version"])):
        raise ValueError("Incomplete native package receipt")
    for mode in ("local", "cloud", "dual"):
        observation = json.loads((batch / f"native-{mode}.json").read_text(encoding="utf-8-sig"))
        webviews = observation.get("webview2")
        if (not isinstance(webviews, list) or not webviews
                or not isinstance(observation.get("processId"), int)
                or observation["processId"] <= 0
                or any(not isinstance(v, dict) or not v.get("version")
                       or not isinstance(v.get("processId"), int)
                       or v["processId"] <= 0 for v in webviews)):
            raise ValueError(f"Missing actual WebView2 process details: {mode}")
        if (observation.get("mode") != mode or observation.get("level") != "native"
                or observation.get("applicationSha256") != artifact["applicationSha256"]
                or observation.get("sourceReceiptSha256") != digest(batch / "source.json")
                or not observation.get("webview2")):
            raise ValueError(f"Missing actual Tauri/WebView2 observation: {mode}")
    for row in rows:
        if (row["level"] != "native" or not row.get("actual", "").strip()
                or not row.get("evidence")):
            raise ValueError(f"Missing native behavior evidence: {row['id']}")
        for reference in row["evidence"]:
            verified_file(batch, reference)
    print(json.dumps({"status": "evidence-complete", "cases": len(rows),
                     "version": source["version"], "sourceSha256": source["sourceSha256"],
                     "note": "Evidence consistency only; human native review remains required."}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare").add_argument("--output", type=Path, required=True)
    commands.add_parser("check").add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.output) if args.command == "prepare" else check(args.batch)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
