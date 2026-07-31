//! Offline local-server payload installation shared by Windows, macOS, and Linux.

use flate2::read::GzDecoder;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};
use tar::EntryType;
use zip::ZipArchive;

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
    run_smoke_test(&resolved)?;
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

fn validate_manifests(
    server: &ServerBundleManifest,
    runtime: &RuntimeBundleManifest,
) -> Result<(), String> {
    if server.schema != 2 || runtime.schema != 1 {
        return Err("本机服务资源清单版本不受支持".to_string());
    }
    if current_target() == "unsupported" {
        return Err("当前 CPU 架构没有对应的桌面运行时".to_string());
    }
    if server.target != current_target() || runtime.target != current_target() {
        return Err(format!(
            "安装包平台不匹配：需要 {}，服务={}，运行时={}",
            current_target(),
            server.target,
            runtime.target
        ));
    }
    validate_identifier(&runtime.dependency_fingerprint, "dependency fingerprint")?;
    if server.dependency_fingerprint != runtime.dependency_fingerprint {
        return Err("服务资源与 Python 运行时的依赖指纹不一致".to_string());
    }
    if runtime.archive != "runtime-core.tar.gz" {
        return Err("运行时清单引用了未知的归档文件".to_string());
    }
    safe_relative(&runtime.executable, "runtime executable")?;
    safe_relative(&runtime.smoke_test, "runtime smoke test")?;
    Ok(())
}

fn read_server_manifest(path: &Path) -> Result<(ServerBundleManifest, Vec<u8>), String> {
    let raw = fs::read(path).map_err(|error| format!("读取服务清单失败：{error}"))?;
    let manifest =
        serde_json::from_slice(&raw).map_err(|error| format!("解析服务清单失败：{error}"))?;
    Ok((manifest, raw))
}

fn read_runtime_manifest(path: &Path) -> Result<RuntimeBundleManifest, String> {
    let raw = fs::read(path).map_err(|error| format!("读取运行时清单失败：{error}"))?;
    serde_json::from_slice(&raw).map_err(|error| format!("解析运行时清单失败：{error}"))
}

fn ensure_free_space(
    paths: &PayloadPaths<'_>,
    runtime: &RuntimeBundleManifest,
) -> Result<(), String> {
    let available = fs2::available_space(paths.root)
        .map_err(|error| format!("检查本机磁盘空间失败：{error}"))?;
    let source_size = fs::metadata(paths.source_archive)
        .map(|value| value.len())
        .unwrap_or_default();
    let required = runtime
        .unpacked_size
        .saturating_add(runtime.archive_size)
        .saturating_add(source_size.saturating_mul(3))
        .saturating_add(256 * 1024 * 1024);
    if available < required {
        return Err(format!(
            "磁盘空间不足：至少还需 {:.1} GB，当前可用 {:.1} GB",
            required as f64 / 1_073_741_824.0,
            available as f64 / 1_073_741_824.0
        ));
    }
    Ok(())
}

fn validate_source(root: &Path, expected_manifest: &[u8]) -> Result<(), String> {
    for relative in [
        "desktop-bundle.json",
        "pyproject.toml",
        "src/backend/cli.py",
        "src/frontend/dist/index.html",
    ] {
        if !root.join(relative).is_file() {
            return Err(format!("本机服务资源缺少 {relative}"));
        }
    }
    let actual = fs::read(root.join("desktop-bundle.json"))
        .map_err(|error| format!("读取解压后的服务清单失败：{error}"))?;
    if actual != expected_manifest {
        return Err("解压后的服务清单与安装包不一致".to_string());
    }
    Ok(())
}

