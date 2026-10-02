use super::active_path;
use super::previous_path;
use super::ActiveRelease;
use serde::Serialize;
use std::collections::HashSet;
use std::fs;
use std::fs::File;
use std::io;
use std::io::Write;
use std::path::Path;
use std::path::PathBuf;
use std::time::Duration;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;
pub(super) fn activate(root: &Path, active: &ActiveRelease) -> Result<(), String> {
    let current = fs::read(active_path(root)).ok();
    if let Some(current) = current {
        atomic_write(&previous_path(root), &current)?;
    }
    atomic_write_json(&active_path(root), active)
}

pub(super) fn atomic_write_json<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(value)
        .map_err(|error| format!("序列化本机版本状态失败：{error}"))?;
    atomic_write(path, &bytes)
}

pub(super) fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let parent = path.parent().ok_or("本机版本状态路径无父目录")?;
    fs::create_dir_all(parent).map_err(|error| format!("创建版本状态目录失败：{error}"))?;
    let temporary = parent.join(format!(
        ".{}.next-{}",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("state"),
        nonce()
    ));
    let mut file =
        File::create(&temporary).map_err(|error| format!("创建版本状态失败：{error}"))?;
    file.write_all(bytes)
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("写入版本状态失败：{error}"))?;
    replace_file(&temporary, path).map_err(|error| format!("激活本机版本失败：{error}"))
}

#[cfg(not(target_os = "windows"))]
pub(super) fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    fs::rename(source, target)
}

#[cfg(target_os = "windows")]
pub(super) fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    let ok = unsafe {
        MoveFileExW(
            source.as_ptr(),
            target.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if ok == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

pub(super) fn staging_path(parent: &Path, id: &str) -> PathBuf {
    parent.join(format!(".{id}{STAGING_MARKER}{}", nonce()))
}

pub(super) fn nonce() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or_default()
}

pub(super) fn commit_directory(staged: &Path, destination: &Path) -> Result<(), String> {
    if destination.exists() {
        remove_tree(staged);
        return Ok(());
    }
    fs::rename(staged, destination).map_err(|error| format!("提交本机资源失败：{error}"))
}

pub(super) fn remove_tree(path: &Path) {
    if path.is_dir() {
        let _ = fs::remove_dir_all(path);
    } else {
        let _ = fs::remove_file(path);
    }
}

/// 解压暂存目录名里的固定段，`staging_path` 拼名字和 `prune_children` 认残骸用的是
/// 同一个常量。安装中途被杀或断电会留下一份，每份都是一整套 Python 运行时；下次安装
/// 用的是新的 nonce，删不到旧的那份。
const STAGING_MARKER: &str = ".stage-";
/// 多久没动过就认定这份暂存目录已经没人在写了。解压是持续写入的，正在进行的那份
/// 修改时间一直在刷新，不会落进这个窗口。
const ABANDONED_STAGING: Duration = Duration::from_secs(60 * 60);

pub(super) fn is_abandoned(path: &Path) -> bool {
    fs::metadata(path)
        .and_then(|meta| meta.modified())
        .and_then(|modified| {
            SystemTime::now().duration_since(modified).map_err(|_| {
                std::io::Error::new(std::io::ErrorKind::Other, "modified in the future")
            })
        })
        .map(|age| age >= ABANDONED_STAGING)
        .unwrap_or(false)
}

pub(super) fn prune_children(root: &Path, keep: &HashSet<String>) {
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if keep.contains(&name) {
            continue;
        }
        // 点开头的是安装过程中的临时项，正常不该删；但崩溃遗留的解压暂存目录
        // 也长这样，而且没人再认领它——每崩一次就永久多占一整份运行时的空间。
        // 只清明显已经没人在写的那些，免得误伤正在进行的解压。
        if name.starts_with('.') {
            if !name.contains(STAGING_MARKER) || !is_abandoned(&entry.path()) {
                continue;
            }
        }
        remove_tree(&entry.path());
    }
}
