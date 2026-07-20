#!/usr/bin/env bash
#
# HugAgentOS one-command installer for the personal, no-Docker profile.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash
#
# Optional locations:
#   HUGAGENT_HOME=$HOME/.hugagent
#   HUGAGENT_SOURCE_DIR=$HOME/.hugagent/source

set -euo pipefail

REPOSITORY_URL="https://github.com/ZJU-REAL/HugAgentOS.git"
REPOSITORY_BRANCH="main"
HUGAGENT_DATA_DIR="${HUGAGENT_HOME:-${HOME}/.hugagent}"
SOURCE_DIR="${HUGAGENT_SOURCE_DIR:-${HUGAGENT_DATA_DIR}/source}"
VENV_DIR="${HUGAGENT_DATA_DIR}/venv"
CJK_FONT_DIR="${HUGAGENT_DATA_DIR}/fonts"

if [[ -t 1 ]]; then
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    RED=$'\033[31m'
    RESET=$'\033[0m'
else
    GREEN=""
    YELLOW=""
    RED=""
    RESET=""
fi

info() {
    printf '%s> %s%s\n' "${GREEN}" "$*" "${RESET}"
}

warn() {
    printf '%s! %s%s\n' "${YELLOW}" "$*" "${RESET}"
}

die() {
    printf '%sx %s%s\n' "${RED}" "$*" "${RESET}" >&2
    exit 1
}

command -v git >/dev/null 2>&1 || die "Git is required. Install Git and run this command again."
command -v curl >/dev/null 2>&1 || die "curl is required. Install curl and run this command again."

PYTHON_BIN="${PYTHON:-python3}"
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "Python 3.11 or later is required."
"${PYTHON_BIN}" - <<'PYTHON_CHECK' || die "Python 3.11 or later is required."
import sys

raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PYTHON_CHECK

# AgentScope pulls the Python ripgrep package.  On Linux, PyPI only provides a
# prebuilt wheel for x86_64 + glibc 2.39 or newer; other Linux targets build it
# from source and therefore need a Rust toolchain.
NEEDS_RUST="$("${PYTHON_BIN}" - <<'PYTHON_RUST_CHECK'
import platform
import re
import sys

needs_rust = False
if sys.platform.startswith("linux"):
    machine = platform.machine().lower()
    libc_name, libc_version = platform.libc_ver()
    parts = tuple(int(x) for x in re.findall(r"\d+", libc_version)[:2])
    has_compatible_wheel = (
        machine in {"x86_64", "amd64"}
        and libc_name.lower() == "glibc"
        and parts >= (2, 39)
    )
    needs_rust = not has_compatible_wheel
print("yes" if needs_rust else "no")
PYTHON_RUST_CHECK
)"
if [[ "${NEEDS_RUST}" == "yes" ]] && ! command -v cargo >/dev/null 2>&1; then
    die "Rust/Cargo is required on this Linux platform to build the ripgrep dependency. Install the current stable Rust toolchain and run this command again."
fi

command -v node >/dev/null 2>&1 || die "Node.js 20 or later is required to build the web application."
command -v npm >/dev/null 2>&1 || die "npm is required to build the web application."
NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
[[ "${NODE_MAJOR}" =~ ^[0-9]+$ ]] || die "Unable to determine the Node.js version."
(( NODE_MAJOR >= 20 )) || die "Node.js 20 or later is required. Found $(node --version)."

info "Python: $("${PYTHON_BIN}" --version 2>&1)"
info "Node.js: $(node --version)"

mkdir -p "${HUGAGENT_DATA_DIR}" "${CJK_FONT_DIR}"

