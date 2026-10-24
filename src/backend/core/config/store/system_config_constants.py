"""Cache lifetime and defaults used by the system config service."""
from __future__ import annotations
from core.config.edition import CONFIG_KEY_TO_ENV as EDITION_CONFIG_KEY_TO_ENV
from core.config.edition import SEED_CONFIGS as EDITION_SEED_CONFIGS

CACHE_TTL_SECONDS = 30.0


PROVIDER_SCOPED_KNOWLEDGE_FIELDS = ("url", "api_key", "allowed_dataset_ids")


SEED_CONFIGS: list[tuple[str, str | None, str, str, str, bool]] = [
    # query_database
    (
        "query_database.url",
        None,
        "数据库查询接口 URL",
        "NL2SQL 数据库查询服务地址",
        "query_database",
        False,
    ),
    ("query_database.timeout", "40", "超时时间(秒)", "请求超时时间", "query_database", False),
    ("query_database.retry_times", "1", "重试次数", "请求失败重试次数", "query_database", False),
    (
        "query_database.max_output_tokens",
        "45000",
        "最大输出 Token",
        "数据库查询最大输出 Token 数",
        "query_database",
        False,
    ),
    (
        "database_query.capability_enabled",
        "true",
        "数据库查询能力总开关",
        "控制统一「数据库查询」能力是否在能力中心可用。由数据库工具页维护，不写入 catalog.json。",
        "query_database",
        False,
    ),
    (
        "knowledge_base.detail_max_chars",
        "50000",
        "详情最大字符数",
        "知识库文档详情最大字符数",
        "knowledge_base",
        False,
    ),
    # file_parser
    ("file_parser.api_url", None, "文件解析 API URL", "PDF/文档解析服务地址", "file_parser", False),
    ("file_parser.timeout", "60", "超时时间(秒)", "文件解析请求超时", "file_parser", False),
    ("file_parser.lang_list", "ch", "语言列表", "OCR 语言列表", "file_parser", False),
    ("file_parser.backend", "pipeline", "解析后端", "解析后端引擎", "file_parser", False),
    ("file_parser.parse_method", "auto", "解析方法", "auto / ocr / txt", "file_parser", False),
    (
        "file_parser.formula_enable",
        "true",
        "启用公式识别",
        "是否启用公式识别",
        "file_parser",
        False,
    ),
    ("file_parser.table_enable", "true", "启用表格识别", "是否启用表格识别", "file_parser", False),
    # internet_search
    (
        "internet_search.engine",
        "tavily",
        "搜索引擎",
        "互联网搜索引擎 (tavily / baidu / langsearch / deepseek)",
        "internet_search",
        False,
    ),
    (
        "internet_search.tavily_api_key",
        None,
        "Tavily API Key",
        "Tavily 互联网搜索服务密钥",
        "internet_search",
        True,
    ),
    (
        "internet_search.baidu_api_key",
        None,
        "百度搜索 API Key",
        "百度千帆 AppBuilder 搜索服务密钥",
        "internet_search",
        True,
    ),
    (
        "internet_search.langsearch_api_key",
        None,
        "LangSearch API Key",
        "LangSearch Web Search API 服务密钥",
        "internet_search",
        True,
    ),
    (
        "internet_search.deepseek_api_key",
        None,
        "DeepSeek Search API Key",
        "DeepSeek Search API 服务密钥（DeepSeek 开放平台 API Key）",
        "internet_search",
        True,
    ),
    # sandbox / code execution. The admin toggle is the **sole authority** (no env fallback):
    # explicitly defaults to "true"; admins can turn it off in "System config", which writes "false" and persists.
    (
        "sandbox.code_capability_enable",
        "true",
        "主智能体代码执行能力",
        "开启后主/计划/批量/子智能体默认具备文件工具+沙箱+我的空间访问；关闭则仅 Lab "
        "入口可用。本开关为唯一控制源，保存后 ≤30s 生效、无需重启。",
        "sandbox",
        False,
    ),
    # ontology —— independent enterprise governance gate for plugin imports.
    # Unlike the user's ontology preference, this switch cannot be bypassed by
    # turning off ontology validation in personal settings.
    (
        "ontology.force_plugin_import_build_validation",
        "false",
        "强制构建时本体校验",
        "插件导入或安装时必须通过激活 Domain Pack 的构建校验；个人本体开关不能关闭此门禁。",
        "ontology",
        False,
    ),
    # context —— the single compaction trigger ratio. Read per turn by
    # compaction_service.resolve_trigger_ratio and applied by every phase (mid-turn at the ReAct step
    # boundary, pre-turn before a turn samples, post-turn background pre-warm) — it is no longer an
    # "in-turn only" knob competing with CHAT_COMPACT_TRIGGER_RATIO. Admin config takes precedence over
    # env; takes effect on new conversations after saving, no restart needed.
    # ⚠️ Grouped under context, not chat: the chat group is in service_configs._HIDDEN_GROUPS
    # (chat.user_model_switch_enabled is exclusively managed by the model management panel to prevent a double entry point),
    # so attaching to the chat group would hide the whole group in the system config UI.
    (
        "chat.compress_in_turn_ratio",
        "0.90",
        "轮内压缩触发比例",
        "上下文占用超过「模型窗口 × 该值」时触发压缩（0.5~0.90）。计数优先用模型返回的"
        "真实 token 用量，拿不到时才退回字节估算，两者都不含模型输出长度——输出余量已由"
        "「窗口 95% 可用」统一预留。调大=更晚压缩（上下文更完整）；调小=更早压缩。"
        "该比例对轮内、轮前、轮末三个时机统一生效。",
        "context",
        False,
    ),
    # dingtalk —— DingTalk workbench Custom App (enterprise self-built app) credentials. Once AppKey/AppSecret are filled,
    # DingTalk login and the dws command inside the sandbox execute as that self-built app, bypassing DingTalk's co-creation-era
    # "CLI data access" org toggle and self-controlling scope; leave empty to use the dws default shared app (org admin must
    # enable CLI data access first). Sole authority = admin config (no env fallback), takes effect ≤30s after saving, no restart.
    (
        "dingtalk.client_id",
        None,
        "钉钉 AppKey (client-id)",
        "企业自建钉钉应用的 AppKey。在 open-dev.dingtalk.com 创建企业内部应用后获取。",
        "dingtalk",
        True,
    ),
    (
        "dingtalk.client_secret",
        None,
        "钉钉 AppSecret (client-secret)",
        "企业自建钉钉应用的 AppSecret，与 AppKey 配对。",
        "dingtalk",
        True,
    ),
    (
        "dingtalk.trusted_domains",
        "*.dingtalk.com",
        "受信域名白名单",
        "dws bearer token 允许发送的域名，逗号分隔，默认锁死 *.dingtalk.com 防 token 外泄。",
        "dingtalk",
        False,
    ),
    # firecrawl —— deployment-level credentials for the firecrawl web scraping/retrieval CLI. The firecrawl CLI is preinstalled
    # in the sandbox; at runtime these two items are injected as FIRECRAWL_API_KEY / FIRECRAWL_API_URL env vars into
    # every user's sandbox commands (one shared config for all users, mirroring the DingTalk dws Custom App approach).
    # - Cloud version: fill only api_key (starts with fc-).
    # - Self-hosted: fill api_url pointing to the self-built instance (the CLI skips cloud auth once it sees api_url); if the
    #   self-built instance also needs a key, fill both.
    # Sole authority = admin config (no env fallback), takes effect ≤30s after saving, no restart.
    (
        "firecrawl.api_key",
        None,
        "Firecrawl API Key",
        "firecrawl 云版 API Key（fc- 开头），在 firecrawl.dev 注册获取。用自托管实例时可留空。",
        "firecrawl",
        True,
    ),
    (
        "firecrawl.api_url",
        None,
        "Firecrawl 自托管地址",
        "自托管 firecrawl 实例地址（如 http://firecrawl:3002）。填了即走自建实例、CLI 跳过云端鉴权；用云版时留空。",
        "firecrawl",
        False,
    ),
    # lark —— Feishu workbench app (enterprise self-built app) credentials. Once AppID/AppSecret are filled, the lark-cli inside
    # the sandbox uses that self-built app as its OAuth app: admin configures once, and each user completes the device-flow
    # auth login to operate Feishu as themselves. The app config is seeded by the backend into each user's lark HOME
    # (config init), not injected as a global env var — this avoids lark-cli defaulting to the tenant/bot token flow when
    # LARK_APP_* is in the environment, which would break --as user. Sole authority = admin config, ≤30s after saving, no restart.
    (
        "lark.app_id",
        None,
        "飞书 App ID",
        "企业自建飞书应用的 App ID。在 open.feishu.cn 创建企业自建应用后获取。",
        "lark",
        True,
    ),
    (
        "lark.app_secret",
        None,
        "飞书 App Secret",
        "企业自建飞书应用的 App Secret，与 App ID 配对。",
        "lark",
        True,
    ),
    # auth —— login and session. Number of days a session is kept after the user checks "remember login state" (checked by
    # default) on the login page. Sole authority = admin config (no env fallback), takes effect ≤30s after saving, no restart;
    # if unchecked, the default session duration (SESSION_TTL_HOURS) is used.
    (
        "auth.remember_me_days",
        "30",
        "记住登录状态时长(天)",
        "用户在登录页勾选「记住登录状态」后，登录会话保持的天数（默认 30 天）。"
        "未勾选则使用默认会话时长。",
        "auth",
        False,
    ),
    # turbo —— 极速模式（检索速查）。工具集独立于能力目录（catalog）的启停状态：
    # 极速模式装配的 MCP 以本组配置为唯一来源，管理台改动 ≤30s 生效、无需重启。
    (
        "turbo.mcp_server_ids",
        "retrieve_dataset_content,internet_search,web_fetch",
        "极速模式可用工具",
        "极速模式下装配的 MCP 工具集合（逗号分隔的 server id）。"
        "该集合不受「能力目录」中 MCP/技能/智能体/插件启停的影响，是极速模式工具面的唯一来源。",
        "turbo",
        False,
    ),
    (
        "turbo.skill_ids",
        "",
        "极速模式可用技能",
        "极速模式下装配的技能集合（逗号分隔的 skill id）。"
        "极速模式没有 Bash / 沙箱 / 文件写入，选纯文本类技能（写作模板、话术规范等）效果最好；"
        "需要代码执行的技能只会被读到说明、无法执行。同样不受「能力目录」启停影响。",
        "turbo",
        False,
    ),
    (
        "turbo.plugin_ids",
        "",
        "极速模式可用插件",
        "极速模式下装配的插件集合（逗号分隔的 install_id）。"
        "插件是「技能 + 工具」的能力包，选中后其全部工具与技能一并在极速模式生效；"
        "与上面的 MCP 工具集合合并去重，同样不受「能力目录」启停影响。",
        "turbo",
        False,
    ),
    (
        "turbo.manual_invoke_enable",
        "true",
        "允许手动呼唤能力",
        "开启后，用户在极速模式对话框中通过斜杠命令呼唤技能/插件、@ 呼唤子智能体时，"
        "被呼唤的能力将临时装配到本次请求（仅显式呼唤时可用，不会把全量能力清单注入提示词）。"
        "关闭后极速模式严格只用上面的工具集合。",
        "turbo",
        False,
    ),
    (
        "turbo.max_iters",
        "4",
        "迭代轮数上限",
        "极速模式单次回答的最大 ReAct 迭代轮数（含最终作答轮）。"
        "上限越小响应越快；默认 4 对应「最多 1-2 轮并行检索 + 直接作答」的产品契约。",
        "turbo",
        False,
    ),
] + EDITION_SEED_CONFIGS


