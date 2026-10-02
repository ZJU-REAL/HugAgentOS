use flate2::read::GzDecoder;
use sha2::Digest;
use sha2::Sha256;
use std::fs;
use std::fs::File;
use std::io;
use std::io::Read;
use std::path::Component;
use std::path::Path;
use std::path::PathBuf;
use tar::EntryType;
use zip::ZipArchive;
pub(super) fn extract_zip(archive_path: &Path, destination: &Path) -> Result<(), String> {
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

pub(super) fn extract_runtime(archive_path: &Path, destination: &Path) -> Result<(), String> {
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

pub(super) fn safe_archive_path(path: &Path) -> Result<(), String> {
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

pub(super) fn safe_symlink_target(path: &Path, target: &Path) -> Result<(), String> {
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

pub(super) fn safe_relative(value: &str, label: &str) -> Result<PathBuf, String> {
    let path = PathBuf::from(value);
    safe_archive_path(&path).map_err(|_| format!("{label} 不是安全的相对路径"))?;
    if path.as_os_str().is_empty() {
        return Err(format!("{label} 不能为空"));
    }
    Ok(path)
}

pub(super) fn validate_identifier(value: &str, label: &str) -> Result<(), String> {
    if value.len() < 16 || !value.chars().all(|character| character.is_ascii_hexdigit()) {
        return Err(format!("{label} 格式无效"));
    }
    Ok(())
}

pub(super) fn verify_file_hash(path: &Path, expected: &str) -> Result<(), String> {
    let actual = sha256_file(path)?;
    if !actual.eq_ignore_ascii_case(expected) {
        return Err("离线 Python 运行时校验失败，请重新下载安装包".to_string());
    }
    Ok(())
}

pub(super) fn sha256_file(path: &Path) -> Result<String, String> {
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

pub(super) fn sha256_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
