"""pptxgenjs engine — subprocess wrapper around the bundled Node script.

Calls ``node node_scripts/build_presentation.js --out <path>`` with the
JSON spec piped via stdin. Requires Node.js (any version ≥18) and the
``pptxgenjs`` npm package installed globally in the mcp container's
runtime image (see ``Dockerfile.mcp``).

If Node or pptxgenjs is not available, ``build()`` raises ``EngineError``
and callers should fall back to the python-pptx engine.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class EngineError(RuntimeError):
    """Raised when the pptxgenjs engine fails to produce output."""


_SCRIPT_PATH = Path(__file__).parent / "node_scripts" / "build_presentation.js"


def _resolve_node_binary() -> str:
    """Return the absolute path to a working ``node`` executable."""
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        raise EngineError(
            "Node.js not found in PATH. Install nodejs in the mcp container "
            "(see Dockerfile.mcp) or use engine='python-pptx' instead."
        )
    return node


def is_available() -> bool:
    """Cheap probe: do we have node + the build script + pptxgenjs reachable?"""
    if not _SCRIPT_PATH.exists():
        return False
    try:
        node = _resolve_node_binary()
    except EngineError:
        return False
    # Ask node to require pptxgenjs — fastest way to confirm it's installed.
    proc = subprocess.run(
        [node, "-e", "require('pptxgenjs'); process.exit(0)"],
        capture_output=True, text=True, timeout=10,
    )
    return proc.returncode == 0


def build(spec: dict[str, Any], *, output_path: Path, timeout: float = 60.0) -> dict[str, Any]:
    """Build a .pptx via pptxgenjs.

    Args:
        spec: full presentation spec (see node_scripts/build_presentation.js header)
        output_path: absolute path where the .pptx should be written
        timeout: seconds to wait for the node process

    Returns:
        ``{out, slides, size_kb, theme}`` from the node script's stdout

    Raises:
        EngineError if node is missing, the script fails, or output is malformed.
    """
    if not _SCRIPT_PATH.exists():
        raise EngineError(f"build_presentation.js not found at {_SCRIPT_PATH}")
    node = _resolve_node_binary()

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(spec, ensure_ascii=False)

    try:
        proc = subprocess.run(
            [node, str(_SCRIPT_PATH), "--out", str(output_path)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise EngineError(f"node build_presentation.js timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise EngineError(f"failed to spawn node: {exc}") from exc

    if proc.returncode != 0:
        # Node script writes structured errors to stderr; surface verbatim.
        err_msg = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise EngineError(f"pptxgenjs build failed: {err_msg}")

    stdout = (proc.stdout or "").strip()
    if not stdout:
        raise EngineError("pptxgenjs build produced no stdout")

    try:
        result = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise EngineError(f"pptxgenjs build emitted non-JSON stdout: {stdout!r}") from exc

    if result.get("status") != "ok":
        raise EngineError(f"pptxgenjs build returned status={result.get('status')!r}: {result.get('error')}")

    if not output_path.exists():
        raise EngineError(f"pptxgenjs build claimed success but {output_path} missing")

    return result
