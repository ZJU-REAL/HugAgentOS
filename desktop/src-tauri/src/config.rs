//! 桌面客户端运行时配置。
//!
//! 解析优先级从高到低为：环境变量 `HUGAGENT_SERVER_BASE`、
//! `<应用配置目录>/server.json`、编译期默认值。把「服务器地址」做成运行时可配，
//! 是为了一个 .exe 通吃 HugAgentOS / HugAgentOS / 私有化多环境（见桌面方案 §阶段二）。

use serde::{Deserialize, Serialize};
use std::path::Path;

// 编译期默认后端地址：真源在 `brand.rs`（可用构建时环境变量 JX_DEFAULT_SERVER_BASE
// 覆盖）。正式分发也可再用运行时 server.json / HUGAGENT_SERVER_BASE 覆盖。
use crate::brand::{DEFAULT_SERVER_BASE, DESKTOP_UPDATE_BASE};

/// 桌面端使用远程服务，或由客户端托管本机 CE 单机服务。
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeploymentMode {
    Local,
    #[default]
    Remote,
}

/// 初始化时选定的「运行形态」（provisioning），决定客户端提供哪些能力：
///   - `LocalOnly` 本机模式：只跑本机服务，没有「我的空间」，无云端切换。
///   - `CloudOnly` 云端模式：只连云端服务器，有「我的空间」，无本机切换。
///   - `Dual`     双模式：本机 + 云端都可用，对话框里出现「本机/云端」切换。
///
/// 与 [`DeploymentMode`] 的区别：`DeploymentMode` 是**当前正在使用**的目标（双模式下
/// 可运行时切换），`ProvisionMode` 是初始化一次性选定的**能力集合**，之后基本不变。
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvisionMode {
    LocalOnly,
    #[default]
    CloudOnly,
    Dual,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AppConfig {
    /// 服务部署形态。旧版 server.json 没有该字段时保持远程瘦客户端行为。
    #[serde(default)]
    pub deployment_mode: DeploymentMode,

    /// 初始化选定的运行形态（本机 / 云端 / 双模式）。旧版 server.json 没有该字段，
    /// 此时按 `deployment_mode` 反推（Local→LocalOnly，Remote→CloudOnly）。
    #[serde(default)]
    pub provision_mode: Option<ProvisionMode>,

    /// 记住的云端服务器地址。双模式下运行时切到云端复用它，避免每次都再问一遍
    /// （这正是「点云端又要选服务器」问题的根因）。云端/双模式初始化时写入。
    #[serde(default)]
    pub cloud_server_base: String,

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
            deployment_mode: DeploymentMode::Remote,
            provision_mode: None,
            cloud_server_base: String::new(),
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

    pub fn uses_local_server(&self) -> bool {
        self.deployment_mode == DeploymentMode::Local
    }

    /// 当前运行形态。旧配置（无 `provision_mode`）按 `deployment_mode` 反推，
    /// 保证升级用户的既有单机 / 远程客户端行为不变。
    pub fn provision_mode(&self) -> ProvisionMode {
        self.provision_mode.clone().unwrap_or_else(|| match self.deployment_mode {
            DeploymentMode::Local => ProvisionMode::LocalOnly,
            DeploymentMode::Remote => ProvisionMode::CloudOnly,
        })
    }

    /// 记住的云端地址：优先用显式存下的 `cloud_server_base`；为空时，若当前就在远程
    /// 目标上则退回 `server_base`，再不行用编译期默认地址。双模式切云端时用它。
    pub fn cloud_base(&self) -> String {
        let remembered = self.cloud_server_base.trim().trim_end_matches('/');
        if !remembered.is_empty() {
            return remembered.to_string();
        }
        if self.deployment_mode == DeploymentMode::Remote {
            let base = self.server_base_trimmed();
            if !base.is_empty() {
                return base.to_string();
            }
        }
        DEFAULT_SERVER_BASE.trim().trim_end_matches('/').to_string()
    }

    /// 桌面整包更新源与业务服务地址解耦：远程模式默认跟随当前服务器；本机模式
    /// 使用构建时发布源，避免去 127.0.0.1 查询一个并不存在的安装包清单。
    pub fn update_base(&self) -> String {
        if let Ok(value) = std::env::var("HUGAGENT_UPDATE_SERVER_BASE") {
            let value = value.trim().trim_end_matches('/');
            if !value.is_empty() {
                return value.to_string();
            }
        }
        if self.uses_local_server() {
            let configured = DESKTOP_UPDATE_BASE.trim().trim_end_matches('/');
            if !configured.is_empty() {
                return configured.to_string();
            }
            return DEFAULT_SERVER_BASE.trim().trim_end_matches('/').to_string();
        }
        self.server_base_trimmed().to_string()
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
            cfg.deployment_mode = DeploymentMode::Remote;
        }
    }

    // 混合架构（Dual）恒为云端为主：登录/会话必须指向云端身份。历史版本的双模式
    // 曾默认落在本机（或经 activate-local 整体切过去），遗留 server.json 会把登录
    // 页指到本机端。启动时归一化回云端目标；本机后端只作为混合路由的执行面。
    if cfg.provision_mode() == ProvisionMode::Dual
        && cfg.deployment_mode == DeploymentMode::Local
    {
        let cloud = cfg.cloud_base();
        if !cloud.trim().is_empty() {
            cfg.deployment_mode = DeploymentMode::Remote;
            cfg.server_base = cloud;
        }
    }

    cfg
}

