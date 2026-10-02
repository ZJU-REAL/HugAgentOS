use serde::Deserialize;
use serde::Serialize;
use std::collections::HashSet;
use std::path::Path;
use std::path::PathBuf;
pub(super) const MAX_DATA_BACKUPS: usize = 3;
const BACKUP_FILES: &[&str] = &[
    "data.db",
    "data.db-wal",
    "data.db-shm",
    "milvus.db",
    "config.env",
    "secrets.json",
    "catalog.json",
];

#[derive(Debug, Deserialize, Serialize)]
struct DataBackupManifest {
    schema: u32,
    files: Vec<String>,
}

pub(super) fn backup_local_data(
    data_root: &Path,
    backups_root: &Path,
) -> Result<Option<PathBuf>, String> {
    let files: Vec<&str> = BACKUP_FILES
        .iter()
        .copied()
        .filter(|name| data_root.join(name).is_file())
        .collect();
    if files.is_empty() {
        return Ok(None);
    }
    std::fs::create_dir_all(backups_root)
        .map_err(|error| format!("创建数据备份目录失败：{error}"))?;
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_millis())
        .unwrap_or_default();
    let staged = backups_root.join(format!(".stage-{}-{stamp}", std::process::id()));
    let destination = backups_root.join(format!("backup-{stamp}"));
    let _ = std::fs::remove_dir_all(&staged);
    std::fs::create_dir_all(&staged)
        .map_err(|error| format!("创建数据备份暂存目录失败：{error}"))?;
    let result = (|| {
        for name in &files {
            std::fs::copy(data_root.join(name), staged.join(name))
                .map_err(|error| format!("备份 {name} 失败：{error}"))?;
        }
        let manifest = DataBackupManifest {
            schema: 1,
            files: files.iter().map(|value| (*value).to_string()).collect(),
        };
        let bytes = serde_json::to_vec_pretty(&manifest)
            .map_err(|error| format!("序列化数据备份清单失败：{error}"))?;
        std::fs::write(staged.join("backup.json"), bytes)
            .map_err(|error| format!("写入数据备份清单失败：{error}"))?;
        std::fs::rename(&staged, &destination)
            .map_err(|error| format!("提交数据备份失败：{error}"))?;
        Ok::<(), String>(())
    })();
    if let Err(error) = result {
        let _ = std::fs::remove_dir_all(&staged);
        return Err(error);
    }
    Ok(Some(destination))
}

pub(super) fn restore_local_data(data_root: &Path, backup: &Path) -> Result<(), String> {
    let manifest: DataBackupManifest = serde_json::from_slice(
        &std::fs::read(backup.join("backup.json"))
            .map_err(|error| format!("读取数据备份清单失败：{error}"))?,
    )
    .map_err(|error| format!("解析数据备份清单失败：{error}"))?;
    if manifest.schema != 1
        || manifest
            .files
            .iter()
            .any(|name| !BACKUP_FILES.contains(&name.as_str()))
    {
        return Err("数据备份清单无效".to_string());
    }
    std::fs::create_dir_all(data_root).map_err(|error| format!("创建本机数据目录失败：{error}"))?;
    let restored: HashSet<String> = manifest.files.iter().cloned().collect();
    for name in BACKUP_FILES {
        if !restored.contains(*name) {
            let _ = std::fs::remove_file(data_root.join(name));
        }
    }
    for name in manifest.files {
        std::fs::copy(backup.join(&name), data_root.join(&name))
            .map_err(|error| format!("恢复 {name} 失败：{error}"))?;
    }
    Ok(())
}

pub(super) fn prune_data_backups(backups_root: &Path) {
    let Ok(entries) = std::fs::read_dir(backups_root) else {
        return;
    };
    let mut backups: Vec<PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.is_dir()
                && path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.starts_with("backup-"))
        })
        .collect();
    backups.sort();
    let remove_count = backups.len().saturating_sub(MAX_DATA_BACKUPS);
    for path in backups.into_iter().take(remove_count) {
        let _ = std::fs::remove_dir_all(path);
    }
}
