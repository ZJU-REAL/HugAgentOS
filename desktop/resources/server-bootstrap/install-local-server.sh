#!/bin/bash

set -euo pipefail

BundleDir=""
InstallRoot=""
ScriptDir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MacOverrides="$ScriptDir/requirements-macos-overrides.txt"

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
if [[ ! -f "$MacOverrides" ]]; then
  echo "The macOS dependency compatibility overrides are missing." >&2
  exit 3
fi

ToolsDir="$InstallRoot/tools"
UvBin="$ToolsDir/uv"
PythonDir="$InstallRoot/python"
ReleasesDir="$InstallRoot/releases"
CurrentLink="$InstallRoot/current"
PreviousLink="$InstallRoot/current.previous"
CurrentNext="$InstallRoot/current.next"
CandidateDir=""
CandidateCommitted=0
ObsoleteRelease=""

cleanup_candidate() {
  ExitCode=$?
  trap - EXIT
  if [[ "$CandidateCommitted" -eq 0 && -n "$CandidateDir" && -d "$CandidateDir" ]]; then
    /bin/rm -rf -- "$CandidateDir"
  fi
  /bin/rm -f -- "$CurrentNext"
  exit "$ExitCode"
}
trap cleanup_candidate EXIT

download_with_retry() {
  DownloadUrl="$1"
  DownloadTarget="$2"
  /usr/bin/curl \
    --fail \
    --location \
    --silent \
    --show-error \
    --retry 5 \
    --retry-all-errors \
    --retry-delay 2 \
    --connect-timeout 20 \
    --max-time 300 \
    --output "$DownloadTarget" \
    "$DownloadUrl"
}

uv_run() {
  "$UvBin" --system-certs "$@"
}

mkdir -p "$InstallRoot" "$ReleasesDir"

progress 3 "正在检查安装空间…"
AvailableKb="$(/bin/df -Pk "$InstallRoot" | /usr/bin/awk 'NR == 2 { print $4 }')"
BundleKb="$(/usr/bin/du -sk "$BundleDir" | /usr/bin/awk '{ print $1 }')"
MinimumFreeKb="${HUGAGENT_MIN_FREE_KB:-4194304}"
if [[ ! "$AvailableKb" =~ ^[0-9]+$ || ! "$BundleKb" =~ ^[0-9]+$ || ! "$MinimumFreeKb" =~ ^[0-9]+$ ]]; then
  echo "The installer couldn't determine available disk space." >&2
  exit 4
fi
RequiredKb=$((MinimumFreeKb + BundleKb * 2))
if (( AvailableKb < RequiredKb )); then
  RequiredGb=$(((RequiredKb + 1048575) / 1048576))
  AvailableGb=$((AvailableKb / 1048576))
  echo "Not enough disk space: ${AvailableGb} GB available, ${RequiredGb} GB required." >&2
  exit 4
fi

BundleHash="$(/usr/bin/shasum -a 256 "$BundleDir/desktop-bundle.json" | /usr/bin/awk '{ print $1 }')"
CandidateDir="$(/usr/bin/mktemp -d "$ReleasesDir/${BundleHash}.XXXXXX")"
SourceDir="$CandidateDir/source"
VenvDir="$CandidateDir/venv"
VenvPython="$VenvDir/bin/python"

progress 5 "正在复制同版本服务端资源…"
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
  UvVersion="0.11.30"
  case "$(/usr/bin/uname -m)" in
    arm64|aarch64)
      UvArtifact="uv-aarch64-apple-darwin.tar.gz"
      UvDirectory="uv-aarch64-apple-darwin"
      UvSha256="9bed3567d496d8dab84ecf7a1247551ac94ef1baaebb7b65df008dd93e9dc357"
      ;;
    x86_64)
      UvArtifact="uv-x86_64-apple-darwin.tar.gz"
      UvDirectory="uv-x86_64-apple-darwin"
      UvSha256="ce285fbbfbe294b1e1bc6c87c8b59d9622b85383b88b2b132a2df5c73e83d7c1"
      ;;
    *)
      echo "This Mac architecture isn't supported by the local installer." >&2
      exit 4
      ;;
  esac
  UvArchive="$ToolsDir/$UvArtifact"
  UvPrimaryUrl="https://releases.astral.sh/github/uv/releases/download/$UvVersion/$UvArtifact"
  UvFallbackUrl="https://github.com/astral-sh/uv/releases/download/$UvVersion/$UvArtifact"
  if ! download_with_retry "$UvPrimaryUrl" "$UvArchive"; then
    /bin/rm -f -- "$UvArchive"
    download_with_retry "$UvFallbackUrl" "$UvArchive"
  fi
  ActualUvSha256="$(/usr/bin/shasum -a 256 "$UvArchive" | /usr/bin/awk '{ print $1 }')"
  if [[ "$ActualUvSha256" != "$UvSha256" ]]; then
    /bin/rm -f -- "$UvArchive"
    echo "The downloaded runtime manager failed its SHA-256 verification." >&2
    exit 4
  fi
  /usr/bin/tar -xzf "$UvArchive" -C "$ToolsDir" --strip-components 1 "$UvDirectory/uv"
  /bin/chmod 755 "$UvBin"
  /bin/rm -f -- "$UvArchive"
