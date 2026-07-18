"""Strictly probe inside the sandbox whether skill dependencies are actually ready.

A skill's declared pip/npm/apt dependencies **do not** mean they truly exist in the sandbox —
the old logic pended whenever something was declared and blindly marked available once a
rebuild succeeded, both crude judgments. This module actually runs a probe script in the
**currently active sandbox** and decides per-package whether it is truly installed:

- pip: ``importlib.metadata.distribution(name)`` queries the distribution in the sandbox Python interpreter (with PEP 503 normalization);
- apt: ``dpkg -s <name>`` exit code;
- npm: ``npm ls -g <name>`` exit code (dependencies are baked in via ``npm install -g``, see docker/Dockerfile.opensandbox).

Returns the subset that is **still missing**. When the probe is uncertain (sandbox unreachable /
script error / output unparseable) it returns ``None``, and the caller handles it as
"conservative = treat as missing" (a user-confirmed fallback strategy).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MARKER = "__SKILL_DEP_PROBE__"
_KINDS = ("pip", "npm", "apt")


def names_of(entries: Any) -> list[str]:
    """Normalize detect_dependencies entries (``{name,version,source}`` or plain strings) into a list of package names."""
    out: list[str] = []
    for e in entries or []:
        if isinstance(e, str):
            n = e.strip()
        elif isinstance(e, dict):
            n = str(e.get("name") or "").strip()
        else:
            n = ""
        if n and n not in out:
            out.append(n)
    return out


def _build_script(names: dict[str, list[str]]) -> str:
    """Generate the probe script to run inside the sandbox. Package names are embedded as JSON literals (the source is the detector, not free user input).

    Key point: the sandbox's ``execute(language=python)`` runs the system ``/usr/bin/python3``, while the skill's pip
    dependencies are installed under ``$PY_BIN`` (see ``ENV PY_BIN=...`` in docker/Dockerfile.opensandbox +
    ``$PY_BIN -m pip install -r requirements-skills.txt``). So pip must query distributions **against the $PY_BIN
    interpreter**, not the currently running interpreter, otherwise everything is judged missing.
    """
    declared = json.dumps(names, ensure_ascii=False)
    return (
        "import json, os, sys, subprocess, glob, shutil\n"
        f"DECLARED = json.loads(r'''{declared}''')\n"
        "def _pip_target():\n"
        "    c = os.environ.get('PY_BIN')\n"
        "    cands = ([c] if c else []) + sorted(glob.glob('/opt/python/versions/cpython-*/bin/python3'))\n"
        "    for p in cands:\n"
        "        if p and os.path.exists(p): return p\n"
        "    return sys.executable\n"
        "def _pip_missing(ns):\n"
        "    if not ns: return []\n"
        "    py = _pip_target()\n"
        "    code = ('import importlib.metadata as m,json,sys\\n'\n"
        "            'ns=json.loads(sys.argv[1]); out=[]\\n'\n"
        "            'for n in ns:\\n'\n"
        "            ' try: m.distribution(n)\\n'\n"
        "            ' except Exception: out.append(n)\\n'\n"
        "            'print(json.dumps(out))')\n"
        "    try:\n"
        "        r = subprocess.run([py,'-c',code,json.dumps(ns)], capture_output=True, text=True, timeout=60)\n"
        "        if r.returncode == 0 and r.stdout.strip():\n"
        "            return json.loads(r.stdout.strip().splitlines()[-1])\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        import importlib.metadata as _m\n"
        "        out = []\n"
        "        for n in ns:\n"
        "            try: _m.distribution(n)\n"
        "            except Exception: out.append(n)\n"
        "        return out\n"
        "    except Exception:\n"
        "        return list(ns)\n"
        "def _apt_missing(ns):\n"
        "    out = []\n"
        "    for n in ns:\n"
        "        try:\n"
        "            r = subprocess.run(['dpkg','-s',n], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "            if r.returncode != 0: out.append(n)\n"
        "        except Exception:\n"
        "            out.append(n)\n"
        "    return out\n"
        "def _npm_bin():\n"
        "    for c in ['npm','/opt/node/v22.2.0/bin/npm']:\n"
        "        if shutil.which(c) or os.path.exists(c): return c\n"
        "    g = glob.glob('/opt/node/*/bin/npm')\n"
        "    return g[0] if g else 'npm'\n"
        "def _npm_missing(ns):\n"
        "    if not ns: return []\n"
        "    npm = _npm_bin(); out = []\n"
        "    for n in ns:\n"
        "        try:\n"
        "            r = subprocess.run([npm,'ls','-g',n,'--depth=0'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "            if r.returncode != 0: out.append(n)\n"
        "        except Exception:\n"
        "            out.append(n)\n"
        "    return out\n"
        "res = {'pip': _pip_missing(DECLARED.get('pip',[])),\n"
        "       'npm': _npm_missing(DECLARED.get('npm',[])),\n"
        "       'apt': _apt_missing(DECLARED.get('apt',[]))}\n"
        f"print('{_MARKER}' + json.dumps(res))\n"
    )


async def probe_still_missing(
    declared: Optional[dict], *, timeout: int = 90
) -> Optional[dict[str, list[str]]]:
    """Probe in the active sandbox which packages in ``declared`` ({pip/npm/apt: [...]}) are still missing.

    - returns ``{"pip":[...], "npm":[...], "apt":[...]}`` (values are still-missing package names);
    - all ready → three empty lists;
    - probe uncertain (sandbox error / output unparseable) → returns ``None`` (caller handles conservatively).
    """
    names = {k: names_of((declared or {}).get(k)) for k in _KINDS}
    if not any(names.values()):
        return {k: [] for k in _KINDS}

    script = _build_script(names)
    try:
        from core.sandbox.factory import get_sandbox_provider
        from core.sandbox.protocol import ExecuteRequest

        provider = get_sandbox_provider()
        result = await provider.execute(
            ExecuteRequest(
                script_content=script,
                script_name="skill_dep_probe.py",
                language="python",
                timeout=timeout,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a probe failure is always uncertain
        logger.warning("[skill-deps] probe execute failed: %s", exc)
        return None

    out = result.stdout or ""
    idx = out.rfind(_MARKER)
    if idx < 0:
        logger.warning(
            "[skill-deps] probe marker missing (exit=%s, stderr=%s)",
            result.exit_code,
            (result.stderr or "")[:300],
        )
        return None
    try:
        payload = out[idx + len(_MARKER):].strip().splitlines()[0]
        data = json.loads(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[skill-deps] probe output parse failed: %s", exc)
        return None
    return {k: list(data.get(k) or []) for k in _KINDS}
