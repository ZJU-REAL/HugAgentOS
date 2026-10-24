"""Browser -> HTTP -> authorized original file -> native launcher boundary."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import sys

import pytest

from fastapi import FastAPI, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import uvicorn

from api.routes.files import router
from core.auth.backend import UserContext, get_current_user
from core.config import settings as settings_module
from core.db.engine import Base, get_db
from core.db.models import Artifact
from tests.db_support import share_factory


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("node"),
                    reason="Browser/native adapter fixture requires Linux and Node.js")
def test_file_open_browser_http_end_to_end(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[4]
    frontend = root / "src/frontend"
    if not (frontend / "node_modules/playwright").exists():
        pytest.skip("Install frontend browser-test dependencies before running this E2E test")
    node = shutil.which("node")
    assert node
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "report.md").write_text("# Original file\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    entries = tmp_path / "share/applications"
    entries.mkdir(parents=True)
    log = tmp_path / "launched.jsonl"
    for index in range(60):
        (entries / f"viewer-{index}.desktop").write_text(
            f"[Desktop Entry]\nName=Editor {index}\nExec=viewer %f\n"
        )
    # Deterministic OS adapter: real child processes, no desktop session required.
    launcher = f"""#!{sys.executable}
import json, sys
from pathlib import Path
name = Path(sys.argv[0]).name
if name == 'xdg-mime':
    print('text/plain')
elif name == 'gio' and sys.argv[1] == 'mime':
    print('Default application: viewer-0.desktop')
    print('\\n'.join('viewer-' + str(i) + '.desktop' for i in range(60)))
else:
    with open({str(log)!r}, 'a') as stream:
        stream.write(json.dumps([name] + sys.argv[1:]) + '\\n')
"""
    for name in ("xdg-mime", "gio", "xdg-open"):
        command = bin_dir / name
        command.write_text(launcher)
        command.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "share"))
    monkeypatch.setenv("STORAGE_PATH", str(storage))
    settings = settings_module.settings
    monkeypatch.setattr(settings_module, "settings", replace(settings, deploy=replace(settings.deploy, profile="local")))
    # Use the real local storage implementation, isolated from the singleton.
    from core.files.storage.local import LocalStorageBackend
    monkeypatch.setattr("api.routes.files.get_storage", LocalStorageBackend)

    engine = create_engine(f"sqlite:///{tmp_path / 'fixture.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        for identifier, owner, key in (("report", "owner", "report.md"), ("private", "other", "report.md"),
                                       ("missing", "owner", "gone.md"), ("escape", "owner", "../outside.md")):
            db.add(Artifact(artifact_id=identifier, user_id=owner, type="document", title=identifier,
                            filename=identifier + ".md", size_bytes=16, mime_type="text/markdown", storage_key=key))
        db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def fixture_user(request: Request):
        if request.headers.get("x-fixture-user") == "anonymous":
            from fastapi import HTTPException
            raise HTTPException(401)
        return UserContext(user_id=request.headers.get("x-fixture-user", "owner"),
                           user_center_id="fixture", username="Fixture")

    def fixture_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_current_user] = fixture_user
    app.dependency_overrides[get_db] = fixture_db
    share_factory(monkeypatch, sessions)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        completed = subprocess.run(
            [node, "scripts/test-file-open-browser.mjs", "http"], cwd=frontend,
            env={**os.environ, "FILE_OPEN_E2E_BASE": f"http://127.0.0.1:{port}"},
            capture_output=True, text=True, timeout=90,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        print(completed.stdout)
        actions = [json.loads(line) for line in log.read_text().splitlines()]
        assert ["xdg-open", str(storage / "report.md")] in actions
        assert ["xdg-open", str(storage)] in actions
        assert ["gio", "launch", str(entries / "viewer-59.desktop"), str(storage / "report.md")] in actions
        assert len(actions) == 3, "Invalid, unauthorized and missing files must not launch anything"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        engine.dispose()
