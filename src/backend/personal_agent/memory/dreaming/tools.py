"""The only tools a Dreaming agent has: read, write, edit, glob and grep on memory/.

No delete, no network, no shell. The source of an analyzer is a read-only virtual file
(sources/<n>.txt) held in memory, never written into the workspace. Every write goes
through memory.store with the agent's writer and, for an analyzer, the source's id; each
call renews the run's lease first, so a lost lease stops the run at the next tool call.
"""
import fnmatch
import json
import re

from core.infra.exceptions import AppException
from personal_agent.memory import layout, store
from personal_agent.memory.dreaming import runs
from personal_agent.workspace import files

READ_LINES = 2000
GREP_MAX = 200
# A page written back to text it already held in this step is the model undoing itself; after
# this many returns the page is frozen for the step, or it flips until the step cap rolls back
# the whole run (seen live: one paragraph added and removed 30 times).
RETURNS_MAX = 1


def _schema(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


SCHEMAS = [
    _schema("read", "读一个文件，返回带行号的内容。path 是 memory/<文件>.md 或任务消息给出的来源路径。长文件用 offset/limit 分段读。",
            {"path": {"type": "string"}, "offset": {"type": "integer", "description": "从第几行开始，1 起"},
             "limit": {"type": "integer", "description": "最多读多少行，默认 2000"}}, ["path"]),
    _schema("write", "新建或整页替换 memory/ 下的一个文件。已有页面请优先用 edit。",
            {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _schema("edit", "把文件里恰好出现一次的 old_string 换成 new_string。",
            {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}},
            ["path", "old_string", "new_string"]),
    _schema("glob", "按通配符列出 memory/ 下的文件，例如 memory/person-*.md。",
            {"pattern": {"type": "string"}}, ["pattern"]),
    _schema("grep", "在 memory/ 的文件里按正则搜索，返回 路径:行号: 内容。",
            {"pattern": {"type": "string"}, "glob": {"type": "string", "description": "限定文件，默认 memory/*.md"},
             "ignore_case": {"type": "boolean"}}, ["pattern"]),
]


class ToolError(Exception):
    """A refused call, reported to the model as an error result."""


PATTERN_MAX = 200
# A group holding a repeat that is itself repeated, like (a+)+: the shape behind catastrophic
# backtracking. The wiki is small, but the model's regex runs in a thread nobody can stop.
NESTED_REPEAT = re.compile(r"\([^()]*[*+}][^()]*\)[*+{]")


def _pattern(pattern, ignore_case):
    pattern = str(pattern or "")
    if len(pattern) > PATTERN_MAX: raise ToolError("pattern 太长（最多 " + str(PATTERN_MAX) + " 个字符）")
    flags = re.I if ignore_case else 0
    if NESTED_REPEAT.search(pattern): return re.compile(re.escape(pattern), flags)
    try: return re.compile(pattern, flags)
    except re.error as exc: raise ToolError("pattern 不是合法的正则：" + str(exc)) from None


class Toolbox:
    def __init__(self, sessions, ident, run_id, owner, writer, *, source_id=None, sources=None):
        self.sessions, self.ident, self.run_id, self.owner = sessions, ident, run_id, owner
        self.writer, self.source_id = writer, source_id
        self.sources = dict(sources or {})
        self.written = []
        self._seen, self._returns = {}, {}

    def schemas(self):
        return SCHEMAS

    def call(self, name, raw):
        """(output, ok). Lease loss propagates; everything else becomes an error result."""
        try:
            args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
            if not isinstance(args, dict): raise ToolError("参数必须是 JSON 对象")
            handler = {"read": self.read, "write": self.write, "edit": self.edit, "glob": self.glob, "grep": self.grep}.get(name)
            if handler is None: raise ToolError("没有这个工具：" + str(name))
            return handler(**args), True
        except runs.LeaseLost:
            raise
        except TypeError as exc:
            return "错误：参数不对（" + str(exc) + "）", False
        except store.MemoryLintError as exc:
            return "错误：" + exc.message + "\n" + json.dumps([f.as_dict() for f in exc.findings], ensure_ascii=False), False
        except AppException as exc:
            return "错误：" + str(exc.message), False
        except (ToolError, PermissionError, ValueError, LookupError, OSError, re.error) as exc:
            return "错误：" + str(exc), False

    # ------------------------------------------------------------------ paths

    def _memory_path(self, path):
        rel = files.relative(path)
        if rel.split("/")[0] != layout.DIR or "/" in rel[len(layout.DIR) + 1:]:
            raise ToolError("只能访问 memory/ 下的文件")
        return rel

    def _wiki(self, pattern="memory/*.md"):
        pattern = pattern.lstrip("/")
        if pattern.startswith("workspace/"): pattern = pattern[len("workspace/"):]
        return {rel: text for rel, text in store.wiki(self.ident).items() if fnmatch.fnmatch(rel, pattern)}

    # ------------------------------------------------------------------ tools

    def read(self, path, offset=1, limit=READ_LINES):
        key = path.lstrip("/")
        if key in self.sources: text = self.sources[key]
        else:
            text = store.read(self.ident, self._memory_path(path))
            if text is None: raise ToolError("文件不存在：" + path)
        lines = text.splitlines()
        start = max(1, int(offset or 1))
        count = max(1, min(int(limit or READ_LINES), READ_LINES))
        chunk = lines[start - 1:start - 1 + count]
        out = "\n".join(str(start + i).rjust(6) + "\t" + line for i, line in enumerate(chunk))
        if start - 1 + count < len(lines):
            out += "\n[共 " + str(len(lines)) + " 行；用 offset=" + str(start + count) + " 继续读]"
        return out or "[空文件]"

    def write(self, path, content):
        rel = self._memory_path(path)
        if path.lstrip("/") in self.sources: raise ToolError("来源文件是只读的")
        before = self._guard(rel, content)
        self._store(lambda db, agent: store.write(db, agent, rel, content, None, writer=self.writer,
                                                  source_id=self.source_id, run_id=self.run_id))
        self._seen.setdefault(rel, set()).update({before, content})
        self.written.append(rel)
        return "已写入 " + rel

    def edit(self, path, old_string, new_string):
        rel = self._memory_path(path)
        current = store.read(self.ident, rel) or ""
        after = current.replace(old_string, new_string, 1) if old_string and current.count(old_string) == 1 else None
        if after is not None: self._guard(rel, after)
        self._store(lambda db, agent: store.edit(db, agent, rel, old_string, new_string, writer=self.writer,
                                                 source_id=self.source_id, run_id=self.run_id))
        self._seen.setdefault(rel, set()).update({current, after})
        self.written.append(rel)
        return "已修改 " + rel

    def _guard(self, rel, content):
        """Refuse a page's second return to text this step already moved it away from; returns
        the page's current text."""
        current = store.read(self.ident, rel)
        if content != current and content in self._seen.get(rel, ()):
            if self._returns.get(rel, 0) >= RETURNS_MAX:
                raise ToolError(rel + " 在这一步里已经被来回改过：这次写入会把它又改回之前的样子。"
                                "保持页面现在的内容，不要再改这个页面，继续处理别的页面或直接回复 JSON")
            self._returns[rel] = self._returns.get(rel, 0) + 1
        return current

    def glob(self, pattern="memory/*.md"):
        found = sorted(self._wiki(pattern or "memory/*.md"))
        return "\n".join(found) or "[没有匹配的文件]"

    def grep(self, pattern, glob="memory/*.md", ignore_case=False):
        regex = _pattern(pattern, ignore_case)
        out = []
        for rel, text in self._wiki(glob or "memory/*.md").items():
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    out.append(rel + ":" + str(number) + ": " + line[:300])
                    if len(out) >= GREP_MAX: return "\n".join(out) + "\n[结果过多，已截断]"
        return "\n".join(out) or "[没有匹配]"

    def _store(self, change):
        with self.sessions() as db:
            agent, _row, _data = runs.owned(db, self.ident, self.run_id, self.owner)
            change(db, agent)
            db.commit()