fn validate_runtime(root: &Path, expected: &RuntimeBundleManifest) -> Result<(), String> {
    let layout: RuntimeLayout = serde_json::from_slice(
        &fs::read(root.join("runtime-layout.json"))
            .map_err(|error| format!("读取运行时布局失败：{error}"))?,
    )
    .map_err(|error| format!("解析运行时布局失败：{error}"))?;
    let expected_layout = RuntimeLayout {
        schema: expected.schema,
        target: expected.target.clone(),
        python_version: expected.python_version.clone(),
        dependency_fingerprint: expected.dependency_fingerprint.clone(),
        executable: expected.executable.clone(),
        smoke_test: expected.smoke_test.clone(),
    };
    if layout != expected_layout {
        return Err("解压后的 Python 运行时与外部清单不一致".to_string());
    }
    let executable = root.join(safe_relative(&layout.executable, "runtime executable")?);
    let smoke = root.join(safe_relative(&layout.smoke_test, "runtime smoke test")?);
    if !executable.is_file() || !smoke.is_file() {
        return Err("Python 运行时缺少解释器或自检脚本".to_string());
    }
    Ok(())
}

fn resolve_release(root: &Path, active: ActiveRelease) -> Result<ResolvedRelease, String> {
    let source_dir = source_release_dir(root, &active.source_id);
    let runtime_dir = runtime_release_dir(root, &active.runtime_id);
    let executable = runtime_dir.join(safe_relative(&active.executable, "runtime executable")?);
    let smoke_test = runtime_dir.join(safe_relative(&active.smoke_test, "runtime smoke test")?);
    Ok(ResolvedRelease {
        active,
        source_dir,
        executable,
        smoke_test,
    })
}

fn run_smoke_test(release: &ResolvedRelease) -> Result<(), String> {
    let output = Command::new(&release.executable)
        .arg(&release.smoke_test)
        .arg("--source")
        .arg(&release.source_dir)
        .current_dir(&release.source_dir)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .output()
        .map_err(|error| format!("无法运行离线 Python 自检：{error}"))?;
    if !output.status.success() {
        return Err(format!(
            "离线 Python 自检失败：{}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(())
}

fn activate(root: &Path, active: &ActiveRelease) -> Result<(), String> {
    let current = fs::read(active_path(root)).ok();
    if let Some(current) = current {
        atomic_write(&previous_path(root), &current)?;
    }
    atomic_write_json(&active_path(root), active)
}

fn atomic_write_json<T: Serialize>(path: &Path, value: &T) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(value)
        .map_err(|error| format!("序列化本机版本状态失败：{error}"))?;
    atomic_write(path, &bytes)
}

fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), String> {
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
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    fs::rename(source, target)
}

#[cfg(target_os = "windows")]
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
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

fn extract_zip(archive_path: &Path, destination: &Path) -> Result<(), String> {
    let file = File::open(archive_path).map_err(|error| format!("打开服务归档失败：{error}"))?;
    let mut archive =
        ZipArchive::new(file).map_err(|error| format!("解析服务归档失败：{error}"))?;
    for index in 0..archive.len() {
        let mut entry = archive
            .by_index(index)
            .map_err(|error| format!("读取服务归档条目失败：{error}"))?;
        let relative = entry
            .enclosed_name()
            .ok_or_else(|| format!("服务归档包含不安全路径：{}", entry.name()))?
            .to_path_buf();
        let output = destination.join(relative);
        if entry.is_dir() {
            fs::create_dir_all(&output).map_err(|error| format!("创建服务目录失败：{error}"))?;
            continue;
        }
        if let Some(parent) = output.parent() {
            fs::create_dir_all(parent).map_err(|error| format!("创建服务目录失败：{error}"))?;
        }
        let mut file =
            File::create(&output).map_err(|error| format!("创建服务文件失败：{error}"))?;
        io::copy(&mut entry, &mut file).map_err(|error| format!("解压服务文件失败：{error}"))?;
        #[cfg(unix)]
        if let Some(mode) = entry.unix_mode() {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&output, fs::Permissions::from_mode(mode))
                .map_err(|error| format!("恢复服务文件权限失败：{error}"))?;
        }
    }
    Ok(())
}

