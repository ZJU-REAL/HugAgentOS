use super::archive::safe_relative;
use super::archive::validate_identifier;
use super::current_target;
use super::runtime_release_dir;
use super::source_release_dir;
use super::ActiveRelease;
use super::ResolvedRelease;
use super::RuntimeBundleManifest;
use super::RuntimeLayout;
use super::ServerBundleManifest;
use crate::local_payload::PayloadPaths;
use std::fs;
use std::path::Path;
pub(super) fn validate_manifests(
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

pub(super) fn read_server_manifest(path: &Path) -> Result<(ServerBundleManifest, Vec<u8>), String> {
    let raw = fs::read(path).map_err(|error| format!("读取服务清单失败：{error}"))?;
    let manifest =
        serde_json::from_slice(&raw).map_err(|error| format!("解析服务清单失败：{error}"))?;
    Ok((manifest, raw))
}

pub(super) fn read_runtime_manifest(path: &Path) -> Result<RuntimeBundleManifest, String> {
    let raw = fs::read(path).map_err(|error| format!("读取运行时清单失败：{error}"))?;
    serde_json::from_slice(&raw).map_err(|error| format!("解析运行时清单失败：{error}"))
}

pub(super) fn ensure_free_space(
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

pub(super) fn validate_source(root: &Path, expected_manifest: &[u8]) -> Result<(), String> {
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

pub(super) fn validate_runtime(
    root: &Path,
    expected: &RuntimeBundleManifest,
) -> Result<(), String> {
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

pub(super) fn resolve_release(
    root: &Path,
    active: ActiveRelease,
) -> Result<ResolvedRelease, String> {
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
