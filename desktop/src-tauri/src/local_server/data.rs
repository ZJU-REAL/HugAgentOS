use std::path::Path;
use std::path::PathBuf;
/// Resolve the business-data directory independently from the managed runtime.
///
/// The Tauri application-data root is still the right place for the bundled
/// Python/runtime payload. On macOS it contains ``Application Support`` and on
/// Linux it may live below a desktop-specific data root; neither should become a
/// model-generated workspace path. Keep only the runtime there and use the same
/// ``~/.hugagent`` data root as the standalone local installer. Existing desktop
/// data is moved on first launch when that does not overwrite standalone data.
pub fn resolve_local_server_data_dir(runtime_root: &Path, home_dir: Option<&Path>) -> PathBuf {
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    {
        let legacy = runtime_root.join("data");
        let Some(home_dir) = home_dir else {
            return legacy;
        };
        let preferred = home_dir.join(".hugagent");
        if let Err(error) = migrate_legacy_data_dir(&legacy, &preferred) {
            eprintln!("[local-server] 迁移本机数据目录失败，继续使用旧目录：{error}");
            return legacy;
        }
        preferred
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let _ = home_dir;
        runtime_root.join("data")
    }
}

#[cfg(any(target_os = "macos", target_os = "linux", test))]
pub(super) fn migrate_legacy_data_dir(legacy: &Path, preferred: &Path) -> Result<(), String> {
    if !legacy.exists() || legacy == preferred {
        return Ok(());
    }
    let parent = preferred
        .parent()
        .ok_or_else(|| format!("目标目录没有父目录：{}", preferred.display()))?;
    std::fs::create_dir_all(parent)
        .map_err(|error| format!("创建 {} 失败：{error}", parent.display()))?;

    if preferred.exists() {
        let is_empty = preferred
            .read_dir()
            .map_err(|error| format!("读取 {} 失败：{error}", preferred.display()))?
            .next()
            .is_none();
        if !is_empty {
            eprintln!(
                "[local-server] {} 已有数据，将其作为统一数据目录；旧目录 {} 保留为备份",
                preferred.display(),
                legacy.display()
            );
            return Ok(());
        }
        std::fs::remove_dir(preferred)
            .map_err(|error| format!("移除空目录 {} 失败：{error}", preferred.display()))?;
    }

    std::fs::rename(legacy, preferred).map_err(|error| {
        format!(
            "无法把 {} 移到 {}：{error}",
            legacy.display(),
            preferred.display()
        )
    })
}
