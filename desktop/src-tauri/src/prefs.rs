//! 桌面客户端本地 UI 偏好持久化。
//!
//! 目前只存「关闭主窗口时的选择」（最小化 / 退出），落盘在
//! `<应用配置目录>/prefs.json`，让用户选过一次后不再每次弹框。

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

/// 关闭主窗口时的行为。
#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CloseAction {
    /// 最小化到系统托盘继续后台运行。
    Minimize,
    /// 直接退出程序。
    Exit,
}

#[derive(Default, Serialize, Deserialize)]
struct Prefs {
    /// None = 尚未记住，关闭时仍弹框询问。
    #[serde(default)]
    close_action: Option<CloseAction>,
}

fn path(config_dir: &Path) -> PathBuf {
    config_dir.join("prefs.json")
}

fn read(config_dir: &Path) -> Prefs {
    std::fs::read_to_string(path(config_dir))
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_default()
}

fn write(config_dir: &Path, prefs: &Prefs) {
    let _ = std::fs::create_dir_all(config_dir);
    match serde_json::to_string(prefs) {
        Ok(text) => {
            if let Err(e) = std::fs::write(path(config_dir), text) {
                eprintln!("[prefs] 写入失败: {e}");
            }
        }
        Err(e) => eprintln!("[prefs] 序列化失败: {e}"),
    }
}

/// 读取已记住的关闭行为（None = 未记住 → 关闭时弹框询问）。
pub fn load_close_action(config_dir: &Path) -> Option<CloseAction> {
    read(config_dir).close_action
}

/// 记住关闭行为，下次关闭直接执行、不再弹框。
pub fn save_close_action(config_dir: &Path, action: CloseAction) {
    let mut p = read(config_dir);
    p.close_action = Some(action);
    write(config_dir, &p);
}

/// 清除已记住的关闭行为，下次关闭重新弹框询问（托盘「关闭时重新询问」用）。
pub fn clear_close_action(config_dir: &Path) {
    let mut p = read(config_dir);
    p.close_action = None;
    write(config_dir, &p);
}
