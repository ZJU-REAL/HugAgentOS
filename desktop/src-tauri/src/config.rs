//! 桌面客户端运行时配置。
//!
//! 解析优先级：`<应用配置目录>/server.json`  >  环境变量 `HUGAGENT_SERVER_BASE`
//! >  编译期默认值。把「服务器地址」做成运行时可配，是为了一个 .exe 通吃
//!  / HugAgentOS / 私有化多环境（见桌面方案 §阶段二）。

use serde::{Deserialize, Serialize};
use std::path::Path;

// 编译期默认后端地址：真源在 `brand.rs`（可用构建时环境变量 JX_DEFAULT_SERVER_BASE
// 覆盖）。正式分发也可再用运行时 server.json / HUGAGENT_SERVER_BASE 覆盖。
use crate::brand::DEFAULT_SERVER_BASE;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AppConfig {
    /// 后端根地址，例如 `https://agent.example.gov.cn`（不带尾斜杠也可）。
    /// 本地反代把 `/api/*` 转发到这里，跳转登录开 `<server_base>/?desktop=1`。
    #[serde(default = "default_server_base")]
    pub server_base: String,

    /// 后端 session cookie 名（须与后端 `SESSION_COOKIE_NAME` 一致）。
    #[serde(default = "default_cookie_name")]
    pub cookie_name: String,

    /// 内网自签 HTTPS 时设为 true，跳过证书校验（仅限可信内网）。
    #[serde(default)]
    pub insecure_tls: bool,
}

fn default_server_base() -> String {
    DEFAULT_SERVER_BASE.to_string()
}

fn default_cookie_name() -> String {
    "jx_session".to_string()
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            server_base: default_server_base(),
            cookie_name: default_cookie_name(),
            insecure_tls: false,
        }
    }
}

impl AppConfig {
    pub fn server_base_trimmed(&self) -> &str {
        self.server_base.trim_end_matches('/')
    }
}

/// 加载配置：server.json 优先，其次环境变量覆盖 server_base，最后默认值。
pub fn load(config_dir: &Path) -> AppConfig {
    let mut cfg = AppConfig::default();

    let path = config_dir.join("server.json");
    if let Ok(text) = std::fs::read_to_string(&path) {
        if let Ok(parsed) = serde_json::from_str::<AppConfig>(&text) {
            cfg = parsed;
        } else {
            eprintln!("[config] server.json 解析失败，回退默认配置");
        }
    }

    if let Ok(v) = std::env::var("HUGAGENT_SERVER_BASE") {
        if !v.trim().is_empty() {
            cfg.server_base = v;
        }
    }

    cfg
}

/// 把「服务器地址」写回 `server.json`（保留已有 cookie_name / insecure_tls）。
/// 供菜单栏「设置服务器地址…」使用；写入后需重启客户端才生效（config 只在启动读一次）。
pub fn save_server_base(config_dir: &Path, server_base: &str) -> Result<(), String> {
    let mut cfg = load(config_dir);
    cfg.server_base = server_base.trim().trim_end_matches('/').to_string();
    let path = config_dir.join("server.json");
    let text = serde_json::to_string_pretty(&cfg).map_err(|e| format!("序列化失败: {e}"))?;
    std::fs::create_dir_all(config_dir).ok();
    std::fs::write(&path, text).map_err(|e| format!("写入 server.json 失败: {e}"))
}
