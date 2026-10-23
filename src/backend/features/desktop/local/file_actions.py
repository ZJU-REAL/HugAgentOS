"""Native application choices and actions for authenticated local files."""

import configparser
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys

from core.infra.proc import no_window_kwargs
from features.desktop.local.native_app_icons import desktop_icon, windows_icon


def _run(arguments: list[str], *, input_text: str | None = None, timeout: int = 10) -> str:
    result = subprocess.run(
        arguments, input=input_text, capture_output=True, text=True, timeout=timeout,
        **no_window_kwargs(),
    )
    if result.returncode:
        raise RuntimeError("无法查询或打开关联应用")
    return result.stdout.strip()


def file_applications(path: str, *, include_icons: bool = True) -> dict:
    if sys.platform == "darwin":
        script = """
ObjC.import('AppKit');
function appIcon(bundle) {
    try {
        const image = $.NSWorkspace.sharedWorkspace.iconForFile(bundle.bundlePath);
        image.setSize($.NSMakeSize(16,16));
        const bitmap = $.NSBitmapImageRep.imageRepWithData(image.TIFFRepresentation);
        const data = bitmap.representationUsingTypeProperties($.NSPNGFileType, $({}));
        return 'data:image/png;base64,' + ObjC.unwrap(data.base64EncodedStringWithOptions(0));
    } catch (e) { return null; }
}
function run(args) {
    const workspace = $.NSWorkspace.sharedWorkspace;
    const url = $.NSURL.fileURLWithPath(args[0]);
    const preferred = workspace.URLForApplicationToOpenURL(url);
    const urls = workspace.URLsForApplicationsToOpenURL(url);
    const apps = [];
    const seen = new Set();
    const includeIcons = args[1] === 'true';
    for (let index = 0; index < urls.count; index++) {
        const bundle = $.NSBundle.bundleWithURL(urls.objectAtIndex(index));
        const id = ObjC.unwrap(bundle.bundleIdentifier);
        if (!id || seen.has(id)) continue;
        seen.add(id);
        const name = ObjC.unwrap(bundle.objectForInfoDictionaryKey('CFBundleDisplayName'))
            || ObjC.unwrap(bundle.objectForInfoDictionaryKey('CFBundleName')) || id;
        apps.push({id, name, icon: includeIcons ? appIcon(bundle) : null});
    }
    const defaultId = preferred
        ? ObjC.unwrap($.NSBundle.bundleWithURL(preferred).bundleIdentifier) : null;
    return JSON.stringify({applications: apps, default_id: defaultId});
}
"""
        # AppKit can take longer on first access when many applications provide icons.
        # Registration validation excludes icons and retains the shorter timeout.
        return json.loads(_run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-", path, str(include_icons).lower()],
            input_text=script, timeout=45 if include_icons else 10,
        ))
    if sys.platform == "win32":
        return _windows_applications(path, include_icons=include_icons)
    mime = _run(["xdg-mime", "query", "filetype", path]) or mimetypes.guess_type(path)[0]
    if not mime:
        return {"applications": [], "default_id": None}
    output = _run(["gio", "mime", mime])
    identifiers = list(dict.fromkeys(re.findall(r"[\w.@+-]+\.desktop", output)))
    applications = []
    for identifier in identifiers:
        entry = _desktop_entry(identifier)
        if entry is not None:
            applications.append({"id": identifier, "name": entry[1], "icon": desktop_icon(str(entry[0])) if include_icons else None})
    return {"applications": applications, "default_id": identifiers[0] if identifiers else None}


