"""Small native application icons; cached, bounded and returned inline."""

import base64
from functools import lru_cache
from pathlib import Path
import subprocess
import sys
from core.infra.proc import no_window_kwargs


@lru_cache(maxsize=128)
def windows_icon(path: str) -> str | None:
    if sys.platform != "win32":
        return None
    # The path is encoded as data, never interpolated into executable PowerShell.
    value = base64.b64encode(path.encode("utf-8")).decode("ascii")
    script = (
        "Add-Type -AssemblyName System.Drawing; "
        "$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + value + "')); "
        "$i=[Drawing.Icon]::ExtractAssociatedIcon($p); if($i){"
        "$b=$i.ToBitmap(); $m=New-Object IO.MemoryStream; try{"
        "$b.Save($m,[Drawing.Imaging.ImageFormat]::Png); "
        "[Convert]::ToBase64String($m.ToArray())"
        "}finally{$m.Dispose();$b.Dispose();$i.Dispose()}}"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            timeout=8,
            **no_window_kwargs(),
        )
        data = result.stdout.strip()
        if result.returncode == 0 and data and len(data) <= 131072:
            decoded = base64.b64decode(data, validate=True)
            if decoded.startswith(bytes.fromhex("89504e470d0a1a0a")):
                return "data:image/png;base64," + data
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


@lru_cache(maxsize=128)
def desktop_icon(entry_path: str) -> str | None:
    import configparser
    import os

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(entry_path, encoding="utf-8")
        name = parser.get("Desktop Entry", "Icon", fallback="")
        if not name:
            return None
        candidates = [Path(name)] if Path(name).is_absolute() else []
        if Path(name).name == name:
            roots = [Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))]
            roots.extend(
                Path(root)
                for root in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(
                    ":"
                )
            )
            for root in roots:
                candidates.extend(
                    root / "icons/hicolor" / size / "apps" / (name + ".png")
                    for size in ("16x16", "24x24", "32x32", "48x48")
                )
                candidates.append(root / "pixmaps" / (name + ".png"))
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size <= 131072:
                data = candidate.read_bytes()
                if data.startswith(bytes.fromhex("89504e470d0a1a0a")):
                    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    except (OSError, ValueError, configparser.Error):
        pass
    return None
