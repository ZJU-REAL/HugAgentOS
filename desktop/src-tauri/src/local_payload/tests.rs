use super::*;
use crate::child_process::hide_console;
use std::fs::File;
use std::io::{Read, Write};

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
        safe_symlink_target(Path::new("python/lib/current"), Path::new("../python3.11")).is_ok()
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

fn runtime_manifest(fingerprint: &str) -> RuntimeBundleManifest {
    RuntimeBundleManifest {
        schema: 1,
        target: current_target().to_string(),
        python_version: "3.11.9".to_string(),
        dependency_fingerprint: fingerprint.to_string(),
        executable: "python/bin/python3".to_string(),
        smoke_test: "runtime-smoke.py".to_string(),
        archive: "runtime-core.tar.gz".to_string(),
        archive_sha256: "c".repeat(64),
        archive_size: 700,
        unpacked_size: 2000,
    }
}

struct SlimFixture {
    root: PathBuf,
    runtime_archive: PathBuf,
    runtime_manifest: PathBuf,
    manifest: RuntimeBundleManifest,
}

impl SlimFixture {
    fn new(name: &str, archive: &[u8]) -> Self {
        let root = std::env::temp_dir().join(format!(
            "hugagent-slim-{name}-{}-{}",
            std::process::id(),
            nonce()
        ));
        let resources = root.join("resources");
        fs::create_dir_all(&resources).unwrap();
        let manifest = runtime_manifest(&"e".repeat(64));
        let runtime_manifest = resources.join("runtime-manifest.json");
        fs::write(&runtime_manifest, serde_json::to_vec(&manifest).unwrap()).unwrap();
        let runtime_archive = resources.join("runtime-core.tar.gz");
        fs::write(&runtime_archive, archive).unwrap();
        Self {
            root,
            runtime_archive,
            runtime_manifest,
            manifest,
        }
    }

    fn paths(&self) -> PayloadPaths<'_> {
        PayloadPaths {
            root: &self.root,
            source_archive: &self.runtime_archive,
            source_manifest: &self.runtime_manifest,
            runtime_archive: &self.runtime_archive,
            runtime_manifest: &self.runtime_manifest,
        }
    }

    fn unpack_runtime(&self) {
        let dir = runtime_release_dir(&self.root, &self.manifest.dependency_fingerprint);
        fs::create_dir_all(dir.join("python/bin")).unwrap();
        fs::write(dir.join("python/bin/python3"), "fixture").unwrap();
        fs::write(dir.join("runtime-smoke.py"), "fixture").unwrap();
        let layout = RuntimeLayout {
            schema: 1,
            target: self.manifest.target.clone(),
            python_version: self.manifest.python_version.clone(),
            dependency_fingerprint: self.manifest.dependency_fingerprint.clone(),
            executable: self.manifest.executable.clone(),
            smoke_test: self.manifest.smoke_test.clone(),
        };
        fs::write(
            dir.join("runtime-layout.json"),
            serde_json::to_vec(&layout).unwrap(),
        )
        .unwrap();
    }
}

impl Drop for SlimFixture {
    fn drop(&mut self) {
        remove_tree(&self.root);
    }
}

#[test]
fn slim_package_without_its_runtime_asks_for_a_download() {
    if current_target() == "unsupported" {
        return;
    }
    let fixture = SlimFixture::new("download", b"");
    let download = pending_runtime_download(&fixture.paths()).unwrap().unwrap();
    assert_eq!(
        download.name,
        format!("runtime-{}-{}.tar.gz", current_target(), "e".repeat(64))
    );
    assert_eq!(download.sha256, "c".repeat(64));
    assert_eq!((download.archive_size, download.unpacked_size), (700, 2000));
    // Under runtimes/ with the staging marker, so prune sweeps an abandoned download.
    assert!(download
        .destination
        .starts_with(runtimes_root(&fixture.root)));
    let name = download
        .destination
        .file_name()
        .unwrap()
        .to_string_lossy()
        .into_owned();
    assert!(name.starts_with('.') && name.contains(STAGING_MARKER));
}

#[test]
fn bundled_or_unpacked_runtime_needs_no_download() {
    let full = SlimFixture::new("bundled", b"runtime bytes");
    assert!(pending_runtime_download(&full.paths()).unwrap().is_none());

    let slim = SlimFixture::new("unpacked", b"");
    slim.unpack_runtime();
    assert!(runtime_is_ready(&slim.root, &slim.manifest));
    assert!(pending_runtime_download(&slim.paths()).unwrap().is_none());
}

#[test]
fn unpacked_runtime_does_not_need_space_for_its_archive() {
    let fixture = SlimFixture::new("space", b"");
    fs::create_dir_all(&fixture.root).unwrap();
    let mut huge = fixture.manifest.clone();
    huge.unpacked_size = u64::MAX / 4;
    huge.archive_size = u64::MAX / 4;
    assert!(ensure_free_space(&fixture.paths(), &huge, true).is_err());
    assert!(ensure_free_space(&fixture.paths(), &huge, false).is_ok());
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
        let release = install_payloads(
            &paths,
            |_, _| {},
            &std::sync::atomic::AtomicBool::new(false),
        )?;
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
        hide_console(&mut command);
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