fn extract_runtime(archive_path: &Path, destination: &Path) -> Result<(), String> {
    let file = File::open(archive_path).map_err(|error| format!("打开运行时归档失败：{error}"))?;
    let decoder = GzDecoder::new(file);
    let mut archive = tar::Archive::new(decoder);
    let entries = archive
        .entries()
        .map_err(|error| format!("解析运行时归档失败：{error}"))?;
    for entry in entries {
        let mut entry = entry.map_err(|error| format!("读取运行时条目失败：{error}"))?;
        let path = entry
            .path()
            .map_err(|error| format!("读取运行时路径失败：{error}"))?
            .into_owned();
        safe_archive_path(&path)?;
        let entry_type = entry.header().entry_type();
        if entry_type == EntryType::Symlink {
            let target = entry
                .link_name()
                .map_err(|error| format!("读取运行时符号链接失败：{error}"))?
                .ok_or("运行时符号链接缺少目标")?;
            safe_symlink_target(&path, &target)?;
        } else if !(entry_type.is_file() || entry_type.is_dir()) {
            return Err(format!("运行时包含不支持的归档条目：{}", path.display()));
        }
        if !entry
            .unpack_in(destination)
            .map_err(|error| format!("解压运行时条目失败：{error}"))?
        {
            return Err(format!("运行时条目试图越过安装目录：{}", path.display()));
        }
    }
    Ok(())
}

fn safe_archive_path(path: &Path) -> Result<(), String> {
    if path.is_absolute()
        || path.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return Err(format!("归档包含不安全路径：{}", path.display()));
    }
    Ok(())
}

fn safe_symlink_target(path: &Path, target: &Path) -> Result<(), String> {
    if target.is_absolute() {
        return Err(format!("运行时符号链接使用绝对路径：{}", path.display()));
    }
    let mut depth = path
        .parent()
        .map(|value| value.components().count())
        .unwrap_or(0);
    for component in target.components() {
        match component {
            Component::Normal(_) => depth += 1,
            Component::CurDir => {}
            Component::ParentDir if depth > 0 => depth -= 1,
            _ => return Err(format!("运行时符号链接越过安装目录：{}", path.display())),
        }
    }
    Ok(())
}

fn safe_relative(value: &str, label: &str) -> Result<PathBuf, String> {
    let path = PathBuf::from(value);
    safe_archive_path(&path).map_err(|_| format!("{label} 不是安全的相对路径"))?;
    if path.as_os_str().is_empty() {
        return Err(format!("{label} 不能为空"));
    }
    Ok(path)
}

fn validate_identifier(value: &str, label: &str) -> Result<(), String> {
    if value.len() < 16 || !value.chars().all(|character| character.is_ascii_hexdigit()) {
        return Err(format!("{label} 格式无效"));
    }
    Ok(())
}

fn verify_file_hash(path: &Path, expected: &str) -> Result<(), String> {
    let actual = sha256_file(path)?;
    if !actual.eq_ignore_ascii_case(expected) {
        return Err("离线 Python 运行时校验失败，请重新下载安装包".to_string());
    }
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|error| format!("打开运行时归档失败：{error}"))?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 1024 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| format!("读取运行时归档失败：{error}"))?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

fn sha256_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn staging_path(parent: &Path, id: &str) -> PathBuf {
    parent.join(format!(".{id}.stage-{}", nonce()))
}

fn nonce() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or_default()
}

fn commit_directory(staged: &Path, destination: &Path) -> Result<(), String> {
    if destination.exists() {
        remove_tree(staged);
        return Ok(());
    }
    fs::rename(staged, destination).map_err(|error| format!("提交本机资源失败：{error}"))
}

fn remove_tree(path: &Path) {
    if path.is_dir() {
        let _ = fs::remove_dir_all(path);
    } else {
        let _ = fs::remove_file(path);
    }
}

