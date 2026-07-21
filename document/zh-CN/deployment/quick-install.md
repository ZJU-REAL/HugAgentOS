# 无 Docker 一键安装（本地单机）

> 最后更新：2026-07-21 ｜ [English](../../en/deployment/quick-install.md) ｜ 返回 [部署指南](README.md)

面向**个人单机尝鲜**与**二次开发体验**的极简部署方式：一条命令装好，终端引导设管理员、配模型，随后单进程起服务并打开浏览器。全程**零 Docker、零 PostgreSQL、零 Redis**。

技术形态：单进程 uvicorn（同时托管前端静态资源与 API）+ SQLite + 进程内 fakeredis + 子进程 MCP / 沙箱。数据全部落在 `~/.hugagent/`。

> ⚠️ **定位说明**：本方式是**单进程单用户**形态，为个人试用与开发体验而设，**不适合多人协作或生产**。团队 / 生产请用 [Docker Compose 部署](docker-compose.md)。两种形态并存，互不影响。

## 适用场景

| 适合 | 不适合 |
|---|---|
| 个人在自己机器上快速试用 | 多用户 / 团队协作（内存会话、SQLite 单写） |
| 二次开发时低成本跑起整栈 | 生产环境（无容器隔离、无高可用） |
| 没有 Docker 环境、只想尝鲜 | 需要持久沙箱、L3 图谱记忆等重能力 |

## 前置条件

| 项 | 要求 |
|---|---|
| 操作系统 | Linux / macOS；Windows 个人用户优先用[桌面端本机服务一键安装](windows-deployment.md)，命令行安装仍在 WSL2 内执行 |
| Python | ≥ 3.11 |
| Node.js | ≥ 20（公开安装器会在本机构建前端） |
| Rust 与 Cargo | Linux 没有兼容 `ripgrep` 预编译 wheel 时需要，包括 x86_64 且 glibc 低于 2.39 的系统 |
| 网络 | 能访问所配置的大模型 API 端点 |

## 安装

在任意目录运行公开安装器：

```bash
curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash
```

安装脚本会：

