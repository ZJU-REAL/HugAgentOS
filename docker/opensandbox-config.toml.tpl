# OpenSandbox Server 配置（单机 Docker 模式）
# 完整 schema: https://github.com/alibaba/OpenSandbox/blob/main/server/configuration.md

[server]
# 监听 0.0.0.0 让同 docker network 内的 backend 能访问
host = "0.0.0.0"
port = 8080
max_sandbox_timeout_seconds = 86400

# CE Docker initializes a shared controller/backend key.
api_key = "@@OPENSANDBOX_API_KEY@@"

[log]
level = "INFO"

[runtime]
type = "docker"
# execd 镜像版本要和 docker-compose 中预拉的对齐。
# v1.0.17 起 execd 才把命令输出里的空行发出来（上游 c1d19b7c
# "fix(execd): preserve blank lines in command stdout SSE stream"，v1.0.16 还没有）。
# 停在更早的版本，沙箱里 cat 一个靠空行分段的文本，后端收到的就是被压紧的版本，
# 空行在到达后端之前就没了，怎么拼都还原不出来。
execd_image = "opensandbox/execd:v1.0.17"

[storage]
# Bind-mount host-path 前缀白名单。
# ⚠️ 反直觉：留空 `[]` = 拒绝一切 bind mount（不是"允许任何"——POC 时的注释错了）。
# 必须显式列出允许的 host path 前缀；OpenSandbox 用 startswith 匹配。
#
# 唯一允许：把仓库内的 agent skills 目录 read-only 挂进沙盒
# /workspace/skills/，替代旧的 tar 上传 + Python 解包物化路径。
#
# The init service renders JSON-escaped TOML values for the host paths,
# controller key and explicit Compose project name. No host-specific values
# are stored in this template.
allowed_host_paths = [
    "@@HOST_REPO_PATH@@/src/backend/skill_bundles",
    # Personal mounts are selected by the authenticated user, under host storage.
    "@@HOST_STORAGE_PATH@@",
]
volume_default_size = "1Gi"

[store]
# SQLite 持久化沙盒元数据（容器重启不丢已有沙盒）
type = "sqlite"
path = "/var/lib/opensandbox/opensandbox.db"

[docker]
# 控制平面使用主项目网络。定制控制器为每个 sandbox 创建独立 bridge，
# 仅接入该 sandbox、受信任的 backend 和控制器，不发布沙盒宿主端口。
# 网络名规则：${COMPOSE_PROJECT_NAME}_${network_name_in_compose}
network_mode = "@@COMPOSE_PROJECT_NAME@@_hugagent-network"
drop_capabilities = ["AUDIT_WRITE", "MKNOD", "NET_ADMIN", "NET_RAW", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_TIME", "SYS_TTY_CONFIG"]
no_new_privileges = true
apparmor_profile = ""
pids_limit = 4096
seccomp_profile = ""

[ingress]
mode = "direct"

[egress]
image = "opensandbox/egress:v1.0.10"
mode = "dns"

[renew_intent]
# 每次执行时调用 renew-expiration，命中此最小间隔避免频繁刷库
enabled = true
min_interval_seconds = 60