fn prune_children(root: &Path, keep: &HashSet<String>) {
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with('.') || keep.contains(&name) {
            continue;
        }
        remove_tree(&entry.path());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_release(root: &Path, source_id: &str, runtime_id: &str) -> ActiveRelease {
        let source = source_release_dir(root, source_id);
        let runtime = runtime_release_dir(root, runtime_id);
        fs::create_dir_all(source.join("src/backend")).unwrap();
        fs::create_dir_all(source.join("src/frontend/dist")).unwrap();
        fs::create_dir_all(runtime.join("python/bin")).unwrap();
        fs::create_dir_all(runtime.join("smoke")).unwrap();
        fs::write(source.join("src/backend/cli.py"), "# fixture").unwrap();
        fs::write(source.join("src/frontend/dist/index.html"), "fixture").unwrap();
        fs::write(runtime.join("python/bin/python3"), "fixture").unwrap();
        fs::write(runtime.join("smoke/runtime-smoke.py"), "fixture").unwrap();
        ActiveRelease {
            schema: 1,
            desktop_version: "test".to_string(),
            source_revision: "test".to_string(),
            source_id: source_id.to_string(),
            runtime_id: runtime_id.to_string(),
            target: current_target().to_string(),
            executable: "python/bin/python3".to_string(),
            smoke_test: "smoke/runtime-smoke.py".to_string(),
        }
    }

    #[test]
    fn archive_paths_and_symlinks_cannot_escape_install_root() {
        assert!(safe_archive_path(Path::new("python/lib/site.py")).is_ok());
        assert!(safe_archive_path(Path::new("../outside")).is_err());
        assert!(
            safe_symlink_target(Path::new("python/lib/current"), Path::new("../python3.11"))
                .is_ok()
        );
        assert!(safe_symlink_target(
            Path::new("python/lib/current"),
            Path::new("../../../outside")
        )
        .is_err());
    }

    #[test]
    fn previous_release_is_restored_with_atomic_state_files() {
        let root = std::env::temp_dir().join(format!(
            "hugagent-payload-rollback-{}-{}",
            std::process::id(),
            nonce()
        ));
        let old = test_release(&root, &"a".repeat(64), &"b".repeat(64));
        let new = test_release(&root, &"c".repeat(64), &"d".repeat(64));
        atomic_write_json(&active_path(&root), &new).unwrap();
        atomic_write_json(&previous_path(&root), &old).unwrap();

        assert!(restore_previous(&root).unwrap());
        let active = resolved_active(&root).unwrap().unwrap();
        assert_eq!(active.active.source_id, old.source_id);
        let previous = read_release(&root, &previous_path(&root)).unwrap().unwrap();
        assert_eq!(previous.active.source_id, new.source_id);
        remove_tree(&root);
    }

    #[test]
    fn runtime_and_source_manifests_must_match_the_build_target() {
        let fingerprint = "a".repeat(64);
        let server = ServerBundleManifest {
            schema: 2,
            desktop_version: "test".to_string(),
            source_revision: "test".to_string(),
            target: "wrong-target".to_string(),
            dependency_fingerprint: fingerprint.clone(),
        };
        let runtime = RuntimeBundleManifest {
            schema: 1,
            target: current_target().to_string(),
            python_version: "3.11".to_string(),
            dependency_fingerprint: fingerprint,
            executable: "python/bin/python3".to_string(),
            smoke_test: "smoke/runtime-smoke.py".to_string(),
            archive: "runtime-core.tar.gz".to_string(),
            archive_sha256: "b".repeat(64),
            archive_size: 1,
            unpacked_size: 1,
        };
        assert!(validate_manifests(&server, &runtime).is_err());
    }

    #[cfg(any(
        all(target_os = "windows", target_arch = "x86_64"),
        all(target_os = "linux", target_arch = "x86_64"),
        all(
            target_os = "macos",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    ))]
    #[test]
    #[ignore = "extracts the generated 1+ GiB runtime and starts the real local server"]
    fn generated_payload_installs_and_serves_health_endpoint() {
        use std::net::{TcpListener, TcpStream};
        #[cfg(unix)]
        use std::os::unix::process::CommandExt;
        use std::time::Duration;

        fn health(port: u16) -> bool {
            let address = format!("127.0.0.1:{port}").parse().unwrap();
            let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(500))
            else {
                return false;
            };
            let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
            if stream
                .write_all(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                .is_err()
            {
                return false;
            }
            let mut response = String::new();
            stream.read_to_string(&mut response).is_ok()
                && response.contains("200 OK")
                && response.contains(crate::brand::LOCAL_SERVICE_NAME)
        }

        let generated = Path::new(env!("CARGO_MANIFEST_DIR")).join("../generated");
        let root = std::env::temp_dir().join(format!(
            "hugagent-real-payload-{}-{}",
            std::process::id(),
            nonce()
        ));
        let data = root.join("data");
        let install = root.join("install");
        let result = (|| -> Result<(), String> {
            for required in [
                "server-ce.zip",
                "server-ce/desktop-bundle.json",
                "runtime-core.tar.gz",
                "runtime-manifest.json",
            ] {
                if !generated.join(required).is_file() {
                    return Err(format!(
                        "missing generated payload {required}; run node desktop/scripts/prepare-bundle.mjs"
                    ));
                }
            }
            let source_archive = generated.join("server-ce.zip");
            let source_manifest = generated.join("server-ce/desktop-bundle.json");
            let runtime_archive = generated.join("runtime-core.tar.gz");
            let runtime_manifest = generated.join("runtime-manifest.json");
            let paths = PayloadPaths {
                root: &install,
                source_archive: &source_archive,
                source_manifest: &source_manifest,
                runtime_archive: &runtime_archive,
                runtime_manifest: &runtime_manifest,
            };
            let release = install_payloads(&paths, |_, _| {})?;
            let port = TcpListener::bind("127.0.0.1:0")
                .map_err(|error| error.to_string())?
                .local_addr()
                .map_err(|error| error.to_string())?
                .port();
            let log_path = root.join("server.log");
            let stdout = File::create(&log_path).map_err(|error| error.to_string())?;
            let stderr = stdout.try_clone().map_err(|error| error.to_string())?;
            let mut command = Command::new(&release.executable);
            command
                .arg(release.source_dir.join("src/backend/cli.py"))
                .arg("serve")
                .args(["--host", "127.0.0.1", "--port", &port.to_string()])
                .arg("--no-browser")
                .current_dir(&release.source_dir)
                .env("HUGAGENT_HOME", &data)
                .env("PYTHONUTF8", "1")
                .env("PYTHONDONTWRITEBYTECODE", "1")
                .env(
                    "FRONTEND_DIST_DIR",
                    release.source_dir.join("src/frontend/dist"),
                )
                .stdin(Stdio::null())
                .stdout(stdout)
                .stderr(stderr);
            #[cfg(unix)]
            command.process_group(0);
            let mut child = command.spawn().map_err(|error| error.to_string())?;
            let ready = (0..90).any(|_| {
                if health(port) {
                    return true;
                }
                if child.try_wait().ok().flatten().is_some() {
                    return false;
                }
                std::thread::sleep(Duration::from_secs(1));
                false
            });
            #[cfg(unix)]
            let _ = unsafe { libc::kill(-(child.id() as i32), libc::SIGTERM) };
            #[cfg(target_os = "windows")]
            let _ = child.kill();
            let _ = child.wait();
            if !ready {
                return Err(format!(
                    "real local server did not become healthy:\n{}",
                    fs::read_to_string(log_path).unwrap_or_default()
                ));
            }
            Ok(())
        })();
        remove_tree(&root);
        assert!(result.is_ok(), "{}", result.unwrap_err());
    }
}
