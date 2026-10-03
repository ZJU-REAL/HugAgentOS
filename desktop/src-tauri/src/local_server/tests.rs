use super::backup::{backup_local_data, prune_data_backups, restore_local_data, MAX_DATA_BACKUPS};
use super::process::{linux_server_command_matches, mac_server_command_matches};
use super::*;

fn manager(name: &str) -> Arc<LocalServerManager> {
    let base = std::env::temp_dir().join(format!(
        "hugagent-desktop-local-server-{name}-{}",
        std::process::id()
    ));
    let root = base.join("installed");
    let bundle_archive = base.join("server-ce.zip");
    let bundle_manifest = base.join("server-ce-manifest.json");
    let runtime_archive = base.join("runtime-core.tar.gz");
    let runtime_manifest = base.join("runtime-manifest.json");
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    std::fs::write(&bundle_archive, "test archive").unwrap();
    std::fs::write(&runtime_archive, "test runtime").unwrap();
    LocalServerManager::new(
        root,
        base.join("data"),
        bundle_archive,
        bundle_manifest,
        runtime_archive,
        runtime_manifest,
        reqwest::Client::new(),
    )
}

#[test]
fn missing_or_invalid_payload_requires_reinstall() {
    let manager = manager("manifest");
    assert!(!manager.is_installed());
    assert!(manager.needs_install());
}

#[test]
fn shutdown_prevents_the_local_service_from_restarting() {
    let manager = manager("shutdown");

    manager.shutdown().unwrap();

    assert!(!manager.prepare_in_background());
    assert!(manager.shutting_down.load(Ordering::SeqCst));
}

#[test]
fn windows_uninstaller_stops_and_detaches_managed_runtime_cleanup() {
    let hooks = include_str!("../../installer-hooks.nsh");

    assert!(hooks.contains("NSIS_HOOK_PREUNINSTALL"));
    assert!(hooks.contains("taskkill.exe /PID"));
    // 卸载前按可执行文件位置结束安装根下的全部进程，不再只认记录的 PID。
    assert!(hooks.contains("ExecutablePath).StartsWith($$root"));
    assert!(!hooks.contains("server.pid"));
    assert!(hooks.contains("HUGAGENT_DELETE_DATA"));
    assert!(hooks.contains("MB_DEFBUTTON2"));
    assert!(hooks.contains("GetTempFileName"));
    assert!(hooks.contains("ExecShell"));
    assert!(!hooks.contains("RMDir /r"));
    assert!(!hooks.contains("RD /S"));
    assert!(hooks.contains("-ValidateOnly"));
    assert!(hooks.contains("-DetachedRuntime"));
    assert!(hooks.contains("uninstall-cleanup.ps1"));
    assert!(hooks.contains("remove-$R9"));
}

#[test]
fn every_desktop_target_embeds_source_and_offline_runtime() {
    let windows_config = include_str!("../../tauri.windows.conf.json");
    let macos_config = include_str!("../../tauri.macos.conf.json");
    let linux_config = include_str!("../../tauri.linux.conf.json");

    for config in [windows_config, macos_config, linux_config] {
        assert!(config.contains("server-ce.zip"));
        assert!(config.contains("server-ce-manifest.json"));
        assert!(config.contains("runtime-core.tar.gz"));
        assert!(config.contains("runtime-manifest.json"));
        assert!(!config.contains("\"../generated/server-ce\": \"server-ce\""));
    }
}

