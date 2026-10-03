//! Remove expired updater staging files scoped to this application.
const INSTALLER_TEMP_MARKER: &str = "-updater-";
/// 多久之前留下的就算上一轮的残骸。装机是分钟级的事，一小时足够宽。
const STALE_INSTALLER_AGE: std::time::Duration = std::time::Duration::from_secs(60 * 60);

pub(super) fn sweep_stale_installers(app_name: &str) {
    let Ok(entries) = std::fs::read_dir(std::env::temp_dir()) else {
        return;
    };
    let prefix = format!("{}-", app_name.to_ascii_lowercase());
    let Some(keep_after) = std::time::SystemTime::now().checked_sub(STALE_INSTALLER_AGE) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_ascii_lowercase();
        if !name.starts_with(&prefix) || !name.contains(INSTALLER_TEMP_MARKER) {
            continue;
        }
        let stale = entry
            .metadata()
            .and_then(|meta| meta.modified())
            .map(|modified| modified < keep_after)
            .unwrap_or(false);
        if !stale {
            continue;
        }
        let path = entry.path();
        let _ = if path.is_dir() {
            std::fs::remove_dir_all(&path)
        } else {
            std::fs::remove_file(&path)
        };
    }
}
