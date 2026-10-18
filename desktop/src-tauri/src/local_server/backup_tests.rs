use super::{backup_local_data, restore_local_data, restore_local_data_with};
use std::path::Path;

fn temporary_directory() -> tempfile::TempDir {
    // macOS exposes its default temporary directory through /var, a system
    // symlink. Use its real parent so these tests exercise links inside the
    // backup boundary rather than an alias above the fixture.
    let parent = std::env::temp_dir().canonicalize().unwrap();
    tempfile::tempdir_in(parent).unwrap()
}

fn write(root: &Path, name: &str, contents: &[u8]) {
    let path = root.join(name);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, contents).unwrap();
}

#[test]
fn upgrade_rollback_restores_complete_vector_store_and_retains_failed_contents() {
    let temp = temporary_directory();
    let data = temp.path().join("data");
    write(&data, "milvus.db/manifest.json", b"original manifest");
    write(
        &data,
        "milvus.db/collections/memory/segments/0.bin",
        &[0, 255, 17],
    );
    write(&data, "data.db", b"original users");
    write(&data, "storage/unrelated.txt", b"untouched");
    let backup = backup_local_data(&data, &temp.path().join("backups"))
        .unwrap()
        .unwrap();
    assert_eq!(
        std::fs::read(backup.join("milvus.db/collections/memory/segments/0.bin")).unwrap(),
        [0, 255, 17]
    );

    write(&data, "milvus.db/manifest.json", b"modified manifest");
    write(
        &data,
        "milvus.db/collections/memory/segments/0.bin",
        b"modified vector",
    );
    write(
        &data,
        "milvus.db/collections/memory/segments/1.bin",
        b"new vector",
    );
    write(&data, "data.db", b"modified users");
    write(&data, "data.db-wal", b"new wal");
    restore_local_data(&data, &backup).unwrap();

    assert_eq!(
        std::fs::read(data.join("milvus.db/manifest.json")).unwrap(),
        b"original manifest"
    );
    assert_eq!(
        std::fs::read(data.join("milvus.db/collections/memory/segments/0.bin")).unwrap(),
        [0, 255, 17]
    );
    assert!(!data
        .join("milvus.db/collections/memory/segments/1.bin")
        .exists());
    assert!(!data.join("data.db-wal").exists());
    assert_eq!(
        std::fs::read(data.join("data.db")).unwrap(),
        b"original users"
    );
    assert_eq!(
        std::fs::read(data.join("storage/unrelated.txt")).unwrap(),
        b"untouched"
    );
    let displaced = std::fs::read_dir(&data)
        .unwrap()
        .flatten()
        .map(|e| e.path())
        .find(|p| {
            p.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with(".rollback-displaced-")
        })
        .unwrap();
    assert_eq!(
        std::fs::read(displaced.join("milvus.db/collections/memory/segments/1.bin")).unwrap(),
        b"new vector"
    );
    assert_eq!(
        std::fs::read(displaced.join("data.db-wal")).unwrap(),
        b"new wal"
    );
}

#[test]
fn rollback_removes_new_store_from_live_data_without_deleting_it() {
    let temp = temporary_directory();
    let data = temp.path().join("data");
    write(&data, "data.db", b"before");
    let backup = backup_local_data(&data, &temp.path().join("backups"))
        .unwrap()
        .unwrap();
    write(&data, "milvus.db/manifest.json", b"new store");
    restore_local_data(&data, &backup).unwrap();
    assert!(!data.join("milvus.db").exists());
    assert!(std::fs::read_dir(&data)
        .unwrap()
        .flatten()
        .any(|e| e.path().join("milvus.db/manifest.json").exists()));
}

#[test]
fn incomplete_or_traversing_manifest_cannot_modify_live_memory() {
    let temp = temporary_directory();
    let data = temp.path().join("data");
    let backup = temp.path().join("backup");
    write(&data, "milvus.db/manifest.json", b"original");
    for manifest in [
        r#"{"schema":1,"files":["data.db","milvus.db"]}"#,
        r#"{"schema":1,"files":["../outside"]}"#,
        r#"{"schema":1,"files":["milvus.db","milvus.db"]}"#,
    ] {
        write(&backup, "backup.json", manifest.as_bytes());
        assert!(restore_local_data(&data, &backup).is_err());
        assert_eq!(
            std::fs::read(data.join("milvus.db/manifest.json")).unwrap(),
            b"original"
        );
    }
}