/// 把「服务器地址」写回 `server.json`（保留已有 cookie_name / insecure_tls）。
/// 供菜单栏「设置服务器地址…」使用；写入后需重启客户端才生效（config 只在启动读一次）。
pub fn save_server_base(config_dir: &Path, server_base: &str) -> Result<(), String> {
    let mut cfg = load(config_dir);
    cfg.deployment_mode = DeploymentMode::Remote;
    let base = server_base.trim().trim_end_matches('/').to_string();
    // 每次连云端都记住地址：双模式下运行时切回云端就不必再问服务器。
    cfg.cloud_server_base = base.clone();
    cfg.server_base = base;
    save(config_dir, &cfg)
}

/// 客户端是否已完成初始化选型。必须在 server.json 里**显式选过**运行形态，且
/// 云端 / 双模式还要有云端地址——安装器预选（只写形态）与旧版遗留配置（只有
/// deployment_mode）都不算已完成，首启会补一次初始化页（模式 / 地址已预填，
/// 确认后不再询问）。由环境变量强制服务器地址时视作已配置，直接跳过选择页。
pub fn is_provisioned(config_dir: &Path) -> bool {
    if std::env::var("HUGAGENT_SERVER_BASE")
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false)
    {
        return true;
    }
    if !config_dir.join("server.json").is_file() {
        return false;
    }
    let cfg = load(config_dir);
    match cfg.provision_mode {
        None => false,
        Some(ProvisionMode::LocalOnly) => true,
        // 云端 / 双模式：没有云端地址就还没配完（安装器预选即此状态）。
        // 只认 provision() 显式记下的 cloud_server_base——server_base 有编译期
        // 默认值，用它判断会把「预选了云端但还没填地址」误判成已配置。
        Some(_) => !cfg.cloud_server_base.trim().is_empty(),
    }
}

/// 初始化选型落盘：写入运行形态 + 记住云端地址，并按形态设定初始运行目标。
/// 双模式默认落在**本机**（初始化会顺带装好本机环境），云端随时可切且不再追问地址。
pub fn provision(
    config_dir: &Path,
    mode: ProvisionMode,
    local_base: &str,
    cloud_base: &str,
) -> Result<(), String> {
    let mut cfg = load(config_dir);
    let cloud = cloud_base.trim().trim_end_matches('/').to_string();
    if !cloud.is_empty() {
        cfg.cloud_server_base = cloud.clone();
    }
    match mode {
        ProvisionMode::LocalOnly => {
            cfg.deployment_mode = DeploymentMode::Local;
            cfg.server_base = local_base.trim().trim_end_matches('/').to_string();
        }
        ProvisionMode::CloudOnly => {
            cfg.deployment_mode = DeploymentMode::Remote;
            cfg.server_base = cfg.cloud_server_base.clone();
        }
        ProvisionMode::Dual => {
            // 云端为主（混合架构）：默认登录**云端身份**——我的空间、云端会话都在云端。
            // 本机后端作为「本地项目的执行面」常驻/按需接入（见 HYBRID_MODE_DESIGN.md），
            // 不再默认把本机当作用户体系。云端地址已记住。
            let _ = local_base; // 本机基址在混合路由里单独使用，不作为默认目标
            cfg.deployment_mode = DeploymentMode::Remote;
            cfg.server_base = cfg.cloud_server_base.clone();
        }
    }
    cfg.provision_mode = Some(mode);
    save(config_dir, &cfg)
}

