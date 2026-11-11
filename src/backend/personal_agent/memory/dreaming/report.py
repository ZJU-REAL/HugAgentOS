"""DREAMS.md: one report per run, appended once (a marker per run makes a retry idempotent)."""
import os
import re
import unicodedata

from personal_agent.memory import layout, lifecycle, store
from personal_agent.memory.redact import redact
from personal_agent.workspace import files

REPORT_MAX_BYTES = 16 * 1024 * 1024
REMEMBER = re.compile(r"(?im)^\s*(?:请)?(?:记住|remember)\s*[:：]\s*(.+?)\s*$")


def _plain(text):
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def remember_suggestions(ident, sources):
    """Owner lines "记住：…" in conv-/chan- sources that USER.md and MEMORY.md do not hold."""
    held = _plain((store.read(ident, layout.USER) or "") + "\n" + (store.read(ident, layout.MEMORY) or ""))
    out = []
    for source in sources:
        if not source["sid"].startswith(("conv-", "chan-")): continue
        for sid, words in source.get("owner_words") or []:
            for match in REMEMBER.finditer(words or ""):
                quote = redact(match[1])[:500]
                if quote and _plain(quote) not in held and (quote, sid) not in out: out.append((quote, sid))
    return out


def body(run_id, at, data):
    """The report text; data comes from finish.finish."""
    lines = ["## " + at + " · " + run_id, ""]
    if data.get("genesis"): lines += ["创世回放：" + ("已完成" if data.get("genesis_done") else "进行中，下一批稍后继续"), ""]
    lines += ["### 来源（" + str(len(data["results"])) + "）"]
    for item in data["results"]:
        if item["ok"]: lines.append("- " + item["sid"] + "：" + (item.get("summary") or "已处理"))
    failures = [item for item in data["results"] if not item["ok"]]
    if failures or data.get("skipped"):
        lines += ["", "### 失败与跳过"]
        lines += ["- " + item["sid"] + "：" + str(item.get("error") or "未知错误") + "（留到下次）" for item in failures]
        lines += ["- " + item["message_id"] + "：" + item["reason"] for item in data.get("skipped", [])]
    pages = data["pages"]
    lines += ["", "### 页面",
              "- 新建：" + ("、".join(pages["created"]) or "无"),
              "- 修改：" + ("、".join(pages["changed"]) or "无"),
              "- 合并：" + ("、".join(pages["merged"]) or "无")]
    life = data.get("lifecycle") or {}
    if any(life.values()):
        lines += ["", "### 按使用情况",
                  "- 归档（" + str(lifecycle.ARCHIVE_IDLE_DAYS) + " 天没用过、也没有新来源）：" + ("、".join(life.get("archived", ())) or "无"),
                  "- 恢复（归档后又用到）：" + ("、".join(life.get("restored", ())) or "无"),
                  "- 删除（归档后又 " + str(lifecycle.DELETE_IDLE_DAYS) + " 天没用过；可在记忆面板撤销）：" + ("、".join(life.get("deleted", ())) or "无")]
    online = data["online"]
    lines += ["", "### 白天的改动", "- 保留 " + str(online["kept"]) + "，修正 " + str(online["revised"]) + "，撤回 " + str(online["reverted"])]
    if data["conflicts"]:
        lines += ["", "### 新的 CONFLICT"] + ["- " + rel + "：" + line for rel, line in data["conflicts"]]
    if data["warnings"]:
        lines += ["", "### lint 告警"] + ["- " + f.path + "：" + f.detail for f in data["warnings"]]
    if data.get("organizer"):
        lines += ["", "### 整理"] + (["- " + change for change in data["organizer"]["changes"]] or ["- 无"])
    if data.get("orphans"):
        lines += ["", "- journal 里有 " + str(data["orphans"]) + " 条记录在数据库里已找不到对应消息，没有回放"]
    if data["suggestions"]:
        lines += ["", "### 建议你确认后写进 USER.md 或 MEMORY.md 置顶", "Dreaming 不会自己写这两个文件；需要的话请在编辑器里加上："]
        lines += ["- “" + quote + "”（" + sid + "）" for quote, sid in data["suggestions"]]
    return "\n".join(lines).replace("<!--", "&lt;!--") + "\n"


def append(ident, marker_id, text):
    """Append once per marker_id; the caller holds the workspace lock."""
    try:
        raw, info = files._read(files.local_workspace(ident), layout.REPORT, REPORT_MAX_BYTES)
    except FileNotFoundError:
        raw, info = b"", None
    if info is not None and info.st_size > REPORT_MAX_BYTES: raise ValueError("Dreaming report full; archive DREAMS.md before retry")
    previous = raw.decode("utf-8", errors="replace")
    marker = "<!-- pa-dream:" + marker_id + " -->"
    if marker in previous: return False
    data = ((previous or "# Dreaming\n") + "\n" + marker + "\n" + text).encode()
    if len(data) > REPORT_MAX_BYTES: raise ValueError("Dreaming report full; archive DREAMS.md before retry")
    fd = files._open_dir(files.local_workspace(ident), [])
    temp = ".DREAMS." + os.urandom(6).hex() + ".tmp"
    try:
        target = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(target, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        files._own(temp, fd)
        os.replace(temp, layout.REPORT, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try: os.unlink(temp, dir_fd=fd)
        except FileNotFoundError: pass
        os.close(fd)
    return True
