//! 桌面客户端**编译期**品牌 / 环境配置（白标接缝）。
//!
//! 这里集中放「换客户就要改」的东西，全部支持用**构建时环境变量**覆盖，不改代码即可
//! 打出不同品牌 / 指向不同后端的安装包：
//!
//! ```powershell
//! $env:JX_BRAND_NAME='HugAgentOS'
//! $env:JX_DEFAULT_SERVER_BASE='https://agent.example.gov.cn'
//! $env:JX_DESKTOP_UPDATE_BASE='https://downloads.example.gov.cn'
//! $env:JX_BRAND_LOGO_URL='/home/logo.svg'
//! cargo tauri build
//! ```
//!
//! 未设环境变量时用下面的默认值（HugAgentOS）。运行时仍可再被 `<配置目录>/server.json` /
//! `HUGAGENT_SERVER_BASE` 覆盖服务器地址（见 `config.rs`）。
//!
//! 说明：
//! - **产品名 / 安装包名 / 应用图标**（.exe 图标、安装目录）由 `tauri.conf.json` 的
//!   `productName` / `identifier` / `bundle.icon` 决定，那是打包器读的静态 JSON，改品牌时
//!   用 `cargo tauri build --config <overlay.json>` 覆盖，或直接替换 `icons/` 下的图标文件。
//! - **左侧栏 / 页眉大 logo** 由后端 `page_config.branding.logo_url` 驱动（管理台可上传），
//!   桌面端只是把前端 dist 里的默认资源 `public/home/header.svg` 一起打包；换默认 logo 就替换
//!   该文件（构建前）。

/// 应用内可见品牌名：窗口标题、系统托盘、登录卡片、关闭确认框等。
pub const NAME: &str = match option_env!("JX_BRAND_NAME") {
    Some(v) => v,
    None => "HugAgentOS",
};

/// 编译期默认后端地址（运行时可被 server.json / HUGAGENT_SERVER_BASE 覆盖）。
/// 注意：这是本地开发默认，指向本机 dev（localhost:3000）；对外分发时改回正式地址，
/// 或改用构建时环境变量 JX_DEFAULT_SERVER_BASE / 运行时 server.json 覆盖。
pub const DEFAULT_SERVER_BASE: &str = match option_env!("JX_DEFAULT_SERVER_BASE") {
    Some(v) => v,
    None => "http://localhost:3000",
};

/// 桌面安装包更新源。留空时沿用编译期默认后端；本机服务模式不能从 127.0.0.1
/// 获取安装包，因此正式分发本机版时应设置本变量或 JX_DEFAULT_SERVER_BASE。
pub const DESKTOP_UPDATE_BASE: &str = match option_env!("JX_DESKTOP_UPDATE_BASE") {
    Some(v) => v,
    None => "",
};

/// 本机服务 `/health` 返回的 `service` 标识（backend `api/health.py`）。就绪判定
/// 靠它确认端口上跑的是我们的后端——两侧必须一致，否则健康检查 200 也永远
/// 不算就绪，本机模式卡在「启动超时」（HugAgentOS 分支实测踩坑）。
pub const LOCAL_SERVICE_NAME: &str = match option_env!("JX_LOCAL_SERVICE_NAME") {
    Some(v) => v,
    None => "hugagent",
};

/// 登录卡片上展示的 logo（走本地反代的静态路径，或可访问的绝对 URL）。
pub const LOGIN_LOGO_URL: &str = match option_env!("JX_BRAND_LOGO_URL") {
    Some(v) => v,
    None => "/icon.png",
};

/// 「帮助 → 访问官网」打开的地址（编译期可配）。空串则回退到当前后端地址。
pub const WEBSITE_URL: &str = match option_env!("JX_BRAND_WEBSITE_URL") {
    Some(v) => v,
    None => "",
};