1. 校验 Python ≥ 3.11、Node.js ≥ 20、npm、Git，以及 Linux 需要源码构建 `ripgrep` 时所需的 Rust；
2. 把 HugAgentOS 克隆或快进更新到 `~/.hugagent/source`；
3. 检测可选的 LibreOffice；缺失时说明不可用能力并询问是否立即安装，跳过或安装失败不会阻断其余功能；
4. 在 `~/.hugagent/venv` 创建虚拟环境（检测到 [uv](https://github.com/astral-sh/uv) 时使用 uv，否则使用 `python -m venv`），并自动重建上次中断留下的不完整环境；
5. 安装 `requirements.txt`、`hugagent` 控制台命令、内置 Agent Skills 的 Python/Node.js 依赖，以及可选的本地知识库依赖；
6. 把前端构建到 `src/frontend/dist`；
7. 进入交互式首次配置向导。

> 把命令加入 PATH 方便日常使用：`export PATH="$HOME/.hugagent/venv/bin:$PATH"`。

## 首次引导（onboard）

引导全部在终端完成，依次：

**第 1 步 · 管理员账号**——全新 CE 数据目录默认只创建一个本地管理员，账号和初始密码均为 `admin`，首次登录必须修改密码。登录页不提供注册功能，后端也会拒绝注册请求。

**第 2 步 · 配置模型**——选择预设厂商（DeepSeek / OpenAI / Moonshot / Qwen / Ollama）或自定义 OpenAI 兼容端点，填入 `base_url` / 模型名 / `api_key` / 上下文窗口。上下文窗口保守默认 32,768 tokens，并以 `context_length` 持久化；请按模型端点真实支持值调整。引导会**实测一次连通性**（真实调用 `/chat/completions`），失败即时报错，可重配。配好的模型会指派给全部对话角色（主智能体、摘要、追问、计划、代码执行等）。

配好的对话模型会一次指派给全部 7 个对话角色。除此之外还有两类可单独配置的模型（都可跳过）：

**第 2b 步（可选）· 向量 / 索引模型（embedding）**——用于**自建向量知识库**检索与 L2 记忆向量化。填 embedding 端点（`base_url` / 模型名 / `api_key`）。直接回车可跳过，但知识库和永久记忆都会保持关闭；之后可在网页「设置 → 模型服务」补配并指派 embedding 角色。向量存储用嵌入式 **Milvus Lite**（单文件落 `~/.hugagent/milvus.db`，无需服务端）。

**第 2c 步（可选）· 重排模型（reranker）**——对知识库混合检索结果做重排增强，检索更准。填 reranker 端点（`base_url` / 模型名 / `api_key`），直接回车跳过（不配则检索照常、只是不重排）。

> HugAgentOS 共有 9 个模型角色：7 个对话角色（主智能体 / 摘要 / 追问 / 记忆 / 图表 / 计划 / 代码执行，共用上面的对话模型）+ 向量（embedding）+ 重排（reranker）。onboard 覆盖全部三类；登录后还可在网页「设置 → 系统管理 → 模型服务」为单个角色指派不同模型。

**第 3 步 · 选择插件**——从内置插件列表勾选要安装的能力（序号逗号分隔 / `all` 全装 / `none` 跳过；直接回车装 ★ 推荐项）。默认推荐：`automation`（定时任务）、`skill-manager`（技能管理）、`sites`（对话建站）。装 `sites` 时会自动铺入 React 建站工程模板。插件随后可在插件市场随时增减。

**第 4 步（可选）· 配置文件解析服务**——上传 PDF / 扫描件解析需要一个外部解析服务（MinerU 兼容），填入其 API URL 即可（写入 `file_parser.api_url`）；直接回车跳过。Excel / CSV / PPTX / 文本为进程内解析，无需此项。

**第 5 步（可选）· 配置互联网搜索**——智能体联网检索需要一个搜索引擎密钥：选 `tavily`（默认，[tavily.com](https://tavily.com) 注册取 key）或 `baidu`（千帆 AppBuilder），填入 API Key；直接回车跳过。事后也可在网页「设置 → 系统管理 → 服务配置」里补配。

引导末尾会打印**本机能力概览**（Node.js / pandoc / LibreOffice 是否就绪，对应 React 建站 / Word 转换 / PPT 和 Word 在线预览），随后自动起服务并打开 `http://127.0.0.1:3001/`。

> **警告：** 服务默认只监听 `127.0.0.1`。若服务器确实需要接受远程连接，请使用
> `hugagent serve --host 0.0.0.0 --port 3001 --no-browser`，并先配置强管理员密码、
> 防火墙和 HTTPS。不要在不受信任的网络中直接暴露服务。

### 非交互安装（自动化 / CI）

`onboard` 支持旁路参数，用于脚本化安装：

```bash
hugagent onboard \
  --username admin --password '<强密码>' \
  --model-base-url https://api.deepseek.com/v1 \
  --model-api-key '<your-key>' --model-name deepseek-chat \
  --model-context-length 32768 \
  --embed-base-url https://<embed>/v1 --embed-model bge-m3 --embed-api-key '<key>' \  # 可选，向量/索引模型
  --reranker-base-url https://<rerank> --reranker-model bge-reranker --reranker-api-key '<key>' \  # 可选，重排模型
  --plugins automation,skill-manager,sites \  # 逗号分隔 slug / all / none / default
  --file-parser-url http://<解析服务>/parse \  # 可选，PDF/文档解析
  --search-engine tavily --search-api-key '<key>' \  # 可选，互联网搜索
  --no-serve            # 初始化后不自动起服务
# 可选：--no-test 跳过所有模型连通性实测
```

### 装好之后：网页端「系统管理」

登录后点击头像进入**设置**，管理员（`super_admin`）会看到「系统管理」分区，onboard 里配过的东西都能在这里改，不必重跑引导：

- **模型服务**——模型供应商增删改、连通性测试、按角色指派（对应 onboard 第 2/2b/2c 步）；
- **服务配置**——互联网搜索引擎与密钥、文件解析服务、知识库服务、沙箱与上下文开关（对应第 4/5 步），保存后约 30 秒生效、无需重启；
- **我的日志**——本人的工具 / 技能 / 子智能体调用日志与模型 Token 用量。

## 日常使用

```bash
hugagent            # 已初始化 → 起服务并打开浏览器；未初始化 → 自动进引导
hugagent serve      # 显式起服务（--host 改监听地址，--port 改端口，--no-browser 不开浏览器）
hugagent onboard    # 重跑引导 / 改配置
hugagent doctor     # 环境自检（Python 版本、端口占用、数据目录、前端构建、依赖等）
```

## 数据目录

所有状态集中在 `~/.hugagent/`（可用环境变量 `HUGAGENT_HOME` 改位置）：

| 路径 | 内容 |
|---|---|
| `data.db` | SQLite 数据库（业务数据、账号、模型配置、系统配置…） |
| `storage/` | 本地文件存储（我的空间、产物等） |
| `workspace/` | 沙箱工作目录（代码执行落盘处，替代容器内 `/workspace`） |
| `venv/` | 安装脚本创建的虚拟环境 |
| `node/` | PPT/PDF 等 Agent Skills 使用的本地 Node.js 包与 Chromium |
| `logs/` | 后端日志 |

> 卸载即删除 `~/.hugagent/` 目录（数据一并清除）。

## 能力边界

无 Docker 单机模式为轻量而生。下面按「开箱可用 / 需额外条件 / 不可用」说明与 Compose 版的差异。

**开箱可用**
- **核心对话 + ReAct 工具编排 + 计划模式 + 断线续播 + 引用标注**。
- **代码执行（bash / Python）**：沙箱以宿主子进程执行（无容器隔离），使用受限环境变量、执行超时和进程组回收兜底；文件工具（读/写/编辑）与产物暂存（`sandbox_put/get_artifact`）均落在 `~/.hugagent/workspace/`。信任边界是「用户在自己机器上跑自己的助手」，与多租户服务器不同。
- **内置技能**（word / excel / ppt / pdf 编辑等 5 个）：安装时同步到工作区，沙箱可直接调用其脚本。
- **内置工具型 MCP**：互联网搜索 / 网页抓取 / 批量执行 / 知识库检索等——服务本身正常运行（部分需配置对应外部服务或密钥才有数据，见下）。
- **数据可视化（图表）**：安装脚本会装 matplotlib；装上即可用。
- **项目 / 我的空间 / 产物 / 数据画布 / 自动化定时任务（网页创建与触发）/ 文档与提示词**：均在 SQLite + 本地存储上正常工作。
- **自建向量知识库**：用嵌入式 **Milvus Lite**（单文件，无需服务端），**纯向量（dense）检索**；需在 onboard 配置 embedding 模型。要更强的混合检索，把 `MILVUS_URL` 指向真正的 Milvus 服务即可自动切回。
- **L2 向量记忆**：安装器会装好 mem0 与 Milvus Lite，并默认启用记忆运行时。配置并指派可用的 embedding 模型后，用户的永久记忆与自动写入默认开启；缺少 embedding 时，前后端都会阻止打开记忆开关。

- **自动化 / 技能创作 / 建站等插件能力**：`automation` / `skill-manager` / `sites` 是**插件**，可在 onboard 第 3 步一键勾选安装（或事后到插件市场增减）；安装后其 MCP 在本地已自动连通（`http://mcp:*` 主机名会被重写到 `127.0.0.1`）。

**需额外条件**
- **对话建站的 React 工程构建**：装 `sites` 插件后即支持——onboard 把 React 工程模板铺入 `~/.hugagent/site-template/`，首次建站时按需 `npm install`。**需宿主装有 Node.js ≥ 20 + npm**；缺则只能手写静态站点。建站链路的 `/workspace` 路径已参数化到本地工作区（静态站与 Docker 版一致）。
- **办公文档转换与预览**：PPT/Word 在线预览和 Office 转 PDF 需要 LibreOffice。一键安装器检测到缺失时会说明影响并询问是否安装；选择跳过不影响文档生成、下载和其它核心功能。非交互安装可设 `HUGAGENT_INSTALL_LIBREOFFICE=1` 自动安装，或设为 `0` 明确跳过。Word 的其它转换还会用到 `pandoc`，Excel 读写仍可走 openpyxl。
- **PDF / Word 文件解析入库**：PDF 需配置外部解析服务（onboard 第 4 步填 API URL，或 `FILE_PARSER_API_URL`）；Word 需宿主 `pandoc` / `libreoffice`。Excel / CSV / PPTX / 文本为进程内解析、开箱可用。
**不可用**
- **L3 图谱记忆**：需 Neo4j，无嵌入式替代。
- **持久沙箱 / 沙盒依赖在线重建**：依赖 Docker，本地档不可用（优雅降级、不影响其余）。

**其它**
- **单用户 / 单进程**：会话存于内存，进程重启需重新登录；SQLite 不支持多 worker 并发写，**不要**以 `--workers>1` 启动。
- **升级限制**：SQLite 走 `create_all`，只建缺表、不改已有表结构；跨版本若有列变更，需导出数据后重建。

## 故障排查

| 现象 | 处理 |
|---|---|
| 起服务报端口被占用 | `hugagent serve --port <其他端口>`；或先 `hugagent doctor` 查占用 |
| 网页打开是一段 JSON 而非应用 | 前端未构建：`cd src/frontend && npm run build`，或设 `FRONTEND_DIST_DIR` 指向已构建的 `dist` |
| 登录报模型不可用 | 重跑 `hugagent onboard` 重配模型（引导会实测连通性） |
| 想换模型 / 改配置 | 重跑 `hugagent onboard`，或登录后到「设置 → 系统管理 → 模型服务 / 服务配置」调整 |
| PPT/Word 预览提示 LibreOffice 未安装 | 重新运行一键安装器并在提示时选择安装；Debian/Ubuntu 也可执行 `sudo apt-get update && sudo apt-get install -y libreoffice-impress libreoffice-writer libreoffice-calc`，然后重启 HugAgentOS |
| 技能执行反复出现 `fork: Resource temporarily unavailable` | 停止当前服务，重新运行公开安装器完成升级，再启动 `hugagent`。旧版本若留下子进程，先检查当前用户的进程，必要时注销当前登录会话后重试。 |
| 环境是否就绪 | `hugagent doctor` 一次性自检 |

## 相关源码

| 功能 | 文件 |
|---|---|
| 安装脚本 | `install.sh` |
| CLI（onboard / serve / doctor） | `src/backend/cli.py` |
| 本地模式开关 | `src/backend/core/config/settings.py`（`DeploySettings`，`DEPLOY_PROFILE=local`） |
| 前端静态托管 + `/api` 桥接 | `src/backend/api/local_hosting.py` |
| MCP / 沙箱子进程督管 | `src/backend/orchestration/local_subprocess.py` |
| 进程内 fakeredis | `src/backend/core/infra/redis.py`（`REDIS_URL=memory://`） |
| 内置 MCP 目录种子 | `src/backend/core/services/mcp_service.py`（`seed_builtin_mcp_servers_if_empty`） |
| 环境变量参考 | [environment-variables.md](environment-variables.md)（「无 Docker 本地模式」段） |
