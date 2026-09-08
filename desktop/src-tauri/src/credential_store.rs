//! OS credential storage. No plaintext fallback and no secret command-line arguments.
use sha2::{Digest, Sha256};
use std::path::Path;

pub trait CredentialStore {
    fn read(&self, key: &str) -> Result<Option<String>, String>;
    fn write(&self, key: &str, token: &str) -> Result<(), String>;
    fn delete(&self, key: &str) -> Result<(), String>;
}
pub struct SystemCredentialStore;

pub fn account_key(config_dir: &Path, server_base: &str) -> String {
    let path = config_dir
        .canonicalize()
        .unwrap_or_else(|_| config_dir.to_path_buf());
    let mut hash = Sha256::new();
    hash.update(path.to_string_lossy().as_bytes());
    hash.update([0]);
    hash.update(server_base.trim_end_matches('/').as_bytes());
    format!("HugAgentOS/session/{:x}", hash.finalize())
}
impl CredentialStore for SystemCredentialStore {
    fn read(&self, key: &str) -> Result<Option<String>, String> {
        platform::read(key)
    }
    fn write(&self, key: &str, token: &str) -> Result<(), String> {
        platform::write(key, token)
    }
    fn delete(&self, key: &str) -> Result<(), String> {
        platform::delete(key)
    }
}

#[cfg(target_os = "windows")]
mod platform {
    use std::{ffi::c_void, ptr};
    #[repr(C)]
    struct Credential {
        flags: u32,
        kind: u32,
        target: *mut u16,
        comment: *mut u16,
        last_written: [u32; 2],
        blob_size: u32,
        blob: *mut u8,
        persist: u32,
        attribute_count: u32,
        attributes: *mut c_void,
        alias: *mut u16,
        user: *mut u16,
    }
    #[link(name = "advapi32")]
    extern "system" {
        fn CredReadW(target: *const u16, kind: u32, flags: u32, value: *mut *mut Credential)
            -> i32;
        fn CredWriteW(value: *const Credential, flags: u32) -> i32;
        fn CredDeleteW(target: *const u16, kind: u32, flags: u32) -> i32;
        fn CredFree(value: *mut c_void);
    }
    fn wide(value: &str) -> Vec<u16> {
        value.encode_utf16().chain(Some(0)).collect()
    }
    fn error() -> String {
        format!(
            "Windows Credential Manager: {}",
            std::io::Error::last_os_error()
        )
    }
    pub fn read(key: &str) -> Result<Option<String>, String> {
        let key = wide(key);
        let mut value = ptr::null_mut();
        unsafe {
            if CredReadW(key.as_ptr(), 1, 0, &mut value) == 0 {
                return if std::io::Error::last_os_error().raw_os_error() == Some(1168) {
                    Ok(None)
                } else {
                    Err(error())
                };
            }
            let bytes = if (*value).blob_size == 0 {
                Vec::new()
            } else {
                std::slice::from_raw_parts((*value).blob, (*value).blob_size as usize).to_vec()
            };
            CredFree(value.cast());
            String::from_utf8(bytes)
                .map(Some)
                .map_err(|_| "Invalid credential encoding".into())
        }
    }
    pub fn write(key: &str, token: &str) -> Result<(), String> {
        if token.len() > 2560 {
            return Err("Session exceeds Windows credential size".into());
        }
        let mut key = wide(key);
        let mut user = wide("desktop-session");
        let credential = Credential {
            flags: 0,
            kind: 1,
            target: key.as_mut_ptr(),
            comment: ptr::null_mut(),
            last_written: [0, 0],
            blob_size: token.len() as u32,
            blob: token.as_ptr() as *mut u8,
            persist: 2,
            attribute_count: 0,
            attributes: ptr::null_mut(),
            alias: ptr::null_mut(),
            user: user.as_mut_ptr(),
        };
        if unsafe { CredWriteW(&credential, 0) } != 0 {
            Ok(())
        } else {
            Err(error())
        }
    }
    pub fn delete(key: &str) -> Result<(), String> {
        if unsafe { CredDeleteW(wide(key).as_ptr(), 1, 0) } != 0
            || std::io::Error::last_os_error().raw_os_error() == Some(1168)
        {
            Ok(())
        } else {
            Err(error())
        }
    }
}

