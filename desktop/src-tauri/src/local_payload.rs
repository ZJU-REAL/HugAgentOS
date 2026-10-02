//! Offline local-server payload installation shared by Windows, macOS, and Linux.

use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs::{self};
use std::io::{self};
use std::path::{Path, PathBuf};

use std::process::{Command, Stdio};
use std::time::Duration;

mod activation;
mod archive;
mod validation;
use activation::*;
use archive::*;
use validation::*;
#[cfg(test)]
mod tests;
#[derive(Clone, Debug, Deserialize)]
pub struct ServerBundleManifest {
    pub schema: u32,
    pub desktop_version: String,
    pub source_revision: String,
    pub target: String,
    pub dependency_fingerprint: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct RuntimeBundleManifest {
    pub schema: u32,
    pub target: String,
    pub python_version: String,
    pub dependency_fingerprint: String,
    pub executable: String,
    pub smoke_test: String,
    pub archive: String,
    pub archive_sha256: String,
    pub archive_size: u64,
    pub unpacked_size: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
struct RuntimeLayout {
    schema: u32,
    target: String,
    python_version: String,
    dependency_fingerprint: String,
    executable: String,
    smoke_test: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct ActiveRelease {
    pub schema: u32,
    pub desktop_version: String,
    pub source_revision: String,
    pub source_id: String,
    pub runtime_id: String,
    pub target: String,
    pub executable: String,
    pub smoke_test: String,
}

#[derive(Clone, Debug)]
pub struct ResolvedRelease {
    pub active: ActiveRelease,
    pub source_dir: PathBuf,
    pub executable: PathBuf,
    pub smoke_test: PathBuf,
}

pub struct PayloadPaths<'a> {
    pub root: &'a Path,
    pub source_archive: &'a Path,
    pub source_manifest: &'a Path,
    pub runtime_archive: &'a Path,
    pub runtime_manifest: &'a Path,
}

pub fn current_target() -> &'static str {
    if option_env!("HUGAGENT_DESKTOP_BUNDLE") == Some("thin") {
        return "unsupported";
    }
    #[cfg(all(target_os = "windows", target_arch = "x86_64"))]
    return "windows-x86_64";
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    return "darwin-aarch64";
    #[cfg(all(target_os = "macos", target_arch = "x86_64"))]
    return "darwin-x86_64";
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    return "linux-x86_64";
    #[allow(unreachable_code)]
    "unsupported"
}

fn releases_root(root: &Path) -> PathBuf {
    #[cfg(target_os = "windows")]
    return root.join("r");
    #[cfg(not(target_os = "windows"))]
    root.join("releases")
}

fn sources_root(root: &Path) -> PathBuf {
    #[cfg(target_os = "windows")]
    return releases_root(root).join("s");
    #[cfg(not(target_os = "windows"))]
    releases_root(root).join("sources")
}

fn runtimes_root(root: &Path) -> PathBuf {
    #[cfg(target_os = "windows")]
    return releases_root(root).join("p");
    #[cfg(not(target_os = "windows"))]
    releases_root(root).join("runtimes")
}

fn storage_id(id: &str) -> &str {
    #[cfg(target_os = "windows")]
    return id.get(..32).unwrap_or(id);
    #[cfg(not(target_os = "windows"))]
    id
}

fn source_release_dir(root: &Path, id: &str) -> PathBuf {
    sources_root(root).join(storage_id(id))
}

fn runtime_release_dir(root: &Path, id: &str) -> PathBuf {
    runtimes_root(root).join(storage_id(id))
}

fn active_path(root: &Path) -> PathBuf {
    root.join("active.json")
}

fn previous_path(root: &Path) -> PathBuf {
    root.join("previous.json")
}

pub fn resolved_active(root: &Path) -> Result<Option<ResolvedRelease>, String> {
    read_release(root, &active_path(root))
}

fn read_release(root: &Path, path: &Path) -> Result<Option<ResolvedRelease>, String> {
    let raw = match fs::read(path) {
        Ok(raw) => raw,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("读取 {} 失败：{error}", path.display())),
    };
    let active: ActiveRelease = serde_json::from_slice(&raw)
        .map_err(|error| format!("解析 {} 失败：{error}", path.display()))?;
    validate_identifier(&active.source_id, "source_id")?;
    validate_identifier(&active.runtime_id, "runtime_id")?;
    let executable = safe_relative(&active.executable, "runtime executable")?;
    let smoke_test = safe_relative(&active.smoke_test, "runtime smoke test")?;
    let source_dir = source_release_dir(root, &active.source_id);
    let runtime_dir = runtime_release_dir(root, &active.runtime_id);
    let executable = runtime_dir.join(executable);
    let smoke_test = runtime_dir.join(smoke_test);
    if !source_dir.join("src/backend/cli.py").is_file()
        || !source_dir.join("src/frontend/dist/index.html").is_file()
        || !executable.is_file()
        || !smoke_test.is_file()
    {
        return Ok(None);
    }
    Ok(Some(ResolvedRelease {
        active,
        source_dir,
        executable,
        smoke_test,
    }))
}

