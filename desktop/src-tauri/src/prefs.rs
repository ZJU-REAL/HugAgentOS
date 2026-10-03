//! 桌面客户端本地 UI 偏好持久化。
//!
//! 存「关闭主窗口时的选择」（最小化 / 退出）与「视图缩放档位」，落盘在
//! `<应用配置目录>/prefs.json`，让用户选过一次后不再每次重设。

use serde::{Deserialize, Serialize};
use std::io::Write;
use std::path::{Path, PathBuf};
static WRITE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

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
    /// 「视图 → 放大 / 缩小」选定的页面缩放。None = 未调整过，跟随系统显示设置。
    #[serde(default)]
    ui_zoom: Option<f64>,
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

fn update(config_dir: &Path, change: impl FnOnce(&mut Prefs)) {
    let _guard = WRITE_LOCK.lock().unwrap_or_else(|error| error.into_inner());
    let mut prefs = read(config_dir);
    change(&mut prefs);
    let result = (|| -> Result<(), Box<dyn std::error::Error>> {
        std::fs::create_dir_all(config_dir)?;
        let mut staged = tempfile::NamedTempFile::new_in(config_dir)?;
        staged.write_all(&serde_json::to_vec(&prefs)?)?;
        staged.as_file().sync_all()?;
        staged.persist(path(config_dir))?;
        Ok(())
    })();
    if let Err(error) = result {
        eprintln!("[prefs] 写入失败: {error}");
    }
}

/// 读取已记住的关闭行为（None = 未记住 → 关闭时弹框询问）。
#[cfg(any(test, not(target_os = "macos")))]
pub fn load_close_action(config_dir: &Path) -> Option<CloseAction> {
    read(config_dir).close_action
}

/// 记住关闭行为，下次关闭直接执行、不再弹框。
#[cfg(any(test, not(target_os = "macos")))]
pub fn save_close_action(config_dir: &Path, action: CloseAction) {
    update(config_dir, |p| p.close_action = Some(action));
}

/// 清除已记住的关闭行为，下次关闭重新弹框询问（托盘「关闭时重新询问」用）。
pub fn clear_close_action(config_dir: &Path) {
    update(config_dir, |p| p.close_action = None);
}

/// 读取视图缩放档位（None = 未设置过）。取值是否合法由调用方归一化，
/// 与 `load_close_action` 一样只做存取、不带策略。
pub fn load_ui_zoom(config_dir: &Path) -> Option<f64> {
    read(config_dir).ui_zoom
}

/// 记住视图缩放档位，新开窗口与下次启动都沿用。
pub fn save_ui_zoom(config_dir: &Path, zoom: f64) {
    update(config_dir, |p| p.ui_zoom = Some(zoom));
}

#[cfg(test)]
mod tests {
    #[test]
    fn concurrent_fields_survive_atomic_updates() {
        let dir = tempfile::tempdir().unwrap();
        std::thread::scope(|scope| {
            for _ in 0..20 {
                scope.spawn(|| super::save_close_action(dir.path(), super::CloseAction::Minimize));
                scope.spawn(|| super::save_ui_zoom(dir.path(), 1.25));
            }
        });
        assert!(super::load_close_action(dir.path()) == Some(super::CloseAction::Minimize));
        assert_eq!(super::load_ui_zoom(dir.path()), Some(1.25));
        assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 1);
    }
}
