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
    "storage/applications.sqlite",
    "storage/applications.sqlite-wal",
    "storage/applications.sqlite-shm",
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
    validate_path(data_root)?;
    for name in BACKUP_FILES {
        validate_tree(&data_root.join(name))?;
    }
    let files: Vec<&str> = BACKUP_FILES
        .iter()
        .copied()
        .filter(|name| data_root.join(name).exists())
        .collect();
    if files.is_empty() {
        return Ok(None);
    }
    validate_path(backups_root)?;
    std::fs::create_dir_all(backups_root)
        .map_err(|error| format!("创建数据备份目录失败：{error}"))?;
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_millis())
        .unwrap_or_default();
    let staging = tempfile::Builder::new()
        .prefix(".stage-")
        .tempdir_in(backups_root)
        .map_err(|error| format!("创建数据备份暂存目录失败：{error}"))?;
    let staged = staging.path();
    let unique = staged.file_name().unwrap().to_string_lossy();
    let destination = backups_root.join(format!("backup-{stamp}-{unique}"));
    let result = (|| {
        for name in &files {
            if let Some(parent) = staged.join(name).parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|error| format!("创建备份子目录失败：{error}"))?;
            }
            copy_entry(&data_root.join(name), &staged.join(name))?;
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
    result?;
    Ok(Some(destination))
}

pub(super) fn restore_local_data(data_root: &Path, backup: &Path) -> Result<(), String> {
    restore_local_data_with(data_root, backup, |source, destination| {
        std::fs::rename(source, destination)
    })
}

fn restore_local_data_with(
    data_root: &Path,
    backup: &Path,
    mut rename: impl FnMut(&Path, &Path) -> std::io::Result<()>,
) -> Result<(), String> {
    validate_tree(backup)?;
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
    // Validate the whole snapshot and all destinations before changing live data.
    // A rejected path must never leave a partially restored memory store.
    validate_path(data_root)?;
    for name in BACKUP_FILES {
        validate_tree(&data_root.join(name))?;
    }
    let restored: HashSet<String> = manifest.files.iter().cloned().collect();
    if restored.len() != manifest.files.len() {
        return Err("数据备份清单包含重复条目".to_string());
    }
    for name in &manifest.files {
        if !backup.join(name).exists() {
            return Err(format!("数据备份缺少条目：{name}"));
        }
    }
    std::fs::create_dir_all(data_root).map_err(|error| format!("创建本机数据目录失败：{error}"))?;
    let staging = tempfile::Builder::new()
        .prefix(".restore-")
        .tempdir_in(data_root)
        .map_err(|error| format!("创建恢复暂存目录失败：{error}"))?;
    for name in &manifest.files {
        copy_entry(&backup.join(name), &staging.path().join(name))?;
    }
    // Keep displaced data on the same volume as the live entries. A failed
    // publication reverses every completed rename before returning an error.
    let displaced = tempfile::Builder::new()
        .prefix(".rollback-displaced-")
        .tempdir_in(data_root)
        .map_err(|error| format!("创建回滚保留目录失败：{error}"))?;
    let mut published = Vec::new();
    let result = (|| {
        for name in BACKUP_FILES {
            let current = data_root.join(name);
            if current.exists() {
                let saved = displaced.path().join(name);
                std::fs::create_dir_all(saved.parent().unwrap())
                    .map_err(|error| format!("创建回滚保留子目录失败：{error}"))?;
                rename(&current, &saved)
                    .map_err(|error| format!("保留升级后数据 {name} 失败：{error}"))?;
                published.push((current.clone(), saved));
            }
            if restored.contains(*name) {
                std::fs::create_dir_all(current.parent().unwrap())
                    .map_err(|error| format!("创建恢复子目录失败：{error}"))?;
                let staged = staging.path().join(name);
                rename(&staged, &current).map_err(|error| format!("恢复 {name} 失败：{error}"))?;
                published.push((staged, current));
            }
        }
        Ok::<(), String>(())
    })();
    if let Err(error) = result {
        for (source, destination) in published.into_iter().rev() {
            if let Err(undo_error) = rename(&destination, &source) {
                let retained = displaced.keep();
                return Err(format!(
                    "{error}；撤销恢复失败：{undo_error}；禁止启动服务，保留数据位于 {}，原始快照位于 {}",
                    retained.display(), backup.display()
                ));
            }
        }
        return Err(format!("{error}；已撤销恢复，现有数据保持不变"));
    }
    let _ = displaced.keep();
    Ok(())
}

fn validate_path(path: &Path) -> Result<(), String> {
    // Check ancestors too: a regular file below a junction is still unsafe.
    for ancestor in path.ancestors() {
        match std::fs::symlink_metadata(ancestor) {
            Ok(metadata) => reject_link(ancestor, &metadata)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(format!("检查数据路径失败：{error}")),
        }
    }
    Ok(())
}

fn validate_tree(path: &Path) -> Result<(), String> {
    validate_path(path)?;
    let metadata = match std::fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("检查数据条目失败：{error}")),
    };
    reject_link(path, &metadata)?;
    if metadata.is_dir() {
        for entry in
            std::fs::read_dir(path).map_err(|error| format!("读取数据目录失败：{error}"))?
        {
            validate_tree(
                &entry
                    .map_err(|error| format!("读取数据条目失败：{error}"))?
                    .path(),
            )?;
        }
    } else if !metadata.is_file() {
        return Err(format!("数据备份不允许特殊文件：{}", path.display()));
    }
    Ok(())
}

fn reject_link(path: &Path, metadata: &std::fs::Metadata) -> Result<(), String> {
    let linked = metadata.file_type().is_symlink();
    #[cfg(windows)]
    let linked = {
        use std::os::windows::fs::MetadataExt;
        linked
            || metadata.file_attributes()
                & windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT
                != 0
    };
    if linked {
        return Err(format!("数据备份不允许链接或重解析点：{}", path.display()));
    }
    Ok(())
}

fn copy_entry(source: &Path, destination: &Path) -> Result<(), String> {
    let metadata =
        std::fs::symlink_metadata(source).map_err(|error| format!("读取备份条目失败：{error}"))?;
    reject_link(source, &metadata)?;
    if metadata.is_dir() {
        std::fs::create_dir_all(destination)
            .map_err(|error| format!("创建备份子目录失败：{error}"))?;
        for entry in
            std::fs::read_dir(source).map_err(|error| format!("读取备份目录失败：{error}"))?
        {
            let entry = entry.map_err(|error| format!("读取备份条目失败：{error}"))?;
            copy_entry(&entry.path(), &destination.join(entry.file_name()))?;
        }
    } else if metadata.is_file() {
        std::fs::create_dir_all(destination.parent().unwrap())
            .map_err(|error| format!("创建备份父目录失败：{error}"))?;
        std::fs::copy(source, destination).map_err(|error| format!("复制备份条目失败：{error}"))?;
    } else {
        return Err(format!("数据备份不允许特殊文件：{}", source.display()));
    }
    Ok(())
}

#[cfg(test)]
#[path = "backup_tests.rs"]
mod tests;

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