def _desktop_entry(identifier: str) -> tuple[Path, str] | None:
    if Path(identifier).name != identifier:
        return None
    roots = [Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))]
    roots.extend(Path(root) for root in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"))
    for root in roots:
        entry = root / "applications" / identifier
        if not entry.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(entry, encoding="utf-8")
        if parser.get("Desktop Entry", "Hidden", fallback="false") == "true":
            continue
        return entry, parser.get("Desktop Entry", "Name", fallback=identifier)
    return None


def _registry_value(root, key: str, name: str = "") -> str | None:
    import winreg

    try:
        with winreg.OpenKey(root, key) as handle:
            return str(winreg.QueryValueEx(handle, name)[0])
    except OSError:
        return None


def _windows_applications(path: str, *, include_icons: bool = True) -> dict:
    import winreg

    extension = Path(path).suffix
    file_key = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{extension}"
    preferred = _registry_value(winreg.HKEY_CURRENT_USER, file_key + r"\UserChoice", "ProgId")
    preferred = preferred or _registry_value(winreg.HKEY_CLASSES_ROOT, extension)
    identifiers = [preferred] if preferred else []
    for root, key in (
        (winreg.HKEY_CURRENT_USER, file_key + r"\OpenWithProgids"),
        (winreg.HKEY_CLASSES_ROOT, extension + r"\OpenWithProgids"),
    ):
        try:
            with winreg.OpenKey(root, key) as handle:
                for index in range(winreg.QueryInfoKey(handle)[1]):
                    identifiers.append(winreg.EnumValue(handle, index)[0])
        except OSError:
            continue
    applications = []
    for identifier in dict.fromkeys(identifiers):
        command = _registry_value(winreg.HKEY_CLASSES_ROOT, identifier + r"\shell\open\command")
        if not command:
            continue
        name = _windows_default_name(identifier) or _registry_value(winreg.HKEY_CLASSES_ROOT, identifier) or identifier
        icon = None
        if include_icons:
            icon_path = path
            try:
                icon_path = _windows_command(command, path)[0]
            except (OSError, ValueError, RuntimeError):
                pass
            icon = windows_icon(icon_path)
        applications.append({"id": identifier, "name": name, "icon": icon})
    if preferred and preferred not in {item["id"] for item in applications}:
        applications.insert(0, {
            "id": "system-default",
            "name": _windows_default_name(extension) or "系统默认应用",
            "icon": windows_icon(path) if include_icons else None,
        })
        preferred = "system-default"
    return {"applications": applications, "default_id": preferred}


def _windows_default_name(extension: str) -> str | None:
    import ctypes
    from ctypes import wintypes

    length = wintypes.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(length.value)
    query = ctypes.windll.shlwapi.AssocQueryStringW
    query.argtypes = [
        wintypes.DWORD, ctypes.c_int, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = ctypes.c_long
    result = query(0, 4, extension, "open", buffer, ctypes.byref(length))
    return buffer.value if result == 0 else None


def _windows_command(command: str, path: str) -> list[str]:
    import ctypes
    from ctypes import wintypes

    argument_count = ctypes.c_int()
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    arguments = parse(os.path.expandvars(command), ctypes.byref(argument_count))
    if not arguments:
        raise RuntimeError("关联应用命令无效")
    try:
        result = [arguments[index] for index in range(argument_count.value)]
    finally:
        release = ctypes.windll.kernel32.LocalFree
        release.argtypes = [ctypes.c_void_p]
        release.restype = ctypes.c_void_p
        release(arguments)
    replaced = False
    for index, argument in enumerate(result):
        if re.search(r"%[1lL]", argument):
            result[index] = re.sub(r"%[1lL]", lambda _: path, argument)
            replaced = True
    result = [argument for argument in result if argument != "%*"]
    if not replaced:
        result.append(path)
    return result


def open_file(path: str, *, action: str, application: str | None = None) -> None:
    if action not in ("open", "reveal"):
        raise ValueError("无效的文件操作")
    if application is not None:
        choices = file_applications(path, include_icons=False)
        if application not in {item["id"] for item in choices["applications"]}:
            raise ValueError("关联应用已不可用，请重试")
    if sys.platform == "darwin":
        _run(["/usr/bin/open", "-R", path] if action == "reveal" else
             ["/usr/bin/open", "-b", application, path] if application else ["/usr/bin/open", path])
        return
    if sys.platform == "win32":
        if action == "reveal":
            arguments = ["explorer.exe", "/select,", path]
        elif application and application != "system-default":
            import winreg

            command = _registry_value(winreg.HKEY_CLASSES_ROOT, application + r"\shell\open\command")
            if not command:
                raise ValueError("关联应用已不可用，请重试")
            arguments = _windows_command(command, path)
        else:
            os.startfile(path)
            return
    elif action == "reveal":
        arguments = ["xdg-open", str(Path(path).parent)]
    elif application:
        entry = _desktop_entry(application)
        if entry is None:
            raise ValueError("关联应用已不可用，请重试")
        arguments = ["gio", "launch", str(entry[0]), path]
    else:
        arguments = ["xdg-open", path]
    subprocess.Popen(
        arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, **no_window_kwargs(),
    )
