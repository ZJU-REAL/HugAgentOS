"""Credential-free platform-curated MCP marketplace templates.

These rows are discovery templates, not globally enabled MCP connections.  A
user installs one with their own credential (or personal hosted endpoint), and
the backend discovers the live tool list before persisting the private server.
"""

from __future__ import annotations

from typing import Any, Dict, List


def _tool(
    name: str,
    description: str,
    properties: Dict[str, Any] | None = None,
    required: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
        },
    }


# ``risk_report.discovery_mode = per_install`` means the examples below are
# descriptive only.  The authenticated endpoint is probed on every install and
# the concrete private MCP instance keeps the tools actually returned for that
# user/token.  This is necessary because official providers can vary tools by
# account scope, server version, or enabled product.
CURATED_MCP_MARKET_ITEMS: List[Dict[str, Any]] = [
    {
        "slug": "amap-maps",
        "display_name": "高德地图 MCP",
        "description": "高德官方远程 MCP，提供地理编码、POI、天气、距离计算及驾车、公交、步行和骑行路线规划。",
        "user_intro": "适合位置查询、区域调研、出行规划和政务服务场景。安装时填写你自己的高德 Web 服务 API Key。",
        "category": "信息检索",
        "tags": ["高德", "地图", "POI", "天气", "路线规划", "官方"],
        "icon": "/home/mcp/internet.svg",
        "publisher_name": "高德开放平台",
        "version": "official-2026.08",
        "transport": "streamable_http",
        "url": "https://mcp.amap.com/mcp",
        "auth_schema": [
            {
                "key": "AMAP_API_KEY",
                "label": "高德 Web 服务 API Key",
                "target": "query",
                "name": "key",
                "required": True,
                "secret": True,
                "placeholder": "请输入高德开放平台 Key",
                "help_text": "Key 只加密保存到你的个人安装实例，运行时注入 ?key=，不会写入市场 URL。",
                "doc_url": "https://developer.amap.com/api/mcp-server/gettingstarted",
            }
        ],
        "auth_config": {"default_method": "token", "methods": [{"id": "token", "type": "token", "label": "API Key"}]},
        "risk_level": "low",
        "tools": [
            _tool("maps_geo", "将结构化地址转换为经纬度坐标。", {"address": {"type": "string"}}, ["address"]),
            _tool("maps_regeocode", "将经纬度坐标转换为结构化地址。", {"location": {"type": "string"}}, ["location"]),
            _tool("maps_text_search", "按关键词搜索兴趣点（POI）。", {"keywords": {"type": "string"}}, ["keywords"]),
            _tool("maps_weather", "查询城市实时天气和天气预报。", {"city": {"type": "string"}}, ["city"]),
            _tool("maps_direction_driving", "规划驾车路线。"),
            _tool("maps_direction_transit_integrated", "规划公共交通路线。"),
        ],
        "install_notice": "请使用高德开放平台创建的 Web 服务 Key，并按业务范围限制配额和来源。",
        "docs_url": "https://developer.amap.com/api/mcp-server/summary",
    },
    {
        "slug": "metaso-search",
        "display_name": "秘塔搜索 MCP",
        "description": "秘塔官方远程 MCP，支持网页、文档、论文、图片、视频和播客搜索，并提供网页阅读与 RAG 问答。",
        "user_intro": "适合中文深度检索、行业研究和论文资料收集；与平台基础联网搜索互补，按需启用。",
        "category": "信息检索",
        "tags": ["秘塔", "搜索", "论文", "网页阅读", "RAG", "官方"],
        "icon": "/home/mcp/internet.svg",
        "publisher_name": "秘塔科技",
        "version": "official-2026.08",
        "transport": "streamable_http",
        "url": "https://metaso.cn/api/mcp",
        "auth_schema": [
            {
                "key": "METASO_API_KEY",
                "label": "秘塔搜索 API Key",
                "target": "header",
                "name": "Authorization",
                "prefix": "Bearer ",
                "required": True,
                "secret": True,
                "placeholder": "只填写 API Key，无需输入 Bearer",
                "help_text": "系统会自动生成 Authorization: Bearer <API Key>。",
                "doc_url": "https://metaso.cn/search-api/api-keys",
            }
        ],
        "auth_config": {"default_method": "token", "methods": [{"id": "token", "type": "token", "label": "API Key"}]},
        "risk_level": "low",
        "tools": [
            _tool(
                "metaso_web_search",
                "搜索网页、文档、论文、图片、视频或播客。",
                {"q": {"type": "string"}, "scope": {"type": "string"}, "size": {"type": "integer"}},
                ["q"],
            ),
            _tool("metaso_web_reader", "读取指定网页并返回 JSON 或 Markdown。", {"url": {"type": "string"}, "format": {"type": "string"}}, ["url", "format"]),
            _tool("metaso_chat", "基于检索增强生成回答。", {"message": {"type": "string"}}, ["message"]),
        ],
        "install_notice": "秘塔搜索与已有 internet_search/web_fetch 有能力重叠，建议只在需要论文、多媒体或深度中文检索时启用。",
        "docs_url": "https://www.modelscope.cn/mcp/servers/metasota/metaso-search",
    },
    {
        "slug": "github-official",
        "display_name": "GitHub MCP",
        "description": "GitHub 官方远程 MCP，可读取仓库、代码、Issue、Pull Request 和用户上下文，并按令牌权限执行协作操作。",
        "user_intro": "适合研发协作、代码检索、Issue/PR 分析和 CI/CD 排障。请创建最小权限、短有效期的 Fine-grained PAT。",
        "category": "研发工具",
        "tags": ["GitHub", "代码", "Issue", "Pull Request", "CI/CD", "官方"],
        "icon": "/home/mcp/source.svg",
        "publisher_name": "GitHub",
        "version": "official-2026.08",
        "transport": "streamable_http",
        "url": "https://api.githubcopilot.com/mcp/",
        "auth_schema": [
            {
                "key": "GITHUB_PAT",
                "label": "GitHub Personal Access Token",
                "target": "header",
                "name": "Authorization",
                "prefix": "Bearer ",
                "required": True,
                "secret": True,
                "placeholder": "github_pat_… 或 ghp_…",
                "help_text": "系统会自动添加 Bearer 前缀。建议使用 Fine-grained PAT 并只授权需要的仓库。",
                "doc_url": "https://github.com/settings/personal-access-tokens",
                "methods": ["token"],
            }
        ],
        "auth_config": {
            "default_method": "token",
            "methods": [
                {"id": "token", "type": "token", "label": "Personal Access Token"},
                {
                    "id": "oauth2",
                    "type": "oauth2",
                    "label": "OAuth 登录",
                    "client_registration": "manual",
                    "client_id_required": True,
                    "client_secret_required": True,
                    "scopes": [],
                    "help_text": "GitHub 官方远程 MCP 不支持动态客户端注册。请先创建 OAuth App，把回调路径设为当前站点 /api/v1/mcp-market/oauth/callback，再填写 Client ID 和 Client Secret。",
                },
            ],
        },
        "risk_level": "high",
        "tools": [
            _tool("get_file_contents", "读取仓库文件或目录内容。"),
            _tool("search_code", "搜索 GitHub 代码。"),
            _tool("issue_read", "读取 Issue 及评论。"),
            _tool("pull_request_read", "读取 Pull Request、评审和差异。"),
            _tool("issue_write", "创建或修改 Issue。"),
            _tool("create_pull_request", "创建 Pull Request。"),
        ],
        "install_notice": "GitHub 官方远程服务会按 PAT 权限暴露读写工具。请优先创建仓库级 Fine-grained PAT；安装前必须确认高风险操作。",
        "docs_url": "https://github.com/github/github-mcp-server",
    },
    {
        "slug": "gitlab-official",
        "display_name": "GitLab MCP",
        "description": "GitLab 官方远程 MCP，可访问项目、Issue、Merge Request、流水线及相关协作数据。",
        "user_intro": "适合 GitLab 项目研发协作。推荐通过浏览器 OAuth 登录；也支持粘贴已有的 mcp scope OAuth access token。",
        "category": "研发工具",
        "tags": ["GitLab", "代码", "Issue", "Merge Request", "Pipeline", "官方", "Beta"],
        "icon": "/home/mcp/source.svg",
        "publisher_name": "GitLab",
        "version": "official-beta-2026.08",
        "transport": "streamable_http",
        "url": "https://gitlab.com/api/v4/mcp",
        "auth_schema": [
            {
                "key": "GITLAB_OAUTH_ACCESS_TOKEN",
                "label": "GitLab OAuth Access Token（mcp scope）",
                "target": "header",
                "name": "Authorization",
                "prefix": "Bearer ",
                "required": True,
                "secret": True,
                "placeholder": "粘贴 OAuth access token",
                "help_text": "GitLab 官方 MCP 目前以 OAuth 为主；普通 PAT 支持仍在推进中。令牌过期后可在市场中更新凭据。",
                "doc_url": "https://docs.gitlab.com/user/model_context_protocol/mcp_server/",
                "methods": ["token"],
            }
        ],
        "auth_config": {
            "default_method": "oauth2",
            "methods": [
                {
                    "id": "oauth2",
                    "type": "oauth2",
                    "label": "OAuth 登录",
                    "client_registration": "dynamic_or_manual",
                    "scopes": ["mcp"],
                    "help_text": "通过浏览器登录 GitLab；系统使用 PKCE、MCP 元数据发现并安全保存及刷新令牌。",
                },
                {"id": "token", "type": "token", "label": "已有 OAuth Access Token"},
            ],
        },
        "risk_level": "high",
        "tools": [
            _tool("get_issue", "读取 GitLab Issue。"),
            _tool("create_issue", "创建 GitLab Issue。"),
            _tool("get_merge_request", "读取 Merge Request。"),
            _tool("create_merge_request", "创建 Merge Request。"),
            _tool("get_merge_request_pipelines", "读取 Merge Request 流水线。"),
            _tool("create_merge_request_note", "在 Merge Request 中发表评论。"),
        ],
        "install_notice": "GitLab 官方 MCP 仍为 Beta，推荐走浏览器 OAuth；也可粘贴已有的 mcp scope OAuth access token，普通 PAT 可能无法连接。",
        "docs_url": "https://docs.gitlab.com/user/model_context_protocol/mcp_server/",
    },
    {
        "slug": "alibaba-cloud-observability",
        "display_name": "阿里云可观测 MCP",
        "description": "阿里云官方可观测 MCP，可查询 SLS、CloudMonitor、Prometheus、ARMS 的日志、指标、链路和告警。",
        "user_intro": "适合企业运维和故障分析。请先在 ModelScope Hosted、函数计算或受保护环境部署服务并配置最小权限凭据，再粘贴个人 SSE 地址。",
        "category": "业务系统",
        "tags": ["阿里云", "可观测", "日志", "指标", "链路", "告警", "官方"],
        "icon": "/home/mcp/data.svg",
        "publisher_name": "阿里云",
        "version": "official-2026.08",
        "transport": "sse",
        "url": "https://mcp.api-inference.modelscope.net/placeholder/sse",
        "auth_schema": [
            {
                "key": "MCP_ENDPOINT",
                "label": "你的可观测 MCP SSE 地址",
                "target": "url",
                "required": True,
                "secret": True,
                "placeholder": "https://…/sse",
                "help_text": "请先在 ModelScope Hosted 或受保护环境中配置 AK/SK 并生成个人地址。地址会加密保存，不会进入市场记录。",
                "doc_url": "https://help.aliyun.com/en/cms/cloudmonitor-2-0/use-cases/observable-mcp-service-access-to-implement-data-query-and-analysis",
            }
        ],
        "auth_config": {"default_method": "endpoint", "methods": [{"id": "endpoint", "type": "token", "label": "个人服务地址"}]},
        "risk_level": "medium",
        "tools": [
            _tool("sls_list_projects", "列出可访问的日志项目。"),
            _tool("sls_execute_sql", "在 SLS 中执行 SQL 查询。"),
            _tool("cms_execute_promql", "执行 PromQL 指标查询。"),
            _tool("cms_natural_language_query", "用自然语言查询可观测数据。"),
            _tool("umodel_get_logs", "读取实体关联日志。"),
            _tool("umodel_search_traces", "检索调用链。"),
        ],
        "install_notice": "阿里云官方明确不建议把配置了 AK/SK 的服务无保护暴露到公网。HugAgentOS 不直接收集云 AK/SK；这里只保存你个人的受保护 SSE 地址。部分智能分析工具可能产生 STAROps 费用。",
        "docs_url": "https://help.aliyun.com/en/cms/cloudmonitor-2-0/use-cases/observable-mcp-service-access-to-implement-data-query-and-analysis",
    },
]