fn save(config_dir: &Path, cfg: &AppConfig) -> Result<(), String> {
    let path = config_dir.join("server.json");
    let text = serde_json::to_string_pretty(cfg).map_err(|e| format!("序列化失败: {e}"))?;
    std::fs::create_dir_all(config_dir).ok();
    std::fs::write(&path, text).map_err(|e| format!("写入 server.json 失败: {e}"))
}

/// NSIS 交互安装写入的一次性选择。`install-mode` 由安装器保留，用于避免升级时
/// 重复询问；应用只消费 `.pending`，因此用户日后在菜单切换模式不会被旧选择覆盖。
/// 值即运行形态：`local`（仅本机）/ `remote`（仅云端）/ `dual`（本机+云端）。
pub fn pending_installer_mode(config_dir: &Path) -> Option<ProvisionMode> {
    let value = std::fs::read_to_string(config_dir.join("install-mode.pending")).ok()?;
    match value.trim().to_ascii_lowercase().as_str() {
        "local" => Some(ProvisionMode::LocalOnly),
        "remote" => Some(ProvisionMode::CloudOnly),
        "dual" => Some(ProvisionMode::Dual),
        _ => None,
    }
}

/// 安装器选择的「预选」落盘：只记运行形态，不写地址。云端 / 双模式还差服务器
/// 地址，`is_provisioned` 因此仍为 false —— 首启初始化页会按此预选，用户只需
/// 补地址；仅本机不需要地址，直接走完整 `provision`（不经这里）。
pub fn preselect_provision_mode(config_dir: &Path, mode: ProvisionMode) -> Result<(), String> {
    let mut cfg = load(config_dir);
    cfg.provision_mode = Some(mode);
    save(config_dir, &cfg)
}

