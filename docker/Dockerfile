# Multi-stage build for HugAgentOS backend

# Stage 1: Builder
FROM python:3.11-slim AS builder
RUN sed -i 's/deb.debian.org/mirrors.huaweicloud.com/g' /etc/apt/sources.list.d/debian.sources
WORKDIR /build

ENV PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=8 \
    PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple \
    PIP_TRUSTED_HOST=repo.huaweicloud.com

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Production Runtime
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
ARG DWS_VERSION=v1.0.37
RUN if curl -fsSL --retry 2 --retry-delay 2 --retry-all-errors --connect-timeout 15 --max-time 30 \
        "https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/releases/download/${DWS_VERSION}/dws-linux-amd64.tar.gz" \
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
ARG LARK_CLI_VERSION=1.0.34
RUN if curl -fsSL --retry 2 --retry-delay 2 --retry-all-errors --connect-timeout 15 --max-time 30 \
        "https://github.com/larksuite/cli/releases/download/v${LARK_CLI_VERSION}/lark-cli-${LARK_CLI_VERSION}-linux-amd64.tar.gz" \
        -o /tmp/lark-cli.tar.gz; then \
      tar -xzf /tmp/lark-cli.tar.gz -C /tmp && \
      find /tmp -maxdepth 2 -name lark-cli -type f -exec mv {} /usr/local/bin/lark-cli \; && \
      chmod +x /usr/local/bin/lark-cli && lark-cli --version; \
    else \
      echo "[backend] optional lark-cli download failed; Feishu integration will be unavailable" >&2; \
    fi && rm -f /tmp/lark-cli.tar.gz

# himalaya（电子邮箱 CLI）—— 后端以子进程跑 per-user IMAP/SMTP 连通性校验
# （himalaya folder list）。静态二进制（musl，static-pie），无运行时依赖。凭据是用户
# 自填的 config.toml，落 $STORAGE/email_cache/{uid}/，bind-mount 进沙盒。
ARG HIMALAYA_VERSION=1.2.0
RUN if curl -fsSL --retry 2 --retry-delay 2 --retry-all-errors --connect-timeout 15 --max-time 30 \
        "https://github.com/pimalaya/himalaya/releases/download/v${HIMALAYA_VERSION}/himalaya.x86_64-linux.tgz" \
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
    chown -R appuser:appuser /app/src/backend /app/storage /app/logs /app/storage/manual /app/storage/page_config && \
    rm -rf /home/appuser/.cache/matplotlib

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
