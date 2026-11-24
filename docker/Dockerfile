# Multi-stage build for the HugAgentOS backend and mcp images.
#
# Targets (docker-compose.yml selects them with `target:`):
#   production — backend API (default: last stage)
#   mcp        — every MCP server as a streamable-http process
# Both copy the same dependency tree from the shared builder stage, so a build
# of both targets installs requirements.txt once.

# Stage 1: Builder (shared Python dependencies)
FROM python:3.11-slim AS builder
RUN sed -i 's/deb.debian.org/mirrors.huaweicloud.com/g' /etc/apt/sources.list.d/debian.sources
WORKDIR /build

ENV PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=8 \
    PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple \
    PIP_TRUSTED_HOST=repo.huaweicloud.com

# C toolchain for the few dependencies that ship sdists on some architectures.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# The BuildKit cache mount keeps downloaded wheels between builds, so changing
# one line in requirements.txt does not download every dependency again.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user -r requirements.txt

# Stage 2: MCP runtime
# Office document editing lives in the sandbox skills, so this image needs no
# LibreOffice / .NET / Node / Chromium. Launcher: extensions.mcp_servers._launcher
# spawns one streamable-http process per MCP server; the backend connects over HTTP.
FROM python:3.11-slim AS mcp
RUN sed -i 's/deb.debian.org/mirrors.huaweicloud.com/g' /etc/apt/sources.list.d/debian.sources

RUN useradd -m -u 1000 -s /bin/bash appuser
WORKDIR /app

# curl is the HEALTHCHECK probe.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /root/.local /home/appuser/.local

# /app/storage is shared with backend so artifacts saved by MCP servers are
# visible from both containers.
RUN mkdir -p /app/src/backend /app/storage /app/logs && \
    chown -R appuser:appuser /app/src/backend /app/storage /app/logs

ENV PATH=/home/appuser/.local/bin:$PATH
ENV PYTHONPATH=/app/src/backend
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Probe the lowest-port MCP (KB retrieval, always running). The launcher exits
# non-zero on persistent crashes, so the container itself restarts.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:9100/mcp/ || exit 1

USER appuser

# Active MCP ports; retired assignments remain reserved.
EXPOSE 9100 9102 9103 9106 9107 9108 9112 9113 9114 9115 9116 9117 9118

CMD ["python", "-m", "extensions.mcp_servers._launcher"]

# Stage 3: Backend runtime
FROM python:3.11-slim AS production
RUN sed -i 's/deb.debian.org/mirrors.huaweicloud.com/g' /etc/apt/sources.list.d/debian.sources
# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash appuser

WORKDIR /app

# Install runtime dependencies (including font support and document parsing tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    curl \
    gnupg \
    ca-certificates \
    openssh-client \
    tzdata \
    fonts-wqy-zenhei \
    fontconfig \
    pandoc \
    libreoffice-writer \
    libreoffice-impress \
    libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + compose plugin — required for the admin "rebuild sandboxes" flow,
# which shells out to `docker compose build/up` against the host daemon via
# /var/run/docker.sock (mounted in docker-compose.yml).  appuser must also be
# in a group whose GID matches the host docker socket's GID; this is handled
# at runtime via `group_add` in docker-compose.yml (DOCKER_GID env var).
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors --connect-timeout 20 \
      https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    . /etc/os-release && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
      > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# dws（DingTalk Workspace CLI）—— 后端以子进程跑 per-user 设备流 OAuth 登录
# （进程稳定存活，不受沙箱杀后台进程影响；每用户独立 HOME 隔离）。单 Go 二进制。
ARG DWS_VERSION=v1.0.39
ARG TARGETARCH
RUN if curl -fsSL --retry 2 --retry-delay 2 --retry-all-errors --connect-timeout 15 --max-time 30 \
        "https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/releases/download/${DWS_VERSION}/dws-linux-${TARGETARCH:-amd64}.tar.gz" \
        -o /tmp/dws.tar.gz; then \
      tar -xzf /tmp/dws.tar.gz -C /usr/local/bin dws && \
      chmod +x /usr/local/bin/dws && dws --version; \
    else \
      echo "[backend] optional dws download failed; DingTalk integration will be unavailable" >&2; \
    fi && rm -f /tmp/dws.tar.gz