const fn parse_u16_or(value: Option<&str>, fallback: u16) -> u16 {
    let Some(value) = value else {
        return fallback;
    };
    let bytes = value.as_bytes();
    if bytes.is_empty() {
        return fallback;
    }
    let mut index = 0;
    let mut parsed: u32 = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        if byte < b'0' || byte > b'9' {
            return fallback;
        }
        parsed = parsed * 10 + (byte - b'0') as u32;
        if parsed > u16::MAX as u32 {
            return fallback;
        }
        index += 1;
    }
    if parsed == 0 {
        fallback
    } else {
        parsed as u16
    }
}

const fn bytes_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut index = 0;
    while index < left.len() {
        if left[index] != right[index] {
            return false;
        }
        index += 1;
    }
    true
}

/// 构建期布尔开关：只有显式写成真值才算开（`&str` 还不能在 const 上下文里比较，逐字节比）。
const fn env_flag(value: Option<&str>) -> bool {
    let Some(value) = value else {
        return false;
    };
    let bytes = value.as_bytes();
    bytes_eq(bytes, b"1")
        || bytes_eq(bytes, b"true")
        || bytes_eq(bytes, b"TRUE")
        || bytes_eq(bytes, b"yes")
}

/// 本机服务与 sidecar 的端口命名空间，属于白标隔离的一部分。默认沿用 HugAgentOS 既有端口；
/// 换品牌打包时用构建时环境变量整体挪走，两个产品才能装在同一台机器上互不抢占（后端侧
/// 由 `SANDBOX_RUNNER_URL` 与 `HUGAGENT_LOCAL_MCP_PORT_OFFSET` 消费）。
pub const LOCAL_SERVER_PORT: u16 = parse_u16_or(option_env!("JX_LOCAL_SERVER_PORT"), 32101);
pub const LOCAL_SCRIPT_RUNNER_PORT: u16 =
    parse_u16_or(option_env!("JX_LOCAL_SCRIPT_RUNNER_PORT"), 8900);
pub const LOCAL_MCP_PORT_OFFSET: u16 = parse_u16_or(option_env!("JX_LOCAL_MCP_PORT_OFFSET"), 0);

/// 前端保存主题偏好用的 localStorage 键（真源 `src/frontend/src/theme.ts` 的
/// `THEME_STORAGE_KEY`，`index.html` 的引导脚本同键）。壳页面的主题引导脚本读同一个键，
/// **两侧必须一致**——不一致时用户显式选的深/浅色在壳页面失效，只会跟随系统外观。
pub const THEME_STORAGE_KEY: &str = match option_env!("JX_THEME_STORAGE_KEY") {
    Some(v) => v,
    None => "hugagent_theme_mode",
};

/// 「仅交付混合模式」构建开关。
///
/// 默认（不设该变量）打出的包保留三选一：首启让用户在本机 / 云端 / 双模式里挑一个。
/// 设为 `1` / `true` 时这个包只交付「本机 + 云端」双模式——首启不再问运行模式，也不再问
/// 服务器地址，直接进入带动画的初始化页，确认后在同一窗口装好本机执行面，不重启应用。
/// 云端地址取 `JX_DEFAULT_SERVER_BASE`，所以仅混合模式的包必须在构建时把它一并烤进去。
pub const HYBRID_ONLY: bool = env_flag(option_env!("JX_DESKTOP_HYBRID_ONLY"));

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn port_parser_accepts_valid_values_and_rejects_invalid_ones() {
        assert_eq!(parse_u16_or(Some("32201"), 1), 32201);
        assert_eq!(parse_u16_or(Some("0"), 9), 9);
        assert_eq!(parse_u16_or(Some("70000"), 9), 9);
        assert_eq!(parse_u16_or(Some("32x01"), 9), 9);
        assert_eq!(parse_u16_or(None, 9), 9);
    }

    #[test]
    fn hybrid_only_flag_needs_an_explicit_truthy_build_value() {
        assert!(env_flag(Some("1")));
        assert!(env_flag(Some("true")));
        assert!(!env_flag(Some("0")));
        assert!(!env_flag(Some("")));
        assert!(!env_flag(None));
    }
}