pub fn clear_pending_installer_mode(config_dir: &Path) -> Result<(), String> {
    let path = config_dir.join("install-mode.pending");
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("清理安装器选择失败: {error}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(name: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "hugagent-desktop-config-{name}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn legacy_config_defaults_to_remote_mode() {
        let dir = temp_dir("legacy");
        std::fs::write(
            dir.join("server.json"),
            r#"{"server_base":"https://example.test","cookie_name":"jx_session"}"#,
        )
        .unwrap();

        let cfg = load(&dir);
        assert_eq!(cfg.deployment_mode, DeploymentMode::Remote);
        assert_eq!(cfg.server_base, "https://example.test");
    }

    #[test]
    fn switching_modes_is_persisted() {
        let dir = temp_dir("modes");
        provision(&dir, ProvisionMode::LocalOnly, "http://127.0.0.1:32101/", "").unwrap();
        let local = load(&dir);
        assert!(local.uses_local_server());
        assert_eq!(local.server_base, "http://127.0.0.1:32101");

        save_server_base(&dir, "https://example.test/").unwrap();
        let remote = load(&dir);
        assert!(!remote.uses_local_server());
        assert_eq!(remote.server_base, "https://example.test");
    }

    #[test]
    fn dual_mode_normalizes_stale_local_target_back_to_cloud() {
        // 历史双模式默认落本机的遗留 server.json：登录目标必须归一化回云端。
        let dir = temp_dir("dual-normalize");
        std::fs::write(
            dir.join("server.json"),
            r#"{"deployment_mode":"local","provision_mode":"dual",
                "cloud_server_base":"https://cloud.example.test",
                "server_base":"http://127.0.0.1:32101"}"#,
        )
        .unwrap();

        let cfg = load(&dir);
        assert_eq!(cfg.deployment_mode, DeploymentMode::Remote);
        assert_eq!(cfg.server_base, "https://cloud.example.test");
        assert_eq!(cfg.provision_mode(), ProvisionMode::Dual);
    }

    #[test]
    fn legacy_config_infers_provision_mode_from_deployment() {
        let dir = temp_dir("legacy-provision");
        std::fs::write(
            dir.join("server.json"),
            r#"{"deployment_mode":"local","server_base":"http://127.0.0.1:32101"}"#,
        )
        .unwrap();
        assert_eq!(load(&dir).provision_mode(), ProvisionMode::LocalOnly);

        std::fs::write(
            dir.join("server.json"),
            r#"{"server_base":"https://example.test"}"#,
        )
        .unwrap();
        assert_eq!(load(&dir).provision_mode(), ProvisionMode::CloudOnly);
    }

    #[test]
    fn provision_dual_is_cloud_primary_but_remembers_cloud() {
        let dir = temp_dir("provision-dual");
        provision(
            &dir,
            ProvisionMode::Dual,
            "http://127.0.0.1:32101",
            "https://team.example.test/",
        )
        .unwrap();
        let cfg = load(&dir);
        assert_eq!(cfg.provision_mode(), ProvisionMode::Dual);
        // 云端为主：默认落在云端身份，不默认用本机用户体系。
        assert!(!cfg.uses_local_server());
        assert_eq!(cfg.server_base, "https://team.example.test");
        assert_eq!(cfg.cloud_base(), "https://team.example.test");
    }

    #[test]
    fn provision_cloud_only_uses_given_address() {
        let dir = temp_dir("provision-cloud");
        provision(&dir, ProvisionMode::CloudOnly, "http://127.0.0.1:32101", "https://c.example.test").unwrap();
        let cfg = load(&dir);
        assert_eq!(cfg.provision_mode(), ProvisionMode::CloudOnly);
        assert!(!cfg.uses_local_server());
        assert_eq!(cfg.server_base, "https://c.example.test");
    }

    #[test]
    fn unprovisioned_dir_is_detected() {
        let dir = temp_dir("unprovisioned");
        assert!(!is_provisioned(&dir));
        provision(&dir, ProvisionMode::LocalOnly, "http://127.0.0.1:32101", "").unwrap();
        assert!(is_provisioned(&dir));
    }

    #[test]
    fn pending_installer_choice_is_one_time() {
        let dir = temp_dir("installer-choice");
        std::fs::write(dir.join("install-mode.pending"), "local").unwrap();
        assert_eq!(pending_installer_mode(&dir), Some(ProvisionMode::LocalOnly));
        clear_pending_installer_mode(&dir).unwrap();
        assert_eq!(pending_installer_mode(&dir), None);

        std::fs::write(dir.join("install-mode.pending"), "dual").unwrap();
        assert_eq!(pending_installer_mode(&dir), Some(ProvisionMode::Dual));
        std::fs::write(dir.join("install-mode.pending"), "remote").unwrap();
        assert_eq!(pending_installer_mode(&dir), Some(ProvisionMode::CloudOnly));
    }

    #[test]
    fn legacy_server_json_requires_reprovision() {
        // 旧版遗留配置只有 deployment_mode：升级后要补一次初始化页（预填确认）。
        let dir = temp_dir("legacy-reprovision");
        std::fs::write(
            dir.join("server.json"),
            r#"{"deployment_mode":"local","server_base":"http://127.0.0.1:32101"}"#,
        )
        .unwrap();
        assert!(!is_provisioned(&dir));
    }

    #[test]
    fn preselected_cloud_mode_waits_for_address() {
        // 安装器预选云端/双模式：形态已定但地址未填 → 仍未完成初始化；
        // 初始化页补完地址（provision）后才算配好。
        let dir = temp_dir("preselect-dual");
        preselect_provision_mode(&dir, ProvisionMode::Dual).unwrap();
        assert!(!is_provisioned(&dir));
        provision(
            &dir,
            ProvisionMode::Dual,
            "http://127.0.0.1:32101",
            "https://team.example.test",
        )
        .unwrap();
        assert!(is_provisioned(&dir));
    }
}