pub fn needs_install(paths: &PayloadPaths<'_>) -> bool {
    let Ok((server, server_raw)) = read_server_manifest(paths.source_manifest) else {
        return true;
    };
    let Ok(runtime) = read_runtime_manifest(paths.runtime_manifest) else {
        return true;
    };
    let Ok(Some(active)) = resolved_active(paths.root) else {
        return true;
    };
    let source_id = sha256_bytes(&server_raw);
    active.active.source_id != source_id
        || active.active.runtime_id != runtime.dependency_fingerprint
        || active.active.target != current_target()
        || server.dependency_fingerprint != runtime.dependency_fingerprint
}

pub fn install_payloads<F>(
    paths: &PayloadPaths<'_>,
    mut progress: F,
    cancelled: &std::sync::atomic::AtomicBool,
) -> Result<ResolvedRelease, String>
where
    F: FnMut(u8, &str),
{
    fs::create_dir_all(paths.root).map_err(|error| format!("创建本机服务目录失败：{error}"))?;
    let (server, server_raw) = read_server_manifest(paths.source_manifest)?;
    let runtime = read_runtime_manifest(paths.runtime_manifest)?;
    validate_manifests(&server, &runtime)?;
    verify_file_hash(paths.runtime_archive, &runtime.archive_sha256)?;
    ensure_free_space(paths, &runtime)?;

    let source_id = sha256_bytes(&server_raw);
    let runtime_id = runtime.dependency_fingerprint.clone();
    let sources_root = sources_root(paths.root);
    let runtimes_root = runtimes_root(paths.root);
    fs::create_dir_all(&sources_root)
        .and_then(|_| fs::create_dir_all(&runtimes_root))
        .map_err(|error| format!("创建本机版本目录失败：{error}"))?;

    progress(8, "正在解压同版本服务资源…");
    let source_dir = source_release_dir(paths.root, &source_id);
    if !source_dir.is_dir() {
        let staged = staging_path(&sources_root, storage_id(&source_id));
        remove_tree(&staged);
        fs::create_dir_all(&staged).map_err(|error| format!("创建服务暂存目录失败：{error}"))?;
        if let Err(error) = extract_zip(paths.source_archive, &staged)
            .and_then(|_| validate_source(&staged, &server_raw))
            .and_then(|_| commit_directory(&staged, &source_dir))
        {
            remove_tree(&staged);
            return Err(error);
        }
    } else {
        validate_source(&source_dir, &server_raw)?;
    }

    progress(35, "正在解压离线 Python 运行环境…");
    let runtime_dir = runtime_release_dir(paths.root, &runtime_id);
    if !runtime_dir.is_dir() {
        let staged = staging_path(&runtimes_root, storage_id(&runtime_id));
        remove_tree(&staged);
        fs::create_dir_all(&staged).map_err(|error| format!("创建运行时暂存目录失败：{error}"))?;
        if let Err(error) = extract_runtime(paths.runtime_archive, &staged)
            .and_then(|_| validate_runtime(&staged, &runtime))
            .and_then(|_| commit_directory(&staged, &runtime_dir))
        {
            remove_tree(&staged);
            return Err(error);
        }
    } else {
        validate_runtime(&runtime_dir, &runtime)?;
    }

    progress(82, "正在验证本机服务运行环境…");
    let active = ActiveRelease {
        schema: 1,
        desktop_version: server.desktop_version,
        source_revision: server.source_revision,
        source_id,
        runtime_id,
        target: runtime.target,
        executable: runtime.executable,
        smoke_test: runtime.smoke_test,
    };
    let resolved = resolve_release(paths.root, active.clone())?;
    run_smoke_test(&resolved, cancelled)?;
    if cancelled.load(std::sync::atomic::Ordering::SeqCst) {
        return Err("安装已取消".into());
    }
    activate(paths.root, &active)?;
    progress(90, "离线本机服务已安装，正在启动…");
    resolve_release(paths.root, active)
}

pub fn restore_previous(root: &Path) -> Result<bool, String> {
    let Some(previous) = read_release(root, &previous_path(root))? else {
        return Ok(false);
    };
    let current = fs::read(active_path(root)).ok();
    atomic_write_json(&active_path(root), &previous.active)?;
    if let Some(current) = current {
        atomic_write(&previous_path(root), &current)?;
    }
    Ok(true)
}

pub fn prune_old_releases(root: &Path) {
    let mut keep_sources = HashSet::new();
    let mut keep_runtimes = HashSet::new();
    for path in [active_path(root), previous_path(root)] {
        if let Ok(Some(release)) = read_release(root, &path) {
            keep_sources.insert(storage_id(&release.active.source_id).to_string());
            keep_runtimes.insert(storage_id(&release.active.runtime_id).to_string());
        }
    }
    prune_children(&sources_root(root), &keep_sources);
    prune_children(&runtimes_root(root), &keep_runtimes);
}

fn run_smoke_test(
    release: &ResolvedRelease,
    cancelled: &std::sync::atomic::AtomicBool,
) -> Result<(), String> {
    let mut command = Command::new(&release.executable);
    command
        .arg(&release.smoke_test)
        .arg("--source")
        .arg(&release.source_dir)
        .current_dir(&release.source_dir)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null());
    crate::child_process::run_diagnostic(&mut command, cancelled, Duration::from_secs(120))
}