fi
if [[ ! -x "$UvBin" ]]; then
  echo "The local runtime manager couldn't be installed." >&2
  exit 4
fi

export UV_CACHE_DIR="$InstallRoot/cache/uv"
export UV_PYTHON_INSTALL_DIR="$PythonDir"
export UV_PYTHON_BIN_DIR="$InstallRoot/python-bin"
export UV_HTTP_RETRIES=5

progress 20 "正在下载 Python 3.11 运行环境…"
uv_run python install 3.11 --install-dir "$PythonDir" --no-bin

progress 26 "正在创建独立 Python 环境…"
uv_run venv --python 3.11 "$VenvDir"

progress 34 "正在准备 Python 安装工具…"
uv_run pip install --python "$VenvPython" --upgrade pip setuptools wheel

progress 42 "正在安装服务端依赖，首次安装需要数分钟…"
uv_run pip install --python "$VenvPython" \
  --requirements "$SourceDir/requirements.txt"

progress 70 "正在安装本机脚本与文档处理能力…"
uv_run pip install --python "$VenvPython" \
  --requirements "$SourceDir/docker/requirements-script-runner.txt" \
  --overrides "$MacOverrides" \
  --only-binary pikepdf

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
uv_run pip install --python "$VenvPython" --no-deps --editable "$SourceDir"

if [[ ! -x "$VenvDir/bin/hugagent" ]]; then
  echo "The HugAgentOS service command wasn't installed correctly." >&2
  exit 5
fi
if ! "$VenvDir/bin/hugagent" --help >/dev/null; then
  echo "The installed HugAgentOS service failed its startup validation." >&2
  exit 5
fi

/bin/cp "$BundleDir/desktop-bundle.json" "$CandidateDir/desktop-bundle.json"

progress 88 "正在安全切换到新版本…"
if [[ -L "$PreviousLink" ]]; then
  PreviousPreviousTarget="$(/usr/bin/readlink "$PreviousLink")"
  case "$PreviousPreviousTarget" in
    "$ReleasesDir"/*) ObsoleteRelease="$PreviousPreviousTarget" ;;
  esac
fi
/bin/rm -f -- "$PreviousLink" "$CurrentNext"
if [[ -L "$CurrentLink" ]]; then
  PreviousTarget="$(/usr/bin/readlink "$CurrentLink")"
  /bin/ln -s "$PreviousTarget" "$PreviousLink"
elif [[ -e "$CurrentLink" ]]; then
  echo "The local release pointer is invalid; the existing installation was left unchanged." >&2
  exit 5
elif [[ -x "$InstallRoot/venv/bin/hugagent" && -d "$InstallRoot/source" ]]; then
  if [[ -f "$InstallRoot/installed-bundle.json" ]]; then
    /bin/cp "$InstallRoot/installed-bundle.json" "$InstallRoot/desktop-bundle.json"
  fi
  /bin/ln -s "$InstallRoot" "$PreviousLink"
fi
/bin/ln -s "$CandidateDir" "$CurrentNext"
if ! /bin/mv -fh "$CurrentNext" "$CurrentLink" 2>/dev/null; then
  # GNU mv (used by the Linux script test) spells BSD/macOS `-h` as `-T`.
  /bin/mv -fT "$CurrentNext" "$CurrentLink"
fi
CandidateCommitted=1
if [[ -n "$ObsoleteRelease" && "$ObsoleteRelease" != "$CandidateDir" ]]; then
  /bin/rm -rf -- "$ObsoleteRelease" || true
fi

progress 90 "本机服务安装完成，正在启动…"
printf 'Local server installed at %s\n' "$CandidateDir"