has_local_cjk_font() {
    local candidate
    for candidate in \
        "${CJK_FONT_DIR}"/*.[tT][tT][fFcC] \
        "${CJK_FONT_DIR}"/*.[oO][tT][fF]; do
        [[ -f "${candidate}" ]] && return 0
    done
    return 1
}

# PDF Agent Skills need a Unicode font to render Chinese instead of square
# placeholders. Prefer an existing system font; otherwise download and unpack
# WenQuanYi into the user's HugAgentOS data directory without requiring root.
if [[ ! -f /usr/share/fonts/truetype/wqy/wqy-zenhei.ttc ]] && ! has_local_cjk_font; then
    if command -v apt-get >/dev/null 2>&1 && command -v dpkg-deb >/dev/null 2>&1; then
        info "Installing a local CJK font for PDF Agent Skills"
        FONT_PACKAGE_DIR="$(mktemp -d)"
        if (
            cd "${FONT_PACKAGE_DIR}"
            apt-get download fonts-wqy-zenhei >/dev/null 2>&1
            FONT_PACKAGE="$(find . -maxdepth 1 -type f -name 'fonts-wqy-zenhei_*.deb' -print -quit)"
            [[ -n "${FONT_PACKAGE}" ]]
            dpkg-deb -x "${FONT_PACKAGE}" extracted
            FONT_FILE="$(find extracted -type f -iname 'wqy-zenhei.ttc' -print -quit)"
            [[ -n "${FONT_FILE}" ]]
            cp "${FONT_FILE}" "${CJK_FONT_DIR}/wqy-zenhei.ttc"
        ); then
            info "Local CJK font is ready"
        else
            warn "Unable to download a CJK font. PDFs can still be created, but Chinese text may render as square placeholders."
        fi
        rm -rf -- "${FONT_PACKAGE_DIR}"
    else
        warn "No supported CJK font was found. PDFs can still be created, but Chinese text may render as square placeholders."
    fi
fi

if [[ -e "${SOURCE_DIR}" && ! -d "${SOURCE_DIR}/.git" ]]; then
    die "${SOURCE_DIR} exists but is not a Git checkout. Set HUGAGENT_SOURCE_DIR to another path."
fi

if [[ -d "${SOURCE_DIR}/.git" ]]; then
    ORIGIN_URL="$(git -C "${SOURCE_DIR}" remote get-url origin 2>/dev/null || true)"
    case "${ORIGIN_URL}" in
        "${REPOSITORY_URL}"|"https://github.com/ZJU-REAL/HugAgentOS"|"git@github.com:ZJU-REAL/HugAgentOS.git") ;;
        *) die "${SOURCE_DIR} points to a different Git repository: ${ORIGIN_URL}" ;;
    esac

    if [[ -n "$(git -C "${SOURCE_DIR}" status --porcelain)" ]]; then
        die "${SOURCE_DIR} contains local changes. Commit or move them before updating HugAgentOS."
    fi

    info "Updating HugAgentOS in ${SOURCE_DIR}"
    git -C "${SOURCE_DIR}" checkout "${REPOSITORY_BRANCH}" >/dev/null
    git -C "${SOURCE_DIR}" pull --ff-only origin "${REPOSITORY_BRANCH}"
else
    info "Downloading HugAgentOS to ${SOURCE_DIR}"
    git clone --depth 1 --branch "${REPOSITORY_BRANCH}" "${REPOSITORY_URL}" "${SOURCE_DIR}"
fi

venv_is_usable() {
    [[ -x "${VENV_DIR}/bin/python" ]] || return 1
    "${VENV_DIR}/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1 || return 1
    if command -v uv >/dev/null 2>&1; then
        uv pip list --python "${VENV_DIR}/bin/python" >/dev/null 2>&1
    else
        "${VENV_DIR}/bin/python" -m pip --version >/dev/null 2>&1
    fi
}

if ! venv_is_usable; then
    if [[ -d "${VENV_DIR}" ]]; then
        warn "The existing virtual environment is incomplete or uses an unsupported Python version; rebuilding it."
    fi
    if command -v uv >/dev/null 2>&1; then
        info "Creating the Python environment with uv"
        uv venv --clear "${VENV_DIR}" --python "${PYTHON_BIN}" >/dev/null
    else
        info "Creating the Python environment with venv"
        "${PYTHON_BIN}" -m venv --clear "${VENV_DIR}" || \
            die "Unable to create a virtual environment. On Debian/Ubuntu, install python3.11-venv (or python3-venv) and run this command again."
    fi
fi

VENV_PYTHON="${VENV_DIR}/bin/python"
if command -v uv >/dev/null 2>&1; then
    pip_install() {
        uv pip install --python "${VENV_PYTHON}" "$@"
    }
else
    "${VENV_PYTHON}" -m pip install --quiet --upgrade pip
    pip_install() {
        "${VENV_PYTHON}" -m pip install --quiet "$@"
    }
fi

cd "${SOURCE_DIR}"

info "Installing Python dependencies. This can take several minutes."
pip_install -r requirements.txt
info "Installing Agent Skills Python dependencies"
pip_install -r docker/requirements-script-runner.txt
pip_install --no-deps -e .

SKILL_NODE_DIR="${HUGAGENT_DATA_DIR}/node"
PLAYWRIGHT_BROWSER_DIR="${SKILL_NODE_DIR}/browsers"
info "Installing Agent Skills Node.js dependencies"
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install --silent --no-audit --no-fund \
    --no-package-lock --prefix "${SKILL_NODE_DIR}" pptxgenjs playwright
if PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSER_DIR}" \
    "${SKILL_NODE_DIR}/node_modules/.bin/playwright" install chromium; then
    info "Chromium for PDF rendering is ready"
else
    warn "Chromium download failed. Word, Excel, and PPT generation still work; advanced PDF cover rendering will use its fallback."
fi

info "Installing optional local knowledge-base support"
pip_install milvus-lite || warn "milvus-lite is unavailable on this platform. Vector knowledge bases will remain disabled."

info "Building the web application"
(
    cd src/frontend
    npm install --silent --no-audit --no-fund --no-package-lock
    BUILD_NODE_OPTIONS="${NODE_OPTIONS:-}"
    if [[ "${BUILD_NODE_OPTIONS}" != *"--max-old-space-size="* ]]; then
        BUILD_NODE_OPTIONS="${BUILD_NODE_OPTIONS} --max-old-space-size=6144"
    fi
    VITE_EDITION=ce VITE_DEFAULT_LANGUAGE=en NODE_OPTIONS="${BUILD_NODE_OPTIONS# }" npm run build
)

HUGAGENT_BIN="${VENV_DIR}/bin/hugagent"
[[ -x "${HUGAGENT_BIN}" ]] || die "The HugAgentOS command was not installed correctly."

printf '\n'
info "HugAgentOS is installed."
printf '  Source: %s\n' "${SOURCE_DIR}"
printf '  Data:   %s\n' "${HUGAGENT_DATA_DIR}"
printf '  Start:  %s\n' "${HUGAGENT_BIN}"
printf '\n'
info "Starting HugAgentOS"

# The CE server seeds admin/admin on a fresh data directory and requires a
# password change immediately after sign-in. Model providers are configured in
# Settings, so the one-command path does not require an interactive wizard.
if [[ -t 1 && -r /dev/tty ]]; then
    exec "${HUGAGENT_BIN}" serve </dev/tty
fi
exec "${HUGAGENT_BIN}" serve