#[test]
fn service_log_rotates_while_running_and_keeps_one_generation() {
    let dir = std::env::temp_dir().join(format!(
        "hugagent-desktop-log-rotate-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("server.log");
    let mut log = RotatingLog::with_limit(&path, 64).unwrap();

    log.append(&[b'a'; 40]);
    assert!(!dir.join("server.log.1").exists(), "未到上限不应滚存");

    log.append(&[b'b'; 40]);
    // 句柄必须真的关掉才改得动名字；否则 Windows 上这一步静默失败，
    // 日志会继续长在同一个文件里——实测攒到过 450 MB。
    let previous = dir.join("server.log.1");
    assert!(previous.exists(), "越过上限后应滚存出上一代");
    assert_eq!(std::fs::read(&previous).unwrap().len(), 80);
    assert_eq!(std::fs::read(&path).unwrap().len(), 0, "当前这份应从零开始");

    log.append(&[b'c'; 40]);
    assert_eq!(
        std::fs::read(&path).unwrap().len(),
        40,
        "滚存后仍可继续写入"
    );
}

#[test]
fn log_tail_keeps_only_recent_lines() {
    let manager = manager("logs");
    let log = manager.root.join("tail.log");
    std::fs::create_dir_all(log.parent().unwrap()).unwrap();
    std::fs::write(&log, "one\ntwo\nthree\n").unwrap();

    assert_eq!(tail_file(&log, 2), vec!["two", "three"]);
}

#[test]
fn upgrade_backup_restores_databases_and_removes_new_wal_files() {
    let manager = manager("data-backup");
    std::fs::create_dir_all(&manager.data_root).unwrap();
    std::fs::write(manager.data_root.join("data.db"), "before").unwrap();
    std::fs::write(manager.data_root.join("config.env"), "old=true").unwrap();
    let backup = backup_local_data(&manager.data_root, &manager.root.join("backups"))
        .unwrap()
        .unwrap();

    std::fs::write(manager.data_root.join("data.db"), "after").unwrap();
    std::fs::write(manager.data_root.join("data.db-wal"), "new wal").unwrap();
    restore_local_data(&manager.data_root, &backup).unwrap();

    assert_eq!(
        std::fs::read_to_string(manager.data_root.join("data.db")).unwrap(),
        "before"
    );
    assert_eq!(
        std::fs::read_to_string(manager.data_root.join("config.env")).unwrap(),
        "old=true"
    );
    assert!(!manager.data_root.join("data.db-wal").exists());
}

#[test]
fn only_three_successful_upgrade_backups_are_retained() {
    let manager = manager("backup-prune");
    let backups = manager.root.join("backups");
    for index in 0..5 {
        std::fs::create_dir_all(backups.join(format!("backup-{index}"))).unwrap();
    }
    prune_data_backups(&backups);
    let count = std::fs::read_dir(backups).unwrap().count();
    assert_eq!(count, MAX_DATA_BACKUPS);
}

#[test]
fn legacy_macos_data_moves_to_dot_hugagent_without_copying() {
    let base = std::env::temp_dir().join(format!(
        "hugagent-desktop-data-migration-{}",
        std::process::id()
    ));
    let legacy = base
        .join("Library")
        .join("Application Support")
        .join("data");
    let preferred = base.join("home").join(".hugagent");
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(legacy.join("workspace").join("site")).unwrap();
    std::fs::write(legacy.join("data.db"), "desktop data").unwrap();

    migrate_legacy_data_dir(&legacy, &preferred).unwrap();

    assert!(!legacy.exists());
    assert_eq!(
        std::fs::read_to_string(preferred.join("data.db")).unwrap(),
        "desktop data"
    );
    assert!(preferred.join("workspace").join("site").is_dir());
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn existing_dot_hugagent_wins_without_overwriting_or_deleting_legacy_data() {
    let base = std::env::temp_dir().join(format!(
        "hugagent-desktop-data-conflict-{}",
        std::process::id()
    ));
    let legacy = base.join("legacy");
    let preferred = base.join("home").join(".hugagent");
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&legacy).unwrap();
    std::fs::create_dir_all(&preferred).unwrap();
    std::fs::write(legacy.join("data.db"), "desktop data").unwrap();
    std::fs::write(preferred.join("data.db"), "standalone data").unwrap();

    migrate_legacy_data_dir(&legacy, &preferred).unwrap();
    assert!(legacy.join("data.db").is_file());
    assert_eq!(
        std::fs::read_to_string(preferred.join("data.db")).unwrap(),
        "standalone data"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn mac_stale_process_match_is_scoped_to_this_install_and_port() {
    let root = Path::new("/Users/test/Library/Application Support/HugAgentOS/local-server");
    assert!(mac_server_command_matches(
            "/Users/test/Library/Application Support/HugAgentOS/local-server/releases/runtimes/abc/python/bin/python3 /Users/test/Library/Application Support/HugAgentOS/local-server/releases/sources/def/src/backend/cli.py serve --host 127.0.0.1 --port 32101",
            root,
        ));
    assert!(!mac_server_command_matches(
        "/tmp/python /tmp/cli.py serve --host 127.0.0.1 --port 32101",
        root,
    ));
    assert!(!mac_server_command_matches(
            "/Users/test/Library/Application Support/HugAgentOS/local-server/releases/runtimes/abc/python/bin/python3 /Users/test/Library/Application Support/HugAgentOS/local-server/releases/sources/def/src/backend/cli.py serve --port 32102",
            root,
        ));
}

#[test]
fn linux_stale_process_match_is_scoped_to_this_install_and_port() {
    let root = Path::new("/home/test/.local/share/hugagent/local-server");
    assert!(linux_server_command_matches(
            "/home/test/.local/share/hugagent/local-server/releases/runtimes/abc/python/bin/python3 /home/test/.local/share/hugagent/local-server/releases/sources/def/src/backend/cli.py serve --host 127.0.0.1 --port 32101",
            root,
        ));
    assert!(!linux_server_command_matches(
        "/tmp/python /tmp/cli.py serve --port 32101",
        root,
    ));
}

#[cfg(target_os = "linux")]
#[test]
fn shutdown_reaps_tree_after_parent_already_exited() {
    assert_orphan_cleanup(false);
}

#[cfg(target_os = "linux")]
#[test]
fn health_observation_reaps_descendants_before_shutdown() {
    assert_orphan_cleanup(true);
}

#[cfg(target_os = "linux")]
fn assert_orphan_cleanup(observe: bool) {
    use std::process::Command;
    let manager = manager(if observe {
        "orphan-monitor"
    } else {
        "orphan-shutdown"
    });
    let marker = manager.root.parent().unwrap().join("descendant.pid");
    let script = r#"import subprocess,sys; code='import signal,os,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); open(sys.argv[1],"w").write(str(os.getpid())); time.sleep(60)'; subprocess.Popen([sys.executable,'-c',code,sys.argv[1]])"#;
    let mut command = Command::new("python3");
    command.args(["-c", script, marker.to_str().unwrap()]);
    super::process::configure_process_group(&mut command);
    let mut child = ManagedProcess::spawn(&mut command).unwrap();
    let mut exited = false;
    for _ in 0..100 {
        exited = std::fs::read_to_string(format!("/proc/{}/stat", child.id()))
            .ok()
            .is_some_and(|s| {
                s.rsplit_once(") ")
                    .is_some_and(|(_, rest)| rest.starts_with('Z'))
            });
        if exited && marker.exists() {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(exited, "parent must exit before shutdown");
    let pid = std::fs::read_to_string(marker)
        .unwrap()
        .parse::<u32>()
        .unwrap();
    if observe {
        assert!(child.try_wait().unwrap().is_some());
    }
    *manager.child.lock().unwrap() = Some(child);
    if !observe {
        manager.shutdown().unwrap();
    }
    let alive = (0..50).all(|_| {
        let running = std::fs::read_to_string(format!("/proc/{pid}/stat"))
            .ok()
            .and_then(|s| s.rsplit_once(") ").map(|(_, rest)| !rest.starts_with('Z')))
            .unwrap_or(false);
        if running {
            std::thread::sleep(Duration::from_millis(10));
        }
        running
    });
    assert!(!alive, "orphan descendant survived manager shutdown");
    manager.shutdown().unwrap();
}
