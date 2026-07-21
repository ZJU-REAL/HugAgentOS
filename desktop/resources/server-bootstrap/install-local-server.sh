#!/bin/bash

set -euo pipefail

BundleDir=""
InstallRoot=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle-dir)
      BundleDir="${2:-}"
      shift 2
      ;;
    --install-root)
      InstallRoot="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

progress() {
  printf 'HUGAGENT_PROGRESS|%s|%s\n' "$1" "$2"
}

if [[ -z "$BundleDir" || -z "$InstallRoot" ]]; then
  echo "Both --bundle-dir and --install-root are required." >&2
  exit 2
fi
if [[ ! -f "$BundleDir/pyproject.toml" ]]; then
  echo "The desktop package doesn't contain a valid CE server payload." >&2
  exit 3
fi
if [[ ! -f "$BundleDir/src/frontend/dist/index.html" ]]; then
  echo "The bundled CE web application is missing." >&2
  exit 3
fi

SourceDir="$InstallRoot/source"
VenvDir="$InstallRoot/venv"
VenvPython="$VenvDir/bin/python"
InstalledManifest="$InstallRoot/installed-bundle.json"
ToolsDir="$InstallRoot/tools"
UvBin="$ToolsDir/uv"
PythonDir="$InstallRoot/python"

mkdir -p "$InstallRoot"

progress 5 "正在复制同版本服务端资源…"
rm -rf "$SourceDir"
mkdir -p "$SourceDir"
if [[ -x /usr/bin/ditto ]]; then
  /usr/bin/ditto "$BundleDir" "$SourceDir"
else
  /bin/cp -R "$BundleDir/." "$SourceDir/"
fi

progress 12 "正在准备独立运行环境…"
if [[ ! -x "$UvBin" ]]; then
  mkdir -p "$ToolsDir"
  progress 16 "正在下载运行环境管理器…"
  /usr/bin/curl --fail --location --silent --show-error \
    https://astral.sh/uv/0.11.30/install.sh \
    | env UV_UNMANAGED_INSTALL="$ToolsDir" UV_NO_MODIFY_PATH=1 /bin/sh
fi
if [[ ! -x "$UvBin" ]]; then
  echo "The local runtime manager couldn't be installed." >&2
  exit 4
fi

export UV_CACHE_DIR="$InstallRoot/cache/uv"
export UV_PYTHON_INSTALL_DIR="$PythonDir"
export UV_PYTHON_BIN_DIR="$InstallRoot/python-bin"

progress 20 "正在下载 Python 3.11 运行环境…"
"$UvBin" python install 3.11 --install-dir "$PythonDir" --no-bin

RebuildVenv=1
if [[ -x "$VenvPython" ]] \
  && "$VenvPython" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  RebuildVenv=0
fi
if [[ "$RebuildVenv" -eq 1 ]]; then
  progress 26 "正在创建独立 Python 环境…"
  rm -rf "$VenvDir"
  "$UvBin" venv --python 3.11 "$VenvDir"
fi

progress 34 "正在准备 Python 安装工具…"
"$UvBin" pip install --python "$VenvPython" --upgrade pip setuptools wheel

progress 42 "正在安装服务端依赖，首次安装需要数分钟…"
"$UvBin" pip install --python "$VenvPython" --prefer-binary \
  --requirements "$SourceDir/requirements.txt"

progress 70 "正在安装本机脚本与文档处理能力…"
"$UvBin" pip install --python "$VenvPython" --prefer-binary \
  --requirements "$SourceDir/docker/requirements-script-runner.txt"

progress 78 "正在检查可选的 Node.js 文档能力…"
NodeExecutable=""
if [[ "${HUGAGENT_SKIP_OPTIONAL_NODE:-0}" != "1" ]]; then
  for Candidate in \
    "$(command -v node 2>/dev/null || true)" \
    "/opt/homebrew/bin/node" \
    "/usr/local/bin/node"; do
    if [[ -n "$Candidate" && -x "$Candidate" ]] \
      && [[ "$("$Candidate" -p 'Number(process.versions.node.split(".")[0]) >= 20 ? "ok" : "old"' 2>/dev/null)" == "ok" ]]; then
      NodeExecutable="$Candidate"
      break
    fi
  done
fi

if [[ -n "$NodeExecutable" ]]; then
  printf '%s' "$NodeExecutable" > "$InstallRoot/node-executable.txt"
  NodeDir="$(dirname "$NodeExecutable")"
  NpmExecutable="$NodeDir/npm"
  if [[ ! -x "$NpmExecutable" ]]; then
    NpmExecutable="$(command -v npm 2>/dev/null || true)"
  fi
  if [[ -n "$NpmExecutable" && -x "$NpmExecutable" ]]; then
    NodeDataDir="$InstallRoot/data/node"
    export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
    if "$NpmExecutable" install --silent --no-audit --no-fund --no-package-lock \
      --prefix "$NodeDataDir" pptxgenjs playwright; then
      Playwright="$NodeDataDir/node_modules/.bin/playwright"
      if [[ -x "$Playwright" ]]; then
        export PLAYWRIGHT_BROWSERS_PATH="$NodeDataDir/browsers"
        "$Playwright" install chromium \
          || echo "Chromium download failed; advanced PDF rendering will use its fallback." >&2
      fi
    else
      echo "Optional Node.js tools couldn't be prepared; the core service will still start." >&2
    fi
  fi
else
  rm -f "$InstallRoot/node-executable.txt"
  echo "Node.js 20+ isn't installed; optional site and document tools remain unavailable." >&2
fi

progress 86 "正在注册 HugAgentOS 本机服务…"
"$UvBin" pip install --python "$VenvPython" --no-deps --editable "$SourceDir"

if [[ ! -x "$VenvDir/bin/hugagent" ]]; then
  echo "The HugAgentOS service command wasn't installed correctly." >&2
  exit 5
fi

/bin/cp "$BundleDir/desktop-bundle.json" "$InstalledManifest"
progress 90 "本机服务安装完成，正在启动…"
printf 'Local server installed at %s\n' "$InstallRoot"