# lark-cli（Feishu/Lark CLI）—— 后端以子进程跑 per-user 扫码设备流 OAuth 登录
# （config init 写应用配置 + auth login 落 user_access_token；每用户独立 HOME 隔离）。
# 后端取**独立的静态 Go 二进制**（GitHub release tarball），无需在 slim 镜像里装 node
# ——沙箱镜像才用 npm 全局装。凭据落 $STORAGE/lark_cache/{uid}/，bind-mount 进沙盒。
# ⚠️ 必须与三个沙箱镜像（Dockerfile.opensandbox / .script-runner / .cube-sandbox）里
# npm 安装的 @larksuite/cli 版本一致——后端写凭据、沙箱读凭据，后端还要解析 `auth status`
# 的输出格式。曾经后端锁 1.0.34、沙箱浮动到 1.0.78，两边行为不一致且无人察觉。
# 所有镜像的 CLI 版本以 docker/tool-versions.env 为准，CI 用 scripts/check_tool_versions.py 校验。
ARG LARK_CLI_VERSION=1.0.78
RUN if curl -fsSL --retry 2 --retry-delay 2 --retry-all-errors --connect-timeout 15 --max-time 30 \
        "https://github.com/larksuite/cli/releases/download/v${LARK_CLI_VERSION}/lark-cli-${LARK_CLI_VERSION}-linux-${TARGETARCH:-amd64}.tar.gz" \
        -o /tmp/lark-cli.tar.gz; then \
      tar -xzf /tmp/lark-cli.tar.gz -C /tmp && \
      find /tmp -maxdepth 2 -name lark-cli -type f -exec mv {} /usr/local/bin/lark-cli \; && \
      chmod +x /usr/local/bin/lark-cli && lark-cli --version; \
    else \
      echo "[backend] required lark-cli download failed; refusing to build a backend without Feishu login support" >&2; \
      exit 1; \
    fi && rm -f /tmp/lark-cli.tar.gz

# himalaya（电子邮箱 CLI）—— 后端以子进程跑 per-user IMAP/SMTP 连通性校验
# （himalaya folder list）。静态二进制（musl，static-pie），无运行时依赖。凭据是用户
# 自填的 config.toml，落 $STORAGE/email_cache/{uid}/，bind-mount 进沙盒。
ARG HIMALAYA_VERSION=1.2.0
RUN HIMALAYA_ARCH="$([ "${TARGETARCH:-amd64}" = "arm64" ] && echo aarch64 || echo x86_64)" && \
    if curl -fsSL --retry 2 --retry-delay 2 --retry-all-errors --connect-timeout 15 --max-time 30 \
        "https://github.com/pimalaya/himalaya/releases/download/v${HIMALAYA_VERSION}/himalaya.${HIMALAYA_ARCH}-linux.tgz" \
        -o /tmp/himalaya.tgz; then \
      tar -xzf /tmp/himalaya.tgz -C /usr/local/bin himalaya && \
      chmod +x /usr/local/bin/himalaya && himalaya --version; \
    else \
      echo "[backend] optional himalaya download failed; email integration will be unavailable" >&2; \
    fi && rm -f /tmp/himalaya.tgz

# Copy installed packages from builder
COPY --from=builder /root/.local /home/appuser/.local

# Prepare mount points and writable directories.
RUN mkdir -p /app/src/backend /app/storage /app/logs /app/storage/manual /app/storage/page_config && \
    chown -R appuser:appuser /app/src/backend /app/storage /app/logs /app/storage/manual /app/storage/page_config

# Set PATH for user-installed packages
ENV PATH=/home/appuser/.local/bin:$PATH
ENV PYTHONPATH=/app/src/backend
ENV TZ=Asia/Shanghai

# Set container timezone to Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ >/etc/timezone

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:3001/health || exit 1

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 3001

# Start command
CMD ["bash", "/app/src/backend/scripts/backend_entrypoint.sh"]
