"""Build the WB-23 matrix from the settled specification, without omitting rows."""
from itertools import product
from pathlib import Path
import re

BASELINE = "17051f568a47e4ac27d4ffb7380a5769ac630014"


def rows(path):
    for line in path.read_text().splitlines():
        if line.startswith("|") and not re.match(r"^\|[- :|]+$", line):
            yield [cell.strip() for cell in line.strip("|").split("|")]


def required_cases(root):
    cases = []
    for cells in rows(root / "internal design docs"):
        if re.fullmatch(r"A\d\d", cells[0]) and cells[0] != "A13":
            cases.append(dict(id=cells[0], expected=cells[1], level="real-browser"))
    for index, cells in enumerate(rows(root / "internal design docs")):
        if cells[0] == "范围":
            continue
        for behavior_index, behavior in enumerate(cells[1].split("、"), start=1):
            cases.append(dict(id=f"R{index:02}-{behavior_index:02}", feature=cells[0],
                              expected=behavior, level="native" if cells[0] == "多端与版本" else "real-browser"))
    for layout, count, height, theme, language in product(
            ("modern", "classic"), (5, 20, 100), (480, 720, 1080),
            ("light", "dark"), ("zh-CN", "en")):
        cases.append(dict(id=f"UI-{layout}-{count}-{height}-{theme}-{language}",
                          expected="搜索、固定/取消固定、鼠标及键盘排序、更多菜单、当前选中、无未打开实例",
                          level="real-browser", layout=layout, applications=count,
                          height=height, theme=theme, language=language))
    for interaction in ("Tab", "Arrow", "Enter", "Escape", "accessible-name",
                        "focus-return", "screen-reader", "theme-contrast"):
        for layout in ("modern", "classic"):
            cases.append(dict(id=f"AX-{layout}-{interaction}", level="real-browser",
                              expected=interaction, layout=layout))
    for failure in ("catalog-unavailable", "handshake-timeout", "crash", "missing-icon",
                    "offline", "reconnect", "revocation", "logout", "uninstall"):
        cases.append(dict(id=f"FAULT-{failure}", level="real-browser",
                          expected="故障局限到对应应用；恢复入口有效；连接/资源及时释放"))
    cases.append(dict(id="PERF-100", level="real-browser",
                      expected="100应用零未打开实例；至少100次固定/排序/更多操作；P95≤100ms；设备/浏览器/构建记录"))
    for ident, level, expected in (
            ("REVIEW-standards", "static", "工程规范双轴审查无阻塞"),
            ("REVIEW-spec", "static", "父规格双轴审查无阻塞"),
            ("CHECK-lint", "static", "全量lint零错误；存量259/23与260/24实际复核"),
            ("CHECK-build", "static", "TypeScript与production build通过"),
            ("DB-sqlite", "real-http", "隔离SQLite数据升级、授权及恢复通过"),
            ("DB-postgresql", "real-http", "隔离真实PostgreSQL数据升级、授权及恢复通过")):
        cases.append(dict(id=ident, level=level, expected=expected))
    return [{**case, "status": "not-run", "actual": "", "evidence": [],
             "steps": [], "issue": "#232"} for case in cases]


def inventory(root):
    import json
    scripts = json.loads((root / "src/frontend/package.json").read_text())["scripts"]
    frontend = []
    for name, command in scripts.items():
        if name.startswith("test:") or name in ("lint", "build"):
            paths = re.findall(r"scripts/[\w./-]+\.(?:mjs|tsx|ts|py)", command)
            sources = "\n".join((root / "src/frontend" / p).read_text()
                                for p in paths if (root / "src/frontend" / p).exists())
            has_fixture = bool(re.search(r"route\.fulfill|route\.fetch|__preferences|fixture", sources))
            has_http = bool(re.search(r"uvicorn|fetch\(|route\.continue", sources))
            frontend.append(dict(name=name, argv=["npm", "run", name],
                                 cwd="src/frontend", sourcePaths=paths,
                                 fixtureDetected=has_fixture, httpDetected=has_http,
                                 classification="needs-source-review",
                                 status="not-run"))
    backend = [str(path.relative_to(root)) for path in
               sorted((root / "src/backend/tests").rglob("test_*.py"))]
    return dict(frontend=frontend, backend=backend,
                note="Inventory is not an execution allowlist. Review DB/storage/ports, fixture and external targets before adding a command to a batch.")
