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