#[cfg(unix)]
#[test]
fn vector_symlinks_in_source_snapshot_and_live_destination_are_rejected() {
    use std::os::unix::fs::symlink;
    let temp = temporary_directory();
    let data = temp.path().join("data");
    let outside = temp.path().join("outside");
    write(&outside, "manifest.json", b"outside");
    std::fs::create_dir_all(&data).unwrap();
    symlink(&outside, data.join("milvus.db")).unwrap();
    assert!(backup_local_data(&data, &temp.path().join("backups")).is_err());
    std::fs::remove_file(data.join("milvus.db")).unwrap();
    write(&data, "milvus.db/manifest.json", b"original");
    let backup = backup_local_data(&data, &temp.path().join("backups"))
        .unwrap()
        .unwrap();
    symlink(&outside, data.join("milvus.db/redirect")).unwrap();
    assert!(restore_local_data(&data, &backup).is_err());
    std::fs::remove_file(data.join("milvus.db/redirect")).unwrap();
    symlink(&outside, backup.join("milvus.db/redirect")).unwrap();
    assert!(restore_local_data(&data, &backup).is_err());
    assert_eq!(
        std::fs::read(outside.join("manifest.json")).unwrap(),
        b"outside"
    );
    assert_eq!(
        std::fs::read(data.join("milvus.db/manifest.json")).unwrap(),
        b"original"
    );
}

#[test]
fn ordinary_file_store_and_empty_directories_are_preserved() {
    let temp = temporary_directory();
    let data = temp.path().join("data");
    write(&data, "milvus.db", b"existing store");
    let backup = backup_local_data(&data, &temp.path().join("backups"))
        .unwrap()
        .unwrap();
    write(&data, "milvus.db", b"changed");
    restore_local_data(&data, &backup).unwrap();
    assert_eq!(
        std::fs::read(data.join("milvus.db")).unwrap(),
        b"existing store"
    );
    std::fs::remove_file(data.join("milvus.db")).unwrap();
    std::fs::create_dir_all(data.join("milvus.db/empty")).unwrap();
    let backup = backup_local_data(&data, &temp.path().join("backups"))
        .unwrap()
        .unwrap();
    restore_local_data(&data, &backup).unwrap();
    assert!(data.join("milvus.db/empty").is_dir());
}

#[test]
fn failed_publication_reverses_completed_database_and_vector_moves() {
    let temp = temporary_directory();
    let data = temp.path().join("data");
    write(&data, "data.db", b"before users");
    write(&data, "data.db-wal", b"before wal");
    write(&data, "milvus.db/manifest.json", b"before memory");
    let backup = backup_local_data(&data, &temp.path().join("backups"))
        .unwrap()
        .unwrap();
    let original_snapshot = std::fs::read(backup.join("backup.json")).unwrap();
    write(&data, "data.db", b"upgrade users");
    write(&data, "data.db-wal", b"upgrade wal");
    write(&data, "data.db-shm", b"upgrade shm");
    write(&data, "milvus.db/manifest.json", b"upgrade memory");
    write(&data, "milvus.db/new.bin", b"upgrade vector");
    let mut faulted = false;
    let error = restore_local_data_with(&data, &backup, |source, destination| {
        if !faulted && destination == data.join("milvus.db") {
            faulted = true;
            return Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "injected publication failure",
            ));
        }
        std::fs::rename(source, destination)
    })
    .unwrap_err();
    assert!(faulted);
    assert!(error.contains("已撤销恢复"));
    assert!(std::fs::read_dir(&data)
        .unwrap()
        .flatten()
        .all(|entry| !entry.file_name().to_string_lossy().starts_with(".")));
    for (name, expected) in [
        ("data.db", b"upgrade users".as_slice()),
        ("data.db-wal", b"upgrade wal".as_slice()),
        ("data.db-shm", b"upgrade shm".as_slice()),
        ("milvus.db/manifest.json", b"upgrade memory".as_slice()),
        ("milvus.db/new.bin", b"upgrade vector".as_slice()),
    ] {
        assert_eq!(std::fs::read(data.join(name)).unwrap(), expected);
    }
    assert_eq!(
        std::fs::read(backup.join("backup.json")).unwrap(),
        original_snapshot
    );
    assert_eq!(
        std::fs::read(backup.join("data.db")).unwrap(),
        b"before users"
    );
    assert_eq!(
        std::fs::read(backup.join("milvus.db/manifest.json")).unwrap(),
        b"before memory"
    );
    assert!(!backup.join("milvus.db/new.bin").exists());
}
