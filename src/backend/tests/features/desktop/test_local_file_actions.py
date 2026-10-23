from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from features.desktop.api.local_file_actions import FileOpenRequest, applications, open_local_file


def test_native_actions_use_authorized_file_identity(monkeypatch):
    calls = []
    monkeypatch.setattr("api.routes.files.local_file_location",
                        lambda fid, user, db: {"path": "/authorized/report.txt"})
    monkeypatch.setattr('features.desktop.local.file_actions.file_applications',
                        lambda path, **kwargs: {"applications": [{"id": "editor", "name": "Editor", "icon": None}],
                                      "default_id": "editor"})
    monkeypatch.setattr('features.desktop.local.file_actions.open_file',
                        lambda path, **kwargs: calls.append((path, kwargs)))
    user = SimpleNamespace(user_id="owner")
    assert applications("ref", user, None)["path"] == "/authorized/report.txt"
    assert open_local_file("ref", FileOpenRequest(application="editor"), user, None) == {"ok": True}
    assert calls == [("/authorized/report.txt", {"action": "open", "application": "editor"})]


def test_native_actions_cannot_bypass_file_authorization(monkeypatch):
    def denied(*args):
        raise HTTPException(403, "Denied")

    monkeypatch.setattr("api.routes.files.local_file_location", denied)
    with pytest.raises(HTTPException) as error:
        open_local_file("private", FileOpenRequest(), SimpleNamespace(user_id="other"), None)
    assert error.value.status_code == 403


def test_native_open_rejects_unregistered_application(monkeypatch):
    from features.desktop.local.file_actions import open_file

    monkeypatch.setattr('features.desktop.local.file_actions.file_applications',
                        lambda path, **kwargs: {"applications": [], "default_id": None})
    with pytest.raises(ValueError, match="关联应用"):
        open_file("/authorized/report.txt", action="open", application="/untrusted/program")


def test_windows_delegate_default_keeps_system_open(monkeypatch):
    import sys
    from features.desktop.local import file_actions as local_file_actions

    def missing_key(*args):
        raise OSError

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_CURRENT_USER="user", HKEY_CLASSES_ROOT="classes", OpenKey=missing_key,
    ))
    monkeypatch.setattr(local_file_actions, "_registry_value",
                        lambda root, key, name="": "AppXDefault" if name == "ProgId" else None)
    monkeypatch.setattr(local_file_actions, "_windows_default_name", lambda extension: "System Viewer")
    monkeypatch.setattr(local_file_actions.sys, "platform", "win32")
    choices = local_file_actions._windows_applications("report.pdf")
    assert choices["default_id"] == "system-default"
    assert choices["applications"][0]["name"] == "System Viewer"
    opened = []
    monkeypatch.setattr(local_file_actions, "file_applications", lambda path, **kwargs: choices)
    monkeypatch.setattr(local_file_actions.os, "startfile", opened.append, raising=False)
    local_file_actions.open_file("report.pdf", action="open", application="system-default")
    assert opened == ["report.pdf"]


def test_application_icon_payload_is_inline_and_bounded(monkeypatch):
    import base64
    from features.desktop.local import native_app_icons
    native_app_icons.windows_icon.cache_clear()
    monkeypatch.setattr(native_app_icons.sys, "platform", "win32")
    png = bytes.fromhex("89504e470d0a1a0a") + b"fixture"
    def run(arguments, **kwargs):
        assert arguments[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
        script = base64.b64decode(arguments[4]).decode("utf-16le")
        assert "ExtractAssociatedIcon" in script
        assert "a'quoted.exe" not in script
        return SimpleNamespace(returncode=0, stdout=base64.b64encode(png).decode())
    monkeypatch.setattr(native_app_icons.subprocess, "run", run)
    monkeypatch.setattr(native_app_icons, "no_window_kwargs", lambda: {})
    result = native_app_icons.windows_icon("C:/Apps/a'quoted.exe")
    assert result.startswith("data:image/png;base64,")
    native_app_icons.windows_icon.cache_clear()


def test_application_icon_failure_has_no_external_fallback(monkeypatch):
    from features.desktop.local import native_app_icons
    native_app_icons.windows_icon.cache_clear()
    monkeypatch.setattr(native_app_icons.sys, "platform", "win32")
    monkeypatch.setattr(native_app_icons, "no_window_kwargs", lambda: {})
    monkeypatch.setattr(native_app_icons.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="not a PNG"))
    assert native_app_icons.windows_icon("C:/Apps/invalid.exe") is None
    native_app_icons.windows_icon.cache_clear()

def test_selected_application_does_not_read_icons(monkeypatch, tmp_path):
    from features.desktop.local import file_actions as actions

    entry = tmp_path / "viewer.desktop"
    entry.write_text("[Desktop Entry]\nName=Viewer\nExec=viewer %f\n")
    monkeypatch.setattr(actions.sys, "platform", "linux")
    monkeypatch.setattr(actions, "_run", lambda args, **kwargs:
                        "text/plain" if args[0] == "xdg-mime" else "viewer.desktop")
    monkeypatch.setattr(actions, "_desktop_entry", lambda identifier: (entry, "Viewer"))
    def slow_icon(path):
        pytest.fail("Opening a selected application must not decode application icons")
    monkeypatch.setattr(actions, "desktop_icon", slow_icon)
    launched = []
    monkeypatch.setattr(actions.subprocess, "Popen", lambda args, **kwargs: launched.append(args))
    actions.open_file(str(tmp_path / "report.txt"), action="open", application="viewer.desktop")
    assert launched == [["gio", "launch", str(entry), str(tmp_path / "report.txt")]]

def test_windows_lightweight_choices_do_not_parse_icon_commands(monkeypatch):
    import sys
    from features.desktop.local import file_actions as actions

    def missing_key(*args):
        raise OSError

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_CURRENT_USER="user", HKEY_CLASSES_ROOT="classes", OpenKey=missing_key,
    ))
    monkeypatch.setattr(actions.sys, "platform", "win32")
    def value(root, key, name=""):
        if name == "ProgId":
            return "Viewer"
        if key.endswith(r"\shell\open\command"):
            return 'viewer "%1"'
        return None
    monkeypatch.setattr(actions, "_registry_value", value)
    monkeypatch.setattr(actions, "_windows_default_name", lambda value: "Viewer")
    def unexpected_parse(*args):
        pytest.fail("Lightweight application discovery must not parse icon executable paths")
    monkeypatch.setattr(actions, "_windows_command", unexpected_parse)
    assert actions.file_applications("report.txt", include_icons=False) == {
        "applications": [{"id": "Viewer", "name": "Viewer", "icon": None}],
        "default_id": "Viewer",
    }