#[cfg(target_os = "macos")]
mod platform {
    use std::{
        ffi::{c_char, c_void},
        ptr,
    };
    const SERVICE: &[u8] = b"HugAgentOS Desktop";
    const NOT_FOUND: i32 = -25300;
    #[link(name = "Security", kind = "framework")]
    extern "C" {
        fn SecKeychainFindGenericPassword(
            keychain: *const c_void,
            service_len: u32,
            service: *const c_char,
            account_len: u32,
            account: *const c_char,
            length: *mut u32,
            data: *mut *mut c_void,
            item: *mut *mut c_void,
        ) -> i32;
        fn SecKeychainAddGenericPassword(
            keychain: *const c_void,
            service_len: u32,
            service: *const c_char,
            account_len: u32,
            account: *const c_char,
            length: u32,
            data: *const c_void,
            item: *mut *mut c_void,
        ) -> i32;
        fn SecKeychainItemModifyAttributesAndData(
            item: *const c_void,
            attributes: *const c_void,
            length: u32,
            data: *const c_void,
        ) -> i32;
        fn SecKeychainItemDelete(item: *const c_void) -> i32;
        fn SecKeychainItemFreeContent(attributes: *mut c_void, data: *mut c_void) -> i32;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFRelease(value: *const c_void);
    }
    fn checked(status: i32) -> Result<(), String> {
        if status == 0 {
            Ok(())
        } else {
            Err(format!("macOS Keychain status {status}"))
        }
    }
    unsafe fn find(
        key: &str,
        length: *mut u32,
        data: *mut *mut c_void,
        item: *mut *mut c_void,
    ) -> i32 {
        SecKeychainFindGenericPassword(
            ptr::null(),
            SERVICE.len() as u32,
            SERVICE.as_ptr().cast(),
            key.len() as u32,
            key.as_ptr().cast(),
            length,
            data,
            item,
        )
    }
    pub fn read(key: &str) -> Result<Option<String>, String> {
        let mut length = 0;
        let mut data = ptr::null_mut();
        let status = unsafe { find(key, &mut length, &mut data, ptr::null_mut()) };
        if status == NOT_FOUND {
            return Ok(None);
        }
        checked(status)?;
        let bytes = if length == 0 {
            Vec::new()
        } else {
            unsafe { std::slice::from_raw_parts(data.cast::<u8>(), length as usize).to_vec() }
        };
        unsafe {
            SecKeychainItemFreeContent(ptr::null_mut(), data);
        }
        String::from_utf8(bytes)
            .map(Some)
            .map_err(|_| "Invalid credential encoding".into())
    }
    pub fn write(key: &str, token: &str) -> Result<(), String> {
        let mut item = ptr::null_mut();
        let status = unsafe { find(key, ptr::null_mut(), ptr::null_mut(), &mut item) };
        if status == NOT_FOUND {
            return checked(unsafe {
                SecKeychainAddGenericPassword(
                    ptr::null(),
                    SERVICE.len() as u32,
                    SERVICE.as_ptr().cast(),
                    key.len() as u32,
                    key.as_ptr().cast(),
                    token.len() as u32,
                    token.as_ptr().cast(),
                    ptr::null_mut(),
                )
            });
        }
        checked(status)?;
        let status = unsafe {
            SecKeychainItemModifyAttributesAndData(
                item,
                ptr::null(),
                token.len() as u32,
                token.as_ptr().cast(),
            )
        };
        unsafe {
            CFRelease(item);
        }
        checked(status)
    }
    pub fn delete(key: &str) -> Result<(), String> {
        let mut item = ptr::null_mut();
        let status = unsafe { find(key, ptr::null_mut(), ptr::null_mut(), &mut item) };
        if status == NOT_FOUND {
            return Ok(());
        }
        checked(status)?;
        let status = unsafe { SecKeychainItemDelete(item) };
        unsafe {
            CFRelease(item);
        }
        checked(status)
    }
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
mod platform {
    use std::io::Write;
    use std::process::{Command, Stdio};
    fn command() -> Command {
        // libsecret's CLI uses the user's Secret Service; secrets go through stdin only.
        let mut cmd = Command::new("secret-tool");
        cmd.stderr(Stdio::null());
        cmd
    }
    pub fn read(key: &str) -> Result<Option<String>, String> {
        let output = command()
            .args(["lookup", "service", "HugAgentOS", "account", key])
            .output()
            .map_err(|_| "System Secret Service is unavailable".to_string())?;
        if !output.status.success() {
            return Ok(None);
        }
        let value = String::from_utf8(output.stdout)
            .map_err(|_| "Invalid credential encoding".to_string())?;
        Ok(Some(value.trim_end_matches('\n').to_string()))
    }
    pub fn write(key: &str, token: &str) -> Result<(), String> {
        let mut child = command()
            .args([
                "store",
                "--label=HugAgentOS desktop session",
                "service",
                "HugAgentOS",
                "account",
                key,
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .map_err(|_| "System Secret Service is unavailable".to_string())?;
        if child
            .stdin
            .take()
            .ok_or("Credential input is unavailable")?
            .write_all(token.as_bytes())
            .is_err()
        {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Credential write failed".into());
        }
        if child
            .wait()
            .map_err(|_| "Credential store failed")?
            .success()
        {
            Ok(())
        } else {
            Err("System Secret Service refused persistence".into())
        }
    }
    pub fn delete(key: &str) -> Result<(), String> {
        let status = command()
            .args(["clear", "service", "HugAgentOS", "account", key])
            .stdout(Stdio::null())
            .status()
            .map_err(|_| "System Secret Service is unavailable".to_string())?;
        if status.success() || status.code() == Some(1) {
            Ok(())
        } else {
            Err("Credential deletion failed".into())
        }
    }
}