CONFIG_KEY_TO_ENV: dict[str, str] = {
    "query_database.url": "QUERY_DATABASE_URL",
    "query_database.timeout": "QUERY_DATABASE_TIMEOUT_SECONDS",
    "query_database.retry_times": "QUERY_DATABASE_RETRY_TIMES",
    "query_database.max_output_tokens": "QUERY_DATABASE_MAX_OUTPUT_TOKENS",
    "knowledge_base.detail_max_chars": "KB_DETAIL_CONTENT_MAX_CHARS",
    "file_parser.api_url": "FILE_PARSER_API_URL",
    "file_parser.timeout": "FILE_PARSER_TIMEOUT",
    "file_parser.lang_list": "FILE_PARSER_LANG_LIST",
    "file_parser.backend": "FILE_PARSER_BACKEND",
    "file_parser.parse_method": "FILE_PARSER_PARSE_METHOD",
    "file_parser.formula_enable": "FILE_PARSER_FORMULA_ENABLE",
    "file_parser.table_enable": "FILE_PARSER_TABLE_ENABLE",
    "internet_search.engine": "INTERNET_SEARCH_ENGINE",
    "internet_search.tavily_api_key": "TAVILY_API_KEY",
    "internet_search.baidu_api_key": "BAIDU_API_KEY",
    "internet_search.langsearch_api_key": "LANGSEARCH_API_KEY",
    "internet_search.deepseek_api_key": "DEEPSEEK_SEARCH_API_KEY",
    # Note: sandbox.code_capability_enable deliberately does **not** map to an env var —— the admin toggle is the sole
    # authority (plan B), explicitly seeded to "true", and env CODE_CAPABILITY_ENABLED is retired.
    **EDITION_CONFIG_KEY_TO_ENV,
}


ENV_TO_CONFIG_KEY: dict[str, str] = {v: k for k, v in CONFIG_KEY_TO_ENV.items()}


DEFAULT_TURBO_MCP_SERVER_IDS = frozenset(
    {"retrieve_dataset_content", "internet_search", "web_fetch"}
)
