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
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "Python 3.10 or later is required."
"${PYTHON_BIN}" - <<'PYTHON_CHECK' || die "Python 3.10 or later is required."
import sys

raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PYTHON_CHECK

command -v node >/dev/null 2>&1 || die "Node.js 20 or later is required to build the web application."
command -v npm >/dev/null 2>&1 || die "npm is required to build the web application."
NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
[[ "${NODE_MAJOR}" =~ ^[0-9]+$ ]] || die "Unable to determine the Node.js version."
(( NODE_MAJOR >= 20 )) || die "Node.js 20 or later is required. Found $(node --version)."

info "Python: $("${PYTHON_BIN}" --version 2>&1)"
info "Node.js: $(node --version)"

mkdir -p "${HUGAGENT_DATA_DIR}"

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

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    if command -v uv >/dev/null 2>&1; then
        info "Creating the Python environment with uv"
        uv venv "${VENV_DIR}" --python "${PYTHON_BIN}" >/dev/null
    else
        info "Creating the Python environment with venv"
        "${PYTHON_BIN}" -m venv "${VENV_DIR}" || die "Unable to create a virtual environment. Install the Python venv package."
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
pip_install --no-deps -e .

info "Installing optional local knowledge-base support"
pip_install milvus-lite || warn "milvus-lite is unavailable on this platform. Vector knowledge bases will remain disabled."

info "Installing optional chart support"
pip_install matplotlib || warn "matplotlib installation failed. Chart generation will remain disabled."

info "Building the web application"
(
    cd src/frontend
    npm install --silent --no-audit --no-fund --no-package-lock
    npm run build
)

HUGAGENT_BIN="${VENV_DIR}/bin/hugagent"
[[ -x "${HUGAGENT_BIN}" ]] || die "The HugAgentOS command was not installed correctly."

printf '\n'
info "HugAgentOS is installed."
printf '  Source: %s\n' "${SOURCE_DIR}"
printf '  Data:   %s\n' "${HUGAGENT_DATA_DIR}"
printf '  Start:  %s\n' "${HUGAGENT_BIN}"
printf '\n'
info "Starting the first-run setup"

# Keep the interactive wizard connected to the terminal when this script is
# executed through a curl-to-bash pipeline.
if [[ -r /dev/tty ]]; then
    exec "${HUGAGENT_BIN}" </dev/tty
fi
exec "${HUGAGENT_BIN}"
